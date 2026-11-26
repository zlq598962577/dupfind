"""CLI 入口（typer）：scan / apply 与退出码约定。"""

from __future__ import annotations

import json
import ntpath
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

import typer

from . import __version__
from .apply import KEEP_MODES, ApplyOptions, ExecutionError, run_apply
from .config import Config, ConfigError, load_config, resolve_config_path
from .ignore import IgnoreError, IgnoreList
from .models import GroupRecord, ScanStats
from .pipeline import ScanOptions, run_scan
from .report import REPORT_VERSION, ReportError, build_report, load_report, write_report
from .report_state import load_effective_state
from .restore import (
    CONFLICT_MODES,
    EmptyOptions,
    RestoreError,
    RestoreOptions,
    run_empty,
    run_restore,
)
from .trash import TrashBusy, TrashLock
from .units import format_size, parse_size
from .verify import hash_algorithm

EXIT_OK_WITH_DUPES = 0
EXIT_OK_NO_DUPES = 1
EXIT_USAGE = 2
EXIT_REPORT = 3
EXIT_INTERRUPTED = 130
DEFAULT_THREADS = 4

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Windows 单盘重复文件清理工具")


def _fail(message: str, code: int) -> NoReturn:
    print(f"错误: {message}", file=sys.stderr)
    raise typer.Exit(code)


def _load_settings(config_path: str) -> Config:
    try:
        return load_config(resolve_config_path(config_path or None))
    except ConfigError as exc:
        _fail(str(exc), EXIT_USAGE)


def _resolve_size(value: str | None, fallback: int, label: str) -> int:
    if value is None:
        return fallback
    try:
        return parse_size(value)
    except ValueError as exc:
        _fail(f"{label}: {exc}", EXIT_USAGE)


def _load_ignore(path: str) -> IgnoreList:
    """读取目录忽略清单；默认工作目录的 dupfind-ignore.json（不存在则空）。

    显式 ``--ignore-file`` 指向不存在的文件时直接报错：静默退回空清单会让
    用户以为忽略规则已生效，随后把本想忽略的目录也纳入处置。
    """
    try:
        return IgnoreList.load(resolve_config_path(path or None), required=bool(path))
    except IgnoreError as exc:
        _fail(str(exc), EXIT_USAGE)


def _local_root(root: str) -> str:
    absolute = os.path.abspath(root)
    drive, _ = ntpath.splitdrive(absolute)
    if not drive or not drive.endswith(":") or not os.path.isdir(absolute):
        _fail(f"扫描根路径必须是存在的本地盘符路径: {root}", EXIT_USAGE)
    return absolute


def _summary(
    stats: ScanStats,
    groups: list[GroupRecord],
    candidate_count: int,
    report_path: str,
    *,
    hashed: bool,
) -> dict[str, object]:
    confirmed = [group for group in groups if group.status == "confirmed"]
    suspect = [group for group in groups if group.status == "suspect"]
    shared = [group for group in groups if group.status == "shared"]
    savings = sum(group.savings_bytes for group in confirmed)
    return {
        "root": stats.root,
        "report": report_path,
        "filesScanned": stats.files_scanned,
        "excludedDirs": stats.excluded_dirs,
        "excludedFiles": stats.excluded_files,
        "accessDenied": stats.access_denied,
        "errors": stats.errors,
        "partial": stats.partial,
        "durationMs": stats.duration_ms,
        "candidateGroups": candidate_count,
        "confirmedGroups": len(confirmed),
        "suspectGroups": len(suspect),
        "sharedGroups": len(shared),
        "savingsBytes": savings,
        "hashAlgorithm": hash_algorithm() if hashed else None,
    }


@app.command()
def scan(
    root: str = typer.Argument(..., help="扫描根路径，如 F:\\"),
    out: str = typer.Option("dupfind-report.json", "--out", help="报告输出路径"),
    min_size: str | None = typer.Option(None, "--min-size", help="小于该大小不参与，默认 1KB（0 表示不限）"),
    max_depth: int | None = typer.Option(None, "--max-depth", help="递归深度，默认不限"),
    exclude_add: list[str] = typer.Option([], "--exclude-add", help="追加排除 glob，可多次"),
    no_exclude: bool = typer.Option(False, "--no-exclude", help="关闭全部排除规则"),
    ignore_file: str = typer.Option(
        "", "--ignore-file", help="目录忽略清单路径，默认工作目录 dupfind-ignore.json"
    ),
    no_ignore: bool = typer.Option(False, "--no-ignore", help="本次不应用目录忽略清单"),
    show_excluded: bool = typer.Option(False, "--show-excluded", help="逐条打印被跳过的路径"),
    sample_size: str | None = typer.Option(None, "--sample-size", help="头尾采样大小，默认 4KB"),
    no_hash: bool = typer.Option(False, "--no-hash", help="只做名字+大小筛选，结论为 suspect"),
    content_only: bool = typer.Option(False, "--content-only", help="忽略名字规则，所有同大小文件进入哈希"),
    threads: int | None = typer.Option(None, "--threads", help="哈希并发数，默认 4"),
    cache: str = typer.Option(".dupfind-cache", "--cache", help="哈希缓存文件路径"),
    no_cache: bool = typer.Option(False, "--no-cache", help="禁用缓存"),
    json_output: bool = typer.Option(False, "--json", help="仅输出 JSON 摘要到 stdout"),
    exclude_dry_run: bool = typer.Option(False, "--exclude-dry-run", help="只展示将被排除的内容，不执行扫描"),
    config: str = typer.Option("", "--config", help="配置文件路径，默认读取工作目录的 dupfind.toml"),
    verbose: bool = typer.Option(False, "--verbose", help="输出逐阶段耗时"),
) -> None:
    """扫描单个卷（只读），生成 JSON 报告。"""
    settings = _load_settings(config)
    root_abs = _local_root(root)
    if max_depth is not None and max_depth < 0:
        _fail("--max-depth 不能为负", EXIT_USAGE)

    resolved_min_size = _resolve_size(min_size, settings.min_size, "--min-size")
    resolved_sample_size = _resolve_size(sample_size, settings.sample_size, "--sample-size")
    resolved_threads = threads if threads is not None else (settings.threads or DEFAULT_THREADS)
    if resolved_threads < 1:
        _fail("--threads 必须 >= 1", EXIT_USAGE)
    if resolved_sample_size < 1:
        _fail("--sample-size 必须 > 0", EXIT_USAGE)

    def report_excluded(reason: str, path: str) -> None:
        if show_excluded or exclude_dry_run:
            print(f"[跳过:{reason}] {path}", file=sys.stderr)

    ignore_dirs = [] if no_ignore else _load_ignore(ignore_file).directories
    excluder_options = ScanOptions(
        root=root_abs,
        config=settings,
        min_size=resolved_min_size,
        sample_size=resolved_sample_size,
        threads=resolved_threads,
        max_depth=max_depth,
        exclude_add=list(exclude_add),
        ignore_dirs=ignore_dirs,
        no_exclude=no_exclude,
        no_hash=no_hash,
        content_only=content_only,
        cache_path=None if no_cache else Path(os.path.abspath(cache)),
        exclusions_only=exclude_dry_run,
        on_excluded=report_excluded,
    )

    started = time.perf_counter()
    result = run_scan(excluder_options)
    stats = result.stats
    groups = result.groups
    if verbose:
        print(
            f"[verbose] 遍历: {stats.files_scanned} 文件 / {stats.duration_ms} ms",
            file=sys.stderr,
        )

    if exclude_dry_run:
        print(f"将被跳过：目录 {stats.excluded_dirs} 个，文件 {stats.excluded_files} 个（未写入报告）")
        raise typer.Exit(EXIT_INTERRUPTED if stats.partial else EXIT_OK_WITH_DUPES)

    if verbose:
        print(f"[verbose] 候选组: {result.candidate_count}", file=sys.stderr)
        elapsed = int((time.perf_counter() - started) * 1000)
        print(f"[verbose] 分组/校验: {len(groups)} 组，总计 {elapsed} ms", file=sys.stderr)

    payload = build_report(stats, groups)
    try:
        write_report(out, payload)
    except OSError as exc:
        _fail(f"写入报告失败: {exc}", EXIT_USAGE)

    summary = _summary(stats, groups, result.candidate_count, out, hashed=result.hashed)
    if json_output:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        _print_scan_summary(summary)

    if stats.partial:
        print("扫描被中断，报告已保存（partial=true）。", file=sys.stderr)
        raise typer.Exit(EXIT_INTERRUPTED)
    raise typer.Exit(EXIT_OK_WITH_DUPES if groups else EXIT_OK_NO_DUPES)


def _print_scan_summary(summary: dict[str, object]) -> None:
    print(f"根路径: {summary['root']}")
    print(f"  文件: {summary['filesScanned']}    候选组: {summary['candidateGroups']}")
    print(
        f"  确认组: {summary['confirmedGroups']}    "
        f"疑似组: {summary['suspectGroups']}    硬链接组: {summary['sharedGroups']}"
    )
    print(f"  可节省: {format_size(int(summary['savingsBytes']))}")
    print(
        f"  排除: 目录 {summary['excludedDirs']} / 文件 {summary['excludedFiles']}    "
        f"权限拒绝: {summary['accessDenied']}    错误: {summary['errors']}"
    )
    print(f"  用时: {summary['durationMs']} ms    报告: {summary['report']}")


@app.command()
def apply(
    report_path: str = typer.Argument(..., help="scan 生成的报告 JSON"),
    trash: str | None = typer.Option(None, "--trash", help="移动目标目录，默认 <盘符>:\\_dupfind_trash"),
    page: int | None = typer.Option(None, "--page", help="每页分组数，默认 10"),
    keep: str | None = typer.Option(None, "--keep", help="shortest / newest / ask，默认 ask"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="默认只打印计划动作"),
    yes: bool = typer.Option(False, "--yes", help="跳过逐页确认（配合 --keep）"),
    min_savings: str = typer.Option("0", "--min-savings", help="只处理可节省空间达到阈值的组"),
    no_verify: bool = typer.Option(
        False, "--no-verify-content", help="跳过移动前的内容复核（默认会比对头尾采样）"
    ),
    ignore_file: str = typer.Option(
        "", "--ignore-file", help="目录忽略清单路径，默认工作目录 dupfind-ignore.json"
    ),
    no_ignore: bool = typer.Option(False, "--no-ignore", help="本次不应用目录忽略清单"),
    no_state: bool = typer.Option(
        False, "--no-state", help="不跳过已处置/已忽略的组（状态文件与磁盘不一致时用）"
    ),
    config: str = typer.Option("", "--config", help="配置文件路径"),
) -> None:
    """基于报告批量交互式处置重复文件。"""
    settings = _load_settings(config)
    try:
        document = load_report(report_path)
    except ReportError as exc:
        _fail(str(exc), EXIT_REPORT)

    resolved_keep = (keep if keep is not None else settings.keep_mode).lower()
    if resolved_keep not in KEEP_MODES:
        _fail(f"--keep 必须是 {', '.join(KEEP_MODES)} 之一", EXIT_USAGE)
    resolved_page = page if page is not None else settings.page_size
    if resolved_page < 1:
        _fail("--page 必须 >= 1", EXIT_USAGE)
    resolved_min_savings = _resolve_size(min_savings, 0, "--min-savings")

    resolved_trash = trash or _default_trash(document.root)
    ignore_list = None if no_ignore else _load_ignore(ignore_file)
    skip_group_ids: frozenset[int] = frozenset()
    if not no_state:
        try:
            skip_group_ids = load_effective_state(document, trash=resolved_trash).skip_ids()
        except OSError:
            skip_group_ids = frozenset()
    options = ApplyOptions(
        trash_dir=resolved_trash,
        page_size=resolved_page,
        keep_mode=resolved_keep,
        dry_run=dry_run,
        assume_yes=yes,
        min_savings=resolved_min_savings,
        verify_content=not no_verify,
        ignore=ignore_list,
        skip_group_ids=skip_group_ids,
    )
    try:
        if dry_run:
            code = run_apply(document, options)
        else:
            # 与 Web 处置共用同一把跨进程锁：否则 Web 正在往隔离区移动文件时，
            # CLI 的 empty 会把它们直接删掉，manifest 却还记着 completed
            with TrashLock(_lock_target(resolved_trash)):
                code = run_apply(document, options)
    except TrashBusy as exc:
        _fail(str(exc), EXIT_USAGE)
    except ExecutionError as exc:
        _fail(str(exc), EXIT_REPORT)
    raise typer.Exit(code)


def _lock_target(path: str) -> str:
    """锁文件操作的隔离区目录：manifest 文件取父目录，其余取自身（绝对化）。"""
    text = os.path.abspath(str(path))
    return text if os.path.isdir(text) else ntpath.dirname(text)


def _default_trash(root: str) -> str:
    drive, _ = ntpath.splitdrive(os.path.abspath(root))
    if not drive or not drive.endswith(":"):
        _fail(f"无法从报告根路径推断盘符: {root}", EXIT_USAGE)
    return ntpath.join(drive + "\\", "_dupfind_trash")


@app.command()
def restore(
    target: str = typer.Argument(..., help="manifest JSON 或 trash 目录（目录时处理其中全部 manifest）"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="默认只打印计划动作"),
    on_conflict: str = typer.Option(
        "skip", "--on-conflict", help="原位置已有文件时：skip / rename / overwrite"
    ),
    report: str | None = typer.Option(
        None,
        "--report",
        help="只还原 report 字段指向该报告（绝对路径）的 manifest；不指定时还原整个隔离区",
    ),
) -> None:
    """把隔离区里的文件还原到原位置。"""
    mode = on_conflict.lower()
    if mode not in CONFLICT_MODES:
        _fail(f"--on-conflict 必须是 {', '.join(CONFLICT_MODES)} 之一", EXIT_USAGE)
    if report is not None:
        report = os.path.abspath(report)
        if not os.path.isfile(report):
            _fail(f"--report 指定的报告不存在: {report}", EXIT_USAGE)
    options = RestoreOptions(dry_run=dry_run, on_conflict=mode, report=report)
    try:
        if dry_run:
            code = run_restore(target, options)
        else:
            with TrashLock(_lock_target(target)):
                code = run_restore(target, options)
    except TrashBusy as exc:
        _fail(str(exc), EXIT_USAGE)
    except RestoreError as exc:
        _fail(str(exc), EXIT_REPORT)
    raise typer.Exit(code)


@app.command()
def empty(
    trash: str = typer.Argument(..., help="要清空的隔离区目录"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="默认只列出将删除的文件"),
    purge_manifests: bool = typer.Option(
        False, "--purge-manifests", help="连 manifest 一并删除（默认保留，便于追溯）"
    ),
    yes: bool = typer.Option(False, "--yes", help="跳过确认提示"),
) -> None:
    """清空隔离区，真正释放磁盘空间。"""
    if not dry_run and not yes:
        # 这是不可逆操作，必须显式确认
        try:
            answer = input(f"将永久删除 {trash} 下的全部文件，不可恢复。输入 DELETE 确认: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            raise typer.Exit(EXIT_USAGE) from None
        if answer.strip() != "DELETE":
            print("已取消。")
            raise typer.Exit(EXIT_USAGE)
    try:
        if dry_run:
            code = run_empty(
                trash, EmptyOptions(dry_run=dry_run, keep_manifests=not purge_manifests)
            )
        else:
            # 与 apply/restore 共用锁：Web 任务还在往隔离区移动文件时，
            # 这里删掉的正是它 manifest 里登记的唯一副本
            with TrashLock(_lock_target(trash)):
                code = run_empty(
                    trash, EmptyOptions(dry_run=dry_run, keep_manifests=not purge_manifests)
                )
    except TrashBusy as exc:
        _fail(str(exc), EXIT_USAGE)
    except RestoreError as exc:
        _fail(str(exc), EXIT_USAGE)
    raise typer.Exit(code)


@app.command()
def version() -> None:
    """打印版本。"""
    print(f"dupfind {__version__}（报告格式 v{REPORT_VERSION}）")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
