"""规格第 10 节验收用例：apply 侧（9/10/11/12）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fixture_builder import DEFAULT_SIZE, content_bytes, make_copy_pair, snapshot, write_file
from harness import (
    COMMIT_FLAGS,
    apply_report,
    files_under,
    find_manifests,
    find_mirrored_file,
    load_report,
    manifest_covers,
    scan,
    win_norm,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = DEFAULT_SIZE
ISO_TIME = "2026-11-06T10:00:00+08:00"


def _prepare_confirmed_report(cli, tmp_path: Path) -> tuple[Path, Path, Path, Path, dict]:
    """扫描一个 confirmed 副本对，返回 (数据根, 保留候选, 待移动文件, 报告路径, 报告)。"""
    root = tmp_path / "data"
    root.mkdir()
    keep, dup = make_copy_pair(root, stem="report", ext=".pdf", size=SIZE, seed=21)
    report_path = tmp_path / "report.json"

    result = scan(cli, root, report_path)
    assert result.returncode == 0, result.debug()

    return root, keep, dup, report_path, load_report(report_path)


def test_case_09_apply_defaults_to_dry_run(cli, tmp_path):
    """用例 9：apply 默认 dry-run，不移动任何文件、不写 manifest。"""
    root, keep, dup, report_path, _ = _prepare_confirmed_report(cli, tmp_path)
    trash = tmp_path / "trash"
    before = snapshot(root)

    result = apply_report(
        cli, report_path, "--trash", trash, "--keep", "shortest", "--yes"
    )
    assert result.returncode == 0, result.debug()

    assert keep.exists() and dup.exists(), "默认 dry-run 不应移动文件"
    assert snapshot(root) == before, "默认 dry-run 不应修改文件"
    assert files_under(trash) == [], "默认 dry-run 不应向 trash 写入文件"
    assert find_manifests(trash) == [], "默认 dry-run 不应生成 manifest"


def test_case_10_apply_trash_mirrors_relative_path_and_writes_manifest(cli, tmp_path):
    """用例 10：apply 实际执行后按相对路径落到 trash，且 manifest 可查。"""
    root, keep, dup, report_path, _ = _prepare_confirmed_report(cli, tmp_path)
    trash = tmp_path / "trash"
    expected_bytes = dup.read_bytes()

    result = apply_report(
        cli,
        report_path,
        "--trash",
        trash,
        "--keep",
        "shortest",
        "--yes",
        *COMMIT_FLAGS,
    )
    assert result.returncode == 0, (
        result.debug()
        + "\n提示：apply 缺少关闭 dry-run 的开关。默认约定为 --no-dry-run，"
        "可用环境变量 DUPFIND_COMMIT_FLAGS 覆盖。"
    )

    assert keep.exists(), "按 --keep shortest 应保留较短名字的文件"
    assert not dup.exists(), "副本文件应已移入 trash"

    mirror = find_mirrored_file(trash, dup)
    assert mirror is not None, (
        f"trash 中未找到 {dup} 的镜像路径，实际内容: "
        f"{[str(p.relative_to(trash)) for p in files_under(trash)]}"
    )
    assert mirror.read_bytes() == expected_bytes

    manifests = find_manifests(trash)
    assert manifests, "trash 根目录未找到 manifest-<timestamp>.json"
    assert manifest_covers(manifests, dup, mirror), "manifest 未记录源路径与目标路径"


def test_case_11_partial_report_is_rejected(cli, tmp_path):
    """用例 11：partial=true 的报告必须被 apply 拒绝，且不移动任何文件。"""
    root, keep, dup, report_path, report = _prepare_confirmed_report(cli, tmp_path)

    partial_report = json.loads(report_path.read_text(encoding="utf-8"))
    partial_report["scan"]["partial"] = True
    partial_path = tmp_path / "partial-report.json"
    partial_path.write_text(
        json.dumps(partial_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    trash = tmp_path / "trash"
    before = snapshot(root)
    result = apply_report(
        cli,
        partial_path,
        "--trash",
        trash,
        "--keep",
        "shortest",
        "--yes",
        *COMMIT_FLAGS,
    )
    assert result.returncode in (2, 3), (
        result.debug() + "\npartial 报告必须被拒绝（退出码 2 或 3）"
    )
    assert snapshot(root) == before, "拒绝 partial 报告时不得移动文件"
    assert files_under(trash) == []
    assert find_manifests(trash) == []


def test_case_12_apply_rejects_escape_paths(cli, tmp_path):
    """用例 12（apply 侧）：分组路径含 .. 时必须拒绝，不得越界到 trash 之外。"""
    sandbox = tmp_path / "sandbox"
    root = sandbox / "root"
    root.mkdir(parents=True)

    inside = write_file(root, "secret.txt", size=SIZE, seed=31)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = write_file(outside_dir, "secret.txt", size=SIZE, seed=31)
    outside_bytes = outside.read_bytes()

    # root/../../outside/secret.txt 归一化后落到 outside_dir，是典型逃逸路径
    escaped = str(root / ".." / ".." / "outside" / "secret.txt")

    report = {
        "version": 1,
        "scan": {
            "root": str(root),
            "startedAt": ISO_TIME,
            "durationMs": 0,
            "filesScanned": 2,
            "excludedDirs": 0,
            "excludedFiles": 0,
            "accessDenied": 0,
            "errors": 0,
            "partial": False,
        },
        "groups": [
            {
                "id": 1,
                "status": "confirmed",
                "size": SIZE,
                "savingsBytes": SIZE,
                "files": [
                    {
                        "path": str(inside),
                        "normalizedName": "secret",
                        "mtime": ISO_TIME,
                        "shared": False,
                        "unstable": False,
                    },
                    {
                        "path": escaped,
                        "normalizedName": "secret",
                        "mtime": ISO_TIME,
                        "shared": False,
                        "unstable": False,
                    },
                ],
            }
        ],
    }
    report_path = tmp_path / "escape-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    trash = tmp_path / "trash"
    result = apply_report(
        cli, report_path, "--trash", trash, "--keep", "shortest", "--yes", *COMMIT_FLAGS
    )
    assert result.returncode in (2, 3), (
        result.debug() + "\n包含 .. 的报告路径必须被拒绝（退出码 2 或 3）"
    )
    assert outside.exists() and outside.read_bytes() == outside_bytes, "越界文件被修改"
    assert inside.exists(), "报告被拒绝时不应移动任何文件"
    assert files_under(trash) == []
