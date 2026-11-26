"""把忽略清单应用到报告：过滤文件、重算可节省空间。

视图与处置必须用**同一套**过滤规则，否则会出现「页面显示能省 1GB，点下去
却说没有可处置文件」这种自相矛盾。规则有三条：

* 位于忽略目录内的文件不参与任何计算；
* 一个物理簇只要有成员位于忽略目录，整簇都不参与处置 —— 否则会把「忽略
  目录里还留着一份」的簇当成可释放空间、移走它其余的链，既违背「整簇保留
  或整簇移走」的约定，也会高估释放量（数据仍由忽略目录里的链持有）；
* 过滤后不足两个物理簇的组没有可节省空间，整组不再展示、也不处置。
"""

from __future__ import annotations

import ntpath

from .ignore import IgnoreList
from .report import ReportFile, ReportGroup


def cluster_key(file: ReportFile) -> str:
    """同一物理文件的键；报告里没有 fileId 时退回路径。

    必须用 ``ntpath.normcase`` 而不是 ``lower()``：它还会统一分隔符并把
    ``/`` 转成 ``\\``，否则同一文件的两种写法会被当成两个物理簇。
    """
    return file.file_id or ntpath.normcase(str(file.path))


def cluster_files(files: list[ReportFile]) -> list[list[ReportFile]]:
    """按物理身份聚簇，跳过不稳定文件。"""
    clusters: dict[str, list[ReportFile]] = {}
    for item in files:
        if item.unstable:
            continue
        clusters.setdefault(cluster_key(item), []).append(item)
    return list(clusters.values())


def cluster_occupancy(cluster: list[ReportFile], fallback: int = 0) -> int:
    """一簇硬链接的实际占用：只算一份，取成员中最大者。"""
    sizes = [item.size_on_disk if item.size_on_disk is not None else fallback for item in cluster]
    return max(sizes) if sizes else 0


def savings_of_clusters(clusters: list[list[ReportFile]], fallback: int = 0) -> int:
    """按「保留占用最大的一簇」计算可节省空间（与扫描期口径一致）。"""
    if len(clusters) < 2:
        return 0
    occupied = [cluster_occupancy(cluster, fallback) for cluster in clusters]
    return sum(occupied) - max(occupied)


def savings_of(files: list[ReportFile], fallback: int = 0) -> int:
    return savings_of_clusters(cluster_files(files), fallback)


def visible_files(group: ReportGroup, ignore: IgnoreList | None) -> list[ReportFile]:
    if ignore is None or len(ignore) == 0:
        return list(group.files)
    return [item for item in group.files if not ignore.contains(item.path)]


def hidden_files(group: ReportGroup, ignore: IgnoreList | None) -> list[ReportFile]:
    if ignore is None or len(ignore) == 0:
        return []
    return [item for item in group.files if ignore.contains(item.path)]


def removable_clusters(files: list[ReportFile], ignore: IgnoreList | None) -> list[list[ReportFile]]:
    """可参与处置的物理簇：簇内任一文件被忽略就整簇排除。"""
    clusters = cluster_files(files)
    if ignore is None or len(ignore) == 0:
        return clusters
    return [
        cluster
        for cluster in clusters
        if not any(ignore.contains(item.path) for item in cluster)
    ]


def effective_group(group: ReportGroup, ignore: IgnoreList | None) -> ReportGroup | None:
    """过滤忽略目录后的组；可处置簇不足两个时返回 None（整组不再有意义）。"""
    clusters = removable_clusters(group.files, ignore)
    if len(clusters) < 2:
        return None
    files = [item for cluster in clusters for item in cluster]
    return ReportGroup(
        id=group.id,
        status=group.status,
        size=group.size,
        savings_bytes=savings_of_clusters(clusters, group.size),
        files=files,
    )
