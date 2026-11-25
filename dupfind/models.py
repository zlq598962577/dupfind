"""贯穿扫描/校验/报告阶段的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(slots=True, eq=False)
class FileEntry:
    """一个参与筛选的文件（仅保存必要元数据，控制内存占用）。"""

    path: str
    name: str
    size: int
    mtime: float
    attributes: int
    # 延迟填充字段：只有进入候选组后才计算
    allocated: int | None = None
    file_id: tuple[int, int] | None = None
    normalized_name: str = ""
    tokens: frozenset[str] = frozenset()
    shared: bool = False
    unstable: bool = False

    @property
    def identity(self) -> tuple[str, tuple[int, int]] | str:
        """用于识别"同一物理文件"的键；拿不到 FileId 时退回路径。"""
        if self.file_id is None:
            return self.path
        return self.file_id


@dataclass(slots=True)
class ScanStats:
    root: str
    started_at: datetime
    files_scanned: int = 0
    excluded_dirs: int = 0
    excluded_files: int = 0
    access_denied: int = 0
    errors: int = 0
    partial: bool = False
    duration_ms: int = 0
    # 仅为摘要展示，不写入报告
    reparse_skipped: int = 0


@dataclass(slots=True)
class GroupRecord:
    id: int
    status: str
    size: int
    savings_bytes: int
    files: list[FileEntry] = field(default_factory=list)
    hash_value: str | None = None


@dataclass(slots=True)
class ScanResult:
    files: list[FileEntry]
    stats: ScanStats
