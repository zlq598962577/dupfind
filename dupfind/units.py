"""大小解析/格式化与时间格式化工具。"""

from __future__ import annotations

import re
from datetime import datetime

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB|KIB|MIB|GIB|TIB|PIB)?\s*$", re.IGNORECASE)
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
    "PB": 1024 ** 5,
    "PIB": 1024 ** 5,
}
_UNIT_NAMES = ("B", "KB", "MB", "GB", "TB", "PB")


def parse_size(value: str | int) -> int:
    """把 ``1KB`` / ``2.5MB`` / ``0`` 解析为字节数；非法输入抛 ValueError。"""
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError(f"大小不能为负: {value!r}")
        return value
    match = _SIZE_RE.match(str(value))
    if match is None:
        raise ValueError(f"无法解析大小: {value!r}")
    # 不用 float：``9007199254740993B`` 这种大整数会丢精度
    from decimal import Decimal

    number = Decimal(match.group(1))
    unit = (match.group(2) or "B").upper()
    if number < 0:
        raise ValueError(f"大小不能为负: {value!r}")
    return int(number * _UNITS[unit])


def format_size(num: int) -> str:
    """格式化字节数；四舍五入后满 1024 就进位（``1048575`` 显示 ``1.0 MB``）。"""
    value = float(num)
    for index, unit in enumerate(_UNIT_NAMES):
        # 最后一档或该档能容纳时停下；注意 round(1023.95, 1) == 1024.0
        if index == len(_UNIT_NAMES) - 1 or round(value, 1) < 1024:
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
