"""把报告转换为前端可直接渲染的结构。

视图在原始报告之上叠加两层信息：

* **类别**（``filetypes``）：默认只展示用户文件，代码/配置/程序类可一键切换；
* **处置状态**（``report_state``）与**忽略清单**（``ignore``）：已处理过的组
  不会伪装成待处理，被忽略目录里的文件不再出现，跨目录分组的可节省空间
  按剩余副本重新计算。

这里只做展示层的过滤，不改变报告的权威性：磁盘上发生了什么，仍以报告 +
隔离区 manifest 为准。
"""

from __future__ import annotations

import ntpath
from collections import Counter
from typing import Any

from ..apply import choose_keep, choose_keep_cluster
from ..filetypes import (
    CATEGORIES,
    CATEGORY_LABELS,
    USER_CATEGORIES,
    category_for_path,
    group_category,
    top_dir,
)
from ..ignore import IgnoreList
from ..report import ReportDocument, ReportFile, ReportGroup
from ..report_filter import cluster_files, removable_clusters, savings_of_clusters, visible_files
from ..report_state import EffectiveState, state_path_for
from ..units import iso_from_mtime

KEEP_MODES = ("shortest", "newest")
TOP_DIR_LIMIT = 20
SUGGEST_IGNORE_MIN_GROUPS = 20


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
        "category": category_for_path(path),
    }


def _top_dir_path(root: str, name: str) -> str:
    base = ntpath.normpath(str(root or ""))
    if name == "(根目录)":
        return base
    return ntpath.join(base, name)


def _group_payload(
    group: ReportGroup,
    document: ReportDocument,
    *,
    clusters: list[list[ReportFile]] | None = None,
) -> dict[str, Any]:
    files = list(group.files)
    if clusters is None:
        clusters = cluster_files(files)
    keepers: dict[str, str] = {}
    removal_counts: dict[str, int] = {}
    if len(clusters) >= 2:
        for mode in KEEP_MODES:
            keep_cluster = choose_keep_cluster(clusters, mode)
            keepers[mode] = choose_keep(keep_cluster, mode).path
            removal_counts[mode] = sum(
                len(cluster) for cluster in clusters if cluster is not keep_cluster
            )
    category = group_category(item.path for item in files)
    return {
        "id": group.id,
        "status": group.status,
        "size": group.size,
        # 可节省空间按「可处置的物理簇」计算：被忽略目录拆开的簇不能算进来
        "savingsBytes": savings_of_clusters(clusters, group.size) if len(clusters) >= 2 else 0,
        "physicalCount": len(clusters),
        "keepers": keepers,
        "removalCounts": removal_counts,
        "category": category,
        "topDir": top_dir(files[0].path, document.root),
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
            for item in files
        ],
    }


def build_view(
    document: ReportDocument,
    *,
    ignore: IgnoreList | None = None,
    state: EffectiveState | None = None,
) -> dict[str, Any]:
    """生成 Web 视图：叠加类别、忽略清单与处置状态后的组与汇总。"""
    # 先算好每组的可见文件与可处置簇：排序、卡片数字与目录汇总都用同一口径，
    # 否则会出现「卡片显示可节省 0，却排在可节省 1MB 的组前面」
    prepared = [
        (
            group,
            visible_files(group, ignore),
            [item for item in group.files if ignore is not None and ignore.contains(item.path)],
            removable_clusters(group.files, ignore),
        )
        for group in document.groups
    ]
    prepared.sort(
        key=lambda row: (
            -(savings_of_clusters(row[3], row[0].size) if len(row[3]) >= 2 else 0),
            row[0].size,
            row[0].id,
        )
    )
    groups: list[dict[str, Any]] = []
    counts = {"confirmed": 0, "suspect": 0, "shared": 0}
    savings = 0
    category_counts: Counter[str] = Counter()
    category_savings: Counter[str] = Counter()
    top_dirs: dict[str, dict[str, int]] = {}
    disposal_counts = {"handled": 0, "partial": 0, "ignored": 0, "pending": 0}
    ignored_dirs: dict[str, dict[str, int]] = {}
    ignored_groups = 0
    ignored_files = 0
    ignored_savings = 0
    raw_counts = {"confirmed": 0, "suspect": 0, "shared": 0}
    raw_savings = 0

    for group in document.groups:
        raw_counts[group.status] = raw_counts.get(group.status, 0) + 1
        if group.status == "confirmed":
            raw_savings += group.savings_bytes

    for group, files, hidden, clusters in prepared:
        if hidden:
            ignored_files += len(hidden)
            visible_savings = savings_of_clusters(clusters, group.size) if len(clusters) >= 2 else 0
            lost = group.savings_bytes - visible_savings
            ignored_savings += max(0, lost)
            bucket_key = ignore.match(hidden[0].path) if ignore is not None else None
            if bucket_key is not None:
                bucket = ignored_dirs.setdefault(bucket_key, {"groups": 0, "files": 0, "savingsBytes": 0})
                bucket["groups"] += 1
                bucket["files"] += len(hidden)
                bucket["savingsBytes"] += max(0, lost)
        if len(files) < 2:
            if hidden:
                ignored_groups += 1
            continue

        effective = _group_payload(
            ReportGroup(
                id=group.id,
                status=group.status,
                size=group.size,
                savings_bytes=savings_of_clusters(clusters, group.size),
                files=files,
            ),
            document,
            clusters=clusters,
        )
        effective["ignoredFiles"] = len(hidden)
        record = state.groups.get(group.id) if state is not None else None
        disposal_state = record.state if record is not None else "pending"
        effective["disposalState"] = disposal_state
        # pending 是「恢复为待处理」留下的墓碑，只用来压制 manifest 兜底，
        # 展示上必须和从未处理过完全一样
        display = record if record is not None and record.state != "pending" else None
        effective["disposal"] = (
            {
                "handledAt": display.handled_at,
                "movedFiles": display.moved_files,
                "movedBytes": display.moved_bytes,
            }
            if display is not None
            else None
        )
        groups.append(effective)

        counts[group.status] = counts.get(group.status, 0) + 1
        # 已处置 / 已忽略的组不再「可节省」：空间已经在隔离区里等着清空，
        # 继续计入汇总会让用户以为还有一批没处理
        counted = group.status == "confirmed" and disposal_state not in ("handled", "ignored")
        if counted:
            savings += effective["savingsBytes"]
        disposal_counts[disposal_state] = disposal_counts.get(disposal_state, 0) + 1

        category = effective["category"]
        category_counts[category] += 1
        bucket = top_dirs.setdefault(
            effective["topDir"],
            {"groups": 0, "confirmedGroups": 0, "userGroups": 0, "savingsBytes": 0},
        )
        bucket["groups"] += 1
        # 目录面板的「可节省」与顶部汇总、卡片使用同一口径，处置完整个
        # 目录后不能还显示几 GB 没清理
        if counted:
            category_savings[category] += effective["savingsBytes"]
            bucket["savingsBytes"] += effective["savingsBytes"]
        if group.status == "confirmed":
            bucket["confirmedGroups"] += 1
        if category in USER_CATEGORIES:
            bucket["userGroups"] += 1

    categories = [
        {
            "key": key,
            "label": CATEGORY_LABELS.get(key, key),
            "groups": category_counts[key],
            "savingsBytes": category_savings[key],
            "user": key in USER_CATEGORIES,
        }
        for key in CATEGORIES
        if category_counts[key] > 0
    ]

    top_dir_payload = [
        {
            "dir": name,
            "path": _top_dir_path(document.root, name),
            "groups": bucket["groups"],
            "confirmedGroups": bucket["confirmedGroups"],
            "userGroups": bucket["userGroups"],
            "savingsBytes": bucket["savingsBytes"],
            "suggestIgnore": bucket["groups"] >= SUGGEST_IGNORE_MIN_GROUPS
            and bucket["userGroups"] * 5 <= bucket["groups"],
        }
        for name, bucket in sorted(
            top_dirs.items(), key=lambda item: (-item[1]["groups"], -item[1]["savingsBytes"])
        )[:TOP_DIR_LIMIT]
    ]

    user_groups = sum(category_counts[key] for key in USER_CATEGORIES)
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
            "rawGroups": len(document.groups),
            "rawConfirmedGroups": raw_counts["confirmed"],
            "rawSavingsBytes": raw_savings,
            "ignoredGroups": ignored_groups,
            "ignoredFiles": ignored_files,
            "ignoredSavingsBytes": ignored_savings,
            "userGroups": user_groups,
            "hiddenByTypeGroups": len(groups) - user_groups,
            "disposal": disposal_counts,
        },
        "categories": categories,
        # 前端需要完整的类别字典与「哪些算用户文件」，不能只给非空类别：
        # 空类别下仍要能显示标签、判断筛选范围
        "categoryLabels": {key: CATEGORY_LABELS.get(key, key) for key in CATEGORIES},
        "userCategories": sorted(USER_CATEGORIES),
        "topDirs": top_dir_payload,
        "ignore": {
            "path": str(ignore.path) if ignore is not None else None,
            "directories": [
                {
                    "path": directory,
                    "groups": ignored_dirs.get(directory, {}).get("groups", 0),
                    "files": ignored_dirs.get(directory, {}).get("files", 0),
                    "savingsBytes": ignored_dirs.get(directory, {}).get("savingsBytes", 0),
                }
                for directory in (ignore.directories if ignore is not None else [])
            ],
        },
        "state": {
            "file": str(state_path_for(document.path)),
            "identityOk": bool(state.identity_ok) if state is not None else True,
            "manifestGroups": state.manifest_groups if state is not None else 0,
        },
        "keepModes": list(KEEP_MODES),
        "groups": groups,
    }
