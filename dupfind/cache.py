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
                self._entries[key] = CacheEntry(
                    size=int(value["size"]),
                    mtime=float(value["mtime"]),
                    sample=value.get("sample"),
                    digest=value.get("digest"),
                    algorithm=value.get("algorithm"),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def lookup(self, path: str, size: int, mtime: float) -> CacheEntry | None:
        entry = self._entries.get(path)
        if entry is None:
            return None
        if entry.size != size:
            return None
        if abs(entry.mtime - mtime) > 1e-6:
            return None
        return entry

    def store(self, path: str, size: int, mtime: float, sample: str | None, digest: str | None, algorithm: str) -> None:
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
            size=size, mtime=mtime, sample=sample, digest=resolved_digest, algorithm=algorithm
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
