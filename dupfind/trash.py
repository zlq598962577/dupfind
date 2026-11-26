"""trash 目标规划、路径安全校验与 manifest 记录。"""

from __future__ import annotations

import json
import ntpath
import os
import re
from datetime import datetime

from .jsonio import write_json_atomic
from .units import iso_now
from .win32 import close_handle, long_path, open_exclusive

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
MANIFEST_VERSION = 1


class TrashError(RuntimeError):
    """路径不合法、跨卷或目标越界。"""


def validate_source_path(path: str) -> str:
    """校验报告中的路径：绝对、带盘符、无 ``..`` 逃逸、无扩展前缀。"""
    if not isinstance(path, str) or not path.strip():
        raise TrashError("路径为空")
    if path.startswith("\\\\?\\"):
        raise TrashError(f"路径包含不支持的扩展前缀: {path}")
    normalized = path.replace("/", "\\")
    drive, rest = ntpath.splitdrive(normalized)
    if not _DRIVE_RE.match(drive):
        raise TrashError(f"路径不是本地绝对路径: {path}")
    if not rest:
        raise TrashError(f"路径缺少相对部分: {path}")
    for part in rest.split("\\"):
        if part == "..":
            raise TrashError(f"路径包含 '..' 逃逸: {path}")
    return normalized


def drive_of(path: str) -> str:
    drive, _ = ntpath.splitdrive(os.path.abspath(path))
    return drive.upper()


def relative_to_drive(path: str) -> tuple[str, str]:
    normalized = validate_source_path(path)
    drive, rest = ntpath.splitdrive(normalized)
    return drive[0].upper(), rest.lstrip("\\")


def _normcase(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_inside(child: str, parent: str) -> bool:
    try:
        return ntpath.commonpath([_normcase(child), _normcase(parent)]) == _normcase(parent)
    except ValueError:
        return False


class TrashManager:
    """把源文件映射到 ``<trash>/<盘符>/<相对路径>``，同名冲突追加后缀。"""

    __slots__ = ("_trash", "_reserved")

    def __init__(self, trash_dir: str) -> None:
        drive, _ = ntpath.splitdrive(os.path.abspath(trash_dir))
        if not _DRIVE_RE.match(drive):
            raise TrashError(f"trash 目录必须是本地绝对路径: {trash_dir}")
        self._trash = os.path.abspath(trash_dir)
        self._reserved: set[str] = set()

    @property
    def path(self) -> str:
        return self._trash

    def ensure(self) -> None:
        os.makedirs(self._trash, exist_ok=True)

    def target_for(self, source: str) -> str:
        letter, relative = relative_to_drive(source)
        if letter != drive_of(self._trash)[0]:
            raise TrashError(f"跨卷移动不在本期范围: {source}")
        if _is_inside(source, self._trash):
            raise TrashError(f"源文件已位于 trash 目录: {source}")

        base = ntpath.join(self._trash, letter, relative)
        candidate = base
        counter = 1
        # 必须用 long_path 探测：镜像目标很容易超过 260 字符，普通 os.path.exists
        # 在长路径上恒为 False，于是不追加后缀、直接复用已占用的路径 —— 后续
        # move_file 带 REPLACE_EXISTING 会把隔离区里的旧文件覆盖掉。
        while _normcase(candidate) in self._reserved or os.path.exists(long_path(candidate)):
            stem, extension = ntpath.splitext(base)
            candidate = f"{stem}.dupfind-{counter}{extension}"
            counter += 1
        if not _is_inside(candidate, self._trash):
            raise TrashError(f"trash 目标越界: {candidate}")
        self._reserved.add(_normcase(candidate))
        return candidate


class Manifest:
    """移动前写入，可据此人工回滚。

    写入分两层，避免批量处置时把 manifest 反复整体重写：

    * **journal**（``manifest-<ts>.json.journal``）：每完成一个动作追加一行 JSONL
      并落盘，O(1) 写入，崩溃后仍能完整还原；
    * **快照**（``manifest-<ts>.json``）：全部动作的完整 JSON，在 ``save()`` 时
      原子写入，作为给人看的最终产物与 journal 的压缩结果。

    只追加 journal 而不重写快照，使 N 个文件的写入量从 O(N²) 降到 O(N)。
    """

    JOURNAL_SUFFIX = ".journal"

    __slots__ = ("_path", "_journal_path", "_data", "_journal", "_dirty")

    def __init__(self, trash_dir: str, report_path: str) -> None:
        self._journal, self._path, self._journal_path = self._reserve(trash_dir)
        self._dirty = False
        self._data: dict[str, object] = {
            "version": MANIFEST_VERSION,
            "createdAt": iso_now(),
            "report": report_path,
            "trash": trash_dir,
            "actions": [],
        }

    @staticmethod
    def _reserve(trash_dir: str) -> tuple[object, str, str]:
        """**原子地**占下一个 manifest 名字，返回 (journal 句柄, 快照路径, journal 路径)。

        不能用「``os.path.exists`` 探测再创建」：两个 apply 在同一秒启动时都
        探测到同一个名字可用，随后共用一份 manifest —— 一个任务的 save 会
        覆盖另一个的 actions，共享的 journal 又被先完成者删掉，已移走的文件
        从此没有还原记录，只剩被 ``empty`` 永久删除一条路。

        ``O_CREAT | O_EXCL`` 让「占名」这一步由内核保证互斥：谁先建成 journal
        谁就拿到这个名字，输的那个换下一个后缀重试。
        """
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for suffix in range(1, 1000):
            name = f"manifest-{timestamp}.json" if suffix == 1 else f"manifest-{timestamp}-{suffix}.json"
            snapshot = ntpath.join(trash_dir, name)
            journal_path = snapshot + Manifest.JOURNAL_SUFFIX
            try:
                # 快照文件也必须不存在：否则后续 save() 会覆盖别人的记录
                handle = os.open(
                    long_path(journal_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND,
                    0o666,
                )
            except FileExistsError:
                continue
            if os.path.exists(long_path(snapshot)):
                # journal 建成了但同名快照已存在（旧版遗留），放掉重来
                os.close(handle)
                try:
                    os.unlink(long_path(journal_path))
                except OSError:
                    pass
                continue
            return os.fdopen(handle, "a", encoding="utf-8", newline="\n"), snapshot, journal_path
        raise TrashError(f"无法在隔离区内分配 manifest 文件名: {trash_dir}")

    @property
    def path(self) -> str:
        return self._path

    @property
    def journal_path(self) -> str:
        return self._journal_path

    def _append_journal(self, action: dict[str, object], *, durable: bool) -> None:
        """追加一条动作记录；``durable`` 时 fsync，保证崩溃前已落盘。"""
        if self._journal is None:
            self._journal = open(self._journal_path, "a", encoding="utf-8", newline="\n")
        self._journal.write(json.dumps(action, ensure_ascii=False) + "\n")
        self._journal.flush()
        if durable:
            try:
                os.fsync(self._journal.fileno())
            except OSError:
                pass
        self._dirty = True

    def sync(self) -> None:
        """把 journal 缓冲落盘（fsync）。

        批量处置时不必每个动作都 fsync：整组动作先以 ``durable=False`` 登记，
        再调用一次 ``sync``，随后才移动文件。崩溃最多留下「已登记未移动」的
        pending，restore 会发现目标不存在并跳过，不会丢记录。
        """
        if self._journal is None:
            return
        self._journal.flush()
        try:
            os.fsync(self._journal.fileno())
        except OSError:
            pass

    def add(
        self,
        *,
        group_id: int,
        source: str,
        target: str,
        size: int,
        status: str = "pending",
        durable: bool = True,
    ) -> dict[str, object]:
        """登记一个待执行动作，返回可交给 :meth:`mark` 更新的句柄。

        ``durable=False`` 只 flush 不 fsync，适合「整组登记后统一 :meth:`sync`」
        的批量路径；留空（默认）保持「移动前必须落盘」的单文件语义。
        """
        action: dict[str, object] = {
            "groupId": group_id,
            "src": source,
            "dst": target,
            "size": size,
            "status": status,
            "error": None,
        }
        actions = self._data["actions"]
        assert isinstance(actions, list)
        actions.append(action)
        # 移动发生前必须持久化，崩溃时才知道该还原什么
        self._append_journal(action, durable=durable)
        return action

    def mark(self, action: dict[str, object], *, status: str, error: str | None = None) -> None:
        """更新动作结果；只 flush 不 fsync —— 崩溃时按 pending + 目标存在即可还原。"""
        action["status"] = status
        action["error"] = error
        self._append_journal(action, durable=False)

    def save(self) -> None:
        """写出完整快照并清空 journal（快照已包含全部动作）。"""
        write_json_atomic(self._path, self._data)
        self._dirty = False
        if self._journal is not None:
            try:
                self._journal.close()
            except OSError:
                pass
            self._journal = None
        try:
            os.unlink(self._journal_path)
        except OSError:
            pass

    def __del__(self) -> None:
        """兜底：异常退出时尽量关闭句柄，避免 journal 未 flush。"""
        journal = getattr(self, "_journal", None)
        if journal is not None:
            try:
                journal.close()
            except OSError:
                pass

    def close(self) -> None:
        """收尾：有改动才落快照；一个动作都没做就删掉占位用的空 journal。

        ``_reserve`` 会先建出 journal 文件来「占名」，如果整次处置没有任何
        文件被移动（全部跳过、或计划为空），这个空 journal 没有任何还原
        价值，留着只会让 ``restore`` 多出一个「没有待还原的动作」的 manifest。
        """
        if self._dirty:
            self.save()
        elif self._journal is not None:
            try:
                self._journal.close()
            except OSError:
                pass
            self._journal = None
            try:
                os.unlink(long_path(self._journal_path))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 跨进程隔离区锁
# ---------------------------------------------------------------------------

TRASH_LOCK_SUFFIX = ".lock"


class TrashBusy(RuntimeError):
    """同一隔离区已有处置任务在跑。"""


def _lock_path(trash: str) -> str:
    """锁文件放在隔离区的**同级**，不能放进去。

    放进隔离区的话，``empty`` 会把它当 payload 删掉（目录里所有非 manifest
    文件都会被 unlink），锁就凭空消失，互斥失效。同级文件在 ``empty`` 的
    遍历范围之外，且随隔离区一起消失（用户删掉隔离区时锁也自然没了）。
    """
    parent = ntpath.dirname(ntpath.normpath(trash))
    name = ntpath.basename(ntpath.normpath(trash))
    return ntpath.join(parent, f".{name}{TRASH_LOCK_SUFFIX}")


class TrashLock:
    r"""跨进程的隔离区互斥（独占句柄，进程退出即释放）。

    为什么必须有：apply 把文件移入 ``<盘符>:\_dupfind_trash`` 的过程中，
    另一个标签页/第二个 web 实例/CLI 对同一隔离区执行 ``empty``，会把这些
    刚移入的文件逐个 unlink。apply 的 manifest 仍记为 completed，之后
    ``restore`` 只会报「缺失」并以成功结束 —— 用户完全察觉不到唯一副本
    已经被删掉。

    用 ``CreateFileW`` 的独占共享模式实现，而不是内存锁：CLI 和另一个 web
    实例是不同进程，内存里的 ``threading.Lock`` 管不到它们。
    """

    __slots__ = ("_handle", "_path")

    def __init__(self, trash: str) -> None:
        self._path = _lock_path(trash)
        self._handle: int | None = None

    @property
    def path(self) -> str:
        return self._path

    def acquire(self) -> None:
        try:
            os.makedirs(ntpath.dirname(self._path) or ".", exist_ok=True)
            self._handle = open_exclusive(self._path)
        except OSError as exc:
            raise TrashBusy(
                f"隔离区正被另一个处置任务使用，请等它结束后再试：{ntpath.dirname(self._path)}"
            ) from exc

    def release(self) -> None:
        if self._handle is not None:
            try:
                close_handle(self._handle)
            finally:
                self._handle = None

    def __enter__(self) -> TrashLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.release()
        return False
