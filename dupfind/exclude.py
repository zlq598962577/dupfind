"""排除规则：目录黑名单、文件级过滤与用户追加 glob。"""

from __future__ import annotations

import fnmatch
import ntpath
import os

from .config import Config
from .models import FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class Excluder:
    """一次扫描对应一个实例；所有比较均大小写不敏感。"""

    __slots__ = (
        "_enabled",
        "_drive_root",
        "_system_dirs",
        "_any_level_dirs",
        "_extensions",
        "_root_files",
        "_globs",
    )

    def __init__(self, config: Config, root: str, extra_globs: list[str] | tuple[str, ...] = (), enabled: bool = True) -> None:
        self._enabled = enabled
        drive, _ = ntpath.splitdrive(ntpath.abspath(root))
        self._drive_root = drive + "\\" if drive else ""
        self._system_dirs = {_norm(ntpath.join(self._drive_root, name)) for name in config.system_dirs}
        self._any_level_dirs = {name.lower() for name in config.any_level_dirs}
        self._extensions = {ext.lower() for ext in config.exclude_extensions}
        self._root_files = {name.lower() for name in config.root_files}
        self._globs = [pattern for pattern in (*config.globs, *extra_globs) if pattern]

    def _matches_glob(self, path: str, name: str) -> str | None:
        normalized_path = _norm(path)
        normalized_name = _norm(name)
        for pattern in self._globs:
            normalized_pattern = _norm(pattern)
            if fnmatch.fnmatchcase(normalized_path, normalized_pattern) or fnmatch.fnmatchcase(
                normalized_name, normalized_pattern
            ):
                return pattern
        return None

    def dir_reason(self, path: str, name: str) -> str | None:
        if not self._enabled:
            return None
        if _norm(path) in self._system_dirs:
            return f"系统目录黑名单({name})"
        if name.lower() in self._any_level_dirs:
            return f"目录黑名单({name})"
        pattern = self._matches_glob(path, name)
        if pattern is not None:
            return f"自定义规则({pattern})"
        return None

    def file_reason(self, path: str, name: str, attributes: int, depth: int) -> str | None:
        if not self._enabled:
            return None
        if attributes & FILE_ATTRIBUTE_HIDDEN and attributes & FILE_ATTRIBUTE_SYSTEM:
            return "隐藏+系统属性"
        extension = ntpath.splitext(name)[1].lower()
        if extension and extension in self._extensions:
            return f"扩展名黑名单({extension})"
        if depth == 0 and name.lower() in self._root_files:
            return "系统根文件"
        pattern = self._matches_glob(path, name)
        if pattern is not None:
            return f"自定义规则({pattern})"
        return None
