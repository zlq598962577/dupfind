"""restore / empty：把隔离区里的文件放回原位，或清空隔离区。

还原的安全性依赖三件事：

* **崩溃可恢复**：apply 期间每个动作先写 journal 再移动，因此即使进程被杀，
  journal 仍保留完整的动作清单，:func:`load_manifest` 会把它合并进快照；
* **幂等**：目标已不存在、原位置已存在时视为「已还原」，重复执行不会出错；
* **不越界**：只接受 ``dst`` 位于该 manifest 的 trash 目录内的动作、``src``
  不落在系统保护目录的动作，防止一份伪造的 manifest 把任意文件搬来搬去。
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .console import say
from .jsonio import read_json, write_json_atomic
from .models import FILE_ATTRIBUTE_REPARSE_POINT
from .safety import canonical, protected_reason
from .trash import TrashError, validate_source_path
from .units import format_size, iso_now
from .win32 import long_path, move_file, remove_file, strip_long_prefix

MANIFEST_GLOB = "manifest-*.json"
JOURNAL_SUFFIX = ".journal"
CONFLICT_MODES = ("skip", "rename", "overwrite")
MANIFEST_VERSION = 1
# dupfind 自己产出的 manifest 命名：manifest-YYYYMMDD-HHMMSS[-N].json
# 必须严格匹配，不能只认 "manifest-" 前缀 —— 否则 `assets/manifest-assets.json`
# 这类无关文件会被当成「这是隔离区」的证据，让 empty 删掉真实目录。
_MANIFEST_NAME = re.compile(r"^manifest-\d{8}-\d{6}(?:-\d+)?\.json$")


class RestoreError(RuntimeError):
    """manifest 缺失/损坏，或路径不安全。"""


@dataclass(slots=True)
class Action:
    group_id: int
    src: str
    dst: str
    size: int
    status: str
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.src, self.dst)


@dataclass(slots=True)
class ManifestDocument:
    path: Path
    trash: str
    actions: list[Action] = field(default_factory=list)
    #: 该批动作对应的报告路径（旧版 manifest 可能没有）。用于把处置结果
    #: 回填到报告状态，以及还原后把对应组恢复为「待处理」。
    report: str | None = None

    @property
    def restorable(self) -> list[Action]:
        """待还原的动作。

        ``pending`` 也必须算进来：apply 是「先写 journal(pending) → 移动 →
        标记 completed」，进程在移动之后、标记之前被杀时，journal 里只剩
        pending，但文件其实已经在隔离区了。此时若跳过，文件就永远留在隔离区。

        把 pending 当作可还原是安全的：若移动根本没发生，原位置仍有文件、
        隔离区没有对应文件，:func:`_restore_one` 会走「已还原」分支直接跳过。
        """
        return [action for action in self.actions if action.status in ("completed", "pending")]

    @property
    def already_restored(self) -> list[Action]:
        return [action for action in self.actions if action.status == "restored"]


@dataclass(slots=True)
class RestoreOptions:
    dry_run: bool = True
    on_conflict: str = "skip"


@dataclass(slots=True)
class RestoreStats:
    restored: int = 0
    restored_bytes: int = 0
    skipped: int = 0
    failed: int = 0
    missing: int = 0


@dataclass(slots=True)
class EmptyOptions:
    dry_run: bool = True
    keep_manifests: bool = True


@dataclass(slots=True)
class EmptyStats:
    removed_files: int = 0
    removed_bytes: int = 0
    kept_manifests: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# 读取 manifest
# ---------------------------------------------------------------------------


def _parse_action(raw: Any) -> Action | None:
    if not isinstance(raw, dict):
        return None
    src, dst = raw.get("src"), raw.get("dst")
    if not isinstance(src, str) or not isinstance(dst, str):
        return None
    try:
        size = int(raw.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    try:
        group_id = int(raw.get("groupId", 0) or 0)
    except (TypeError, ValueError):
        # 畸形 manifest 不能让整份记录无法加载；groupId=0 会被状态回填忽略
        group_id = 0
    return Action(
        group_id=group_id,
        src=src,
        dst=dst,
        size=size,
        status=str(raw.get("status", "pending")),
        error=raw.get("error") if isinstance(raw.get("error"), str) else None,
    )


def _collect_snapshot(data: Any) -> dict[tuple[str, str], Action]:
    if not isinstance(data, dict):
        return {}
    actions = data.get("actions")
    if not isinstance(actions, list):
        return {}
    result: dict[tuple[str, str], Action] = {}
    for raw in actions:
        action = _parse_action(raw)
        if action is not None:
            result[action.key] = action
    return result


def _collect_journal(path: Path) -> dict[tuple[str, str], Action]:
    """journal 是追加写的 JSONL；同一动作可能出现多次，后写的为准。"""
    result: dict[tuple[str, str], Action] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            action = _parse_action(json.loads(line))
        except ValueError:
            continue  # 崩溃时可能留下半行，忽略
        if action is not None:
            result[action.key] = action
    return result


def _manifest_paths(path: Path) -> tuple[Path, Path | None]:
    """返回 (快照路径, journal 路径)。接受 .json / .json.journal / 无后缀。"""
    text = str(path)
    if text.endswith(JOURNAL_SUFFIX):
        snapshot = Path(text[: -len(JOURNAL_SUFFIX)])
        return snapshot, path
    return path, Path(text + JOURNAL_SUFFIX)


def load_manifest(path: str | Path) -> ManifestDocument:
    manifest_path = Path(path)
    snapshot_path, journal_path = _manifest_paths(manifest_path)
    if not snapshot_path.is_file() and not (journal_path and journal_path.is_file()):
        raise RestoreError(f"manifest 不存在: {manifest_path}")

    merged: dict[tuple[str, str], Action] = {}
    report: str | None = None
    if snapshot_path.is_file():
        try:
            data = read_json(snapshot_path)
        except (OSError, ValueError) as exc:
            raise RestoreError(f"manifest 无法解析: {exc}") from exc
        merged.update(_collect_snapshot(data))
        if isinstance(data, dict):
            raw_report = data.get("report")
            if isinstance(raw_report, str) and raw_report.strip():
                report = raw_report

    # journal 覆盖快照：崩溃时快照可能停在旧状态，journal 才是最新事实
    if journal_path is not None and journal_path.is_file():
        merged.update(_collect_journal(journal_path))

    # trash 根目录一律由 manifest 文件的实际位置推导，**不信任文件里声明的值**。
    # 否则伪造一份 trash="C:\\" 的 manifest，就能让任意路径通过「dst 必须在
    # trash 内」的校验，把系统文件搬到攻击者指定的位置。
    trash = str(ntpath.dirname(ntpath.abspath(str(snapshot_path))))

    actions = sorted(merged.values(), key=lambda action: (action.group_id, action.src))
    return ManifestDocument(path=snapshot_path, trash=trash, actions=actions, report=report)


def discover_manifests(target: str | Path) -> list[Path]:
    """接受单个 manifest 或一个 trash 目录；目录时按时间倒序返回全部 manifest。

    只写了 journal、快照尚未落盘（apply 中途被强杀）的 manifest 也必须被发现，
    否则「崩溃后还原」这条最重要的保证在传目录时就不成立。
    """
    path = Path(target)
    if path.is_dir():
        found = set(path.glob(MANIFEST_GLOB))
        found.update(
            snapshot for snapshot in (item.with_suffix("") for item in path.glob(MANIFEST_GLOB + JOURNAL_SUFFIX))
        )
        if not found:
            raise RestoreError(f"目录中没有 manifest-*.json: {path}")
        return sorted(found, reverse=True)
    return [path]


# ---------------------------------------------------------------------------
# 路径校验
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_inside(child: str, parent: str) -> bool:
    """基于规约后的真实路径判断包含关系。

    只用字符串比较不够：trash 内部若有一个指向外部的 junction，字符串上
    仍「在 trash 内」，实际文件却在别处。``canonical`` 会解析 8.3 短名并
    规范化，因此能发现大部分伪装。
    """
    try:
        return ntpath.commonpath([canonical(child), canonical(parent)]) == canonical(parent)
    except ValueError:
        return False


def _validate_action(action: Action, trash: str) -> str | None:
    """还原前校验：源/目标都是本地绝对路径，且 dst 必须位于 trash 内。

    还原会往 ``src`` 写文件，因此 ``src`` 也不能落在系统保护范围内 —— 合法
    manifest 里不会有这种动作（apply 从不移动受保护路径的文件），出现即伪造。
    """
    for label, value in (("src", action.src), ("dst", action.dst)):
        try:
            validate_source_path(value)
        except TrashError as exc:
            return f"{label} 非法：{exc}"
    reason = protected_reason(action.src)
    if reason is not None:
        return f"src 位于受保护路径（{reason}），拒绝写入"
    if not _is_inside(action.dst, trash):
        return "dst 不在该 manifest 的 trash 目录内"
    # 沿路径检查每一级是否重解析点：字符串在 trash 内、实际经 junction 指向外部
    if _crosses_reparse_point(action.dst, trash):
        return "dst 路径经过符号链接/junction，拒绝还原"
    return None


def _crosses_reparse_point(path: str, root: str) -> bool:
    """检查 root 到 path 之间是否有重解析点（junction/符号链接）。"""
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return False
    if relative.startswith(".."):
        return True
    current = os.path.abspath(root)
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        if os.path.lexists(current) and _is_reparse_point(current):
            return True
    return False


def _conflict_target(src: str) -> str:
    """目标已存在时，生成一个不冲突的还原路径。"""
    stem, extension = ntpath.splitext(src)
    counter = 1
    candidate = f"{stem}.restored-{counter}{extension}"
    while os.path.exists(long_path(candidate)):
        counter += 1
        candidate = f"{stem}.restored-{counter}{extension}"
    return candidate


def _move_back(src: str, dst: str, *, replace: bool = False) -> bool:
    """把 dst 移回 src；``replace`` 时覆盖已存在的 src。

    任何失败都返回 False 而不是抛异常 —— 一条动作失败不应该中断整批还原
    （原盘符可能已拔掉、原目录可能已无权限）。
    """
    try:
        os.makedirs(long_path(ntpath.dirname(src)), exist_ok=True)
    except OSError:
        return False
    if not replace:
        try:
            shutil.move(long_path(dst), long_path(src))
            return True
        except OSError:
            return move_file(dst, src)
    # 覆盖场景直接走 MoveFileExW：它带 REPLACE_EXISTING，且失败时两边都不动，
    # 不会像「先删后移」那样在移动失败时把原文件弄丢
    return move_file(dst, src)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def run_restore(target: str | Path, options: RestoreOptions) -> int:
    if options.on_conflict not in CONFLICT_MODES:
        raise RestoreError(f"--on-conflict 必须是 {', '.join(CONFLICT_MODES)} 之一")

    documents = [load_manifest(path) for path in discover_manifests(target)]
    stats = RestoreStats()
    touched: dict[str, str] = {}

    say(f"模式: {'dry-run（不移动）' if options.dry_run else '实际还原'}")
    for document in documents:
        say(f"\nmanifest: {document.path}")
        say(f"trash:    {document.trash}")
        pending = document.restorable
        if not pending:
            say("  没有待还原的动作。")
            continue

        changed = False
        for action in pending:
            outcome = _restore_one(action, document.trash, options, stats)
            if outcome == "restored":
                if not options.dry_run:
                    action.status = "restored"
                    action.error = None
                    changed = True
            elif outcome == "skipped":
                pass

        if changed:
            # 写回失败时不能同步状态：manifest 仍是 completed，同步会把刚
            # 还原的组重新标成已处置，下一次打开报告又是错的
            if _persist(document) and document.report:
                touched[document.report] = document.trash

    if touched:
        # 还原后这些组必须回到「待处理」，否则页面还说它们已处置，用户
        # 既不能再勾选、也看不到文件其实已经回来了。
        from .report_state import sync_after_restore

        for report_path, trash_dir in touched.items():
            sync_after_restore(report_path, trash_dir)

    say()
    prefix = "（dry-run：未移动任何文件）" if options.dry_run else ""
    say(
        f"完成：还原 {stats.restored} 个文件，{format_size(stats.restored_bytes)}；"
        f"跳过 {stats.skipped}，缺失 {stats.missing}，失败 {stats.failed} {prefix}".rstrip()
    )
    # 退出码要能区分「失败」与「没事可做」，否则脚本无法判断
    if stats.failed:
        return 2
    return 0 if stats.restored else 1


def _restore_one(action: Action, trash: str, options: RestoreOptions, stats: RestoreStats) -> str:
    problem = _validate_action(action, trash)
    if problem is not None:
        stats.failed += 1
        say(f"  [拒绝] {action.src} —— {problem}")
        return "failed"

    src_exists = os.path.exists(long_path(action.src))
    dst_exists = os.path.exists(long_path(action.dst))
    if src_exists and dst_exists and os.path.samefile(long_path(action.src), long_path(action.dst)):
        # 同一物理文件（硬链接或已还原），无需移动
        stats.skipped += 1
        say(f"  [已是同一文件] {action.src}")
        return "skipped"

    # 幂等：原位置已有文件、隔离区已无文件。
    if src_exists and not dst_exists:
        if action.status == "completed":
            # 上一次确实移动过（completed），但可能在写快照前崩溃 —— 必须
            # 按 restored 处理并落盘。只当 skipped 的话 manifest 会永远停在
            # completed，状态文件也就永远显示「已处置」，无法恢复为待处理。
            stats.restored += 1
            stats.restored_bytes += action.size
            say(f"  [已还原] {action.src}")
            return "restored"
        # pending：只是登记了、移动根本没发生，没有可还原的动作
        stats.skipped += 1
        say(f"  [未移动] {action.src}（登记后没有移动，无需还原）")
        return "skipped"
    if not dst_exists:
        stats.missing += 1
        say(f"  [缺失] {action.dst} 不存在，跳过")
        return "missing"

    target = action.src
    replace = False
    if src_exists:
        if options.on_conflict == "skip":
            stats.skipped += 1
            say(f"  [冲突] {action.src} 已存在，跳过（可用 --on-conflict rename 另存）")
            return "skipped"
        if options.on_conflict == "rename":
            target = _conflict_target(action.src)
        else:  # overwrite
            replace = True

    # dry-run 必须在任何写操作之前返回 —— 否则 overwrite 会真的删掉文件
    if options.dry_run:
        verb = "覆盖并还原" if replace else "还原"
        say(f"  [DRY-RUN] {verb} {action.dst} -> {target}")
        stats.restored += 1
        stats.restored_bytes += action.size
        return "restored"

    if not _move_back(target, action.dst, replace=replace):
        stats.failed += 1
        say(f"  [失败] {action.dst} -> {target}")
        return "failed"
    stats.restored += 1
    stats.restored_bytes += action.size
    say(f"  [还原] {action.dst} -> {target}")
    return "restored"


def _persist(document: ManifestDocument) -> bool:
    """把已还原的动作写回快照，使重复执行时自动跳过；返回是否成功。"""
    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "createdAt": iso_now(),
        "report": None,
    }
    # 保留原有字段（createdAt / report），不要因为写回状态就把来源信息抹掉
    try:
        existing = read_json(document.path)
        if isinstance(existing, dict):
            payload.update(existing)
    except (OSError, ValueError):
        pass
    payload["version"] = MANIFEST_VERSION
    payload["restoredAt"] = iso_now()
    payload["trash"] = document.trash
    if document.report:
        payload["report"] = document.report
    payload["actions"] = [
        {
            "groupId": action.group_id,
            "src": action.src,
            "dst": action.dst,
            "size": action.size,
            "status": action.status,
            "error": action.error,
        }
        for action in document.actions
    ]
    try:
        write_json_atomic(document.path, payload)
    except OSError as exc:
        say(f"  [警告] 无法写回 manifest：{exc}")
        return False
    # journal 里的旧状态会覆盖刚写回的快照（load_manifest 中 journal 优先），
    # 不删掉的话下次执行看到的仍是 completed，写回等于白做。
    journal = Path(str(document.path) + JOURNAL_SUFFIX)
    try:
        journal.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        say(f"  [警告] 无法清理 journal：{exc}（下次仍可能显示为未还原）")
        return False
    return True


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


def _validate_trash(trash: str) -> str:
    """校验清空目标：必须是本地绝对路径、不是盘符根目录、也不在系统保护范围内。

    ``empty`` 是不可逆操作，这里必须比 ``apply`` 更严格 —— 用户完全可能手滑
    写成 ``dupfind empty C:\\Windows``。
    """
    text = str(trash or "").strip().replace("/", "\\")
    if text.startswith("\\\\?\\UNC\\"):
        raise RestoreError(f"trash 必须是本地盘符路径: {trash}")
    if text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    if not ntpath.isabs(text):
        raise RestoreError(f"trash 必须是本地绝对路径: {trash}")
    if text.startswith("\\\\"):
        raise RestoreError(f"trash 必须是本地盘符路径（不接受 UNC 共享）: {trash}")
    resolved = canonical(text)
    drive, rest = ntpath.splitdrive(resolved)
    if not drive:
        raise RestoreError(f"trash 必须是本地绝对路径: {trash}")
    parts = [part for part in rest.replace("/", "\\").split("\\") if part]
    if not parts:
        raise RestoreError(f"拒绝清空盘符根目录: {trash}")
    reason = protected_reason(trash)
    if reason is not None:
        raise RestoreError(f"拒绝清空受保护目录: {trash}（{reason}）")
    # 根目录本身是 junction/符号链接时，os.walk 会顺着它删到别处去。
    # 真实隔离区是 dupfind 自己建的普通目录，不会出现这种情况。
    if _is_reparse_point(text):
        raise RestoreError(f"拒绝清空符号链接/junction: {trash}")
    return os.path.abspath(text)


def _collect_trash(root: str) -> tuple[list[Path], list[Path], int, int]:
    """枚举隔离区里的 payload 与 manifest。

    手工递归而不用 ``os.walk``，原因有两条：

    * ``os.walk`` 在 Windows 上会跟随 junction（Python 3.11 实测），
      指向隔离区之外的目录会被一起遍历甚至删除；
    * ``os.walk`` 传入的根路径没有 ``\\\\?\\`` 前缀，镜像目录一旦超过 260
      字符，深层的文件根本列不出来 —— 隔离区看起来「清空完成」，文件却还在。

    manifest 只认 trash 根目录下、dupfind 自己命名的文件；嵌套的「manifest-」
    文件是用户数据，不能当成还原记录，也不能拿它当「这是隔离区」的证据。
    """
    files: list[Path] = []
    manifests: list[Path] = []
    skipped_links = 0
    total_bytes = 0

    def visit(current: str) -> None:
        nonlocal skipped_links, total_bytes
        try:
            with os.scandir(long_path(current)) as iterator:
                entries = list(iterator)
        except OSError:
            return
        at_root = _same_dir(current, root)
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    is_dir = True
                else:
                    is_dir = False
            except OSError:
                continue
            if _is_reparse_point(entry.path):
                skipped_links += 1
                continue
            if is_dir:
                visit(entry.path)
                continue
            if at_root and (
                _MANIFEST_NAME.match(path.name)
                or (
                    path.name.endswith(JOURNAL_SUFFIX)
                    and _MANIFEST_NAME.match(path.name[: -len(JOURNAL_SUFFIX)])
                )
            ):
                # journal 与快照是同一份记录，必须一起保留或一起删除：
                # 漏掉 journal 会让崩溃后的最新动作被当成普通 payload 删掉，
                # 保留下来的空快照又让 restore 以为「没有可还原的动作」。
                manifests.append(Path(strip_long_prefix(entry.path)))
                continue
            files.append(Path(strip_long_prefix(entry.path)))
            try:
                total_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass

    visit(root)
    return files, manifests, skipped_links, total_bytes


def run_empty(trash: str, options: EmptyOptions) -> int:
    root = _validate_trash(trash)
    if not os.path.isdir(long_path(root)):
        say(f"trash 目录不存在：{root}")
        return 1

    stats = EmptyStats()
    files, manifests, skipped_links, total_bytes = _collect_trash(root)
    stats.removed_bytes = total_bytes
    # manifests 里还混着 .json.journal（与快照同生共死），计数时只算快照
    snapshot_count = sum(1 for path in manifests if not path.name.endswith(JOURNAL_SUFFIX))
    if options.keep_manifests:
        stats.kept_manifests = snapshot_count
    if skipped_links:
        say(f"跳过 {skipped_links} 个符号链接/junction（不跟随，避免越界删除）")

    # 真正的隔离区一定有 manifest。没有 manifest 的目录不是隔离区 —— 多半是
    # 用户把路径写错成了资料目录（dupfind empty D:\照片），这是不可逆操作，
    # 必须拒绝而不是照删。
    if not manifests:
        raise RestoreError(
            f"目录根下没有 dupfind 的 manifest-<时间戳>.json，看起来不是隔离区，拒绝清空: {root}"
        )

    say(f"trash: {root}")
    say(f"模式: {'dry-run（不删除）' if options.dry_run else '实际删除'}")
    say(f"待删除 {len(files)} 个文件，{format_size(stats.removed_bytes)}")
    if manifests:
        if options.keep_manifests:
            say(f"保留 {snapshot_count} 个 manifest（还原记录；用 --purge-manifests 一并删除）")
        else:
            say(f"另有 {snapshot_count} 个 manifest 将一并删除")

    if not files and not (manifests and not options.keep_manifests):
        # 没有 payload 可删时仍然清理空目录：restore 之后镜像目录会留下空壳
        # （trash\C\Users\...），不清掉的话隔离区看起来还是一堆东西。
        if not options.dry_run:
            _prune_empty_dirs(root)
        say("没有可删除的文件。")
        return 0

    if options.dry_run:
        for path in files[:20]:
            say(f"  [DRY-RUN] 删除 {path}")
        if len(files) > 20:
            say(f"  …… 另有 {len(files) - 20} 个文件")
        if not options.keep_manifests:
            for path in manifests:
                say(f"  [DRY-RUN] 删除 {path}")
        say("\n（dry-run：未删除任何文件，加 --no-dry-run 执行）")
        return 0

    for path in files:
        try:
            # 不能直接用 os.unlink：隔离区里的文件常带只读位（微信/QQ 收的文件、
            # 安装包），Windows 上对只读文件 unlink 一律 WinError 5，
            # 「清空隔离区」就会永远失败。remove_file 会先清掉只读属性。
            remove_file(str(path))
            stats.removed_files += 1
        except OSError as exc:
            stats.failed += 1
            say(f"  [失败] {path} —— {exc}")

    # 顺序要紧：先删 payload 再删 manifest。否则 journal 的目录先被清空，
    # 后面删除 manifest 失败时，还原记录就丢了。
    if not options.keep_manifests:
        if stats.failed:
            # 有 payload 删不掉时，它的还原记录还有用，不能一并清掉
            say(f"  有 {stats.failed} 个文件删除失败，保留全部 manifest 以便还原")
        else:
            for path in manifests:
                try:
                    remove_file(str(path))
                except OSError as exc:
                    stats.failed += 1
                    say(f"  [失败] {path} —— {exc}")

    _prune_empty_dirs(root)

    say(
        f"\n完成：删除 {stats.removed_files} 个文件，释放 {format_size(stats.removed_bytes)}；"
        f"失败 {stats.failed}"
    )
    return 2 if stats.failed else 0


def _is_reparse_point(path: str) -> bool:
    """判断是否符号链接/junction；失败时保守地当作是（宁可不删）。"""
    try:
        info = os.lstat(long_path(path))
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    # 没有 st_file_attributes 时退回 st_reparse_tag / 链接计数判断
    return getattr(info, "st_reparse_tag", 0) != 0


def _prune_empty_dirs(root: str) -> None:
    """自底向上删除空目录；只删真正空的目录，因此保留 manifest 的目录自然留下。

    不能直接用 ``os.walk``：它在 Windows 上会跟随 junction，随后对
    ``trash\\link\\sub`` 调用 ``os.rmdir`` 删掉的是链接指向的**外部真实目录**
    （实测：junction 指向的目录里的空子目录会被删掉）。这里手工递归，遇到
    重解析点既不进入也不删除。
    """
    order: list[str] = []

    def collect(current: str) -> None:
        try:
            with os.scandir(long_path(current)) as iterator:
                entries = list(iterator)
        except OSError:
            return
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if _is_reparse_point(entry.path):
                continue
            collect(entry.path)
        order.append(current)

    collect(root)
    for current in order:
        # scandir 返回的路径带 \\?\ 前缀，直接和 root 比字符串永远不相等，
        # 会把 trash 根目录也删掉
        if _same_dir(current, root):
            continue
        try:
            if not os.listdir(long_path(current)):
                os.rmdir(long_path(current))
        except OSError:
            pass


def _same_dir(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return os.path.normcase(os.path.normpath(strip_long_prefix(value)))

    return norm(left) == norm(right)
