"""Web 处置闭环：预览、执行、还原、清空。

这一层只做三件事，全部复用 CLI 已有的逻辑：

* :func:`build_preview` —— 把报告算成「会移动哪些文件、释放多少」的预览；
* :class:`DisposalTaskManager` —— 后台线程执行，前端轮询进度；
* :func:`run_restore_task` / :func:`run_empty_task` —— 复用 ``restore.py``。

安全约定与 CLI 完全一致：受保护路径不动、隔离区副本不当保留项、内容复核、
manifest 先写后移动。Web 只是换了个入口，不新增任何判定规则。
"""

from __future__ import annotations

import ntpath
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..apply import ApplyOptions, ApplyStats, DisposalPlan, ExecutionError, build_plan, execute_plan
from ..console import capture
from ..report import ReportDocument
from ..restore import EmptyOptions, RestoreError, RestoreOptions, run_empty, run_restore
from ..trash import TrashError
from ..units import format_size
from ..win32 import close_handle, open_exclusive

MAX_TASKS = 20
#: 隔离区锁文件的后缀，放在隔离区**外面**：放进隔离区会被 empty 当成 payload 删掉
TRASH_LOCK_SUFFIX = ".lock"
# 超过这个规模的处置要求前端输入 DELETE 二次确认（与 CLI 的 empty 一致）
CONFIRM_THRESHOLD_FILES = 20
CONFIRM_THRESHOLD_BYTES = 1 << 30


@dataclass(slots=True)
class DirectoryBucket:
    directory: str
    files: int
    bytes: int


@dataclass(slots=True)
class PreviewPayload:
    """执行前预览：用户必须能看清「按目录/扩展名聚合后要动什么」。"""

    groupCount: int
    fileCount: int
    freedBytes: int
    freedText: str
    requiresConfirmation: bool
    byDirectory: list[dict[str, Any]]
    byExtension: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    keepMode: str
    trash: str
    dryRun: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "groupCount": self.groupCount,
            "fileCount": self.fileCount,
            "freedBytes": self.freedBytes,
            "freedText": self.freedText,
            "requiresConfirmation": self.requiresConfirmation,
            "byDirectory": self.byDirectory,
            "byExtension": self.byExtension,
            "skipped": self.skipped,
            "keepMode": self.keepMode,
            "trash": self.trash,
            "dryRun": self.dryRun,
        }


def _directory_key(path: str, depth: int) -> str:
    """把目录压成「盘符 + 末尾 depth 层」，中间省略。

    取**开头**几层会把用户目录塌成一条：``C:\\Users\\me`` 下面 Downloads、
    Documents、Desktop 全归一处，预览就失去意义。取末尾几层才是用户认得的
    位置（``C:\\…\\me\\Downloads``），条数也仍然有界。
    """
    parts = [part for part in ntpath.dirname(path).split(ntpath.sep) if part]
    if not parts:
        return path
    drive, rest = parts[0], parts[1:]
    if len(rest) <= depth:
        return ntpath.sep.join(parts)
    return ntpath.sep.join([drive, "…", *rest[-depth:]])


def _aggregate(
    document: ReportDocument, plan: DisposalPlan, *, depth: int = 2
) -> tuple[list[DirectoryBucket], Counter[str], Counter[str]]:
    """按目录（取末尾 depth 层）与扩展名聚合待移动文件。"""
    directories: Counter[str] = Counter()
    directory_bytes: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    extension_bytes: Counter[str] = Counter()

    by_id = {group.id: group for group in document.groups}
    for planned in plan.groups:
        group = by_id.get(planned.group_id)
        if group is None:
            continue
        for cluster in planned.removals:
            for item in cluster:
                key = _directory_key(item.path, depth)
                size = item.size_on_disk if item.size_on_disk is not None else group.size
                directories[key] += 1
                directory_bytes[key] += size
                extension = ntpath.splitext(item.path)[1].lower() or "(无扩展名)"
                extensions[extension] += 1
                extension_bytes[extension] += size

    buckets = [
        DirectoryBucket(directory=key, files=count, bytes=directory_bytes[key])
        for key, count in directories.most_common(12)
    ]
    return buckets, extensions, extension_bytes


def build_preview(
    document: ReportDocument,
    options: ApplyOptions,
    group_ids: list[int] | None = None,
) -> PreviewPayload:
    """算出预览；不写任何文件。"""
    plan = build_plan(document, options, group_ids)
    buckets, extensions, extension_bytes = _aggregate(document, plan)
    file_count = plan.file_count
    freed = plan.freed_bytes
    return PreviewPayload(
        groupCount=plan.group_count,
        fileCount=file_count,
        freedBytes=freed,
        freedText=format_size(freed),
        requiresConfirmation=file_count > CONFIRM_THRESHOLD_FILES or freed > CONFIRM_THRESHOLD_BYTES,
        byDirectory=[
            {"directory": bucket.directory, "files": bucket.files, "bytes": bucket.bytes, "text": format_size(bucket.bytes)}
            for bucket in buckets
        ],
        byExtension=[
            {"extension": extension, "files": count, "bytes": extension_bytes[extension], "text": format_size(extension_bytes[extension])}
            for extension, count in extensions.most_common(10)
        ],
        skipped=[{"groupId": group_id, "reason": reason} for group_id, reason in plan.skipped],
        keepMode=plan.mode,
        trash=options.trash_dir,
        dryRun=options.dry_run,
    )


@dataclass
class DisposalTask:
    """一次处置/还原/清空任务的状态快照。"""

    id: str
    kind: str  # apply / restore / empty
    state: str = "running"  # running / succeeded / failed / cancelled
    phase: str = "planning"
    total_groups: int = 0
    processed_groups: int = 0
    moved_files: int = 0
    moved_bytes: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    message: str | None = None
    error: str | None = None
    trash: str = ""
    report_path: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "phase": self.phase,
            "totalGroups": self.total_groups,
            "processedGroups": self.processed_groups,
            "movedFiles": self.moved_files,
            "movedBytes": self.moved_bytes,
            "movedText": format_size(self.moved_bytes),
            "skippedFiles": self.skipped_files,
            "failedFiles": self.failed_files,
            "message": self.message,
            "error": self.error,
            "trash": self.trash,
            "reportPath": self.report_path,
            "durationMs": int(elapsed * 1000),
        }


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
    """跨进程的隔离区互斥（独占句柄，进程退出即释放）。

    为什么必须有：apply 把文件移入 ``<盘符>:\\_dupfind_trash`` 的过程中，
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


class DisposalTaskManager:
    """后台执行处置任务；与扫描任务管理器并行存在、互不干扰。"""

    def __init__(self) -> None:
        self._tasks: dict[str, DisposalTask] = {}
        self._lock = threading.Lock()

    def get(self, task_id: str) -> DisposalTask | None:
        return self._tasks.get(task_id)

    def last(self) -> DisposalTask | None:
        with self._lock:
            if not self._tasks:
                return None
            return max(self._tasks.values(), key=lambda task: task.started_at)

    def running_for(self, trash: str) -> DisposalTask | None:
        """同一隔离区上仍在跑的任务（本进程内）。"""
        target = os.path.normcase(os.path.abspath(trash))
        with self._lock:
            for task in self._tasks.values():
                if task.state != "running":
                    continue
                if os.path.normcase(os.path.abspath(task.trash)) == target:
                    return task
        return None

    def _new(self, kind: str, *, trash: str, report_path: str = "") -> DisposalTask:
        task = DisposalTask(id=str(uuid.uuid4()), kind=kind, trash=trash, report_path=report_path)
        with self._lock:
            self._prune()
            self._tasks[task.id] = task
        return task

    def _prune(self) -> None:
        if len(self._tasks) < MAX_TASKS:
            return
        finished = sorted(
            (task for task in self._tasks.values() if task.state != "running"),
            key=lambda task: task.started_at,
        )
        for task in finished[: len(self._tasks) - MAX_TASKS + 1]:
            self._tasks.pop(task.id, None)

    def _claim(self, kind: str, trash: str, report_path: str = "") -> tuple[DisposalTask, TrashLock]:
        """登记任务并**同步**占住隔离区锁。

        锁必须在返回前拿到：放到后台线程里抢锁的话，两个请求都会拿到 200，
        用户看到两个「进行中」，后到的那个才默默失败。
        """
        if self.running_for(trash) is not None:
            raise TrashBusy(f"该隔离区已有任务在执行：{trash}")
        lock = TrashLock(trash)
        lock.acquire()
        try:
            task = self._new(kind, trash=trash, report_path=report_path)
        except BaseException:
            lock.release()
            raise
        return task, lock

    # --- 启动 -------------------------------------------------------

    def start_apply(
        self,
        document: ReportDocument,
        options: ApplyOptions,
        group_ids: list[int] | None,
    ) -> DisposalTask:
        task, lock = self._claim("apply", options.trash_dir, str(document.path))
        thread = threading.Thread(
            target=self._run_apply,
            args=(task, document, options, group_ids, lock),
            name=f"dupfind-apply-{task.id[:8]}",
            daemon=True,
        )
        thread.start()
        return task

    def start_restore(self, target: str, options: RestoreOptions) -> DisposalTask:
        task, lock = self._claim("restore", target, target)
        thread = threading.Thread(
            target=self._run_restore,
            args=(task, target, options, lock),
            name=f"dupfind-restore-{task.id[:8]}",
            daemon=True,
        )
        thread.start()
        return task

    def start_empty(self, trash: str, options: EmptyOptions) -> DisposalTask:
        task, lock = self._claim("empty", trash)
        thread = threading.Thread(
            target=self._run_empty,
            args=(task, trash, options, lock),
            name=f"dupfind-empty-{task.id[:8]}",
            daemon=True,
        )
        thread.start()
        return task

    # --- 执行 -------------------------------------------------------

    def _run_apply(
        self,
        task: DisposalTask,
        document: ReportDocument,
        options: ApplyOptions,
        group_ids: list[int] | None,
        lock: TrashLock,
    ) -> None:
        # execute_plan 会逐组打印处置明细（CLI 需要）；Web 下收进线程局部缓冲区，
        # 避免污染服务端日志，也不会像 redirect_stdout 那样改全局 sys.stdout。
        # stats 在 execute_plan 之外持有：中途抛异常时，已经移动的文件数只存在
        # 这个对象里。拿不到它就会向用户报「移动 0 个」，而文件其实在隔离区，
        # 用户既不知道要还原，也可能直接去清空。
        stats = ApplyStats()
        try:
            plan = build_plan(document, options, group_ids)
            task.total_groups = plan.group_count
            task.phase = "applying"
            if plan.group_count == 0:
                task.message = "没有可处置的确认组"
                task.state = "succeeded"
                return
            with capture():
                execute_plan(
                    document,
                    plan,
                    options,
                    on_progress=lambda done, total: self._progress(task, done, total),
                    stats=stats,
                )
            task.state = "failed" if stats.failed_files else "succeeded"
            if stats.failed_files:
                task.error = f"{stats.failed_files} 个文件移动失败"
        except (ExecutionError, TrashError, RestoreError, OSError) as exc:
            task.state = "failed"
            task.error = str(exc)
        except Exception as exc:  # 后台任务必须把错误落到任务状态里
            task.state = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 无论成功失败，都如实报告已经移动了多少 —— 前端据此决定是否
            # 提供「还原 / 清空隔离区」入口
            task.moved_files = stats.moved_files
            task.moved_bytes = stats.moved_bytes
            task.skipped_files = stats.skipped_files
            task.failed_files = stats.failed_files
            lock.release()
            task.phase = "done"
            task.finished_at = time.time()

    def _progress(self, task: DisposalTask, done: int, total: int) -> None:
        task.processed_groups = done
        task.total_groups = total

    def _run_restore(
        self, task: DisposalTask, target: str, options: RestoreOptions, lock: TrashLock
    ) -> None:
        # run_restore / run_empty 是给 CLI 写的，直接 print 到 stdout。Web 下把
        # 这些输出收进线程局部缓冲区，既避免污染服务端日志，也能在失败时当作诊断信息。
        try:
            task.phase = "restoring"
            with capture():
                code = run_restore(target, options)
            task.message = "还原完成" if code == 0 else ("没有可还原的动作" if code == 1 else "部分动作失败")
            task.state = "failed" if code == 2 else "succeeded"
        except (RestoreError, OSError) as exc:
            task.state = "failed"
            task.error = str(exc)
        except Exception as exc:
            task.state = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            lock.release()
            task.phase = "done"
            task.finished_at = time.time()

    def _run_empty(
        self, task: DisposalTask, trash: str, options: EmptyOptions, lock: TrashLock
    ) -> None:
        try:
            task.phase = "emptying"
            with capture():
                code = run_empty(trash, options)
            task.message = "隔离区已清空" if code == 0 else "部分文件删除失败"
            task.state = "failed" if code == 2 else "succeeded"
        except (RestoreError, OSError) as exc:
            task.state = "failed"
            task.error = str(exc)
        except Exception as exc:
            task.state = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            lock.release()
            task.phase = "done"
            task.finished_at = time.time()


def validate_trash_path(trash: str) -> str:
    """Web 传入的隔离区路径必须是本地绝对路径（真正的安全判定在 restore.py）。

    相对路径不在这里「顺手解析成当前目录」：隔离区是清空操作的目标，静默
    拼到进程的工作目录上会让用户以为清的是 ``D:\\_dupfind_trash``，实际清的
    是别处。宁可报错让用户写全。
    """
    text = trash.strip()
    if not ntpath.isabs(text):
        raise RestoreError(f"隔离区必须是本地绝对路径: {trash}")
    absolute = ntpath.abspath(text)
    drive, _ = ntpath.splitdrive(absolute)
    if not drive or not drive.endswith(":"):
        raise RestoreError(f"隔离区必须是本地绝对路径: {trash}")
    return absolute


def default_trash_for(root: str) -> str:
    """与 CLI 一致：``<盘符>:\\_dupfind_trash``。"""
    drive, _ = ntpath.splitdrive(os.path.abspath(root))
    if not drive or not drive.endswith(":"):
        raise RestoreError(f"无法从根路径推断盘符: {root}")
    return ntpath.join(drive + "\\", "_dupfind_trash")


__all__ = [
    "CONFIRM_THRESHOLD_BYTES",
    "CONFIRM_THRESHOLD_FILES",
    "DisposalTask",
    "DisposalTaskManager",
    "PreviewPayload",
    "TrashBusy",
    "TrashLock",
    "build_preview",
    "default_trash_for",
    "validate_trash_path",
]
