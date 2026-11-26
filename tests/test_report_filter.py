"""视图与处置共用的过滤规则：聚簇、可节省空间、忽略目录。"""

from __future__ import annotations

from pathlib import Path

from dupfind.ignore import IgnoreList
from dupfind.report import ReportFile, ReportGroup
from dupfind.report_filter import (
    cluster_files,
    cluster_occupancy,
    effective_group,
    hidden_files,
    savings_of,
    visible_files,
)

SIZE = 4096


def _file(path: str, *, file_id: str | None = None, unstable: bool = False, size: int | None = SIZE) -> ReportFile:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return ReportFile(
        path=path,
        size_on_disk=size,
        normalized_name=name,
        mtime=1000.0,
        file_id=file_id,
        shared=False,
        unstable=unstable,
    )


def _group(files: list[ReportFile], *, size: int = SIZE, group_id: int = 1) -> ReportGroup:
    clusters = cluster_files(files)
    savings = sum(cluster_occupancy(cluster, size) for cluster in clusters) - max(
        (cluster_occupancy(cluster, size) for cluster in clusters), default=0
    )
    return ReportGroup(id=group_id, status="confirmed", size=size, savings_bytes=savings, files=files)


def test_same_file_id_is_one_physical_cluster():
    files = [
        _file(r"D:\a\one.jpg", file_id="id-1"),
        _file(r"D:\b\two.jpg", file_id="id-1"),
        _file(r"D:\b\three.jpg", file_id="id-2"),
        _file(r"D:\c\four.jpg"),
    ]
    clusters = cluster_files(files)
    assert len(clusters) == 3
    assert len(clusters[0]) == 2


def test_files_without_file_id_fall_back_to_path():
    files = [_file(r"D:\a\one.jpg"), _file(r"d:\A\ONE.JPG")]
    assert len(cluster_files(files)) == 1


def test_files_without_file_id_normalize_separators():
    """回退键必须用 ntpath.normcase：它还统一 / 与 \\，否则同一文件被拆成两簇。"""
    files = [_file("D:/a/one.jpg"), _file(r"d:\A\ONE.JPG")]
    assert len(cluster_files(files)) == 1


def test_unstable_files_are_skipped():
    files = [_file(r"D:\a\one.jpg", file_id="id-1"), _file(r"D:\b\two.jpg", file_id="id-2", unstable=True)]
    clusters = cluster_files(files)
    assert len(clusters) == 1
    assert clusters[0][0].path == r"D:\a\one.jpg"


def test_cluster_occupancy_counts_hardlinks_once():
    cluster = [_file(r"D:\a\one.jpg", file_id="id-1"), _file(r"D:\b\two.jpg", file_id="id-1")]
    assert cluster_occupancy(cluster, SIZE) == SIZE


def test_cluster_occupancy_falls_back_to_group_size():
    cluster = [_file(r"D:\a\one.jpg", size=None)]
    assert cluster_occupancy(cluster, SIZE) == SIZE


def test_savings_of_keeps_the_largest_cluster():
    files = [
        _file(r"D:\a\one.jpg", file_id="id-1", size=100),
        _file(r"D:\b\two.jpg", file_id="id-2", size=100),
        _file(r"D:\b\two-link.jpg", file_id="id-2", size=100),
    ]
    assert savings_of(files, SIZE) == 100


def test_savings_of_single_cluster_is_zero():
    files = [_file(r"D:\a\one.jpg", file_id="id-1"), _file(r"D:\b\two.jpg", file_id="id-1")]
    assert savings_of(files, SIZE) == 0


def test_visible_and_hidden_files(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    group = _group(
        [
            _file(r"D:\Games\Cache\a.jpg", file_id="id-1"),
            _file(r"D:\Photos\b.jpg", file_id="id-2"),
            _file(r"D:\Photos\c.jpg", file_id="id-3"),
        ]
    )
    assert [item.path for item in visible_files(group, ignore)] == [
        r"D:\Photos\b.jpg",
        r"D:\Photos\c.jpg",
    ]
    assert [item.path for item in hidden_files(group, ignore)] == [r"D:\Games\Cache\a.jpg"]
    assert visible_files(group, None) == group.files


def test_effective_group_recalculates_savings(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    group = ReportGroup(
        id=3,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE * 2,
        files=[
            _file(r"D:\Games\Cache\a.jpg", file_id="id-1"),
            _file(r"D:\Photos\b.jpg", file_id="id-2"),
            _file(r"D:\Photos\c.jpg", file_id="id-3"),
        ],
    )
    effective = effective_group(group, ignore)
    assert effective is not None
    assert len(effective.files) == 2
    assert effective.savings_bytes == SIZE, "隐藏一份后只剩一份可节省"


def test_effective_group_drops_whole_cluster_with_ignored_member(tmp_path):
    """忽略目录里的一份硬链接，同簇其余链也不能被处置：数据仍在忽略目录里，
    移走可见的链既不会释放空间，也违背「整簇保留或整簇移走」。"""
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    group = ReportGroup(
        id=5,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE * 2,
        files=[
            _file(r"D:\Games\Cache\a.jpg", file_id="id-1"),
            _file(r"D:\Photos\a-link.jpg", file_id="id-1"),
            _file(r"D:\Photos\b.jpg", file_id="id-2"),
            _file(r"D:\Photos\c.jpg", file_id="id-3"),
        ],
    )
    effective = effective_group(group, ignore)
    assert effective is not None
    assert [item.path for item in effective.files] == [r"D:\Photos\b.jpg", r"D:\Photos\c.jpg"]
    assert effective.savings_bytes == SIZE


def test_effective_group_is_none_when_too_few_remain(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    group = ReportGroup(
        id=4,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            _file(r"D:\Games\Cache\a.jpg", file_id="id-1"),
            _file(r"D:\Photos\b.jpg", file_id="id-2"),
        ],
    )
    assert effective_group(group, ignore) is None


def test_effective_group_without_ignore_returns_all_files():
    group = _group([_file(r"D:\a.jpg", file_id="id-1"), _file(r"D:\b.jpg", file_id="id-2")])
    effective = effective_group(group, None)
    assert effective is not None
    assert len(effective.files) == 2
    assert isinstance(effective, ReportGroup)
    assert Path(effective.files[0].path).name == "a.jpg"
