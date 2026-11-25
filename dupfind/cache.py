"""采样/哈希结果缓存：以 path + size + mtime 为键，跨次运行复用。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json_atomic

CACHE_VERSION = 1


@dataclass(slots=True)
class CacheEntry:
    size: int
    mtime: float
    sample: str | None
    digest: str | None
    algorithm: str | None
    # 采样摘要依赖 sample_size（头尾各切多少字节）。不记下来的话，换
    # --sample-size 之后会拿到旧尺寸算出的摘要，和本次新算的摘要不等，
    # 内容完全相同的文件被分到不同 bucket —— 直接漏报。
    sample_size: int | None = None


class HashCache:
    """缓存不是必需的；加载失败（损坏/旧版本）时静默丢弃即可。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: dict[str, CacheEntry] = {}
        self._dirty = False

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def load(self) -> None:
        if self._path is None:
            return
        try:
            raw = read_json(self._path)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                raw_sample_size = value.get("sampleSize")
                self._entries[key] = CacheEntry(
                    size=int(value["size"]),
                    mtime=float(value["mtime"]),
                    sample=value.get("sample"),
                    digest=value.get("digest"),
                    algorithm=value.get("algorithm"),
                    sample_size=int(raw_sample_size) if isinstance(raw_sample_size, int) else None,
                )
            except (KeyError, TypeError, ValueError):
                continue

    def lookup(self, path: str, size: int, mtime: float, *, sample_size: int | None = None) -> CacheEntry | None:
        entry = self._entries.get(path)
        if entry is None:
            return None
        if entry.size != size:
            return None
        if abs(entry.mtime - mtime) > 1e-6:
            return None
        # 采样尺寸未知（旧版缓存没记这个字段）或已改变时，摘要一律不可复用。
        # 旧缓存因此只会被重新采样一次，代价很小；反过来复用则可能漏报。
        if sample_size is not None and entry.sample_size != sample_size:
            if entry.digest is None:
                return None
            return CacheEntry(
                size=entry.size,
                mtime=entry.mtime,
                sample=None,
                digest=entry.digest,
                algorithm=entry.algorithm,
                sample_size=sample_size,
            )
        return entry

    def store(
        self,
        path: str,
        size: int,
        mtime: float,
        sample: str | None,
        digest: str | None,
        algorithm: str,
        *,
        sample_size: int | None = None,
    ) -> None:
        """写入一条缓存；``sample_size`` 只在 ``sample`` 非空时有意义。"""
        existing = self._entries.get(path)
        resolved_digest = digest
        if (
            resolved_digest is None
            and existing is not None
            and existing.size == size
            and abs(existing.mtime - mtime) <= 1e-6
            and existing.algorithm == algorithm
        ):
            resolved_digest = existing.digest
        self._entries[path] = CacheEntry(
            size=size,
            mtime=mtime,
            sample=sample,
            digest=resolved_digest,
            algorithm=algorithm,
            sample_size=sample_size if sample is not None else existing.sample_size if existing else None,
        )
        self._dirty = True

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        payload: dict[str, Any] = {
            "version": CACHE_VERSION,
            "entries": {
                path: {
                    "size": entry.size,
                    "mtime": entry.mtime,
                    "sample": entry.sample,
                    "digest": entry.digest,
                    "algorithm": entry.algorithm,
                    "sampleSize": entry.sample_size,
                }
                for path, entry in self._entries.items()
            },
        }
        try:
            write_json_atomic(self._path, payload)
        except OSError:
            pass
        self._dirty = False


def default_cache_path() -> Path:
    return Path(os.path.abspath(".dupfind-cache"))
