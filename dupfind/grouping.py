"""按大小分桶 + 词元包含判定，生成候选组。"""

from __future__ import annotations

from collections import defaultdict

from .models import FileEntry
from .normalize import FilenameNormalizer


def annotate_names(files: list[FileEntry], normalizer: FilenameNormalizer) -> None:
    """为每个文件填充归一化名与词元集合。"""
    for entry in files:
        entry.normalized_name = normalizer.normalize(entry.name)
        entry.tokens = normalizer.token_set(entry.normalized_name)


def _bucket_by_size(files: list[FileEntry]) -> dict[int, list[FileEntry]]:
    buckets: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in files:
        buckets[entry.size].append(entry)
    return buckets


def build_candidate_groups(files: list[FileEntry], normalizer: FilenameNormalizer) -> list[list[FileEntry]]:
    """返回候选组；组内成员同大小且归一化文件名存在包含关系。"""
    annotate_names(files, normalizer)
    groups: list[list[FileEntry]] = []
    for bucket in _bucket_by_size(files).values():
        if len(bucket) >= 2:
            groups.extend(_group_bucket(bucket, normalizer))
    return groups


def content_only_groups(files: list[FileEntry], normalizer: FilenameNormalizer) -> list[list[FileEntry]]:
    """--content-only：忽略名字规则，所有同大小文件（数量 ≥ 2）进入哈希。"""
    annotate_names(files, normalizer)
    return [bucket for bucket in _bucket_by_size(files).values() if len(bucket) >= 2]


class _UnionFind:
    __slots__ = ("_parent",)

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        root = index
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[index] != root:
            self._parent[index], index = root, self._parent[index]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def _group_bucket(bucket: list[FileEntry], normalizer: FilenameNormalizer) -> list[list[FileEntry]]:
    """桶内以词元集合为节点做子集匹配，再用并查集合并连通分量。"""
    set_to_files: dict[frozenset[str], list[int]] = defaultdict(list)
    for index, entry in enumerate(bucket):
        if entry.tokens:
            set_to_files[entry.tokens].append(index)

    token_sets = list(set_to_files.keys())
    if not token_sets:
        return []

    token_index: dict[str, list[int]] = defaultdict(list)
    for position, token_set in enumerate(token_sets):
        for token in token_set:
            token_index[token].append(position)

    union_find = _UnionFind(len(token_sets))
    for position, token_set in enumerate(token_sets):
        candidates: set[int] = set()
        for token in token_set:
            candidates.update(token_index[token])
        for other_position in candidates:
            other = token_sets[other_position]
            if len(other) > len(token_set):
                continue
            sample_name = bucket[set_to_files[other][0]].normalized_name
            if normalizer.is_contained(sample_name, other, token_set):
                union_find.union(position, other_position)

    components: dict[int, list[int]] = defaultdict(list)
    for position in range(len(token_sets)):
        components[union_find.find(position)].append(position)
    return _components_to_groups(token_sets, set_to_files, components, bucket)


def _components_to_groups(
    token_sets: list[frozenset[str]],
    set_to_files: dict[frozenset[str], list[int]],
    components: dict[int, list[int]],
    bucket: list[FileEntry],
) -> list[list[FileEntry]]:
    groups: list[list[FileEntry]] = []
    for component in components.values():
        members: list[FileEntry] = []
        for position in component:
            members.extend(bucket[index] for index in set_to_files[token_sets[position]])
        if len(members) >= 2:
            groups.append(members)
    return groups
