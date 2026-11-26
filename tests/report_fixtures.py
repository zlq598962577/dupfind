"""测试用的报告与文件构造工具（状态 / 忽略 / Web 状态用例共用）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SIZE = 4096


def write_file(path: Path, size: int = SIZE, fill: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * size)[:size])
    return path


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def report_entry(path: Path, *, file_id: str, size: int = SIZE) -> dict:
    return {
        "path": str(path),
        "normalizedName": path.stem,
        "mtime": iso_mtime(path),
        "fileId": file_id,
        "shared": False,
        "unstable": False,
        "sizeOnDisk": size,
    }


def write_report(
    tmp_path: Path,
    groups: list[list[Path]],
    *,
    size: int = SIZE,
    partial: bool = False,
    status: str = "confirmed",
    started_at: str = "2026-01-01T00:00:00+08:00",
    name: str = "report.json",
) -> Path:
    """按给定文件列表写出一份合法报告；``groups`` 的每一项是一组重复文件。"""
    payload = {
        "version": 1,
        "scan": {
            "root": str(tmp_path),
            "startedAt": started_at,
            "durationMs": 1,
            "filesScanned": sum(len(group) for group in groups),
            "excludedDirs": 0,
            "excludedFiles": 0,
            "accessDenied": 0,
            "errors": 0,
            "partial": partial,
        },
        "groups": [
            {
                "id": index,
                "status": status,
                "size": size,
                "savingsBytes": size * (len(group) - 1),
                "files": [
                    report_entry(path, file_id=f"0x{index:08X}{item:016X}", size=size)
                    for item, path in enumerate(group)
                ],
            }
            for index, group in enumerate(groups, start=1)
        ],
    }
    report = tmp_path / name
    report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return report
