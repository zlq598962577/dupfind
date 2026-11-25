"""删除前的安全护栏：系统目录保护与内容复核。

与扫描期的排除规则（``exclude.py``）**相互独立**：扫描时可以用
``--no-exclude`` 关掉过滤，但这里的保护永远生效，避免一份宽松扫描生成的
报告被直接拿去批量处置系统文件。
"""

from __future__ import annotations

import ntpath
import os

from .config import SYSTEM_ROOT_DIRS, SYSTEM_ROOT_FILES
from .verify import sample_digest
from .win32 import long_path

DEFAULT_SAMPLE_SIZE = 4096

_PROTECTED_DIRS = frozenset(name.lower() for name in SYSTEM_ROOT_DIRS)
_PROTECTED_ROOT_FILES = frozenset(name.lower() for name in SYSTEM_ROOT_FILES)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def canonical(path: str) -> str:
    """把路径规约成可比较的形式，用于安全判定。

    Windows 上同一个目录有多种写法，直接比字符串会被绕过：

    * ``C:\\Windows ``（尾随空格）—— 系统会静默剥掉尾随空格；
    * ``C:\\PROGRA~1`` —— 8.3 短名，指向 ``C:\\Program Files``；
    * ``\\\\?\\C:\\Windows`` —— 扩展长度前缀。

    :func:`os.path.realpath` 在 Windows 上会解析 8.3 短名并规范化分隔符，
    因此先剥前缀、再做 realpath，最后统一大小写。
    """
    text = str(path).strip()
    # 剥掉扩展长度前缀，否则 realpath 会把它当成相对路径
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    try:
        resolved = os.path.realpath(text)
    except (OSError, ValueError):
        resolved = os.path.abspath(text)
    return os.path.normcase(os.path.normpath(resolved))


def protected_reason(path: str) -> str | None:
    """若路径位于系统保护范围（盘符根目录的系统目录 / 系统根文件）则返回原因。

    判定基于 :func:`canonical`，因此尾随空格、8.3 短名、扩展长度前缀都绕不过去。
    """
    drive, rest = ntpath.splitdrive(canonical(path))
    if not drive:
        return "不是本地绝对路径"
    parts = [part for part in rest.split(ntpath.sep) if part]
    if not parts:
        return "路径指向盘符根目录"
    if len(parts) == 1 and parts[0].lower() in _PROTECTED_ROOT_FILES:
        return f"系统根文件({parts[0]})"
    if parts[0].lower() in _PROTECTED_DIRS:
        return f"系统保护目录({parts[0]})"
    return None


def content_matches(removal: str, keep: str, size: int, sample_size: int = DEFAULT_SAMPLE_SIZE) -> tuple[bool, str | None]:
    """移动前复核：待删文件与保留文件的大小、头尾采样必须一致。

    扫描时的全量哈希已确认过内容相同；这里再查一次，挡住「扫描之后文件被
    改动过」的情况。成本是每个文件两次 8KB 读取。
    """
    try:
        info = os.stat(long_path(removal))
        if int(info.st_size) != size:
            return False, f"大小已变化（{int(info.st_size)} ≠ {size}）"
        removal_digest = sample_digest(removal, size, sample_size)
        keep_digest = sample_digest(keep, size, sample_size)
    except PermissionError:
        return False, "权限拒绝"
    except OSError as exc:
        return False, f"无法读取: {exc}"
    if removal_digest != keep_digest:
        return False, "内容采样与保留文件不一致"
    return True, None
