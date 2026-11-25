"""报告构建与读取（版本校验）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json_atomic
from .models import FileEntry, GroupRecord, ScanStats
from .units import iso_from_mtime, parse_iso

REPORT_VERSION = 1
VALID_STATUSES = ("confirmed", "suspect", "shared")


class ReportError(RuntimeError):
    """报告格式/版本不匹配（退出码 3）。"""


def _format_file_id(entry: FileEntry) -> str | None:
    if entry.file_id is None:
        return None
    volume, index = entry.file_id
    return f"0x{volume:08X}{index:016X}"


def build_report(stats: ScanStats, groups: list[GroupRecord]) -> dict[str, Any]:
    ordered = sorted(groups, key=lambda group: (-group.savings_bytes, -group.size, group.id))
    payload: dict[str, Any] = {
        "version": REPORT_VERSION,
        "scan": {
            "root": stats.root,
            "startedAt": stats.started_at.astimezone().isoformat(timespec="seconds"),
            "durationMs": stats.duration_ms,
            "filesScanned": stats.files_scanned,
            "excludedDirs": stats.excluded_dirs,
            "excludedFiles": stats.excluded_files,
            "accessDenied": stats.access_denied,
            "errors": stats.errors,
            "partial": stats.partial,
        },
        "groups": [],
    }
    for index, group in enumerate(ordered, start=1):
        group.id = index
        payload["groups"].append(
            {
                "id": group.id,
                "status": group.status,
                "size": group.size,
                "savingsBytes": group.savings_bytes,
                "files": [
                    {
                        "path": entry.path,
                        "normalizedName": entry.normalized_name,
                        "mtime": iso_from_mtime(entry.mtime),
                        "fileId": _format_file_id(entry),
                        "shared": entry.shared,
                        "unstable": entry.unstable,
                        "sizeOnDisk": entry.allocated if entry.allocated is not None else entry.size,
                    }
                    for entry in group.files
                ],
            }
        )
    return payload


def write_report(path: str | Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)


@dataclass(slots=True)
class ReportFile:
    path: str
    size_on_disk: int | None
    normalized_name: str
    mtime: float | None
    file_id: str | None
    shared: bool
    unstable: bool


@dataclass(slots=True)
class ReportGroup:
    id: int
    status: str
    size: int
    savings_bytes: int
    files: list[ReportFile] = field(default_factory=list)


@dataclass(slots=True)
class ReportDocument:
    version: int
    path: Path
    root: str
    partial: bool
    groups: list[ReportGroup]
    raw_scan: dict[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportError(message)


def _is_count(value: Any) -> bool:
    """真正的整数计数；排除 bool（``True`` 是 int 子类）与 float。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_group_id(raw: Any, fallback: int, label: str) -> int:
    """组 id 必须是整数；``"abc"`` / ``None`` / ``NaN`` 都要转成 ReportError。

    直接 ``int(...)`` 会抛 ValueError/TypeError 冒到 CLI 之外，退出码变成 1
    并打印 traceback，而报告格式错误的约定退出码是 3。
    """
    if raw is None:
        return fallback
    if _is_count(raw):
        return raw
    raise ReportError(f"{label} 非法: {raw!r}")


def load_report(path: str | Path) -> ReportDocument:
    report_path = Path(path)
    try:
        raw = read_json(report_path)
    except OSError as exc:
        raise ReportError(f"无法读取报告: {exc}") from exc
    except ValueError as exc:
        raise ReportError(f"报告不是合法 JSON: {exc}") from exc

    _require(isinstance(raw, dict), "报告根节点必须是对象")
    version = raw.get("version")
    # 必须是真正的整数：True == 1、1.0 == 1，用 == 比较会放过这两种写法
    _require(
        _is_count(version) and version == REPORT_VERSION,
        f"报告版本不匹配（期望 {REPORT_VERSION}，实际 {version!r}）",
    )

    scan = raw.get("scan")
    _require(isinstance(scan, dict), "缺少 scan 字段")
    root = scan.get("root")
    _require(isinstance(root, str) and root, "scan.root 必须是非空字符串")
    # 必须是 JSON 布尔：字符串 "false" 会被 bool() 变成 True，把合法报告误判为 partial
    raw_partial = scan.get("partial", False)
    _require(isinstance(raw_partial, bool), f"scan.partial 必须是布尔值: {raw_partial!r}")
    partial = raw_partial

    groups_raw = raw.get("groups")
    _require(isinstance(groups_raw, list), "groups 必须是数组")

    groups: list[ReportGroup] = []
    for index, item in enumerate(groups_raw):
        _require(isinstance(item, dict), f"groups[{index}] 必须是对象")
        status = item.get("status")
        _require(status in VALID_STATUSES, f"groups[{index}].status 非法: {status!r}")
        size = item.get("size")
        _require(_is_count(size) and size >= 0, f"groups[{index}].size 非法")
        savings = item.get("savingsBytes", 0)
        _require(_is_count(savings) and savings >= 0, f"groups[{index}].savingsBytes 非法")
        files_raw = item.get("files")
        _require(isinstance(files_raw, list) and files_raw, f"groups[{index}].files 必须是非空数组")
        files: list[ReportFile] = []
        for file_index, file_item in enumerate(files_raw):
            _require(isinstance(file_item, dict), f"groups[{index}].files[{file_index}] 必须是对象")
            file_path = file_item.get("path")
            _require(
                isinstance(file_path, str) and file_path,
                f"groups[{index}].files[{file_index}].path 非法",
            )
            raw_mtime = file_item.get("mtime")
            parsed_mtime = parse_iso(raw_mtime) if isinstance(raw_mtime, str) else None
            size_on_disk = file_item.get("sizeOnDisk")
            files.append(
                ReportFile(
                    path=file_path,
                    size_on_disk=size_on_disk if isinstance(size_on_disk, int) else None,
                    normalized_name=str(file_item.get("normalizedName", "")),
                    mtime=parsed_mtime,
                    file_id=file_item.get("fileId") if isinstance(file_item.get("fileId"), str) else None,
                    shared=bool(file_item.get("shared", False)),
                    unstable=bool(file_item.get("unstable", False)),
                )
            )
        groups.append(
            ReportGroup(
                id=_parse_group_id(item.get("id"), index + 1, f"groups[{index}].id"),
                status=str(status),
                size=size,
                savings_bytes=savings,
                files=files,
            )
        )

    return ReportDocument(
        version=int(version),
        path=report_path,
        root=root,
        partial=partial,
        groups=groups,
        raw_scan=scan,
    )
