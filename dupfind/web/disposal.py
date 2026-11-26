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
from ..report_state import record_apply_result
from ..restore import EmptyOptions, RestoreError, RestoreOptions, run_empty, run_restore
from ..trash import TrashBusy, TrashError, TrashLock
from ..units import format_size
from ..win32 import long_path

MAX_TASKS = 20
# 超过这个规模的处置要求前端输入 DELETE 二次确认（与 CLI 的 empty 一致）
CONFIRM_THRESHOLD_FILES = 20
CONFIRM_THRESHOLD_BYTES = 1 << 30
#: 一次处置超过这么多组，同样要求输入 DELETE：单看文件数会漏掉「全是小组」
#: 但涉及上千个目录、几乎无法人工复核的批量操作。
CONFIRM_THRESHOLD_GROUPS = 100


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
    skippedByReason: list[dict[str, Any]]
    missingFiles: int
    estimatedSeconds: int
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
            "skippedByReason": self.skippedByReason,
            "missingFiles": self.missingFiles,
            "estimatedSeconds": self.estimatedSeconds,
            "byDirectory": self.byDirectory,
            "byExtension": self.byExtension,
            "skipped": self.skipped,
            "keepMode": self.keepMode,
            "trash": self.trash,
            "dryRun": self.dryRun,
        }


#: 内容复核的粗略耗时（5400rpm HDD 串行实测约 78ms/文件）。只用于给用户一个
#: 量级，不能当作承诺：SSD 上会快一个数量级，网络盘上会更慢。
SECONDS_PER_FILE_VERIFY = 0.078
SECONDS_PER_FILE_MOVE = 0.012


def _missing_files(plan: DisposalPlan) -> int:
    """快速检查待移动文件是否仍然存在（目录级 scandir，避免逐文件 stat）。"""
    directory_cache: dict[str, set[str]] = {}
    missing = 0
    for planned in plan.groups:
        for cluster in planned.removals:
            for item in cluster:
                directory = ntpath.dirname(item.path)
                key = os.path.normcase(directory)
                names = directory_cache.get(key)
                if names is None:
                    names = set()
                    try:
                        with os.scandir(long_path(directory)) as iterator:
                            for entry in iterator:
                                names.add(os.path.normcase(entry.name))
                    except (OSError, ValueError):
                        # NUL 等非法路径会抛 ValueError；预览不能因此整页 500
                        names = set()
                    directory_cache[key] = names
                if os.path.normcase(ntpath.basename(item.path)) not in names:
                    missing += 1
    return missing


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
    reason_counts = Counter(reason for _, reason in plan.skipped)
    per_file = SECONDS_PER_FILE_MOVE + (SECONDS_PER_FILE_VERIFY if options.verify_content else 0.0)
    return PreviewPayload(
        groupCount=plan.group_count,
        fileCount=file_count,
        freedBytes=freed,
        freedText=format_size(freed),
        requiresConfirmation=(
            file_count > CONFIRM_THRESHOLD_FILES
            or freed > CONFIRM_THRESHOLD_BYTES
            or plan.group_count > CONFIRM_THRESHOLD_GROUPS
        ),
        skippedByReason=[
            {"reason": reason, "groups": count} for reason, count in reason_counts.most_common(12)
        ],
        missingFiles=_missing_files(plan) if file_count else 0,
        estimatedSeconds=int(file_count * per_file),
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
    #: 终态时供前端就地更新视图：哪些组已处置 / 部分处置 / 仍未处理
    handled_group_ids: list[int] = field(default_factory=list)
    partial_group_ids: list[int] = field(default_factory=list)
    remaining_group_ids: list[int] = field(default_factory=list)
    state_written: bool = True
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
            "handledGroupIds": list(self.handled_group_ids),
            "partialGroupIds": list(self.partial_group_ids),
            "remainingGroupIds": list(self.remaining_group_ids),
            "stateWritten": self.state_written,
        }


class DisposalTaskManager:
    """后台执行处置任务；与扫描任务管理器并行存在、互不干扰。"""

    def __init__(self) -> None:
        self._tasks: dict[str, DisposalTask] = {}
        self._lock = threading.Lock()

    def get(self, task_id: str) -> DisposalTask | None:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """请求停止一个 apply 任务；只在组边界生效（不会把一组拆开）。"""
        task = self._tasks.get(task_id)
        if task is None or task.state != "running" or task.kind != "apply":
            return False
        task.cancel_event.set()
        return True

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
        plan = None
        # 终态（succeeded/failed/cancelled）直到 finally 复制完统计后才发布：
        # 否则轮询可能先看到「成功、移动 0 个」，前端会失去还原入口
        outcome = "failed"
        try:
            plan = build_plan(document, options, group_ids)
            task.total_groups = plan.group_count
            task.phase = "applying"
            if plan.group_count == 0:
                task.message = "没有可处置的确认组"
                outcome = "succeeded"
            else:
                with capture():
                    execute_plan(
                        document,
                        plan,
                        options,
                        on_progress=lambda done, total: self._progress(task, done, total),
                        stats=stats,
                        should_stop=task.cancel_event.is_set,
                    )
                if stats.cancelled:
                    outcome = "cancelled"
                    task.message = f"已停止：完成 {len(stats.processed_group_ids)} / {plan.group_count} 组"
                elif stats.failed_files:
                    task.error = f"{stats.failed_files} 个文件移动失败"
                else:
                    outcome = "succeeded"
        except (ExecutionError, TrashError, RestoreError, OSError) as exc:
            task.error = str(exc)
        except Exception as exc:  # 后台任务必须把错误落到任务状态里
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 无论成功失败，都如实报告已经移动了多少 —— 前端据此决定是否
            # 提供「还原 / 清空隔离区」入口
            task.moved_files = stats.moved_files
            task.moved_bytes = stats.moved_bytes
            task.skipped_files = stats.skipped_files
            task.failed_files = stats.failed_files
            task.handled_group_ids = [
                group_id for group_id, (state, _, _) in stats.group_results.items() if state == "handled"
            ]
            task.partial_group_ids = [
                group_id for group_id, (state, _, _) in stats.group_results.items() if state == "partial"
            ]
            if plan is not None:
                processed = set(stats.processed_group_ids)
                task.remaining_group_ids = [
                    item.group_id for item in plan.groups if item.group_id not in processed
                ]
            if stats.group_results:
                try:
                    if not record_apply_result(document, stats.group_results, manifest=stats.manifest_path):
                        task.state_written = False
                except OSError:
                    task.state_written = False
                if not task.state_written:
                    note = "处置状态未能写入；下次加载报告时这些组可能仍显示为待处理"
                    task.message = f"{task.message}（{note}）" if task.message else note
            task.phase = "done"
            task.finished_at = time.time()
            task.state = outcome
            lock.release()

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
            with capture() as output:
                code = run_empty(trash, options)
            task.message = "隔离区已清空" if code == 0 else "部分文件删除失败"
            task.state = "failed" if code == 2 else "succeeded"
            if code == 2:
                # 把逐文件的失败原因带给前端。只说「部分文件删除失败」，用户
                # 不知道是哪个文件、为什么删不掉，也就无从处理。
                task.error = _failure_detail(output.getvalue())
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


def _failure_detail(output: str, limit: int = 5) -> str:
    """从 CLI 输出里挑出 ``[失败]`` 行，作为给用户看的原因。"""
    lines = [line.strip() for line in output.splitlines() if "[失败]" in line]
    if not lines:
        return "部分文件删除失败"
    shown = lines[:limit]
    if len(lines) > limit:
        shown.append(f"…… 另有 {len(lines) - limit} 个")
    return "\n".join(shown)


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
