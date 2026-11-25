"""规格第 10 节验收用例 6：硬链接标记 shared=true 且不计入可节省空间。"""

from __future__ import annotations

import sys

import pytest

from fixture_builder import (
    DEFAULT_SIZE,
    HardlinkNotSupported,
    make_copy_pair,
    make_hardlink_pair,
)
from harness import find_groups_with_paths, load_report, scan, total_savings, win_norm

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = DEFAULT_SIZE


def test_case_06_hardlinks_are_shared_and_not_counted(cli, tmp_path):
    """硬链接对 → status=shared、shared=true、savingsBytes=0，且不计入总节省。"""
    root = tmp_path / "data"
    root.mkdir()

    normal = make_copy_pair(root, directory="normal", stem="正文文档", ext=".dat", size=SIZE, seed=0)
    try:
        source, link = make_hardlink_pair(
            root, directory="links", stem="镜像备份", ext=".dat", size=SIZE, seed=1
        )
    except HardlinkNotSupported as exc:
        pytest.skip(
            f"当前临时目录无法创建硬链接（{exc}）。"
            "人工核对：在 NTFS 卷执行 fsutil hardlink create <链接> <源文件>，"
            "再运行 dupfind scan <根目录>，确认该组 status=shared、文件 shared=true、"
            "savingsBytes=0 且不计入总可节省空间。"
        )

    out = tmp_path / "case06.json"
    result = scan(cli, root, out)
    assert result.returncode == 0, result.debug()

    report = load_report(out)

    # 普通副本对仍应 confirmed，并产生 SIZE 的可节省空间
    normal_groups = find_groups_with_paths(report, *normal)
    assert len(normal_groups) == 1
    assert normal_groups[0]["status"] == "confirmed"
    assert int(normal_groups[0]["savingsBytes"]) == SIZE

    # 硬链接对被识别为 shared，且可节省空间为 0
    link_groups = find_groups_with_paths(report, source, link)
    assert len(link_groups) == 1, f"硬链接未合并到同一分组: {report['groups']}"
    shared_group = link_groups[0]
    assert shared_group["status"] == "shared"
    assert {win_norm(item["path"]) for item in shared_group["files"]} == {
        win_norm(source),
        win_norm(link),
    }
    assert all(item["shared"] is True for item in shared_group["files"])
    assert int(shared_group["savingsBytes"]) == 0

    assert total_savings(report) == SIZE, "硬链接的占用不应计入总可节省空间"
