"""报告处置状态：记录哪些组已经处理过，让刷新后的页面不再「装作没发生」。

报告本身是不可变的扫描快照，处置只写隔离区的 manifest。由此产生一个用户
能直接看到的问题：处理了几组之后重新加载报告，它们仍然显示为待处理，用户
无从判断「到底有没有处理过」。

这里在报告旁边维护一份 ``<report>.state.json``：

* **身份校验**：记录扫描根、开始时间、文件数与组数。报告被重新扫描覆盖后
  身份不同，旧状态自动失效，不会把新报告的组误标为已处置；
* **manifest 兜底**：状态文件丢失（换机器、被清理、程序崩溃在写状态之前）
  时，从隔离区 manifest 的 ``report`` 字段反推已处置组 —— 已移动的文件本来
  就有持久记录，不必再抄一份；
* **显式待处理墓碑**：用户把手动忽略 / 已处置的组恢复为待处理时，写一条
  ``pending`` 记录而不是删除 —— 否则 manifest 兜底会立刻把同组补回 handled，
  「恢复」等于没做；
* **原子写**：每次变更整体写出（临时文件 + 替换）。状态文件只有几 MB，
  写入次数很少（一次处置结束一次、手动标记一次），因此不做 journal：
  崩溃时 manifest 才是权威事实，状态文件丢了也能重建。读-改-写在同一把
  可重入锁内完成，避免并发请求互相覆盖。
"""

from __future__ import annotations

import ntpath
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json_atomic
from .report import ReportDocument, ReportError, load_report
from .units import iso_now

STATE_SUFFIX = ".state.json"
STATE_VERSION = 1
#: ``pending`` 是用户显式恢复待处理时留下的墓碑，用来压制 manifest 兜底；
#: 它在视图里与「从未处理过」完全一样，也不会被 skip_ids 跳过
GROUP_STATES = ("handled", "partial", "ignored", "pending")
#: 一次视图最多从多少份 manifest 回填状态；再多也只影响「很早以前」的记录
MANIFEST_SCAN_LIMIT = 200

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str | Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class ReportStateError(RuntimeError):
    """状态文件损坏或身份不匹配。"""


@dataclass(slots=True)
class GroupState:
    state: str
    handled_at: str = ""
    moved_files: int = 0
    moved_bytes: int = 0
    manifest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "handledAt": self.handled_at,
            "movedFiles": self.moved_files,
            "movedBytes": self.moved_bytes,
            "manifest": self.manifest,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GroupState | None:
        if not isinstance(raw, dict):
            return None
        state = raw.get("state")
        if state not in GROUP_STATES:
            return None
        try:
            moved_files = int(raw.get("movedFiles") or 0)
            moved_bytes = int(raw.get("movedBytes") or 0)
        except (TypeError, ValueError):
            moved_files = 0
            moved_bytes = 0
        return cls(
            state=str(state),
            handled_at=str(raw.get("handledAt") or ""),
            moved_files=moved_files,
            moved_bytes=moved_bytes,
            manifest=raw.get("manifest") if isinstance(raw.get("manifest"), str) else None,
        )


@dataclass(slots=True)
class EffectiveState:
    """视图与处置共用的「已处置」结论。"""

    groups: dict[int, GroupState]
    identity_ok: bool = True
    manifest_groups: int = 0

    def state_of(self, group_id: int) -> str:
        record = self.groups.get(group_id)
        return record.state if record is not None else "pending"

    def skip_ids(self) -> frozenset[int]:
        """已处置 / 已忽略的组：不再参与处置。"""
        return frozenset(
            group_id
            for group_id, record in self.groups.items()
            if record.state in ("handled", "ignored")
        )


def state_path_for(report_path: str | Path) -> Path:
    return Path(str(report_path) + STATE_SUFFIX)


def report_identity(document: ReportDocument) -> dict[str, Any]:
    scan = document.raw_scan or {}
    return {
        "root": document.root,
        "startedAt": scan.get("startedAt"),
        "filesScanned": scan.get("filesScanned"),
        "groups": len(document.groups),
    }


def _identity_matches(saved: Any, current: dict[str, Any]) -> bool:
    if not isinstance(saved, dict):
        return False
    try:
        saved_root = str(saved.get("root") or "")
        if os.path.normcase(ntpath.normpath(saved_root)) != os.path.normcase(
            ntpath.normpath(str(current.get("root") or ""))
        ):
            return False
        return (
            saved.get("startedAt") == current.get("startedAt")
            and saved.get("filesScanned") == current.get("filesScanned")
            and saved.get("groups") == current.get("groups")
        )
    except (TypeError, ValueError):
        return False


class ReportStateStore:
    """单个报告的状态文件；所有写操作按路径加锁，避免并发请求互相覆盖。"""

    __slots__ = ("path", "_groups", "_identity")

    def __init__(self, report_path: str | Path) -> None:
        self.path = state_path_for(report_path)
        self._groups: dict[int, GroupState] = {}
        self._identity: dict[str, Any] | None = None

    @property
    def groups(self) -> dict[int, GroupState]:
        return dict(self._groups)

    def load(self, identity: dict[str, Any] | None = None) -> bool:
        """读取状态；返回 False 表示文件属于另一份报告（身份不匹配）。"""
        with _lock_for(self.path):
            if not self.path.is_file():
                self._groups = {}
                self._identity = dict(identity) if identity is not None else None
                return True
            try:
                data = read_json(self.path)
            except (OSError, ValueError):
                # 状态文件损坏不应让视图打不开：按「没有状态」处理，
                # manifest 兜底仍然有效。
                self._groups = {}
                self._identity = dict(identity) if identity is not None else None
                return True
            if not isinstance(data, dict):
                self._groups = {}
                self._identity = dict(identity) if identity is not None else None
                return True
            version = data.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version != STATE_VERSION:
                self._groups = {}
                self._identity = dict(identity) if identity is not None else None
                return True
            saved_identity = data.get("identity")
            if identity is not None and not _identity_matches(saved_identity, identity):
                self._groups = {}
                self._identity = dict(identity)
                return False
            self._identity = dict(identity) if identity is not None else (
                saved_identity if isinstance(saved_identity, dict) else None
            )
            groups: dict[int, GroupState] = {}
            raw_groups = data.get("groups")
            if isinstance(raw_groups, dict):
                for key, value in raw_groups.items():
                    try:
                        group_id = int(key)
                    except (TypeError, ValueError):
                        continue
                    record = GroupState.from_dict(value)
                    if record is not None:
                        groups[group_id] = record
            self._groups = groups
            return True

    def state_of(self, group_id: int) -> str:
        record = self._groups.get(group_id)
        return record.state if record is not None else "pending"

    def set_group(
        self,
        group_id: int,
        state: str | None,
        *,
        moved_files: int = 0,
        moved_bytes: int = 0,
        manifest: str | None = None,
        persist: bool = True,
    ) -> None:
        if state is None:
            self._groups.pop(group_id, None)
        else:
            if state not in GROUP_STATES:
                raise ReportStateError(f"未知的组状态: {state}")
            self._groups[group_id] = GroupState(
                state=state,
                handled_at=iso_now(),
                moved_files=moved_files,
                moved_bytes=moved_bytes,
                manifest=manifest,
            )
        if persist:
            self.flush()

    def replace_groups(self, groups: dict[int, GroupState]) -> None:
        self._groups = dict(groups)
        self.flush()

    def flush(self) -> None:
        with _lock_for(self.path):
            payload = {
                "version": STATE_VERSION,
                "identity": self._identity,
                "updatedAt": iso_now(),
                "groups": {str(group_id): record.to_dict() for group_id, record in sorted(self._groups.items())},
            }
            write_json_atomic(self.path, payload)


# ---------------------------------------------------------------------------
# manifest 回填
# ---------------------------------------------------------------------------


def _default_trash(root: str) -> str | None:
    drive, _ = ntpath.splitdrive(os.path.abspath(root))
    if not drive or not drive.endswith(":"):
        return None
    return ntpath.join(drive + "\\", "_dupfind_trash")


def scan_manifest_states(report_path: str | Path, trash: str | None, *, limit: int = MANIFEST_SCAN_LIMIT) -> tuple[dict[int, GroupState], int]:
    """从隔离区 manifest 反推某份报告的已处置组。

    只认 ``status == "completed"`` 的动作：``pending`` 可能只是「登记了、
    还没移动」（预登记批量落盘时会有一批），把它算成已处置会让用户以为
    文件已经进隔离区，实际还在原地。
    """
    if not trash or not os.path.isdir(trash):
        return {}, 0
    from .restore import RestoreError, discover_manifests, load_manifest

    try:
        manifests = discover_manifests(trash)
    except (RestoreError, ValueError, TypeError, OSError):
        return {}, 0
    target = os.path.normcase(ntpath.normpath(str(report_path)))
    result: dict[int, GroupState] = {}
    matched = 0
    for path in manifests[:limit]:
        try:
            document = load_manifest(path)
        except (RestoreError, ValueError, TypeError, OSError):
            # 一份畸形 manifest 不应让整份报告 / 整个隔离区都打不开
            continue
        if not document.report:
            continue
        if os.path.normcase(ntpath.normpath(document.report)) != target:
            continue
        matched += 1
        for action in document.actions:
            if action.status != "completed" or action.group_id <= 0:
                continue
            record = result.get(action.group_id)
            if record is None:
                record = GroupState(state="handled", handled_at="", manifest=str(document.path))
                result[action.group_id] = record
            record.moved_files += 1
            record.moved_bytes += action.size
    return result, matched


def load_effective_state(
    document: ReportDocument,
    *,
    trash: str | None = None,
    include_manifests: bool = True,
) -> EffectiveState:
    """状态文件 + manifest 的合并结论。

    优先级：状态文件里的记录 > manifest 推导。``ignored`` 只存在于状态文件；
    ``handled`` 两份都有时以状态文件为准（它带更完整的计数）。manifest 负责
    补齐状态文件没有的历史处置。
    """
    store = ReportStateStore(document.path)
    identity = report_identity(document)
    identity_ok = store.load(identity)
    groups = dict(store.groups) if identity_ok else {}
    manifest_count = 0
    # 身份不匹配说明报告已经重新扫描，组号是按新报告重排的；此时旧 manifest
    # 里的 groupId 指向的是另一组文件，绝不能拿来回填
    if include_manifests and identity_ok:
        trash_dir = trash if trash is not None else _default_trash(document.root)
        manifest_groups, manifest_count = scan_manifest_states(document.path, trash_dir)
        for group_id, record in manifest_groups.items():
            groups.setdefault(group_id, record)
    return EffectiveState(groups=groups, identity_ok=identity_ok, manifest_groups=manifest_count)


# ---------------------------------------------------------------------------
# 写入：apply 结果 / 手动标记 / 还原同步
# ---------------------------------------------------------------------------


def record_apply_result(
    document: ReportDocument,
    group_results: dict[int, tuple[str, int, int]],
    *,
    manifest: str | None = None,
) -> bool:
    """把一次处置的逐组结果写进状态文件。

    状态文件属于另一份报告（已重新扫描）时，旧记录本就不适用，这里直接以
    当前身份重建并覆盖。写盘失败（``OSError``）会向上抛出，由调用方决定是
    警告还是标记任务异常。

    ``group_results`` 的值是 ``(state, moved_files, moved_bytes)``，state 只能
    是 ``handled`` 或 ``partial``。重试一个 partial 组时把之前的移动数累加，
    避免卡片上的「已移动 N 个文件」倒退。
    """
    if not group_results:
        return True
    store = ReportStateStore(document.path)
    with _lock_for(store.path):
        store.load(report_identity(document))
        existing = store.groups
        for group_id, (state, moved_files, moved_bytes) in group_results.items():
            previous = existing.get(group_id)
            if previous is not None and previous.state in ("handled", "partial"):
                moved_files += previous.moved_files
                moved_bytes += previous.moved_bytes
            store.set_group(
                group_id,
                state,
                moved_files=moved_files,
                moved_bytes=moved_bytes,
                manifest=manifest,
                persist=False,
            )
        store.flush()
    return True


def set_manual_group_state(document: ReportDocument, group_id: int, state: str | None) -> bool:
    """手动把一组标为「已忽略」或恢复「待处理」。

    ``state=None`` 写入 ``pending`` 墓碑而不是删除记录：manifest 兜底只补齐
    状态文件里没有的组，墓碑因此能挡住它，让「恢复为待处理」真正生效。
    """
    store = ReportStateStore(document.path)
    with _lock_for(store.path):
        # 身份不匹配说明报告已重扫，旧记录不适用；load 会清空并以当前身份重建
        store.load(report_identity(document))
        store.set_group(group_id, "pending" if state is None else state)
    return True


def _directory_key(path: str | Path) -> str:
    return os.path.normcase(ntpath.normpath(os.path.abspath(str(path))))


def _is_within(path: str, root_key: str) -> bool:
    try:
        return ntpath.commonpath([_directory_key(path), root_key]) == root_key
    except ValueError:
        return False


def sync_after_restore(report_path: str | Path, trash: str | None) -> None:
    """还原之后重算状态：被还原的组必须回到「待处理」。

    只按**本隔离区** manifest 的 completed 动作重算：清除 manifest 落在本
    隔离区、且已经没有完成动作的记录；其他隔离区的 handled 记录与用户显式
    标记的 ``ignored`` / ``pending`` 原样保留。报告文件不存在或身份不匹配时
    静默跳过 —— 还原本身已经完成，状态只是展示层的信息。
    """
    if not report_path or not trash:
        return
    try:
        document = load_report(report_path)
    except (ReportError, OSError, ValueError):
        return
    try:
        store = ReportStateStore(document.path)
        with _lock_for(store.path):
            if not store.load(report_identity(document)):
                return
            groups = store.groups
            manifest_groups, _ = scan_manifest_states(document.path, trash)
            trash_key = _directory_key(trash)
            for group_id, record in list(groups.items()):
                if record.state in ("ignored", "pending"):
                    continue
                # 没有 manifest 字段的记录无法归属到某个隔离区（旧版状态文件
                # 或测试构造），一并保守清除；仍处于 completed 的会由下面的
                # manifest 扫描补回来
                if record.manifest is None or _is_within(record.manifest, trash_key):
                    if group_id not in manifest_groups:
                        groups.pop(group_id)
            for group_id, record in manifest_groups.items():
                groups.setdefault(group_id, record)
            store.replace_groups(groups)
    except OSError:
        return


__all__ = [
    "GROUP_STATES",
    "EffectiveState",
    "GroupState",
    "ReportStateError",
    "ReportStateStore",
    "STATE_SUFFIX",
    "load_effective_state",
    "record_apply_result",
    "report_identity",
    "scan_manifest_states",
    "set_manual_group_state",
    "state_path_for",
    "sync_after_restore",
]
