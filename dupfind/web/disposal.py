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
from ..report_state import EffectiveState, record_apply_result
from ..restore import (
    JOURNAL_SUFFIX,
    EmptyOptions,
    RestoreError,
    RestoreOptions,
    RestoreStats,
    discover_manifests,
    load_manifest,
    run_empty,
    run_restore,
    validate_action,
)
from ..safety import CanonicalCache, canonical
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
#: 还原预览的读取边界：拒绝静默截断，超出部分会在结果里标记 truncated。
MAX_PREVIEW_MANIFESTS = 500
MAX_PREVIEW_MANIFEST_BYTES = 64 * 1024 * 1024
#: 单个预览最多逐动作校验多少个动作；超出时结果标记 truncated，前端禁用还原。
MAX_PREVIEW_ACTIONS = 5000


class RestoreScopeError(RuntimeError):
    """组级还原范围不合法：组不存在、没有处置记录，或 manifest 与报告不符。"""


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
    #: 还原任务的统计。moved_* 只用于 apply，restore 单独一组字段，避免前端
    #: 把「还原了 3 个」误读成「移动了 3 个到隔离区」。
    restored_files: int = 0
    restored_bytes: int = 0
    already_restored: int = 0
    missing_files: int = 0
    state_synced: bool = True
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
            "restoredFiles": self.restored_files,
            "restoredBytes": self.restored_bytes,
            "restoredText": format_size(self.restored_bytes),
            "alreadyRestored": self.already_restored,
            "missingFiles": self.missing_files,
            "stateSynced": self.state_synced,
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
        target = _trash_key(trash)
        with self._lock:
            for task in self._tasks.values():
                if task.state != "running":
                    continue
                if _trash_key(task.trash) == target:
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

    def start_restore(
        self, target: str, options: RestoreOptions, *, report_path: str = ""
    ) -> DisposalTask:
        task, lock = self._claim("restore", target, report_path)
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
        stats = RestoreStats()
        outcome = "failed"
        try:
            task.phase = "restoring"
            with capture():
                code = run_restore(target, options, stats=stats)
            task.message = "还原完成" if code == 0 else ("没有可还原的动作" if code == 1 else "部分动作失败")
            if not stats.state_synced:
                note = "处置状态未能同步；页面可能仍把这些组显示为已处置"
                task.message = f"{task.message}（{note}）"
            elif stats.unreadable_manifests:
                task.message = f"{task.message}（跳过 {stats.unreadable_manifests} 份无法读取的记录）"
            outcome = "failed" if code == 2 else "succeeded"
        except (RestoreError, OSError) as exc:
            task.error = str(exc)
        except Exception as exc:
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 与 apply 一样：统计先于终态发布，前端轮询到 succeeded 时数字已经就绪
            task.restored_files = stats.restored
            task.restored_bytes = stats.restored_bytes
            task.already_restored = stats.already_restored
            task.missing_files = stats.missing
            task.skipped_files = stats.skipped
            task.failed_files = stats.failed
            task.state_synced = stats.state_synced
            task.phase = "done"
            task.finished_at = time.time()
            # 终态最后发布：轮询看到 succeeded 时其余字段都已就绪
            task.state = outcome
            lock.release()

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


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _trash_key(path: str) -> str:
    """同一物理隔离区的 junction / 8.3 短名写法必须落到同一个键。"""
    try:
        return canonical(path)
    except (OSError, ValueError):
        return _path_key(path)


def _manifest_report_key(document) -> str | None:
    """manifest 的 report 只认绝对路径；相对路径是旧版产物，无法可靠归属。"""
    if not document.report:
        return None
    text = document.report.replace("/", "\\")
    drive, _ = ntpath.splitdrive(text)
    if not ntpath.isabs(text) or not drive:
        return None
    try:
        return _path_key(text)
    except (OSError, ValueError):
        return None


def _restore_group_sources(document: ReportDocument) -> dict[int, frozenset[str]]:
    """每个组的文件路径集合，用于确认 manifest 动作确实属于当前报告的该组。"""
    return {
        group.id: frozenset(_path_key(item.path) for item in group.files)
        for group in document.groups
    }


def restore_target_groups(
    document: ReportDocument, state: EffectiveState, group_ids: list[int] | None
) -> list[int]:
    """这份报告的还原目标组。

    状态文件丢失时 ``load_effective_state`` 会用 manifest 回填组号；如果报告
    被重新扫描过，回填出的旧组号可能已经不存在于当前报告。这里必须与报告
    的组集合求交 —— 否则下游会用旧组号索引只包含报告组的映射。
    """
    eligible = {
        group_id
        for group_id, record in state.groups.items()
        if record.state in ("handled", "partial")
    }
    report_group_ids = {group.id for group in document.groups}
    if group_ids is None:
        return sorted(eligible & report_group_ids)
    wanted = sorted(set(group_ids))
    for group_id in wanted:
        if group_id not in report_group_ids:
            raise RestoreScopeError(f"报告中不存在第 {group_id} 组")
        if group_id not in eligible:
            raise RestoreScopeError(f"第 {group_id} 组没有可还原的处置记录")
    return wanted


def restore_trash_candidates(document: ReportDocument, state: EffectiveState) -> list[str]:
    """这份报告的处置记录可能落在哪些隔离区。

    默认隔离区 + 状态文件里每条 handled/partial 记录的 manifest 父目录。
    状态文件丢失后仍能从 manifest 回填的组也带 manifest 路径，因此自定义
    隔离区同样能被发现；完全无法归属的旧批次由 CLI 整区还原兜底。
    """
    candidates: list[str] = []
    try:
        candidates.append(default_trash_for(document.root))
    except RestoreError:
        pass
    for record in state.groups.values():
        if record.state not in ("handled", "partial") or not record.manifest:
            continue
        parent = ntpath.dirname(record.manifest)
        if parent:
            candidates.append(parent)
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            absolute = ntpath.abspath(path)
        except (OSError, ValueError):
            continue
        # 同一物理隔离区的 junction / 8.3 短名写法要合并，否则同一批
        # manifest 会被扫描两次，crossTrashGroupIds 把每个组误判成跨隔离区。
        try:
            key = canonical(absolute)
        except (OSError, ValueError):
            key = _path_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        result.append(absolute)
    return result


def _manifest_file_size(path) -> int:
    """预览读取上限要算上 journal：仅 journal 的崩溃记录没有快照文件。"""
    total = 0
    for candidate in (str(path), str(path) + JOURNAL_SUFFIX):
        try:
            total += os.path.getsize(candidate)
        except OSError:
            pass
    return total


def _action_kind(action, trash: str, *, cache: CanonicalCache | None = None) -> str:
    """预览用：这个动作执行时会归入哪一类（与 _restore_one 的顺序一致）。"""
    # 真实执行第一步就是 _validate_action；预览必须先跑同一套校验，否则
    # 非法 dst / 受保护 src 的动作会被算进「将还原」，执行时才报失败。
    if validate_action(action, trash, cache=cache) is not None:
        return "rejected"
    src_exists = os.path.exists(long_path(action.src))
    dst_exists = os.path.exists(long_path(action.dst))
    if src_exists and dst_exists:
        try:
            if os.path.samefile(long_path(action.src), long_path(action.dst)):
                return "already"
        except OSError:
            pass
        # 默认 on_conflict=skip 会跳过；rename/overwrite 才会动
        return "conflict"
    if src_exists and not dst_exists:
        return "already" if action.status == "completed" else "pending"
    if not dst_exists:
        return "missing"
    return "restorable"


def build_restore_preview(
    document: ReportDocument,
    state: EffectiveState,
    group_ids: list[int] | None = None,
) -> dict[str, Any]:
    """报告级/组级还原的只读预览：不写文件、不拿隔离区锁。

    只扫描两种目录：默认隔离区，以及状态文件里记录过的 manifest 父目录。
    不会接受任意路径，避免把用户资料目录里的同名文件当 manifest 读。
    """
    all_eligible = {
        group_id
        for group_id, record in state.groups.items()
        if record.state in ("handled", "partial")
    }
    report_group_ids = {group.id for group in document.groups}
    stale = sorted(all_eligible - report_group_ids)
    targets = restore_target_groups(document, state, group_ids)
    sources = _restore_group_sources(document)
    target_set = set(targets)
    report_key = _path_key(document.path)
    group_files = {group_id: 0 for group_id in targets}
    group_bytes = {group_id: 0 for group_id in targets}
    group_trashes: dict[int, set[str]] = {group_id: set() for group_id in targets}
    collected: list[tuple[int, str, Any, dict[str, Any]]] = []
    unsafe: set[int] = set()
    trashes: list[dict[str, Any]] = []
    unscoped = 0
    truncated = False

    for trash in restore_trash_candidates(document, state):
        bucket: dict[str, Any] = {
            "path": trash,
            "exists": os.path.isdir(trash),
            "files": 0,
            "bytes": 0,
            "groups": 0,
            "groupIds": [],
            "missing": 0,
            "alreadyRestored": 0,
            "skippedPending": 0,
            "conflicts": 0,
            "rejected": 0,
            "unreadable": 0,
            "unscopedManifests": 0,
            "otherReportManifests": 0,
            "truncated": False,
        }
        trashes.append(bucket)
        if not bucket["exists"]:
            continue
        try:
            manifests = discover_manifests(trash)
        except RestoreError:
            # 目录存在但不是隔离区：可能是默认隔离区尚未创建
            continue
        if len(manifests) > MAX_PREVIEW_MANIFESTS:
            manifests = manifests[:MAX_PREVIEW_MANIFESTS]
            bucket["truncated"] = True
            truncated = True
        for path in manifests:
            if len(collected) >= MAX_PREVIEW_ACTIONS:
                bucket["truncated"] = True
                truncated = True
                break
            try:
                # journal-only 记录的「快照路径」并不存在，getsize 要看快照与
                # journal 的总和；否则崩溃恢复记录会被当成不可读跳过。
                if _manifest_file_size(path) > MAX_PREVIEW_MANIFEST_BYTES:
                    bucket["unreadable"] += 1
                    bucket["truncated"] = True
                    truncated = True
                    continue
                manifest = load_manifest(path)
            except (OSError, RestoreError):
                bucket["unreadable"] += 1
                continue
            manifest_key = _manifest_report_key(manifest)
            if manifest_key is None:
                # 旧版相对路径 / 无 report / journal-only：无法可靠归属
                bucket["unscopedManifests"] += 1
                unscoped += 1
                continue
            if manifest_key != report_key:
                # 同一隔离区里其他报告的批次，不是「旧格式」，不能误导用户
                bucket["otherReportManifests"] += 1
                continue
            for action in manifest.restorable:
                if action.group_id in target_set:
                    if len(collected) >= MAX_PREVIEW_ACTIONS:
                        bucket["truncated"] = True
                        truncated = True
                        break
                    collected.append((action.group_id, trash, action, bucket))
        if len(collected) >= MAX_PREVIEW_ACTIONS:
            break

    for group_id, _, action, _ in collected:
        if _path_key(action.src) not in sources.get(group_id, frozenset()):
            unsafe.add(group_id)

    caches: dict[str, CanonicalCache] = {}
    for group_id, trash, action, bucket in collected:
        if group_id in unsafe:
            continue
        cache = caches.get(trash)
        if cache is None:
            cache = CanonicalCache()
            caches[trash] = cache
        kind = _action_kind(action, trash, cache=cache)
        if kind == "restorable":
            group_files[group_id] += 1
            group_bytes[group_id] += action.size
            group_trashes[group_id].add(trash)
            bucket["files"] += 1
            bucket["bytes"] += action.size
            continue
        if kind == "already":
            bucket["alreadyRestored"] += 1
        elif kind == "missing":
            bucket["missing"] += 1
        elif kind == "pending":
            bucket["skippedPending"] += 1
        elif kind == "rejected":
            bucket["rejected"] += 1
        else:
            bucket["conflicts"] += 1

    for bucket in trashes:
        bucket["groupIds"] = sorted(
            group_id for group_id in targets if bucket["path"] in group_trashes[group_id]
        )
        bucket["groups"] = len(bucket["groupIds"])

    # 只要收集到任何动作（含 already/missing/conflict），这组就不算「没找到」；
    # 纯幂等收尾的组不能同时被提示成「没有可还原的文件」。
    seen = {group_id for group_id, _, _, _ in collected}
    cross_trash = sorted(
        group_id for group_id, buckets in group_trashes.items() if len(buckets) > 1
    )
    groups_payload = [
        {
            "groupId": group_id,
            "files": group_files[group_id],
            "bytes": group_bytes[group_id],
            "text": format_size(group_bytes[group_id]),
        }
        for group_id in targets
        if group_files[group_id] > 0
    ]
    total_files = sum(group_files.values())
    total_bytes = sum(group_bytes.values())
    return {
        "reportPath": str(document.path),
        "identityOk": state.identity_ok,
        "groupIds": targets,
        "groups": groups_payload,
        "trashes": trashes,
        "crossTrashGroupIds": cross_trash,
        "unsafeGroupIds": sorted(unsafe),
        "missingGroupIds": sorted(set(targets) - seen - unsafe),
        "staleGroupIds": stale,
        "unscopedManifests": unscoped,
        "totalOtherReportManifests": sum(
            bucket["otherReportManifests"] for bucket in trashes
        ),
        "truncated": truncated,
        "totalFiles": total_files,
        "totalBytes": total_bytes,
        "totalText": format_size(total_bytes),
        "totalAlready": sum(bucket["alreadyRestored"] for bucket in trashes),
        "totalRejected": sum(bucket["rejected"] for bucket in trashes),
        "totalUnreadable": sum(bucket["unreadable"] for bucket in trashes),
    }


def scan_restore_scope(
    document: ReportDocument, trash: str, group_ids: list[int]
) -> set[int]:
    """返回动作与当前报告组不一致的组号（不抛错，供报告级排除）。

    只靠 groupId 不够：状态文件丢失时 ``load_effective_state`` 会用旧 manifest
    回填组号，而报告重扫后组号已经重排。动作的 ``src`` 路径是更硬的证据 ——
    它必须出现在当前报告该组的文件列表里。

    读不出来的 manifest 与执行侧一致地跳过（``run_restore`` 按报告过滤时也
    不会读它）；超过预览上限的 manifest 执行侧却会照常读取，因此必须拒绝，
    不能静默放行未经校验的动作。
    """
    sources = _restore_group_sources(document)
    for group_id in group_ids:
        if group_id not in sources:
            raise RestoreScopeError(f"报告中不存在第 {group_id} 组")
    try:
        manifests = discover_manifests(trash)
    except RestoreError:
        return set()
    if len(manifests) > MAX_PREVIEW_MANIFESTS:
        raise RestoreScopeError(
            f"隔离区 manifest 超过 {MAX_PREVIEW_MANIFESTS} 份，无法安全校验组归属；"
            "请用 CLI 整区还原"
        )
    report_key = _path_key(document.path)
    target_set = set(group_ids)
    unsafe: set[int] = set()
    for path in manifests:
        if _manifest_file_size(path) > MAX_PREVIEW_MANIFEST_BYTES:
            raise RestoreScopeError(
                "隔离区里有超过读取上限的 manifest，无法校验组归属；请用 CLI 整区还原"
            )
        try:
            manifest = load_manifest(path)
        except (OSError, RestoreError):
            continue
        if _manifest_report_key(manifest) != report_key:
            continue
        for action in manifest.restorable:
            if action.group_id in target_set and _path_key(action.src) not in sources[action.group_id]:
                unsafe.add(action.group_id)
    return unsafe


def validate_restore_scope(
    document: ReportDocument, trash: str, group_ids: list[int]
) -> None:
    """执行前的组级安全校验：任何组与当前报告不一致就整批拒绝。"""
    unsafe = scan_restore_scope(document, trash, group_ids)
    if unsafe:
        listed = "、".join(str(group_id) for group_id in sorted(unsafe))
        raise RestoreScopeError(
            f"第 {listed} 组的隔离区记录与当前报告不一致"
            "（报告可能已重新扫描），已拒绝按组还原"
        )


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
    "RestoreScopeError",
    "TrashBusy",
    "TrashLock",
    "build_preview",
    "build_restore_preview",
    "default_trash_for",
    "restore_target_groups",
    "restore_trash_candidates",
    "scan_restore_scope",
    "validate_restore_scope",
    "validate_trash_path",
]
