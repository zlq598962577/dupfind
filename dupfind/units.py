"""大小解析/格式化与时间格式化工具。"""

from __future__ import annotations

import re
from datetime import datetime

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?\s*$", re.IGNORECASE)
_UNITS = {
    "B": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
}


def parse_size(value: str | int) -> int:
    """把 ``1KB`` / ``2.5MB`` / ``0`` 解析为字节数；非法输入抛 ValueError。"""
    if isinstance(value, int):
        return value
    match = _SIZE_RE.match(value)
    if match is None:
        raise ValueError(f"无法解析大小: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    if number < 0:
        raise ValueError(f"大小不能为负: {value!r}")
    return int(number * _UNITS[unit])


def format_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None
