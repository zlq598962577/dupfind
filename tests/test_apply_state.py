"""apply 与忽略清单 / 处置状态 / 中途停止的集成行为。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dupfind.apply import ApplyOptions, build_plan, execute_plan, run_apply
from dupfind.ignore import IgnoreList
from dupfind.report import load_report
from dupfind.report_state import load_effective_state

from report_fixtures import SIZE, write_file, write_report

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")


@pytest.fixture
def scan(tmp_path: Path) -> dict:
    root = tmp_path / "data"
    root.mkdir()
    files = [
        write_file(root / "docs" / "报告.pdf"),
        write_file(root / "docs" / "报告 - 副本.pdf"),
        write_file(root / "pics" / "照片.zip"),
        write_file(root / "pics" / "照片 - 副本.zip"),
    ]
    report = write_report(tmp_path, [[files[0], files[1]], [files[2], files[3]]])
    return {"root": root, "files": files, "report": report, "trash": tmp_path / "trash"}


def _options(trash: Path, **overrides) -> ApplyOptions:
    options = ApplyOptions(trash_dir=str(trash), keep_mode="shortest", dry_run=False, assume_yes=True)
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def test_build_plan_skips_already_handled_groups(scan):
    document = load_report(scan["report"])
    options = _options(scan["trash"], skip_group_ids=frozenset({1}))
    plan = build_plan(document, options)
    assert [group.group_id for group in plan.groups] == [2]
    assert (1, "已处置或已忽略") in plan.skipped


def test_build_plan_drops_groups_fully_inside_ignored_directories(scan, tmp_path):
    document = load_report(scan["report"])
    ignore = IgnoreList(tmp_path / "ignore.json", [str(scan["root"] / "docs")])
    plan = build_plan(document, _options(scan["trash"], ignore=ignore))
    assert [group.group_id for group in plan.groups] == [2]
    assert (1, "组内文件位于忽略目录") in plan.skipped


def test_ignored_files_reduce_savings_before_threshold(scan, tmp_path):
    """跨忽略边界的组按剩余副本重算可节省空间，再用它比较阈值。"""
    report = write_report(
        tmp_path,
        [[*scan["files"]],],
        name="mixed.json",
    )
    document = load_report(report)
    ignore = IgnoreList(tmp_path / "ignore.json", [str(scan["root"] / "docs")])

    # 原始 4 份物理数据 → 可节省 3×SIZE，超过阈值
    plan = build_plan(document, _options(scan["trash"], min_savings=2 * SIZE))
    assert [group.group_id for group in plan.groups] == [1]

    # 忽略 docs 后只剩 pics 的 2 份 → 可节省 SIZE，低于阈值
    plan = build_plan(document, _options(scan["trash"], ignore=ignore, min_savings=2 * SIZE))
    assert plan.groups == []
    assert (1, "低于最小可节省阈值") in plan.skipped


def test_ignored_boundary_still_disposable_when_two_copies_remain(scan, tmp_path):
    report = write_report(
        tmp_path,
        [[scan["root"] / "docs" / "报告.pdf", *scan["files"][2:]]],
        name="mixed.json",
    )
    document = load_report(report)
    ignore = IgnoreList(tmp_path / "ignore.json", [str(scan["root"] / "docs")])

    # 忽略 docs 后还剩 pics 的两份：仍然可以正常处置
    plan = build_plan(document, _options(scan["trash"], ignore=ignore))
    assert [group.group_id for group in plan.groups] == [1]
    assert plan.groups[0].files_to_move == 1, "应只移走 pics 里的那份副本"
    assert all(
        str(scan["root"] / "docs").lower() not in item.path.lower()
        for cluster in plan.groups[0].removals
        for item in cluster
    )


def test_group_fully_ignored_is_not_planned(scan, tmp_path):
    report = write_report(tmp_path, [[*scan["files"][:2]]], name="docs-only.json")
    document = load_report(report)
    ignore = IgnoreList(tmp_path / "ignore.json", [str(scan["root"] / "docs")])
    plan = build_plan(document, _options(scan["trash"], ignore=ignore))
    assert plan.groups == []
    assert (1, "组内文件位于忽略目录") in plan.skipped


def test_execute_plan_never_moves_ignored_files(scan, tmp_path):
    document = load_report(scan["report"])
    ignore = IgnoreList(tmp_path / "ignore.json", [str(scan["root"] / "docs")])
    options = _options(scan["trash"], ignore=ignore)
    plan = build_plan(document, options)
    execute_plan(document, plan, options)

    assert (scan["root"] / "docs" / "报告.pdf").is_file()
    assert (scan["root"] / "docs" / "报告 - 副本.pdf").is_file(), "忽略目录里的文件被移动了"
    assert not (scan["root"] / "pics" / "照片 - 副本.zip").exists()


def test_execute_plan_stops_at_group_boundary(scan):
    document = load_report(scan["report"])
    options = _options(scan["trash"])
    plan = build_plan(document, options)

    calls: list[int] = []

    def should_stop() -> bool:
        calls.append(1)
        return len(calls) > 1

    stats = execute_plan(document, plan, options, should_stop=should_stop)
    assert stats.cancelled is True
    assert stats.processed_group_ids == [1]
    assert not (scan["root"] / "docs" / "报告 - 副本.pdf").exists(), "第一组应已处理完"
    assert (scan["root"] / "pics" / "照片 - 副本.zip").is_file(), "第二组不应被碰"


def test_run_apply_records_state(scan):
    document = load_report(scan["report"])
    options = _options(scan["trash"])
    assert run_apply(document, options) == 0

    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.skip_ids() == frozenset({1, 2})
    assert state.groups[1].moved_files == 1
    assert state.groups[2].moved_files == 1


def test_dry_run_writes_no_state(scan):
    document = load_report(scan["report"])
    options = _options(scan["trash"], dry_run=True)
    run_apply(document, options)

    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.groups == {}
    assert (scan["root"] / "docs" / "报告 - 副本.pdf").is_file()


def test_partial_group_upgrades_when_remaining_copies_already_gone(tmp_path):
    """重试 partial 组时，已移走的副本不算阻塞：剩余的一份移动成功即 handled。"""
    root = tmp_path / "data"
    root.mkdir()
    keep = write_file(root / "keep.jpg")
    gone = write_file(root / "a-very-long-removed-name.jpg")
    move = write_file(root / "move.jpg")
    report = write_report(tmp_path, [[keep, gone, move]], name="partial.json")
    document = load_report(report)

    # 模拟上一次部分处置：gone 已经被移进隔离区
    trash = tmp_path / "trash"
    trash.mkdir()
    gone.rename(trash / "gone.jpg")

    options = _options(trash)
    stats = execute_plan(document, build_plan(document, options), options)
    assert stats.missing_files == 1
    assert stats.group_results == {1: ("handled", 1, SIZE)}
