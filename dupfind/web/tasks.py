"""Web 后台扫描任务：线程内跑只读流水线，前端轮询进度并可取消。"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, ConfigError, load_config
from ..pipeline import ScanOptions, ScanProgress, run_scan
from ..report import build_report, write_report

MAX_TASKS = 20
DEFAULT_THREADS = 4


class TaskError(RuntimeError):
    pass


@dataclass
class ScanTask:
    id: str
    root: str
    report_path: str
    min_size: int
    no_hash: bool
    content_only: bool
    max_depth: int | None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    state: str = "running"
    phase: str = "scanning"
    files_scanned: int = 0
    excluded_dirs: int = 0
    excluded_files: int = 0
    access_denied: int = 0
    errors: int = 0
    candidate_groups: int = 0
    processed_groups: int = 0
    confirmed_groups: int = 0
    partial: bool = False
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def apply_progress(self, progress: ScanProgress) -> None:
        self.phase = progress.phase
        self.files_scanned = progress.files_scanned
        self.excluded_dirs = progress.excluded_dirs
        self.excluded_files = progress.excluded_files
        self.access_denied = progress.access_denied
        self.errors = progress.errors
        self.candidate_groups = progress.candidate_groups
        self.processed_groups = progress.processed_groups
        self.confirmed_groups = progress.confirmed_groups
        self.partial = progress.partial

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "state": self.state,
            "phase": self.phase,
            "root": self.root,
            "reportPath": self.report_path,
            "filesScanned": self.files_scanned,
            "excludedDirs": self.excluded_dirs,
            "excludedFiles": self.excluded_files,
            "accessDenied": self.access_denied,
            "errors": self.errors,
            "candidateGroups": self.candidate_groups,
            "processedGroups": self.processed_groups,
            "confirmedGroups": self.confirmed_groups,
            "partial": self.partial,
            "error": self.error,
            "durationMs": int(elapsed * 1000),
            "minSize": self.min_size,
            "noHash": self.no_hash,
            "contentOnly": self.content_only,
        }


class ScanTaskManager:
    """任务生命周期 + 报告文件命名；同一时刻允许多任务（各自独立报告）。"""

    def __init__(
        self,
        report_dir: Path,
        *,
        config: Config | None = None,
        cache_path: Path | None = None,
        threads: int | None = None,
    ) -> None:
        self.report_dir = report_dir
        self._config = config or _load_web_config()
        self._threads = threads or (self._config.threads or DEFAULT_THREADS)
        self._cache_path = cache_path if cache_path is not None else Path(os.path.abspath(".dupfind-cache"))
        self._tasks: dict[str, ScanTask] = {}
        self._lock = threading.Lock()
        self._last_report: str | None = None

    @property
    def config(self) -> Config:
        return self._config

    def last_report(self) -> str | None:
        return self._last_report

    def volumes(self) -> list[str]:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        volumes: list[str] = []
        for index in range(26):
            if mask & (1 << index):
                letter = chr(ord("A") + index)
                candidate = f"{letter}:\\"
                if os.path.isdir(candidate):
                    volumes.append(candidate)
        return volumes

    def start(
        self,
        *,
        root: str,
        min_size: int,
        no_hash: bool,
        content_only: bool,
        max_depth: int | None,
    ) -> ScanTask:
        task_id = str(uuid.uuid4())
        report_path = self.report_dir / f"scan-{time.strftime('%Y%m%d-%H%M%S')}-{task_id[:8]}.json"
        task = ScanTask(
            id=task_id,
            root=root,
            report_path=str(report_path),
            min_size=min_size,
            no_hash=no_hash,
            content_only=content_only,
            max_depth=max_depth,
        )
        with self._lock:
            self._prune()
            self._tasks[task_id] = task
        thread = threading.Thread(target=self._run, args=(task,), name=f"dupfind-scan-{task_id[:8]}", daemon=True)
        thread.start()
        return task

    def get(self, task_id: str) -> ScanTask | None:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state != "running":
            return False
        task.cancel_event.set()
        return True

    def _run(self, task: ScanTask) -> None:
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            options = ScanOptions(
                root=task.root,
                config=self._config,
                min_size=task.min_size,
                sample_size=self._config.sample_size,
                threads=self._threads,
                max_depth=task.max_depth,
                no_hash=task.no_hash,
                content_only=task.content_only,
                cache_path=self._cache_path,
                on_progress=task.apply_progress,
                should_stop=task.cancel_event.is_set,
            )
            result = run_scan(options)
            payload = build_report(result.stats, result.groups)
            write_report(task.report_path, payload)
            task.partial = result.stats.partial
            task.confirmed_groups = sum(1 for group in result.groups if group.status == "confirmed")
            task.candidate_groups = result.candidate_count
            task.state = "cancelled" if result.stats.partial else "succeeded"
            self._last_report = task.report_path
        except Exception as exc:  # 后台任务必须把错误落到任务状态里
            task.state = "failed"
            task.error = str(exc)
        finally:
            task.phase = "done"
            task.finished_at = time.time()

    def _prune(self) -> None:
        if len(self._tasks) < MAX_TASKS:
            return
        finished = sorted(
            (task for task in self._tasks.values() if task.state != "running"),
            key=lambda task: task.started_at,
        )
        for task in finished[: len(self._tasks) - MAX_TASKS + 1]:
            self._tasks.pop(task.id, None)


def _load_web_config() -> Config:
    try:
        return load_config(None)
    except ConfigError:
        return Config()
