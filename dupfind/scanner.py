"""目录遍历（只读），产出参与筛选的文件元数据与统计。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime

from .exclude import Excluder
from .models import FileEntry, ScanResult, ScanStats
from .win32 import is_reparse_point, scandir, strip_long_prefix

ExcludeReporter = Callable[[str, str], None]
ProgressReporter = Callable[[ScanStats], None]
StopCheck = Callable[[], bool]


class Scanner:
    """递归遍历单个卷；不跟随符号链接/重解析点，权限拒绝计数后继续。"""

    PROGRESS_INTERVAL = 2048

    __slots__ = ("_root", "_excluder", "_min_size", "_max_depth", "_on_excluded", "_on_progress", "_should_stop")

    def __init__(
        self,
        root: str,
        excluder: Excluder,
        *,
        min_size: int,
        max_depth: int | None = None,
        on_excluded: ExcludeReporter | None = None,
        on_progress: ProgressReporter | None = None,
        should_stop: StopCheck | None = None,
    ) -> None:
        self._root = os.path.abspath(root)
        self._excluder = excluder
        self._min_size = min_size
        self._max_depth = max_depth
        self._on_excluded = on_excluded
        self._on_progress = on_progress
        self._should_stop = should_stop

    def scan(self) -> ScanResult:
        stats = ScanStats(root=self._root, started_at=datetime.now().astimezone())
        started = time.perf_counter()
        files: list[FileEntry] = []
        stack: list[tuple[str, int]] = [(self._root, 0)]
        processed = 0
        stopped = False
        try:
            while stack and not stopped:
                if self._should_stop is not None and self._should_stop():
                    stats.partial = True
                    break
                current, depth = stack.pop()
                try:
                    entries = scandir(current)
                except PermissionError:
                    stats.access_denied += 1
                    continue
                except OSError:
                    stats.errors += 1
                    continue

                for entry in entries:
                    try:
                        self._handle_entry(entry, depth, files, stats, stack)
                    except PermissionError:
                        stats.access_denied += 1
                    except OSError:
                        stats.errors += 1
                    processed += 1
                    if processed % self.PROGRESS_INTERVAL == 0:
                        self._notify(stats)
                        if self._should_stop is not None and self._should_stop():
                            stats.partial = True
                            stopped = True
                            break
        except KeyboardInterrupt:
            stats.partial = True
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        self._notify(stats)
        return ScanResult(files=files, stats=stats)

    def _notify(self, stats: ScanStats) -> None:
        if self._on_progress is not None:
            self._on_progress(stats)

    def _handle_entry(
        self,
        entry: os.DirEntry,
        depth: int,
        files: list[FileEntry],
        stats: ScanStats,
        stack: list[tuple[str, int]],
    ) -> None:
        path = strip_long_prefix(entry.path)
        name = entry.name
        try:
            file_stat = entry.stat(follow_symlinks=False)
        except PermissionError:
            stats.access_denied += 1
            return
        except OSError:
            stats.errors += 1
            return
        attributes = getattr(file_stat, "st_file_attributes", 0)
        is_directory = entry.is_dir(follow_symlinks=False)

        if is_reparse_point(attributes):
            stats.reparse_skipped += 1
            if is_directory:
                stats.excluded_dirs += 1
            else:
                stats.excluded_files += 1
            self._excluded("符号链接/重解析点", path)
            return

        if is_directory:
            reason = self._excluder.dir_reason(path, name)
            if reason is not None:
                stats.excluded_dirs += 1
                self._excluded(reason, path)
                return
            if self._max_depth is None or depth < self._max_depth:
                stack.append((path, depth + 1))
            return

        reason = self._excluder.file_reason(path, name, attributes, depth)
        if reason is not None:
            stats.excluded_files += 1
            self._excluded(reason, path)
            return

        size = int(file_stat.st_size)
        if size <= 0 or size < self._min_size:
            stats.excluded_files += 1
            self._excluded("大小过滤", path)
            return

        files.append(
            FileEntry(
                path=path,
                name=name,
                size=size,
                mtime=float(file_stat.st_mtime),
                attributes=attributes,
            )
        )
        stats.files_scanned += 1

    def _excluded(self, reason: str, path: str) -> None:
        if self._on_excluded is not None:
            self._on_excluded(reason, path)
