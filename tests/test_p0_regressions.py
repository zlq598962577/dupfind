"""P0 三处修复的回归测试（不依赖 CLI 子进程，直接调用模块）。

1. manifest 追加式 journal：N 个动作只产生 O(N) 字节写入，且崩溃后可还原；
2. 保留判定按物理簇：混合硬链接的组能正确算出可节省空间并可处置；
3. 安全护栏：系统保护路径与内容复核拦截。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_builder import DEFAULT_SIZE, content_bytes, write_file  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = DEFAULT_SIZE


def _entry(path: Path, *, allocated: int | None = None, file_id=None, shared=False):
    from dupfind.models import FileEntry

    stat = path.stat()
    entry = FileEntry(
        path=str(path),
        name=path.name,
        size=stat.st_size,
        mtime=stat.st_mtime,
        attributes=0,
    )
    entry.allocated = allocated if allocated is not None else stat.st_size
    entry.file_id = file_id
    entry.shared = shared
    return entry


# ---------------------------------------------------------------------------
# 1. manifest 写入复杂度
# ---------------------------------------------------------------------------


def test_manifest_journal_is_linear_not_quadratic(tmp_path):
    """N 个动作的总写入量应与 N 成正比，而不是 O(N²)。"""
    from dupfind.trash import Manifest

    trash = tmp_path / "trash"
    trash.mkdir()
    manifest = Manifest(str(trash), "report.json")

    n = 500
    for i in range(n):
        action = manifest.add(group_id=i, source=f"D:\\data\\f{i}.bin", target=f"{trash}\\f{i}.bin", size=SIZE)
        manifest.mark(action, status="completed")
    manifest.close()

    # 快照 + journal 合计应远小于「每动作全量重写」的量级
    total = sum(p.stat().st_size for p in trash.rglob("*") if p.is_file())
    # 朴素实现约为 n * (n/2) * 每条约 180 字节 ≈ 22 MB
    assert total < n * 400, f"manifest 写入量异常放大：{total} 字节（{n} 个动作）"

    snapshot = json.loads(Path(manifest.path).read_text(encoding="utf-8"))
    assert len(snapshot["actions"]) == n
    assert all(action["status"] == "completed" for action in snapshot["actions"])


def test_manifest_journal_survives_crash(tmp_path):
    """进程在快照写出前中断时，journal 仍保留每个动作，可据此还原。"""
    from dupfind.trash import Manifest

    trash = tmp_path / "trash"
    trash.mkdir()
    manifest = Manifest(str(trash), "report.json")
    for i in range(3):
        manifest.add(group_id=i, source=f"D:\\data\\f{i}.bin", target=f"{trash}\\f{i}.bin", size=SIZE)
    # 故意不调用 save()/close()，模拟崩溃

    journal = Path(manifest.journal_path)
    assert journal.is_file(), "移动前未落 journal，崩溃后无法还原"
    lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    assert {line["src"] for line in lines} == {"D:\\data\\f0.bin", "D:\\data\\f1.bin", "D:\\data\\f2.bin"}


# ---------------------------------------------------------------------------
# 2. 物理簇：混合硬链接组
# ---------------------------------------------------------------------------


def test_savings_counts_mixed_hardlink_cluster(tmp_path):
    """1 个独立副本 + 2 个互为硬链接 → 可节省 = 该独立副本的占用，而不是 0。"""
    from dupfind.verify import compute_savings

    body = content_bytes(SIZE, seed=3)
    standalone = write_file(tmp_path, "a.bin", data=body)
    linked_a = write_file(tmp_path, "b.bin", data=body)
    linked_b = tmp_path / "c.bin"
    try:
        os.link(linked_a, linked_b)
    except OSError as exc:
        pytest.skip(f"当前临时目录无法创建硬链接：{exc}")

    entries = [
        _entry(standalone, file_id=(1, 100)),
        _entry(linked_a, file_id=(1, 200), shared=True),
        _entry(linked_b, file_id=(1, 200), shared=True),
    ]
    assert compute_savings(entries) == SIZE, "混合硬链接组的可节省空间被误算为 0"


def test_savings_zero_when_single_physical_file(tmp_path):
    """整组都是同一物理文件的硬链接 → 可节省必须为 0。"""
    from dupfind.verify import compute_savings

    body = content_bytes(SIZE, seed=4)
    a = write_file(tmp_path, "a.bin", data=body)
    b = tmp_path / "b.bin"
    try:
        os.link(a, b)
    except OSError as exc:
        pytest.skip(f"当前临时目录无法创建硬链接：{exc}")

    entries = [_entry(a, file_id=(1, 100), shared=True), _entry(b, file_id=(1, 100), shared=True)]
    assert compute_savings(entries) == 0


def test_plan_group_can_dispose_mixed_hardlink_group(tmp_path):
    """混合硬链接组必须可处置：保留一个簇，另一个簇整簇移走。

    这里 ``shortest`` 保留独立副本 ``a.bin``，把互为硬链接的 ``b``/``c`` 整簇
    移走 —— 与真实场景一致（原文档留在原位，缓存目录里的硬链接副本被清理）。
    """
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    body = content_bytes(SIZE, seed=5)
    standalone = write_file(tmp_path, "a.bin", data=body)
    linked_a = write_file(tmp_path, "b.bin", data=body)
    linked_b = tmp_path / "c.bin"
    try:
        os.link(linked_a, linked_b)
    except OSError as exc:
        pytest.skip(f"当前临时目录无法创建硬链接：{exc}")

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile(str(standalone), SIZE, "a", None, "0x1", False, False),
            ReportFile(str(linked_a), SIZE, "b", None, "0x2", True, False),
            ReportFile(str(linked_b), SIZE, "c", None, "0x2", True, False),
        ],
    )
    keep, removals, problem = plan_group(group)
    assert problem is None, f"混合硬链接组被错误拒绝：{problem}"
    # 保留簇与待删簇各占一份物理数据，合计覆盖组内全部路径
    assert len(keep) + sum(len(cluster) for cluster in removals) == 3
    assert len(removals) == 1, "应只移走一个物理簇"
    # 互为硬链接的 b/c 必须同进同出，绝不能只移走其中一个
    removed_paths = {item.path for cluster in removals for item in cluster}
    assert removed_paths == {str(linked_a), str(linked_b)} or removed_paths == {str(standalone)}
    assert (removed_paths == {str(linked_a), str(linked_b)}) == (len(keep) == 1 and keep[0].path == str(standalone))


# ---------------------------------------------------------------------------
# 3. 安全护栏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "C:\\Program Files\\app\\lib.dll",
        "C:\\ProgramData\\data.bin",
        "C:\\pagefile.sys",
    ],
)
def test_protected_reason_blocks_system_paths(path):
    from dupfind.safety import protected_reason

    assert protected_reason(path) is not None, f"系统路径未被拦截：{path}"


@pytest.mark.parametrize(
    "path",
    [
        "D:\\Projects\\a\\b.bin",
        "C:\\Users\\someone\\Documents\\报告.pdf",
    ],
)
def test_protected_reason_allows_user_paths(path):
    from dupfind.safety import protected_reason

    assert protected_reason(path) is None, f"用户路径被误拦截：{path}"


def test_protected_group_is_skipped_not_moved(tmp_path):
    """组内待删文件位于系统保护目录时，该组必须被跳过，不得移动任何文件。"""
    from dupfind.apply import ApplyOptions, run_apply
    from dupfind.report import ReportDocument, ReportFile, ReportGroup

    trash = tmp_path / "trash"
    document = ReportDocument(
        version=1,
        path=tmp_path / "r.json",
        root="C:\\",
        partial=False,
        groups=[
            ReportGroup(
                id=1,
                status="confirmed",
                size=SIZE,
                savings_bytes=SIZE,
                files=[
                    ReportFile("C:\\Windows\\a.dll", SIZE, "a", None, "0x1", False, False),
                    ReportFile("C:\\Windows\\b.dll", SIZE, "b", None, "0x2", False, False),
                ],
            )
        ],
        raw_scan={"root": "C:\\"},
    )
    code = run_apply(document, ApplyOptions(trash_dir=str(trash), assume_yes=True, dry_run=False))
    assert code == 0
    assert not trash.exists() or not [p for p in trash.rglob("*") if p.is_file()], "受保护路径的文件被移动了"


def test_protected_removal_skipped_but_user_keep_allowed(tmp_path):
    """保留项位于系统目录、待删项在用户目录时：正常处置（保护只针对待删文件）。"""
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile("C:\\Program Files\\app\\lib.dll", SIZE, "lib", None, "0x1", False, False),
            ReportFile(str(tmp_path / "lib-copy.dll"), SIZE, "lib", None, "0x2", False, False),
        ],
    )
    keep, removals, problem = plan_group(group)
    assert problem is None
    assert any(item.path.startswith("C:\\Program Files") for item in keep)
    assert all(not item.path.startswith("C:\\Program Files") for cluster in removals for item in cluster)


def test_protected_cluster_overrides_keep_mode(tmp_path):
    """受保护路径优先于 --keep 策略：即便 newest 想保留用户副本，也必须保住系统目录那份。"""
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            # 系统目录里的那份 mtime 更早
            ReportFile("C:\\Windows\\System32\\x.dll", SIZE, "x", 100.0, "0x1", False, False),
            # 用户目录里的副本 mtime 更新 —— newest 策略本会保留它
            ReportFile(str(tmp_path / "x-copy.dll"), SIZE, "x", 999.0, "0x2", False, False),
        ],
    )
    keep, removals, problem = plan_group(group, mode="newest")
    assert problem is None, problem
    assert keep[0].path.startswith("C:\\Windows"), "受保护簇未被强制保留"
    assert removals[0][0].path == str(tmp_path / "x-copy.dll")


def test_two_protected_clusters_refused(tmp_path):
    """两簇都位于受保护目录时，无法安全处置，必须整组放弃。"""
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile("C:\\Windows\\a.dll", SIZE, "a", None, "0x1", False, False),
            ReportFile("C:\\Program Files\\b.dll", SIZE, "b", None, "0x2", False, False),
        ],
    )
    keep, removals, problem = plan_group(group)
    assert problem is not None and "受保护" in problem


def test_interactive_choice_cannot_remove_protected(tmp_path):
    """交互模式下用户选错保留项，也必须被改回受保护簇。"""
    from dupfind.apply import ApplyOptions, _Executor, ApplyStats, _choose_group_files
    from dupfind.report import ReportDocument, ReportFile, ReportGroup

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile("C:\\Windows\\a.dll", SIZE, "a", None, "0x1", False, False),
            ReportFile(str(tmp_path / "a-copy.dll"), SIZE, "a", None, "0x2", False, False),
        ],
    )
    document = ReportDocument(1, tmp_path / "r.json", "C:\\", False, [group], {"root": "C:\\"})
    options = ApplyOptions(trash_dir=str(tmp_path / "trash"), dry_run=True)
    executor = _Executor(document, options, ApplyStats())

    # 用户选择第 2 项（用户目录副本）作为保留项
    result = _choose_group_files(group, options, executor, input_func=lambda _: "2")
    assert result == "apply"
    # dry-run 下不会真移动，但受保护文件被选作待删时会被 _move_one 拦下
    assert executor._stats.skipped_files >= 1, "受保护路径未被拦截"


def test_content_mismatch_blocks_move(tmp_path):
    """待删文件内容与保留文件不一致时，内容复核必须拦住移动。"""
    from dupfind.safety import content_matches

    keep = write_file(tmp_path, "keep.bin", data=content_bytes(SIZE, seed=1))
    changed = write_file(tmp_path, "changed.bin", data=content_bytes(SIZE, seed=2))

    matches, reason = content_matches(str(changed), str(keep), SIZE)
    assert not matches and reason is not None

    same = write_file(tmp_path, "same.bin", data=content_bytes(SIZE, seed=1))
    matches, reason = content_matches(str(same), str(keep), SIZE)
    assert matches and reason is None
