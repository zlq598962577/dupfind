"""文件类型分类：默认视图只展示用户文件，程序/代码/配置可切换查看。"""

from __future__ import annotations

from dupfind.filetypes import (
    CATEGORIES,
    CATEGORY_LABELS,
    USER_CATEGORIES,
    category_for_name,
    category_for_path,
    group_category,
    is_user_category,
    top_dir,
)


def test_every_category_has_a_label():
    assert set(CATEGORY_LABELS) == set(CATEGORIES)


def test_user_categories_are_known_categories():
    assert USER_CATEGORIES <= set(CATEGORIES)


def test_program_code_and_config_are_hidden_by_default():
    for category in ("program", "code", "config"):
        assert category not in USER_CATEGORIES
        assert not is_user_category(category)


def test_extension_classification_is_case_insensitive():
    assert category_for_name("照片.JPG") == "image"
    assert category_for_name("movie.mkv") == "video"
    assert category_for_name("song.flac") == "audio"
    assert category_for_name("报告.pdf") == "document"
    assert category_for_name("备份.ZIP") == "archive"
    assert category_for_name("setup.MSI") == "installer"
    assert category_for_name("font.ttf") == "font"
    assert category_for_name("app.exe") == "program"
    assert category_for_name("main.py") == "code"
    assert category_for_name("dupfind.toml") == "config"


def test_unknown_extensions_and_names_are_other():
    assert category_for_name("data.xyz") == "other"
    assert category_for_name("no_extension") == "other"
    assert category_for_name("") == "other"


def test_well_known_names_without_extension():
    assert category_for_name("Dockerfile") == "code"
    assert category_for_name("Makefile") == "code"
    assert category_for_name(".gitignore") == "config"
    assert category_for_name("README") == "document"


def test_category_for_path_uses_basename():
    assert category_for_path(r"D:\downloads\photo.png") == "image"
    assert category_for_path(r"D:\src\module.ts") == "code"


def test_group_category_prefers_user_files():
    """组内只要有一份像用户文件，就按用户文件归类，不让用户「看不见」。"""
    assert group_category([r"D:\x\a.py", r"D:\x\a.jpg"]) == "image"
    assert group_category([r"D:\x\a.py", r"D:\x\a.json"]) == "code"
    assert group_category([r"D:\x\a.pyc", r"D:\x\a.dll"]) == "program"
    # 都是非用户文件时按优先级取第一个命中的类别，不改变「默认隐藏」的结论
    assert not is_user_category(group_category([r"D:\x\a.dll", r"D:\x\a.json"]))


def test_top_dir_is_first_level_under_root():
    assert top_dir(r"D:\data\Downloads\a.jpg", r"D:\data") == "Downloads"
    assert top_dir(r"D:\data\a.jpg", r"D:\data") == "(根目录)"
    assert top_dir(r"D:\data", r"D:\data") == "(根目录)"
    # 不在根下的路径不能被算进某个「顶层目录」里
    assert top_dir(r"E:\other\a.jpg", r"D:\data") == "(根目录)"


def test_top_dir_keeps_names_starting_with_dots():
    """Windows 允许名为 "..cache" 的目录；它不能被并进扫描根，
    否则目录面板上的「忽略此目录」会把整个扫描根忽略掉。"""
    assert top_dir(r"D:\data\..cache\a.jpg", r"D:\data") == "..cache"
    assert top_dir(r"D:\data\...tmp\a.jpg", r"D:\data") == "...tmp"
    assert top_dir(r"D:\data\..\other\a.jpg", r"D:\data") == "(根目录)"
