"""夹具生成器自检：不依赖 dupfind 实现，保证测试骨架本身可运行。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fixture_builder import (
    DEFAULT_SIZE,
    HardlinkNotSupported,
    content_bytes,
    make_copy_pair,
    make_hardlink_pair,
    make_img_pair,
    make_same_size_different_content_pair,
    make_zero_byte_pair,
    populate_excluded_tree,
    snapshot,
    write_file,
)


def test_copy_pair_is_same_size_and_same_content(tmp_path):
    base, dup = make_copy_pair(tmp_path, size=DEFAULT_SIZE, seed=3)
    assert base.name == "报告.pdf"
    assert dup.name == "报告-副本.pdf"
    assert base.stat().st_size == dup.stat().st_size == DEFAULT_SIZE
    assert base.read_bytes() == dup.read_bytes()


def test_different_content_pair_has_same_size_but_distinct_bytes(tmp_path):
    first, second = make_same_size_different_content_pair(tmp_path, size=DEFAULT_SIZE)
    assert first.stat().st_size == second.stat().st_size == DEFAULT_SIZE
    assert first.read_bytes() != second.read_bytes()


def test_zero_byte_pair_is_empty(tmp_path):
    first, second = make_zero_byte_pair(tmp_path)
    assert first.stat().st_size == 0
    assert second.stat().st_size == 0


def test_img_pair_is_same_size_and_same_content(tmp_path):
    first, second = make_img_pair(tmp_path, size=DEFAULT_SIZE, seed=7)
    assert first.name == "img1.jpg"
    assert second.name == "img12.jpg"
    assert first.read_bytes() == second.read_bytes()


def test_excluded_tree_layout(tmp_path):
    pairs = populate_excluded_tree(tmp_path, size=DEFAULT_SIZE)
    assert set(pairs) == {"keep", ".git", "node_modules"}
    assert (tmp_path / "keep" / "报告单.pdf").is_file()
    assert (tmp_path / ".git" / "配置档.txt").is_file()
    assert (tmp_path / "node_modules" / "bundle.js").is_file()
    for base, dup in pairs.values():
        assert base.read_bytes() == dup.read_bytes()


def test_content_bytes_is_deterministic_and_seed_sensitive():
    assert content_bytes(16, 1) == content_bytes(16, 1)
    assert content_bytes(16, 1) != content_bytes(16, 2)
    assert len(content_bytes(DEFAULT_SIZE, 0)) == DEFAULT_SIZE


def test_snapshot_detects_modified_and_removed_files(tmp_path):
    target = write_file(tmp_path, "a.txt", size=64, seed=0)
    before = snapshot(tmp_path)
    target.write_bytes(b"changed")
    changed = snapshot(tmp_path)
    assert before != changed

    target.unlink()
    after_delete = snapshot(tmp_path)
    assert after_delete == {}


@pytest.mark.skipif(sys.platform != "win32", reason="硬链接夹具仅在 Windows 上验证")
def test_hardlink_pair_shares_file_identity(tmp_path):
    try:
        source, link = make_hardlink_pair(tmp_path, size=64, seed=1)
    except HardlinkNotSupported as exc:
        pytest.skip(f"当前临时目录无法创建硬链接: {exc}")

    source_stat = os.stat(source)
    link_stat = os.stat(link)
    assert source_stat.st_dev == link_stat.st_dev
    assert source_stat.st_ino == link_stat.st_ino
    assert Path(link).is_file()
