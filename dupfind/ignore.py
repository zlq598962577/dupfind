"""持久化的目录忽略清单：扫描、视图与处置三处共用同一份规则。

用户抱怨「重复文件里混着别的程序的文件」。默认排除只能认出盘根的系统目录
与少数开发目录，认不出 ``E:\\WPS Office``、微信缓存这类普通路径。这里给用户
一个自己的清单：加入某个目录后，

* 后续扫描不再进入该目录（省时间，报告里也不再出现）；
* 已有报告在 Web 中加载时，该目录下的文件会被隐藏，跨目录的分组按剩余
  副本重新计算可节省空间；
* 处置（预览与执行）同样跳过这些文件 —— 视图里看不见的东西，绝不会被移动。

清单只用于「不展示、不处置」，不是安全护栏：即使清单被清空或损坏，
``apply`` 的受保护路径检查、内容复核与 manifest 依然照常生效。
"""

from __future__ import annotations

import ntpath
import os
import threading
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json_atomic
from .units import iso_now

IGNORE_FILENAME = "dupfind-ignore.json"
IGNORE_VERSION = 1

_WRITE_LOCK = threading.RLock()


class IgnoreError(RuntimeError):
    """忽略清单无法读取、格式非法，或目录路径不合法。"""


def default_ignore_path() -> Path:
    """与 ``dupfind.toml`` 一致：默认放在进程工作目录。"""
    return Path.cwd() / IGNORE_FILENAME


def normalize_directory(path: str) -> str:
    """校验并规范化一个目录条目，返回保留原始大小写的绝对路径。

    拒绝盘符根目录：忽略整个 ``E:\\`` 等于让工具什么也不做，多半是用户
    写错；明确报错比静默接受更安全。
    """
    text = str(path or "").strip()
    if not text:
        raise IgnoreError("目录路径为空")
    # 先统一分隔符再查扩展前缀：'//?/D:/Games' 在替换前看起来只是相对路径，
    # 规范化后却会变成带 \\?\ 的值，写进清单后下次加载会整份读不出来
    normalized = text.replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        raise IgnoreError(f"路径包含不支持的扩展前缀: {path}")
    if not ntpath.isabs(normalized):
        raise IgnoreError(f"必须是本地绝对路径: {path}")
    # 必须在 normpath 之前检查：normpath 会把 ".." 折叠掉，之后再也看不到，
    # 这段校验就成了死代码
    for part in normalized.split("\\"):
        if part == "..":
            raise IgnoreError(f"路径包含 '..' 逃逸: {path}")
    absolute = ntpath.normpath(ntpath.abspath(normalized))
    drive, rest = ntpath.splitdrive(absolute)
    if not drive or not drive.endswith(":"):
        raise IgnoreError(f"必须是本地盘符路径: {path}")
    if not rest.strip("\\"):
        raise IgnoreError(f"拒绝忽略整个盘符根目录: {path}")
    return absolute


def _match_key(path: str) -> str:
    text = str(path or "")
    if not ntpath.isabs(text):
        text = ntpath.abspath(text)
    return os.path.normcase(ntpath.normpath(text))


def _ancestors(path: str) -> list[str]:
    """文件路径自身及其各级父目录（直到盘符根）。"""
    result: list[str] = []
    current = _match_key(path)
    while True:
        result.append(current)
        parent = ntpath.dirname(current)
        if parent == current:
            return result
        current = parent


class IgnoreList:
    """一份忽略清单；``contains`` 按子树语义匹配（目录自身及其全部后代）。"""

    __slots__ = ("path", "_directories", "_keys", "_originals")

    def __init__(self, path: Path, directories: list[str] | None = None) -> None:
        self.path = Path(path)
        self._directories: list[str] = []
        self._keys: set[str] = set()
        self._originals: dict[str, str] = {}
        for directory in directories or []:
            try:
                self._append(str(directory))
            except IgnoreError:
                # 旧版本或手工编辑可能留下无法规范化的条目。跳过它，别让
                # 整份清单因此不可读；下次 save 时会把它清掉。
                continue

    # --- 构造 -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None, *, required: bool = False) -> IgnoreList:
        target = Path(path) if path is not None else default_ignore_path()
        if not target.is_file():
            if required:
                raise IgnoreError(f"忽略清单不存在: {target}")
            return cls(target)
        try:
            data = read_json(target)
        except OSError as exc:
            raise IgnoreError(f"无法读取忽略清单: {exc}") from exc
        except ValueError as exc:
            raise IgnoreError(f"忽略清单不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise IgnoreError("忽略清单根节点必须是对象")
        version = data.get("version", IGNORE_VERSION)
        if not isinstance(version, int) or isinstance(version, bool) or version != IGNORE_VERSION:
            raise IgnoreError(f"忽略清单版本不匹配（期望 {IGNORE_VERSION}，实际 {version!r}）")
        directories = data.get("directories", [])
        if not isinstance(directories, list) or any(not isinstance(item, str) for item in directories):
            raise IgnoreError("忽略清单的 directories 必须是字符串数组")
        return cls(target, [str(item) for item in directories])

    # --- 查询 -------------------------------------------------------

    @property
    def directories(self) -> list[str]:
        return list(self._directories)

    def __len__(self) -> int:
        return len(self._directories)

    def contains(self, path: str) -> bool:
        """路径是否位于任一被忽略目录的子树内。"""
        if not self._keys:
            return False
        for candidate in _ancestors(path):
            if candidate in self._keys:
                return True
        return False

    def match(self, path: str) -> str | None:
        """返回命中的忽略条目（原始写法），未命中返回 None。"""
        if not self._keys:
            return None
        for candidate in _ancestors(path):
            hit = self._originals.get(candidate)
            if hit is not None:
                return hit
        return None

    def matches_directory(self, path: str) -> str | None:
        """若 ``path`` 自身（作为目录）被忽略，返回命中的条目。"""
        return self._originals.get(_match_key(path))

    # --- 修改 -------------------------------------------------------

    def _append(self, directory: str) -> bool:
        normalized = normalize_directory(directory)
        key = _match_key(normalized)
        if key in self._keys:
            return False
        self._directories.append(normalized)
        self._keys.add(key)
        self._originals[key] = normalized
        return True

    def add(self, directory: str) -> bool:
        changed = self._append(directory)
        if changed:
            self._directories.sort(key=_match_key)
        return changed

    def remove(self, directory: str) -> bool:
        key = _match_key(normalize_directory(directory))
        kept = [item for item in self._directories if _match_key(item) != key]
        if len(kept) == len(self._directories):
            return False
        self._directories = kept
        self._keys = {_match_key(item) for item in kept}
        self._originals = {_match_key(item): item for item in kept}
        return True

    # --- 持久化 -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": IGNORE_VERSION,
            "updatedAt": iso_now(),
            "directories": list(self._directories),
        }

    def save(self) -> None:
        with _WRITE_LOCK:
            write_json_atomic(self.path, self.to_dict())


def update_ignore(
    path: str | Path | None,
    *,
    add: str | None = None,
    remove: str | None = None,
) -> IgnoreList:
    """读-改-写一步完成，供并发的 Web 请求使用。"""
    with _WRITE_LOCK:
        ignore = IgnoreList.load(path)
        if add is not None:
            ignore.add(add)
        if remove is not None:
            ignore.remove(remove)
        ignore.save()
        return ignore
