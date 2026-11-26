"""apply 命令：把报告中的确认重复文件批量移入 trash（默认 dry-run）。

处置以**物理簇**为单位：同一簇内的路径互为硬链接、只占一份空间，因此要么
整簇保留，要么整簇移走，绝不能只移动其中一部分。
"""

from __future__ import annotations

import ntpath
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .console import say
from .ignore import IgnoreList
from .report import ReportDocument, ReportFile, ReportGroup
from .report_filter import cluster_files, cluster_occupancy, effective_group
from .report_state import record_apply_result
from .safety import DEFAULT_SAMPLE_SIZE, CanonicalCache, canonical, content_matches, protected_reason
from .trash import Manifest, TrashError, TrashManager, validate_source_path
from .units import format_size
from .win32 import long_path, move_file

KEEP_MODES = ("shortest", "newest", "ask")
MTIME_TOLERANCE = 2.0


@dataclass(slots=True)
class ApplyOptions:
    trash_dir: str
    page_size: int = 10
    keep_mode: str = "ask"
    dry_run: bool = True
    assume_yes: bool = False
    min_savings: int = 0
    verify_content: bool = True
    sample_size: int = DEFAULT_SAMPLE_SIZE
    #: 用户忽略的目录；其中的文件不参与规划与执行（视图与处置共用同一规则）
    ignore: IgnoreList | None = None
    #: 已处置 / 已忽略的组，直接跳过，避免重复扫描已不存在的文件
    skip_group_ids: frozenset[int] = frozenset()


@dataclass(slots=True)
class ApplyStats:
    handled_groups: int = 0
    moved_files: int = 0
    moved_bytes: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    #: 预检时发现已不在原处的文件（多半是之前处置过、重试时又被计划）
    missing_files: int = 0
    refused_groups: int = 0
    #: 逐组结果 ``{groupId: (state, moved_files, moved_bytes)}``，state 为
    #: ``handled``（全部成功）或 ``partial``（有跳过/失败）。供状态文件记录。
    group_results: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    #: 已走到收尾的组（含 0 移动），用于计算取消后还剩哪些组
    processed_group_ids: list[int] = field(default_factory=list)
    #: 本次写出的 manifest（没有实际移动时为 None）
    manifest_path: str | None = None
    #: 用户在组边界处停止
    cancelled: bool = False


@dataclass(slots=True)
class PlannedGroup:
    """一个组已确定下来的处置方案（保留簇 + 待移簇）。"""

    group_id: int
    keep: list[ReportFile]
    removals: list[list[ReportFile]]
    freed_bytes: int

    @property
    def files_to_move(self) -> int:
        return sum(len(cluster) for cluster in self.removals)


@dataclass(slots=True)
class DisposalPlan:
    """一次批量处置的完整计划，供 CLI 之外的调用方（Web）预览与执行。

    ``skipped`` 里记录被拒绝的组与原因 —— 预览界面必须把这些展示出来，
    否则用户会以为「勾了 N 组就处理了 N 组」。
    """

    mode: str = "shortest"
    groups: list[PlannedGroup] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def file_count(self) -> int:
        return sum(group.files_to_move for group in self.groups)

    @property
    def freed_bytes(self) -> int:
        return sum(group.freed_bytes for group in self.groups)


class ExecutionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 物理簇与保留策略
# ---------------------------------------------------------------------------


def _identity(file: ReportFile) -> str:
    """同一物理文件的键；报告里没有 fileId 时退回路径。"""
    return file.file_id or ntpath.normcase(file.path)


def physical_clusters(group: ReportGroup) -> list[list[ReportFile]]:
    """把组内文件按物理身份聚簇，跳过不稳定文件。"""
    return cluster_files(group.files)


def _cluster_occupancy(cluster: list[ReportFile], fallback: int = 0) -> int:
    """一簇硬链接的实际占用：只算一份，取成员中最大者。

    报告缺少 ``sizeOnDisk`` 时退回组内逻辑大小，避免把可节省空间算成 0。
    """
    return cluster_occupancy(cluster, fallback)


def _normalized_key(file: ReportFile) -> tuple[int, int, str, str]:
    name = file.normalized_name or ntpath.basename(file.path)
    base = ntpath.basename(file.path)
    return (len(name), len(base), base.lower(), file.path.lower())


def choose_keep(files: list[ReportFile], mode: str) -> ReportFile:
    """在单个文件列表中按策略选出保留项。"""
    if mode == "newest":
        return max(files, key=lambda item: (item.mtime or 0.0, item.path))
    return min(files, key=_normalized_key)


def choose_keep_cluster(clusters: list[list[ReportFile]], mode: str) -> list[ReportFile]:
    """按策略选出保留的物理簇。

    ``shortest`` 选「含最短名称路径」的簇，``newest`` 选「mtime 最新」的簇。
    整簇保留，因此簇内所有硬链接路径都还在。
    """
    if mode == "newest":
        return max(clusters, key=lambda cluster: max((item.mtime or 0.0) for item in cluster))
    return min(clusters, key=lambda cluster: min(_normalized_key(item) for item in cluster))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


#: 预检发现文件已不在原处时的固定原因；调用方据此区分「已经处置过的
#: 旧副本」和「内容不符/受保护」这类真正阻止移动的问题
MISSING_PROBLEM = "文件不存在或不是普通文件"


def _validate_live_file(group: ReportGroup, item: ReportFile) -> str | None:
    if not os.path.isfile(long_path(item.path)):
        return MISSING_PROBLEM
    try:
        info = os.stat(long_path(item.path))
    except PermissionError:
        return "权限拒绝"
    except OSError as exc:
        return f"无法访问: {exc}"
    if int(info.st_size) != group.size:
        return f"大小已变化（{format_size(int(info.st_size))}）"
    if item.mtime is not None and abs(float(info.st_mtime) - item.mtime) > MTIME_TOLERANCE:
        return "修改时间已变化"
    return None


def _is_protected_cluster(cluster: list[ReportFile], cache: CanonicalCache | None = None) -> bool:
    return any(protected_reason(item.path, cache) is not None for item in cluster)


def _is_in_trash(path: str, trash: str | None, cache: CanonicalCache | None = None) -> bool:
    """文件是否已经躺在隔离区里（按规约后的真实路径判断）。"""
    if not trash:
        return False
    try:
        # 隔离区目录本身可能是 junction，且可能带尾分隔符 —— 用
        # canonical_directory 完整解析，否则 commonpath 比较会失配，
        # 把「已在隔离区」的副本当成普通文件，甚至选为保留项
        trash_key = cache.canonical_directory(trash) if cache is not None else canonical(trash)
        path_key = cache.canonical(path) if cache is not None else canonical(path)
        return ntpath.commonpath([path_key, trash_key]) == trash_key
    except ValueError:
        return False


def _trash_cluster(cluster: list[ReportFile], trash: str | None, cache: CanonicalCache | None = None) -> bool:
    return any(_is_in_trash(item.path, trash, cache) for item in cluster)


def plan_group(
    group: ReportGroup,
    mode: str = "shortest",
    trash: str | None = None,
    cache: CanonicalCache | None = None,
) -> tuple[list[ReportFile], list[list[ReportFile]], str | None]:
    """返回 (保留簇, 待删簇列表, 问题)。

    处置以物理簇为单位：保留一簇，其余簇整体移走。

    受保护路径的优先级高于 ``--keep`` 策略 —— 系统目录里的文件绝不能被移走，
    因此**只要有一簇含受保护路径，就强制保留它**，改删用户目录里的副本。这比
    「命中受保护路径就整组放弃」更符合实际：原件在 Program Files、副本在下载
    目录时，应当清理下载目录那份。

    ``trash`` 给出时，已在隔离区里的簇永不作为保留项：否则「活文件」会被移进
    隔离区、而保留项本身就在隔离区，用户随后执行 ``empty`` 就把唯一的数据删光。
    """
    clusters = physical_clusters(group)
    if len(clusters) < 2:
        if not clusters:
            return [], [], "组内没有可处置文件"
        return [], [], "组内只有一份物理数据（硬链接），无可节省空间"

    protected = [cluster for cluster in clusters if _is_protected_cluster(cluster, cache)]
    if len(protected) >= 2:
        return [], [], "组内有多份位于受保护目录的数据，无法安全处置"

    if protected:
        keep = protected[0]
    else:
        outside = [cluster for cluster in clusters if not _trash_cluster(cluster, trash, cache)]
        if not outside:
            return [], [], "组内所有副本都已位于隔离区，无可安全处置的文件"
        keep = choose_keep_cluster(outside, mode)
    removals = [cluster for cluster in clusters if cluster is not keep]

    for cluster in removals:
        for item in cluster:
            try:
                validate_source_path(item.path)
            except TrashError as exc:
                return [], [], str(exc)
            # 受保护簇已作为 keep 排除；这里命中说明 keep 选错或数据异常
            reason = protected_reason(item.path, cache)
            if reason is not None:
                return [], [], f"拒绝处置受保护路径：{reason}"
    return keep, removals, None


def _validate_report_paths(document: ReportDocument) -> None:
    """执行任何动作前先整体校验路径合法性（越界/非法即拒绝整个报告）。

    受保护的系统路径不在这里整体拒绝 —— 保留项位于系统目录是正常的（用户
    目录里的副本去重、原件留在 Program Files 很常见）。保护检查在
    :func:`plan_group` 里针对**待删文件**执行，命中即跳过该组。
    """
    for group in document.groups:
        for item in group.files:
            try:
                validate_source_path(item.path)
            except TrashError as exc:
                raise ExecutionError(f"报告包含非法路径，拒绝处理: {exc}") from exc


def build_plan(
    document: ReportDocument,
    options: ApplyOptions,
    group_ids: Iterable[int] | None = None,
) -> DisposalPlan:
    """把报告算成一份可预览、可执行的处置计划。

    ``group_ids`` 给出时只处理这些组（Web 的勾选），否则处理全部符合条件的组。
    这里不写任何文件，因此可以安全地用于「执行前预览」。
    """
    if document.partial:
        raise ExecutionError("报告标记为 partial（扫描未完成），拒绝处理")
    _validate_report_paths(document)

    selected = set(group_ids) if group_ids is not None else None
    mode = options.keep_mode if options.keep_mode != "ask" else "shortest"
    plan = DisposalPlan(mode=mode)
    cache = CanonicalCache()

    for group in document.groups:
        if selected is not None and group.id not in selected:
            continue
        if group.id in options.skip_group_ids:
            plan.skipped.append((group.id, "已处置或已忽略"))
            continue
        if group.status != "confirmed":
            plan.skipped.append((group.id, "非确认组"))
            continue
        # 忽略目录里的文件既不展示也不处置；跨忽略边界的分组按剩余副本重算，
        # 剩余不足两份就直接跳过，绝不「看不见却动了它」。
        effective = effective_group(group, options.ignore)
        if effective is None:
            plan.skipped.append((group.id, "组内文件位于忽略目录"))
            continue
        if effective.savings_bytes < options.min_savings:
            plan.skipped.append((group.id, "低于最小可节省阈值"))
            continue
        keep, removals, problem = plan_group(effective, mode, trash=options.trash_dir, cache=cache)
        if problem is not None:
            plan.skipped.append((group.id, problem))
            continue
        freed = sum(_cluster_occupancy(cluster, group.size) for cluster in removals)
        plan.groups.append(PlannedGroup(group_id=group.id, keep=keep, removals=removals, freed_bytes=freed))
    return plan


def execute_plan(
    document: ReportDocument,
    plan: DisposalPlan,
    options: ApplyOptions,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    stats: ApplyStats | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ApplyStats:
    """执行 :func:`build_plan` 的结果，返回统计。

    ``on_progress(已处理组数, 总组数)`` 每处理完一组回调一次，供 Web 进度条使用。

    ``should_stop`` 只在**组边界**检查：一组文件要么整簇移动、要么不动，
    中途停止会留下「保留项已走、副本还在」的半成品，比多等一组更糟。

    ``stats`` 允许调用方预先提供统计对象：中途抛异常时，异常会穿过这个
    函数，而**已经移动的文件数**只存在于这个对象里。调用方把它留着，就能
    在失败时告诉用户「已经移动了 N 个、可以还原」，而不是报「移动 0 个」。
    """
    by_id = {group.id: group for group in document.groups}
    stats = stats if stats is not None else ApplyStats()
    stats.refused_groups = len(plan.skipped)
    executor = _Executor(document, options, stats)
    total = len(plan.groups)
    try:
        for processed, planned in enumerate(plan.groups, start=1):
            if should_stop is not None and should_stop():
                stats.cancelled = True
                break
            group = by_id.get(planned.group_id)
            if group is None:
                continue
            executor.move_clusters(group, planned.keep, planned.removals, plan.mode)
            if on_progress is not None:
                on_progress(processed, total)
    finally:
        executor.close()
    return stats


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


class _Executor:
    """负责一次 apply 会话内的移动、manifest 与计数。"""

    def __init__(self, document: ReportDocument, options: ApplyOptions, stats: ApplyStats) -> None:
        self._document = document
        self._options = options
        self._stats = stats
        self._trash = TrashManager(options.trash_dir)
        self._manifest: Manifest | None = None
        self._cache = CanonicalCache()

    @property
    def stats(self) -> ApplyStats:
        return self._stats

    @property
    def manifest_path(self) -> str | None:
        return self._manifest.path if self._manifest is not None else None

    def _ensure_manifest(self) -> Manifest:
        if self._manifest is None:
            self._trash.ensure()
            # report 字段会被 restore 从任意工作目录读取，必须是绝对路径
            self._manifest = Manifest(self._trash.path, ntpath.abspath(str(self._document.path)))
            self._manifest.save()
            self._stats.manifest_path = self._manifest.path
        return self._manifest

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()

    def move_clusters(
        self,
        group: ReportGroup,
        keep: list[ReportFile],
        removals: list[list[ReportFile]],
        mode: str = "shortest",
    ) -> None:
        # 保留簇内的「代表路径」按实际策略选，否则 --keep newest 时会打印错文件
        keeper = choose_keep(keep, mode)
        say(f"  保留: {keeper.path}" + (f"（另有 {len(keep) - 1} 个硬链接路径）" if len(keep) > 1 else ""))
        stats = self._stats
        before = (
            stats.moved_files,
            stats.moved_bytes,
            stats.skipped_files,
            stats.failed_files,
            stats.missing_files,
        )
        if self._options.dry_run:
            self._dry_run_clusters(group, keeper, removals)
        else:
            self._move_clusters_reserved(group, keeper, removals)
        moved = stats.moved_files - before[0]
        skipped = stats.skipped_files - before[2]
        failed = stats.failed_files - before[3]
        missing = stats.missing_files - before[4]
        # 「文件已不在原处」不算阻塞：它多半是上一次部分处置时已经移走的
        # 副本。重试一个 partial 组时，若剩下的文件全部成功、其余都已不存在，
        # 这一组实际上已经处置完，必须允许升级为 handled，否则永远停在
        # 「部分处置」；反之，内容不符、受保护、移动失败才是真正没做完。
        blocked = skipped - missing + failed
        if moved > 0 or (missing > 0 and blocked == 0):
            state = "handled" if blocked == 0 else "partial"
            stats.group_results[group.id] = (state, moved, stats.moved_bytes - before[1])
        stats.processed_group_ids.append(group.id)
        stats.handled_groups += 1

    def _count_skip(self, problem: str) -> None:
        self._stats.skipped_files += 1
        if problem == MISSING_PROBLEM:
            self._stats.missing_files += 1

    def _dry_run_clusters(
        self, group: ReportGroup, keeper: ReportFile, removals: list[list[ReportFile]]
    ) -> None:
        stats = self._stats
        for cluster in removals:
            completed = True
            for item in cluster:
                problem = self._precheck(group, item, keeper)
                if problem is not None:
                    self._count_skip(problem)
                    say(f"  [跳过] {item.path} —— {problem}")
                    completed = False
                    continue
                try:
                    target = self._trash.target_for(item.path)
                except TrashError as exc:
                    stats.skipped_files += 1
                    say(f"  [跳过] {item.path} —— {exc}")
                    completed = False
                    continue
                say(f"  [DRY-RUN] 移动 {item.path} -> {target}")
                stats.moved_files += 1
            if completed:
                stats.moved_bytes += _cluster_occupancy(cluster, group.size)

    def _move_clusters_reserved(
        self, group: ReportGroup, keeper: ReportFile, removals: list[list[ReportFile]]
    ) -> None:
        """先把整组动作登记进 journal 并落盘，再逐个移动。

        每个文件移动前都 fsync 一次 journal 要 27ms/文件，全量处置会多出十几
        分钟；不落盘就移动则违背「崩溃可恢复」：文件已在隔离区、记录却丢了，
        就再也找不回来。折中：整组动作先登记、一次 fsync，然后移动。崩溃最多
        留下「登记了但没移动」的 pending —— restore 把 pending 当可还原时会
        发现目标不存在并跳过，不会误删任何东西。
        """
        stats = self._stats
        # 先预检、后建 manifest：若所有文件都已不在原处（重复执行同一组），
        # 不应留下一个空隔离区目录和 actions:[] 的空 manifest。
        prepared: list[tuple[list[ReportFile], list[tuple[ReportFile, str]]]] = []
        for cluster in removals:
            entries: list[tuple[ReportFile, str]] = []
            for item in cluster:
                problem = self._precheck(group, item, keeper)
                if problem is not None:
                    self._count_skip(problem)
                    say(f"  [跳过] {item.path} —— {problem}")
                    continue
                try:
                    target = self._trash.target_for(item.path)
                except TrashError as exc:
                    stats.skipped_files += 1
                    say(f"  [跳过] {item.path} —— {exc}")
                    continue
                entries.append((item, target))
            prepared.append((cluster, entries))
        if not any(entries for _, entries in prepared):
            return
        manifest = self._ensure_manifest()
        registered: list[tuple[list[ReportFile], list[tuple[ReportFile, str, dict[str, object]]]]] = []
        for cluster, entries in prepared:
            actions: list[tuple[ReportFile, str, dict[str, object]]] = []
            for item, target in entries:
                action = manifest.add(
                    group_id=group.id,
                    source=item.path,
                    target=target,
                    size=group.size,
                    durable=False,
                )
                actions.append((item, target, action))
            registered.append((cluster, actions))
        # 整组登记一次性落盘；之后才允许移动
        manifest.sync()
        for cluster, actions in registered:
            all_moved = len(actions) == len(cluster)
            for item, target, action in actions:
                all_moved = self._move_reserved_one(manifest, item, target, action) and all_moved
            if all_moved:
                # 整簇全部移走才算释放了这份物理数据；有一个失败就保守地不计入
                stats.moved_bytes += _cluster_occupancy(cluster, group.size)

    def _precheck(self, group: ReportGroup, item: ReportFile, keeper: ReportFile) -> str | None:
        # 最后一道防线：无论上层如何规划，受保护路径一律不移动
        protected = protected_reason(item.path, self._cache)
        if protected is not None:
            return f"受保护路径（{protected}）"
        problem = _validate_live_file(group, item)
        if problem is not None:
            return problem
        if self._options.verify_content:
            matches, reason = content_matches(item.path, keeper.path, group.size, self._options.sample_size)
            if not matches:
                return f"内容复核失败：{reason}"
        return None

    def _move_reserved_one(
        self,
        manifest: Manifest,
        item: ReportFile,
        target: str,
        action: dict[str, object],
    ) -> bool:
        try:
            os.makedirs(long_path(ntpath.dirname(target)), exist_ok=True)
            shutil.move(item.path, target)
        except OSError:
            if not move_file(item.path, target):
                manifest.mark(action, status="failed", error="移动失败")
                self._stats.failed_files += 1
                say(f"  [失败] {item.path} -> {target}")
                return False
        manifest.mark(action, status="completed")
        self._stats.moved_files += 1
        say(f"  [移动] {item.path} -> {target}")
        return True


def run_apply(
    document: ReportDocument,
    options: ApplyOptions,
    *,
    input_func: Callable[[str], str] = input,
) -> int:
    plan = build_plan(document, options)
    say(f"报告: {document.path}")
    say(f"trash: {options.trash_dir}    模式: {'dry-run（不移动）' if options.dry_run else '实际移动'}")
    say()

    if plan.skipped:
        reason_counts: dict[str, int] = {}
        for _, problem in plan.skipped:
            reason_counts[problem] = reason_counts.get(problem, 0) + 1
        summary = "，".join(
            f"{reason} {count} 组" for reason, count in sorted(reason_counts.items(), key=lambda item: -item[1])
        )
        say(f"跳过 {len(plan.skipped)} 组：{summary}")
        for group_id, problem in plan.skipped[:10]:
            say(f"  [跳过组 #{group_id}] {problem}")
        if len(plan.skipped) > 10:
            say(f"  …… 另有 {len(plan.skipped) - 10} 组")
        say()

    has_confirmed = any(group.status == "confirmed" for group in document.groups)
    if not plan.groups:
        say("没有可处理的确认重复组。")
        return 0 if has_confirmed else 1

    try:
        executor = _Executor(document, options, ApplyStats())
    except TrashError as exc:
        raise ExecutionError(str(exc)) from exc
    stats = executor.stats
    stats.refused_groups = len(plan.skipped)
    applicable = [group for group in document.groups if group.id in {item.group_id for item in plan.groups}]
    try:
        if options.assume_yes:
            for group in applicable:
                _apply_with_strategy(group, options, executor)
        else:
            _interactive(applicable, options, executor, input_func=input_func)
    finally:
        executor.close()

    if not options.dry_run and stats.group_results:
        try:
            saved = record_apply_result(document, stats.group_results, manifest=executor.manifest_path)
        except OSError:
            saved = False
        if saved:
            say(f"已记录 {len(stats.group_results)} 组的处置状态（下次加载报告时显示为已处置）")
        else:
            say("  [警告] 处置状态写入失败；下次加载报告时这些组可能仍显示为待处理（manifest 仍可还原）")
    say()
    prefix = "（dry-run：未移动任何文件）" if options.dry_run else ""
    say(
        f"完成：处理 {stats.handled_groups} 组，移动 {stats.moved_files} 个文件，"
        f"释放 {format_size(stats.moved_bytes)}；跳过 {stats.skipped_files}，失败 {stats.failed_files} {prefix}".rstrip()
    )
    return 0 if has_confirmed else 1


def _apply_with_strategy(group: ReportGroup, options: ApplyOptions, executor: _Executor) -> None:
    mode = options.keep_mode if options.keep_mode != "ask" else "shortest"
    effective = effective_group(group, options.ignore)
    if effective is None:
        say(f"[跳过组 #{group.id}] 组内文件位于忽略目录")
        return
    keep, removals, problem = plan_group(effective, mode, trash=options.trash_dir)
    if problem is not None:
        say(f"[跳过组 #{group.id}] {problem}")
        return
    say(f"\n组 #{group.id}（{format_size(group.size)}，可节省 {format_size(effective.savings_bytes)}）")
    executor.move_clusters(group, keep, removals, mode)


def _interactive(
    groups: list[ReportGroup],
    options: ApplyOptions,
    executor: _Executor,
    *,
    input_func: Callable[[str], str],
) -> None:
    decisions: dict[int, str] = {}
    page_start = 0
    page_size = max(1, options.page_size)
    while page_start < len(groups):
        page = groups[page_start : page_start + page_size]
        pending = [group for group in page if group.id not in decisions]
        if not pending:
            page_start += page_size
            continue
        _print_page(page, decisions, page_start, len(groups), options.ignore)
        command = _read_command(input_func, len(page))
        if command == "q":
            say("已退出，已完成操作均已落盘。")
            return
        if command == "p":
            page_start = max(0, page_start - page_size)
            continue
        if command == "s":
            for group in pending:
                decisions[group.id] = "skip"
            page_start += page_size
            continue
        if command == "k":
            for group in pending:
                _apply_with_strategy(group, options, executor)
                decisions[group.id] = "apply"
            page_start += page_size
            continue
        if command == "a":
            for group in groups[page_start:]:
                if group.id in decisions:
                    continue
                _apply_with_strategy(group, options, executor)
                decisions[group.id] = "apply"
            page_start = len(groups)
            continue
        if command.isdigit():
            index = int(command) - 1
            if index >= len(page):
                say("编号超出本页范围。")
                continue
            group = page[index]
            if group.id in decisions:
                say(f"组 #{group.id} 已处理，请选择其他组。")
                continue
            result = _choose_group_files(group, options, executor, input_func=input_func)
            if result:
                decisions[group.id] = result
            continue


def _print_page(
    page: list[ReportGroup],
    decisions: dict[int, str],
    page_start: int,
    total: int,
    ignore: IgnoreList | None = None,
) -> None:
    say()
    say(f"=== 第 {page_start + 1}-{page_start + len(page)} 组 / 共 {total} 组 ===")
    for index, group in enumerate(page, start=1):
        state = decisions.get(group.id)
        marker = {"apply": " [已处理]", "skip": " [已跳过]"}.get(state or "", "")
        effective = effective_group(group, ignore)
        if effective is None:
            say(f"[{index}] #{group.id}  组内文件位于忽略目录，跳过")
            continue
        clusters = physical_clusters(effective)
        links = len(effective.files) - len(clusters)
        link_note = f"，含 {links} 个硬链接路径" if links > 0 else ""
        say(
            f"[{index}] #{group.id}  {format_size(group.size)}  "
            f"可节省 {format_size(effective.savings_bytes)}  "
            f"{len(clusters)} 份物理数据{link_note}{marker}"
        )
        for cluster in clusters:
            for item in cluster:
                flags = []
                if item.unstable:
                    flags.append("不稳定")
                suffix = f"  ({', '.join(flags)})" if flags else ""
                say(f"      - {item.path}{suffix}")
    say()
    say("[k] 本页全部按策略处理  [s] 本页跳过  [1-9] 逐组选择  [a] 全部剩余处理  [p] 上一页  [q] 退出")


def _read_command(input_func: Callable[[str], str], page_length: int) -> str:
    while True:
        try:
            raw = input_func("> ")
        except (EOFError, KeyboardInterrupt):
            return "q"
        command = raw.strip().lower()
        if command in {"k", "s", "a", "p", "q"}:
            return command
        if command.isdigit() and 1 <= int(command) <= page_length:
            return command
        say("无效指令，请按提示输入。")


def _choose_group_files(
    group: ReportGroup,
    options: ApplyOptions,
    executor: _Executor,
    *,
    input_func: Callable[[str], str],
) -> str:
    """返回 "apply" / "skip"；无效输入时返回空串表示本组仍未处理。"""
    effective = effective_group(group, options.ignore)
    if effective is None:
        say(f"[跳过组 #{group.id}] 组内文件位于忽略目录")
        return "skip"
    clusters = physical_clusters(effective)
    if len(clusters) < 2:
        say(f"[跳过组 #{group.id}] 组内只有一份物理数据，无可节省空间")
        return "skip"
    protected = [cluster for cluster in clusters if _is_protected_cluster(cluster)]
    if len(protected) >= 2:
        say(f"[跳过组 #{group.id}] 组内有多份位于受保护目录的数据，无法安全处置")
        return "skip"

    in_trash = [cluster for cluster in clusters if _trash_cluster(cluster, options.trash_dir)]
    say(f"\n组 #{group.id}（保留其中一份物理数据，其余整簇移走）：")
    for index, cluster in enumerate(clusters, start=1):
        head = cluster[0].path
        extra = f"（另有 {len(cluster) - 1} 个硬链接路径）" if len(cluster) > 1 else ""
        note = ""
        if cluster in protected:
            note = "  [受保护，必须保留]"
        elif cluster in in_trash:
            note = "  [已在隔离区，不能作为保留项]"
        say(f"  保留 [{index}] {head}{extra}{note}")
        for item in cluster[1:]:
            say(f"           └ {item.path}")
    while True:
        try:
            raw = input_func("> 输入保留编号，或 [s] 跳过该组: ")
        except (EOFError, KeyboardInterrupt):
            return ""
        command = raw.strip().lower()
        if command == "s":
            return "skip"
        if command.isdigit() and 1 <= int(command) <= len(clusters):
            keep = clusters[int(command) - 1]
            # 用户选择不得覆盖系统保护：选错时自动改选受保护簇
            if protected and keep is not protected[0]:
                say(f"该选择会把受保护路径移走，已改为保留：{protected[0][0].path}")
                keep = protected[0]
            # 同理不能把隔离区里的副本选作保留项，否则活文件会被移进隔离区
            elif keep in in_trash:
                outside = [cluster for cluster in clusters if cluster not in in_trash]
                if not outside:
                    say(f"[跳过组 #{group.id}] 组内所有副本都已在隔离区，无可安全处置的文件")
                    return "skip"
                replacement = choose_keep_cluster(outside, options.keep_mode if options.keep_mode != "ask" else "shortest")
                say(f"该选择保留的是隔离区里的副本，已改为保留：{replacement[0].path}")
                keep = replacement
            removals = [cluster for cluster in clusters if cluster is not keep]
            executor.move_clusters(group, keep, removals)
            return "apply"
        say("请输入有效编号或 s。")
