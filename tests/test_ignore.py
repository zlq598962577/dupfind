"""目录忽略清单：子树匹配、校验、持久化与读-改-写。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dupfind.ignore import (
    IgnoreError,
    IgnoreList,
    normalize_directory,
    update_ignore,
)


def test_normalize_rejects_relative_paths():
    with pytest.raises(IgnoreError, match="绝对路径"):
        normalize_directory(r"Games\Cache")


def test_normalize_rejects_drive_root():
    """忽略整个 E:\\ 等于让扫描什么也不做，必须报错而不是静默接受。"""
    for bad in (r"E:\\", "E:/", "E:"):
        with pytest.raises(IgnoreError):
            normalize_directory(bad)


def test_normalize_rejects_dotdot_escape():
    with pytest.raises(IgnoreError, match="\\.\\."):
        normalize_directory(r"D:\Games\..\Windows")


def test_normalize_rejects_extended_prefix():
    with pytest.raises(IgnoreError, match="扩展前缀"):
        normalize_directory(r"\\?\D:\Games")


def test_normalize_rejects_extended_prefix_with_forward_slashes():
    """先统一分隔符再查前缀：//?/D:/Games 规范化后就是 \\\\?\\D:\\Games，
    写进清单会让整份文件在下次加载时不可读。"""
    with pytest.raises(IgnoreError, match="扩展前缀"):
        normalize_directory("//?/D:/Games")


def test_load_skips_entries_that_no_longer_normalize(tmp_path):
    """旧版本/手工编辑留下的坏条目不能让整份清单锁死。"""
    target = tmp_path / "ignore.json"
    target.write_text(
        json.dumps({"version": 1, "directories": ["\\\\?\\D:\\Games", "D:\\OK"]}),
        encoding="utf-8",
    )
    ignore = IgnoreList.load(target)
    assert ignore.directories == [r"D:\OK"]
    assert ignore.contains(r"D:\OK\a.tmp") is True


def test_contains_matches_subtree_case_insensitively(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    assert ignore.contains(r"D:\Games\Cache\a\b.tmp")
    assert ignore.contains(r"d:\games\cache\A.TMP")
    assert ignore.contains(r"D:\Games\Cache")
    assert not ignore.contains(r"D:\Games\Cache2\a.tmp")
    assert not ignore.contains(r"D:\Games\a.tmp")


def test_match_returns_original_entry(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache"])
    assert ignore.match(r"D:\Games\Cache\a.tmp") == r"D:\Games\Cache"
    assert ignore.match(r"D:\Games\a.tmp") is None
    assert ignore.matches_directory(r"d:\games\cache") == r"D:\Games\Cache"


def test_save_and_load_roundtrip(tmp_path):
    target = tmp_path / "ignore.json"
    ignore = IgnoreList(target)
    assert ignore.add(r"D:\Games\Cache") is True
    assert ignore.add(r"D:\Games\Cache") is False, "重复添加应无变化"
    ignore.add(r"D:\WPS Office")
    ignore.save()

    loaded = IgnoreList.load(target)
    assert loaded.directories == [r"D:\Games\Cache", r"D:\WPS Office"]
    assert loaded.contains(r"D:\WPS Office\addons\x.dll")


def test_remove_drops_the_entry(tmp_path):
    ignore = IgnoreList(tmp_path / "ignore.json", [r"D:\Games\Cache", r"D:\WPS Office"])
    assert ignore.remove(r"d:\games\cache") is True
    assert ignore.directories == [r"D:\WPS Office"]
    assert ignore.contains(r"D:\Games\Cache\a.tmp") is False
    assert ignore.remove(r"D:\Games\Cache") is False


def test_update_ignore_reads_modifies_and_writes(tmp_path):
    target = tmp_path / "ignore.json"
    update_ignore(target, add=r"D:\Games\Cache")
    update_ignore(target, add=r"D:\WPS Office")
    listed = update_ignore(target, remove=r"D:\Games\Cache")
    assert listed.directories == [r"D:\WPS Office"]
    assert IgnoreList.load(target).directories == [r"D:\WPS Office"]


def test_missing_file_is_empty_by_default(tmp_path):
    ignore = IgnoreList.load(tmp_path / "absent.json")
    assert ignore.directories == []
    assert ignore.contains(r"D:\anything") is False


def test_missing_file_can_be_required(tmp_path):
    with pytest.raises(IgnoreError, match="不存在"):
        IgnoreList.load(tmp_path / "absent.json", required=True)


def test_corrupt_file_raises(tmp_path):
    target = tmp_path / "ignore.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(IgnoreError, match="合法 JSON"):
        IgnoreList.load(target)


def test_version_mismatch_raises(tmp_path):
    target = tmp_path / "ignore.json"
    target.write_text('{"version": 99, "directories": []}', encoding="utf-8")
    with pytest.raises(IgnoreError, match="版本"):
        IgnoreList.load(target)


def test_bad_directories_payload_raises(tmp_path):
    target = tmp_path / "ignore.json"
    target.write_text('{"version": 1, "directories": "D:\\\\x"}', encoding="utf-8")
    with pytest.raises(IgnoreError, match="字符串数组"):
        IgnoreList.load(target)


def test_default_ignore_path_points_to_cwd():
    from dupfind.ignore import default_ignore_path

    assert Path(default_ignore_path()).name == "dupfind-ignore.json"


def test_explicit_ignore_file_must_exist(tmp_path):
    """--ignore-file 拼错时不能静默退回空清单，否则用户以为忽略规则生效。"""
    import typer

    from dupfind.cli import _load_ignore

    with pytest.raises(typer.Exit) as excinfo:
        _load_ignore(str(tmp_path / "missing.json"))
    assert excinfo.value.exit_code == 2
