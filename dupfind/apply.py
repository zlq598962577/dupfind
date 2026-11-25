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
from .report import ReportDocument, ReportFile, ReportGroup
from .safety import DEFAULT_SAMPLE_SIZE, canonical, content_matches, protected_reason
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


@dataclass(slots=True)
class ApplyStats:
    handled_groups: int = 0
    moved_files: int = 0
    moved_bytes: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    refused_groups: int = 0


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
    clusters: dict[str, list[ReportFile]] = {}
    for item in group.files:
        if item.unstable:
            continue
        clusters.setdefault(_identity(item), []).append(item)
    return list(clusters.values())


def _cluster_occupancy(cluster: list[ReportFile], fallback: int = 0) -> int:
    """一簇硬链接的实际占用：只算一份，取成员中最大者。

    报告缺少 ``sizeOnDisk`` 时退回组内逻辑大小，避免把可节省空间算成 0。
    """
    sizes = [
        item.size_on_disk if item.size_on_disk is not None else fallback for item in cluster
    ]
    return max(sizes) if sizes else 0


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


def _validate_live_file(group: ReportGroup, item: ReportFile) -> str | None:
    if not os.path.isfile(long_path(item.path)):
        return "文件不存在或不是普通文件"
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


def _is_protected_cluster(cluster: list[ReportFile]) -> bool:
    return any(protected_reason(item.path) is not None for item in cluster)


def _is_in_trash(path: str, trash: str | None) -> bool:
    """文件是否已经躺在隔离区里（按规约后的真实路径判断）。"""
    if not trash:
        return False
    try:
        return ntpath.commonpath([canonical(path), canonical(trash)]) == canonical(trash)
    except ValueError:
        return False


def _trash_cluster(cluster: list[ReportFile], trash: str | None) -> bool:
    return any(_is_in_trash(item.path, trash) for item in cluster)


def plan_group(
    group: ReportGroup,
    mode: str = "shortest",
    trash: str | None = None,
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

    protected = [cluster for cluster in clusters if _is_protected_cluster(cluster)]
    if len(protected) >= 2:
        return [], [], "组内有多份位于受保护目录的数据，无法安全处置"

    if protected:
        keep = protected[0]
    else:
        outside = [cluster for cluster in clusters if not _trash_cluster(cluster, trash)]
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
            reason = protected_reason(item.path)
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

    for group in document.groups:
        if selected is not None and group.id not in selected:
            continue
        if group.status != "confirmed":
            plan.skipped.append((group.id, "非确认组"))
            continue
        if group.savings_bytes < options.min_savings:
            plan.skipped.append((group.id, "低于最小可节省阈值"))
            continue
        keep, removals, problem = plan_group(group, mode, trash=options.trash_dir)
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
) -> ApplyStats:
    """执行 :func:`build_plan` 的结果，返回统计。

    ``on_progress(已处理组数, 总组数)`` 每处理完一组回调一次，供 Web 进度条使用。

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

    @property
    def stats(self) -> ApplyStats:
        return self._stats

    def _ensure_manifest(self) -> Manifest:
        if self._manifest is None:
            self._trash.ensure()
            self._manifest = Manifest(self._trash.path, str(self._document.path))
            self._manifest.save()
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
        for cluster in removals:
            freed = _cluster_occupancy(cluster, group.size)
            # 整簇全部移走才算释放了这份物理数据；有一个失败就保守地不计入
            all_moved = True
            for item in cluster:
                all_moved = self._move_one(group, item, keeper) and all_moved
            if all_moved:
                self._stats.moved_bytes += freed
        self._stats.handled_groups += 1

    def _move_one(self, group: ReportGroup, item: ReportFile, keeper: ReportFile) -> bool:
        # 最后一道防线：无论上层如何规划，受保护路径一律不移动
        protected = protected_reason(item.path)
        if protected is not None:
            self._stats.skipped_files += 1
            say(f"  [跳过] {item.path} —— 受保护路径（{protected}）")
            return False
        problem = _validate_live_file(group, item)
        if problem is not None:
            self._stats.skipped_files += 1
            say(f"  [跳过] {item.path} —— {problem}")
            return False
        if self._options.verify_content:
            matches, reason = content_matches(item.path, keeper.path, group.size, self._options.sample_size)
            if not matches:
                self._stats.skipped_files += 1
                say(f"  [跳过] {item.path} —— 内容复核失败：{reason}")
                return False
        try:
            target = self._trash.target_for(item.path)
        except TrashError as exc:
            self._stats.skipped_files += 1
            say(f"  [跳过] {item.path} —— {exc}")
            return False

        if self._options.dry_run:
            say(f"  [DRY-RUN] 移动 {item.path} -> {target}")
            self._stats.moved_files += 1
            return True

        manifest = self._ensure_manifest()
        action = manifest.add(group_id=group.id, source=item.path, target=target, size=group.size)
        try:
            os.makedirs(ntpath.dirname(target), exist_ok=True)
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

    for group_id, problem in plan.skipped:
        say(f"[跳过组 #{group_id}] {problem}")

    has_confirmed = any(group.status == "confirmed" for group in document.groups)
    if not plan.groups:
        say("没有可处理的确认重复组。")
        return 0 if has_confirmed else 1
    if plan.skipped:
        say(f"（另有 {len(plan.skipped)} 组未纳入本次处置：非确认/低于阈值/无可节省空间/受保护）")
        say()

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

    say()
    prefix = "（dry-run：未移动任何文件）" if options.dry_run else ""
    say(
        f"完成：处理 {stats.handled_groups} 组，移动 {stats.moved_files} 个文件，"
        f"释放 {format_size(stats.moved_bytes)}；跳过 {stats.skipped_files}，失败 {stats.failed_files} {prefix}".rstrip()
    )
    return 0 if has_confirmed else 1


def _apply_with_strategy(group: ReportGroup, options: ApplyOptions, executor: _Executor) -> None:
    mode = options.keep_mode if options.keep_mode != "ask" else "shortest"
    keep, removals, problem = plan_group(group, mode, trash=options.trash_dir)
    if problem is not None:
        say(f"[跳过组 #{group.id}] {problem}")
        return
    say(f"\n组 #{group.id}（{format_size(group.size)}，可节省 {format_size(group.savings_bytes)}）")
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
        _print_page(page, decisions, page_start, len(groups))
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


def _print_page(page: list[ReportGroup], decisions: dict[int, str], page_start: int, total: int) -> None:
    say()
    say(f"=== 第 {page_start + 1}-{page_start + len(page)} 组 / 共 {total} 组 ===")
    for index, group in enumerate(page, start=1):
        state = decisions.get(group.id)
        marker = {"apply": " [已处理]", "skip": " [已跳过]"}.get(state or "", "")
        clusters = physical_clusters(group)
        links = len(group.files) - len(clusters)
        link_note = f"，含 {links} 个硬链接路径" if links > 0 else ""
        say(
            f"[{index}] #{group.id}  {format_size(group.size)}  "
            f"可节省 {format_size(group.savings_bytes)}  "
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
    clusters = physical_clusters(group)
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
