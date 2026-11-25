"""扫描流水线：遍历 → 名字筛选 → 采样/哈希校验，供 CLI 与 Web 共用。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .cache import HashCache
from .config import Config
from .exclude import Excluder
from .grouping import build_candidate_groups, content_only_groups
from .models import FileEntry, GroupRecord, ScanStats
from .normalize import FilenameNormalizer
from .scanner import Scanner
from .verify import Verifier


@dataclass(slots=True)
class ScanProgress:
    """进度快照；callback 会在遍历/校验阶段被持续调用。"""

    phase: str
    files_scanned: int
    excluded_dirs: int
    excluded_files: int
    access_denied: int
    errors: int
    candidate_groups: int
    processed_groups: int
    confirmed_groups: int
    partial: bool


@dataclass(slots=True)
class ScanOptions:
    root: str
    config: Config
    min_size: int
    sample_size: int
    threads: int
    max_depth: int | None = None
    exclude_add: list[str] = field(default_factory=list)
    no_exclude: bool = False
    no_hash: bool = False
    content_only: bool = False
    cache_path: Path | None = None
    exclusions_only: bool = False
    on_excluded: Callable[[str, str], None] | None = None
    on_progress: Callable[[ScanProgress], None] | None = None
    should_stop: Callable[[], bool] | None = None


@dataclass(slots=True)
class PipelineResult:
    files: list[FileEntry]
    stats: ScanStats
    groups: list[GroupRecord]
    candidate_count: int
    hashed: bool


def run_scan(options: ScanOptions) -> PipelineResult:
    return _Run(options).execute()


def _suspect_group(files: list[FileEntry]) -> GroupRecord:
    savings = 0
    if len(files) > 1:
        savings = sum(entry.size for entry in files) - max(entry.size for entry in files)
    return GroupRecord(id=0, status="suspect", size=files[0].size, savings_bytes=savings, files=list(files))


class _Run:
    __slots__ = ("_options", "_candidates", "_processed", "_confirmed", "_stats")

    def __init__(self, options: ScanOptions) -> None:
        self._options = options
        self._candidates = 0
        self._processed = 0
        self._confirmed = 0
        self._stats: ScanStats | None = None

    def _emit(self, phase: str, stats: ScanStats) -> None:
        if self._options.on_progress is None:
            return
        self._options.on_progress(
            ScanProgress(
                phase=phase,
                files_scanned=stats.files_scanned,
                excluded_dirs=stats.excluded_dirs,
                excluded_files=stats.excluded_files,
                access_denied=stats.access_denied,
                errors=stats.errors,
                candidate_groups=self._candidates,
                processed_groups=self._processed,
                confirmed_groups=self._confirmed,
                partial=stats.partial,
            )
        )

    def execute(self) -> PipelineResult:
        options = self._options
        excluder = Excluder(
            options.config,
            options.root,
            extra_globs=options.exclude_add,
            enabled=not options.no_exclude,
        )
        scanner = Scanner(
            options.root,
            excluder,
            min_size=options.min_size,
            max_depth=options.max_depth,
            on_excluded=options.on_excluded,
            on_progress=lambda stats: self._emit("scanning", stats),
            should_stop=options.should_stop,
        )
        result = scanner.scan()
        stats = result.stats
        self._stats = stats

        if options.exclusions_only:
            self._emit("done", stats)
            return PipelineResult(files=result.files, stats=stats, groups=[], candidate_count=0, hashed=False)

        normalizer = FilenameNormalizer(tuple(options.config.copy_markers))
        if options.content_only:
            candidates = content_only_groups(result.files, normalizer)
        else:
            candidates = build_candidate_groups(result.files, normalizer)
        self._candidates = len(candidates)
        self._emit("grouping", stats)

        hashing = not options.no_hash and not stats.partial
        if hashing:
            cache = HashCache(options.cache_path)
            cache.load()
            verifier = Verifier(
                sample_size=options.sample_size,
                threads=options.threads,
                cache=cache,
                stats=stats,
                hashing=True,
                should_stop=options.should_stop,
                on_group_done=self._on_group_done,
            )
            groups = verifier.build_groups(candidates)
            cache.save()
        elif options.no_hash:
            verifier = Verifier(
                sample_size=options.sample_size,
                threads=options.threads,
                cache=HashCache(None),
                stats=stats,
                hashing=False,
                should_stop=options.should_stop,
                on_group_done=self._on_group_done,
            )
            groups = verifier.build_groups(candidates)
        else:  # 扫描被取消/中断：保留名字筛选结果，标记为 suspect
            groups = [_suspect_group(files) for files in candidates]

        self._emit("done", stats)
        return PipelineResult(
            files=result.files,
            stats=stats,
            groups=groups,
            candidate_count=len(candidates),
            hashed=hashing,
        )

    def _on_group_done(self, processed: int, confirmed: int) -> None:
        self._processed = processed
        self._confirmed = confirmed
        if self._stats is not None:
            self._emit("verifying", self._stats)
