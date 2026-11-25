"""Windows Win32 API 的最小封装（ctypes），不预留跨平台分支。"""

from __future__ import annotations

import ctypes
import ntpath
import os
from ctypes import wintypes

from .models import FILE_ATTRIBUTE_REPARSE_POINT

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
INVALID_FILE_SIZE = 0xFFFFFFFF
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
MOVEFILE_REPLACE_EXISTING = 0x00000001
MOVEFILE_COPY_ALLOWED = 0x00000002
MOVEFILE_WRITE_THROUGH = 0x00000008


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


_GetCompressedFileSizeW = _kernel32.GetCompressedFileSizeW
_GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_GetCompressedFileSizeW.restype = wintypes.DWORD

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CreateFileW.restype = wintypes.HANDLE

_GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
_GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
_GetFileInformationByHandle.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

_MoveFileExW = _kernel32.MoveFileExW
_MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_MoveFileExW.restype = wintypes.BOOL


def long_path(path: str) -> str:
    """统一加 ``\\\\?\\`` 前缀（UNC 用 ``\\\\?\\UNC\\``），用于突破 260 字符限制。"""
    if path.startswith("\\\\?\\"):
        return path
    absolute = ntpath.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def strip_long_prefix(path: str) -> str:
    """去掉扩展前缀，得到可展示/可写入报告的普通路径。"""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\") :]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\") :]
    return path


def allocated_size(path: str) -> int | None:
    """实际占用（SizeOnDisk），稀疏/压缩文件小于逻辑大小；失败返回 None。"""
    high = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    low = _GetCompressedFileSizeW(long_path(path), ctypes.byref(high))
    if low == INVALID_FILE_SIZE and ctypes.get_last_error() != 0:
        return None
    return (high.value << 32) | low


def file_identity(path: str) -> tuple[int, int] | None:
    """(卷序列号, FileId)，用于识别硬链接；失败返回 None。"""
    handle = _CreateFileW(
        long_path(path),
        0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        return None
    try:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None
        return (int(info.dwVolumeSerialNumber), (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow))
    finally:
        _CloseHandle(handle)


def move_file(src: str, dst: str) -> bool:
    """MoveFileExW 兜底（允许跨卷复制），返回是否成功。"""
    return bool(
        _MoveFileExW(
            long_path(src),
            long_path(dst),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED | MOVEFILE_WRITE_THROUGH,
        )
    )


def is_reparse_point(attributes: int) -> bool:
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def scandir(path: str) -> list[os.DirEntry]:
    """对长路径安全的目录枚举（一次性读出，避免句柄长期占用）。"""
    with os.scandir(long_path(path)) as iterator:
        return list(iterator)
