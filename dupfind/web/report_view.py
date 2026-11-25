"""把报告转换为前端可直接渲染的结构（含 --keep 策略标注）。"""

from __future__ import annotations

import ntpath
from typing import Any

from ..apply import choose_keep, choose_keep_cluster, physical_clusters
from ..report import ReportDocument
from ..units import iso_from_mtime

KEEP_MODES = ("shortest", "newest")


def _file_payload(path: str, size: int, size_on_disk: int | None, mtime: float | None, normalized: str, shared: bool, unstable: bool) -> dict[str, Any]:
    return {
        "path": path,
        "name": ntpath.basename(path),
        "size": size,
        "sizeOnDisk": size_on_disk if size_on_disk is not None else size,
        "mtime": iso_from_mtime(mtime) if mtime is not None else None,
        "normalizedName": normalized,
        "shared": shared,
        "unstable": unstable,
    }


def build_view(document: ReportDocument) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    counts = {"confirmed": 0, "suspect": 0, "shared": 0}
    savings = 0

    for group in sorted(document.groups, key=lambda item: (-item.savings_bytes, item.size, item.id)):
        counts[group.status] = counts.get(group.status, 0) + 1
        if group.status == "confirmed":
            savings += group.savings_bytes

        clusters = physical_clusters(group)
        keepers: dict[str, str] = {}
        # 每种策略下「实际会被移动的文件数」。前端 action bar 要用它，而不是
        # files.length - 1 —— 保留簇可能包含多个硬链接路径（整簇保留），
        # unstable 文件也不参与，用 -1 估算会偏大。
        removal_counts: dict[str, int] = {}
        if len(clusters) >= 2:
            for mode in KEEP_MODES:
                keep_cluster = choose_keep_cluster(clusters, mode)
                keepers[mode] = choose_keep(keep_cluster, mode).path
                removal_counts[mode] = sum(
                    len(cluster) for cluster in clusters if cluster is not keep_cluster
                )

        groups.append(
            {
                "id": group.id,
                "status": group.status,
                "size": group.size,
                "savingsBytes": group.savings_bytes,
                "physicalCount": len(clusters),
                "keepers": keepers,
                "removalCounts": removal_counts,
                "files": [
                    _file_payload(
                        item.path,
                        group.size,
                        item.size_on_disk,
                        item.mtime,
                        item.normalized_name,
                        item.shared,
                        item.unstable,
                    )
                    for item in group.files
                ],
            }
        )

    return {
        "version": document.version,
        "reportPath": str(document.path),
        "root": document.root,
        "partial": document.partial,
        "scan": document.raw_scan,
        "summary": {
            "groups": len(groups),
            "confirmedGroups": counts["confirmed"],
            "suspectGroups": counts["suspect"],
            "sharedGroups": counts["shared"],
            "savingsBytes": savings,
        },
        "keepModes": list(KEEP_MODES),
        "groups": groups,
    }
