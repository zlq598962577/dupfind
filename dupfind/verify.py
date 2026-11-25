"""采样 + 全量哈希 + 硬链接识别，把候选组收敛为确认组。"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import HashCache
from .models import FileEntry, GroupRecord, ScanStats
from .win32 import allocated_size, file_identity, long_path

try:  # BLAKE3 优先，缺失时退回 SHA-256
    import blake3 as _blake3
except ImportError:  # pragma: no cover - 取决于环境是否安装
    _blake3 = None

BLOCK_SIZE = 1 << 20


def hash_algorithm() -> str:
    return "blake3" if _blake3 is not None else "sha256"


def _new_hasher():
    if _blake3 is not None:
        return _blake3.blake3()
    return hashlib.sha256()


def sample_digest(path: str, size: int, sample_size: int) -> str:
    """首 4KB + 尾 4KB 的摘要；文件不足两段时自然覆盖全部内容。"""
    with open(long_path(path), "rb") as fp:
        head = fp.read(sample_size)
        tail = b""
        if size > sample_size:
            fp.seek(max(size - sample_size, 0))
            tail = fp.read(sample_size)
    return hashlib.sha256(head + b"\x00" + tail).hexdigest()


def full_digest(path: str) -> str:
    hasher = _new_hasher()
    with open(long_path(path), "rb") as fp:
        while True:
            chunk = fp.read(BLOCK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _same_snapshot(entry: FileEntry, st: os.stat_result) -> bool:
    return int(st.st_size) == entry.size and abs(float(st.st_mtime) - entry.mtime) <= 1e-6


def physical_clusters(entries: list[FileEntry]) -> list[list[FileEntry]]:
    """按物理身份（FileId，拿不到时退回路径）把文件聚成簇。

    同一簇内的路径指向同一份磁盘数据（硬链接），只占一份空间；不同簇才是
    真正各自占用空间的独立副本。不稳定文件不参与。
    """
    clusters: dict[object, list[FileEntry]] = defaultdict(list)
    for entry in entries:
        if entry.unstable:
            continue
        clusters[entry.identity].append(entry)
    return list(clusters.values())


def _cluster_occupancy(cluster: list[FileEntry]) -> int:
    """一簇硬链接的实际占用：只算一份，取成员中最大者（保守）。"""
    return max(
        entry.allocated if entry.allocated is not None else entry.size for entry in cluster
    )


def compute_savings(entries: list[FileEntry]) -> int:
    """除保留项外的实际占用之和。

    按物理簇计算：组内必须存在 ≥2 个独立物理文件才可能节省空间；保留占用
    最大的那一簇，其余簇全额计入。这样「1 个独立副本 + 若干互为硬链接的
    文件」也能正确得出可节省值，而不是被判成 0。
    """
    clusters = physical_clusters(entries)
    if len(clusters) < 2:
        return 0
    occupied = [_cluster_occupancy(cluster) for cluster in clusters]
    return sum(occupied) - max(occupied)


class Verifier:
    """一次扫描对应一个实例。"""

    __slots__ = ("_sample_size", "_threads", "_cache", "_stats", "_hashing", "_algorithm", "_should_stop", "_on_group_done")

    def __init__(
        self,
        *,
        sample_size: int,
        threads: int,
        cache: HashCache,
        stats: ScanStats,
        hashing: bool = True,
        should_stop: Callable[[], bool] | None = None,
        on_group_done: Callable[[int, int], None] | None = None,
    ) -> None:
        self._sample_size = sample_size
        self._threads = max(1, threads)
        self._cache = cache
        self._stats = stats
        self._hashing = hashing
        self._algorithm = hash_algorithm()
        self._should_stop = should_stop
        self._on_group_done = on_group_done

    def build_groups(self, candidate_groups: list[list[FileEntry]]) -> list[GroupRecord]:
        records: list[GroupRecord] = []
        confirmed = 0
        for processed, files in enumerate(candidate_groups, start=1):
            if self._should_stop is not None and self._should_stop():
                self._stats.partial = True
                break
            try:
                produced = self._verify_group(files)
            except KeyboardInterrupt:
                self._stats.partial = True
                break
            records.extend(produced)
            # 增量累加：不要每次重新遍历全部 records，否则整体是 O(n²)
            confirmed += sum(1 for record in produced if record.status == "confirmed")
            if self._on_group_done is not None:
                self._on_group_done(processed, confirmed)
        return records

    # --- 单个候选组 -------------------------------------------------

    def _verify_group(self, files: list[FileEntry]) -> list[GroupRecord]:
        if not self._hashing:
            return [self._suspect_group(files)]

        identity_groups: dict[object, list[FileEntry]] = defaultdict(list)
        for entry in files:
            entry.allocated = allocated_size(entry.path)
            if entry.allocated is None:
                entry.allocated = entry.size
            entry.file_id = file_identity(entry.path)
            identity_groups[entry.identity].append(entry)

        if len(identity_groups) == 1:
            for entry in files:
                entry.shared = True
            return [GroupRecord(id=0, status="shared", size=files[0].size, savings_bytes=0, files=files)]

        for duplicates in identity_groups.values():
            if len(duplicates) > 1:
                for entry in duplicates:
                    entry.shared = True

        representatives = [entries[0] for entries in identity_groups.values()]
        sample_buckets: dict[str, list[FileEntry]] = defaultdict(list)
        for entry in representatives:
            digest = self._sample(entry)
            if digest is not None:
                sample_buckets[digest].append(entry)

        records: list[GroupRecord] = []
        for bucket in sample_buckets.values():
            if len(bucket) < 2:
                continue
            records.extend(self._confirm(bucket, identity_groups))
        return records

    def _confirm(
        self,
        representatives: list[FileEntry],
        identity_groups: dict[object, list[FileEntry]],
    ) -> list[GroupRecord]:
        digests = self._hash_many(representatives)
        by_digest: dict[str, list[FileEntry]] = defaultdict(list)
        for entry, digest in digests.items():
            if digest is not None:
                by_digest[digest].append(entry)

        records: list[GroupRecord] = []
        for digest, members in by_digest.items():
            stable = [entry for entry in members if not entry.unstable]
            if len(stable) < 2:
                continue
            expanded: list[FileEntry] = []
            for entry in members:
                expanded.extend(identity_groups[entry.identity])
            records.append(
                GroupRecord(
                    id=0,
                    status="confirmed",
                    size=expanded[0].size,
                    savings_bytes=compute_savings(expanded),
                    files=expanded,
                    hash_value=digest,
                )
            )
        return records

    def _suspect_group(self, files: list[FileEntry]) -> GroupRecord:
        # 必须同时填 file_id：否则 compute_savings 会把硬链接当成各自独立的
        # 副本，报告里的 savingsBytes 被算大（规格 6.2：硬链接不占额外空间）。
        for entry in files:
            entry.allocated = allocated_size(entry.path)
            if entry.allocated is None:
                entry.allocated = entry.size
            entry.file_id = file_identity(entry.path)
        return GroupRecord(
            id=0,
            status="suspect",
            size=files[0].size,
            savings_bytes=compute_savings(files),
            files=files,
        )

    # --- I/O --------------------------------------------------------

    def _sample(self, entry: FileEntry) -> str | None:
        cached = self._cache.lookup(entry.path, entry.size, entry.mtime, sample_size=self._sample_size)
        if cached is not None and cached.sample:
            return cached.sample
        try:
            before = os.stat(long_path(entry.path))
            digest = sample_digest(entry.path, entry.size, self._sample_size)
            after = os.stat(long_path(entry.path))
        except PermissionError:
            self._stats.access_denied += 1
            return None
        except OSError:
            self._stats.errors += 1
            return None
        if not _same_snapshot(entry, before) or not _same_snapshot(entry, after):
            entry.unstable = True
        self._cache.store(
            entry.path, entry.size, entry.mtime, digest, None, self._algorithm, sample_size=self._sample_size
        )
        return digest

    def _hash_entry(self, entry: FileEntry) -> str | None:
        cached = self._cache.lookup(entry.path, entry.size, entry.mtime)
        if cached is not None and cached.digest and cached.algorithm == self._algorithm:
            return cached.digest
        try:
            before = os.stat(long_path(entry.path))
            digest = full_digest(entry.path)
            after = os.stat(long_path(entry.path))
        except PermissionError:
            self._stats.access_denied += 1
            return None
        except OSError:
            self._stats.errors += 1
            return None
        if not _same_snapshot(entry, before) or not _same_snapshot(entry, after):
            entry.unstable = True
        self._cache.store(entry.path, entry.size, entry.mtime, None, digest, self._algorithm)
        return digest

    def _hash_many(self, entries: list[FileEntry]) -> dict[FileEntry, str | None]:
        if len(entries) <= 1 or self._threads == 1:
            return {entry: self._hash_entry(entry) for entry in entries}
        results: dict[FileEntry, str | None] = {}
        executor = ThreadPoolExecutor(max_workers=self._threads)
        try:
            futures = {executor.submit(self._hash_entry, entry): entry for entry in entries}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        except KeyboardInterrupt:
            self._stats.partial = True
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results
