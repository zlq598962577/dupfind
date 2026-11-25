"""规格第 10 节验收用例：scan 侧（1/2/3/4/5/7/8，以及 12 的报告路径安全）。

CLI 未就绪时由 ``cli`` 夹具自动 skip。ACL 拒绝访问用例在管理员会话或
icacls 未生效时 skip，并在 skip 原因中给出手工核对步骤。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fixture_builder import (
    DEFAULT_SIZE,
    make_copy_pair,
    make_img_pair,
    make_readonly,
    make_same_size_different_content_pair,
    make_writable,
    make_zero_byte_pair,
    populate_excluded_tree,
    populate_permission_tree,
)
from harness import (
    assert_no_path_escape,
    assert_sorted_by_savings,
    find_groups_with_paths,
    load_report,
    scan,
    win_norm,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = DEFAULT_SIZE


def _paths(group: dict) -> set[str]:
    return {win_norm(item["path"]) for item in group.get("files", [])}


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 非 Windows 或 ctypes 不可用时按非管理员处理
        return False


def _deny_dir_read(directory: Path) -> str:
    """用 icacls 拒绝当前用户读取目录，返回用户名（供恢复）。"""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    assert user, "无法获取当前用户名，无法构造权限拒绝场景"
    subprocess.run(
        ["icacls", str(directory), "/deny", f"{user}:(OI)(CI)(RX)"],
        check=True,
        capture_output=True,
        text=True,
    )
    return user


def _restore_dir_acl(directory: Path, user: str) -> None:
    """尽量恢复目录权限，避免影响 pytest 临时目录清理。"""
    subprocess.run(
        ["icacls", str(directory), "/reset", "/T", "/C", "/Q"],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["icacls", str(directory), "/grant", f"{user}:(OI)(CI)F", "/T", "/C", "/Q"],
        capture_output=True,
        text=True,
    )


def _probe_denied(directory: Path) -> bool:
    try:
        with os.scandir(directory) as iterator:
            next(iter(iterator), None)
        return False
    except PermissionError:
        return True


def test_case_01_same_size_same_content_confirmed_with_savings(cli, data_root, tmp_path):
    """用例 1：同大小同内容、名字为副本关系 → confirmed，savings 正确。"""
    base, dup = make_copy_pair(data_root, stem="报告单", ext=".pdf", size=SIZE, seed=3)
    assert base.read_bytes() == dup.read_bytes()

    out = tmp_path / "case01.json"
    result = scan(cli, data_root, out)
    assert result.returncode == 0, result.debug()

    report = load_report(out)
    groups = find_groups_with_paths(report, base, dup)
    assert len(groups) == 1, f"应恰好有一个分组，实际 {report['groups']}"
    group = groups[0]
    assert group["status"] == "confirmed"
    assert _paths(group) == {win_norm(base), win_norm(dup)}
    assert int(group["size"]) == SIZE
    assert int(group["savingsBytes"]) == SIZE
    assert_sorted_by_savings(report)


def test_case_02_same_size_name_related_but_content_differs(cli, data_root, tmp_path):
    """用例 2：同大小、名字包含、内容不同 → 不得进入 confirmed。"""
    first, second = make_same_size_different_content_pair(
        data_root, stem="会议纪要", ext=".docx", size=SIZE, seed_a=0, seed_b=1
    )
    assert first.stat().st_size == second.stat().st_size == SIZE
    assert first.read_bytes() != second.read_bytes()

    out = tmp_path / "case02.json"
    result = scan(cli, data_root, out)
    assert result.returncode in (0, 1), result.debug()

    report = load_report(out)
    groups = find_groups_with_paths(report, first, second)
    assert [g for g in groups if g["status"] == "confirmed"] == [], "内容不同却被判为 confirmed"


def test_case_03_zero_byte_files_never_grouped(cli, data_root, tmp_path):
    """用例 3：0 字节文件的副本对不参与分组（即使 --min-size 0）。"""
    empty_a, empty_b = make_zero_byte_pair(data_root, stem="empty", ext=".txt")
    assert empty_a.stat().st_size == empty_b.stat().st_size == 0

    out = tmp_path / "case03.json"
    result = scan(cli, data_root, out, "--min-size", "0")
    assert result.returncode == 1, result.debug()  # 未发现重复

    report = load_report(out)
    assert report["groups"] == []
    assert find_groups_with_paths(report, empty_a, empty_b) == []


def test_case_04_img1_img12_needs_content_only(cli, data_root, tmp_path):
    """用例 4：img1/img12 默认不作候选；--content-only 下内容相同才确认。"""
    img1, img12 = make_img_pair(data_root, size=SIZE, seed=7)
    assert img1.read_bytes() == img12.read_bytes()

    default_out = tmp_path / "case04-default.json"
    default_result = scan(cli, data_root, default_out)
    assert default_result.returncode == 1, default_result.debug()
    default_report = load_report(default_out)
    assert find_groups_with_paths(default_report, img1, img12) == []

    content_out = tmp_path / "case04-content-only.json"
    content_result = scan(cli, data_root, content_out, "--content-only")
    assert content_result.returncode == 0, content_result.debug()
    content_report = load_report(content_out)
    groups = find_groups_with_paths(content_report, img1, img12)
    assert len(groups) == 1, content_report["groups"]
    assert groups[0]["status"] == "confirmed"
    assert int(groups[0]["savingsBytes"]) == SIZE


def test_case_05_excluded_directories_are_skipped_and_counted(cli, data_root, tmp_path):
    """用例 5：黑名单目录内的文件不被扫描，且计入 excludedDirs。"""
    pairs = populate_excluded_tree(data_root, size=SIZE, seed=0)

    out = tmp_path / "case05.json"
    result = scan(cli, data_root, out, "--show-excluded")
    assert result.returncode == 0, result.debug()

    report = load_report(out)
    assert int(report["scan"]["excludedDirs"]) >= 2, report["scan"]
    all_paths = [win_norm(item["path"]) for group in report["groups"] for item in group["files"]]
    assert not [p for p in all_paths if "\\.git\\" in p or "\\node_modules\\" in p]
    keep_group = find_groups_with_paths(report, *pairs["keep"])
    assert len(keep_group) == 1 and keep_group[0]["status"] == "confirmed"
    assert ".git" in result.output.lower(), "--show-excluded 未打印被排除的路径"

    # 对照组：--no-exclude 后黑名单目录应重新被扫描
    no_exclude_out = tmp_path / "case05-no-exclude.json"
    no_exclude_result = scan(cli, data_root, no_exclude_out, "--no-exclude")
    assert no_exclude_result.returncode == 0, no_exclude_result.debug()
    no_exclude_report = load_report(no_exclude_out)
    assert int(no_exclude_report["scan"]["excludedDirs"]) == 0
    git_groups = find_groups_with_paths(no_exclude_report, *pairs[".git"])
    assert len(git_groups) == 1 and git_groups[0]["status"] == "confirmed"


def test_case_07_readonly_file_still_scanned(cli, data_root, tmp_path):
    """用例 7（只读部分）：只读文件不影响读取，副本对仍能 confirmed。"""
    base, dup = make_copy_pair(data_root, stem="档案资料", ext=".txt", size=SIZE, seed=9)
    make_readonly(dup)
    try:
        out = tmp_path / "case07-readonly.json"
        result = scan(cli, data_root, out)
        assert result.returncode == 0, result.debug()

        report = load_report(out)
        groups = find_groups_with_paths(report, base, dup)
        assert len(groups) == 1 and groups[0]["status"] == "confirmed"
        assert int(report["scan"]["accessDenied"]) == 0
    finally:
        make_writable(dup)


@pytest.mark.skipif(
    _is_elevated(),
    reason="管理员会话下 ACL 拒绝可能不生效；请用普通权限会话手工核对（步骤见用例说明）",
)
def test_case_07_access_denied_directory_counted(cli, tmp_path):
    """用例 7（权限拒绝）：扫描不中断，accessDenied 计数正确。

    手工核对步骤（用例 skip 时）：
    1) 以普通用户打开 PowerShell；
    2) icacls <目录> /deny "%USERNAME%":(OI)(CI)(RX) 制造拒绝访问；
    3) dupfind scan <根目录> --out r.json；
    4) 确认命令正常结束、r.json 的 scan.accessDenied >= 1，且其它目录继续扫描；
    5) icacls <目录> /reset /T 恢复权限。
    """
    root = tmp_path / "data"
    root.mkdir()
    layout = populate_permission_tree(root, size=SIZE)
    locked_dir: Path = layout["locked_dir"]

    user = _deny_dir_read(locked_dir)
    try:
        if not _probe_denied(locked_dir):
            pytest.skip(
                "icacls /deny 未生效（可能管理员/继承权限覆盖）。"
                "人工核对：普通会话执行 icacls <dir> /deny %USERNAME%:(OI)(CI)(RX)，"
                "再运行 dupfind scan，确认 scan.accessDenied>=1 且扫描继续。"
            )

        out = tmp_path / "case07-denied.json"
        result = scan(cli, root, out)
        assert result.returncode in (0, 1), result.debug()

        report = load_report(out)
        assert int(report["scan"]["accessDenied"]) >= 1, report["scan"]
        visible_groups = find_groups_with_paths(report, *layout["visible"])
        assert len(visible_groups) == 1 and visible_groups[0]["status"] == "confirmed"
        assert find_groups_with_paths(report, *layout["locked"]) == []
    finally:
        _restore_dir_acl(locked_dir, user)


def test_case_08_no_hash_reports_all_suspect(cli, data_root, tmp_path):
    """用例 8：--no-hash 时全部结论为 suspect，不得出现 confirmed。"""
    same_content = make_copy_pair(data_root, directory="same", stem="重复文件", ext=".bin", size=SIZE, seed=5)
    different_content = make_same_size_different_content_pair(
        data_root, directory="diff", stem="文档资料", ext=".txt", size=SIZE, seed_a=10, seed_b=11
    )

    out = tmp_path / "case08.json"
    result = scan(cli, data_root, out, "--no-hash")
    assert result.returncode == 0, result.debug()

    report = load_report(out)
    assert report["groups"], "同大小候选存在时 --no-hash 不应输出空分组"
    assert {group["status"] for group in report["groups"]} == {"suspect"}
    group = find_groups_with_paths(report, *same_content)
    assert len(group) == 1 and group[0]["status"] == "suspect"
    assert find_groups_with_paths(report, *different_content) != []


def test_case_12_scan_report_paths_have_no_escape(cli, data_root, tmp_path):
    """用例 12（scan 侧）：报告中的路径均为绝对路径且不含 .. 片段。"""
    make_copy_pair(data_root, stem="路径样本", ext=".txt", size=SIZE, seed=12)

    out = tmp_path / "case12-scan.json"
    result = scan(cli, data_root, out)
    assert result.returncode == 0, result.debug()

    report = load_report(out)
    assert_no_path_escape(report)
    assert win_norm(report["scan"]["root"]) == win_norm(data_root)
