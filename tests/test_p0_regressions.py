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


@pytest.mark.parametrize(
    "path",
    [
        "C:\\WINDOWS\\x.dll",
        "C:\\Windows \\x.dll",
        "C:\\Windows.\\x.dll",
        "\\\\?\\C:\\Windows\\x.dll",
        "C:\\PROGRA~1\\x.dll",
        "C:\\ProgramData\\x.bin",
        "C:\\pagefile.sys",
    ],
)
def test_protected_reason_cannot_be_bypassed(path):
    """Windows 路径有多种等价写法，保护判定必须对全部写法生效。

    尾随空格/点会被系统静默剥掉，8.3 短名指向同一目录，扩展长度前缀会改变
    字符串形态 —— 任何一条漏判都意味着可以删掉系统文件。
    """
    from dupfind.safety import protected_reason

    assert protected_reason(path) is not None, f"保护被绕过：{path}"


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


# ---------------------------------------------------------------------------
# 4. 配置解析错误不能崩栈
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[scan]\nminSize = -5\n",
        "[scan]\nsampleSize = \"abc\"\n",
        "[scan]\nthreads = \"many\"\n",
        "[apply]\npage = \"abc\"\n",
    ],
)
def test_bad_config_raises_config_error(tmp_path, text):
    """配置写错必须报 ConfigError（CLI 转成退出码 2），而不是抛 ValueError 崩栈。"""
    from dupfind.config import ConfigError, load_config

    config_path = tmp_path / "dupfind.toml"
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path)


# ---------------------------------------------------------------------------
# 5. 审查发现的正确性缺陷
# ---------------------------------------------------------------------------


def test_sample_cache_key_includes_sample_size(tmp_path):
    """换 --sample-size 后不能复用旧尺寸的采样摘要 —— 否则完全相同的文件被漏报。"""
    from dupfind.cache import HashCache
    from dupfind.models import FileEntry
    from dupfind.verify import Verifier
    from dupfind.models import ScanStats
    from datetime import datetime

    first = write_file(tmp_path, "a.bin", data=b"A" * 2048 + b"X" * 2048 + b"A" * 2048)
    second = write_file(tmp_path, "b.bin", data=first.read_bytes())

    cache_path = tmp_path / "cache.json"
    def run(sample_size: int):
        cache = HashCache(cache_path)
        cache.load()
        stats = ScanStats(root=str(tmp_path), started_at=datetime.now().astimezone())
        verifier = Verifier(sample_size=sample_size, threads=1, cache=cache, stats=stats, hashing=True)
        entries = [
            FileEntry(path=str(first), name=first.name, size=first.stat().st_size, mtime=first.stat().st_mtime, attributes=0),
            FileEntry(path=str(second), name=second.name, size=second.stat().st_size, mtime=second.stat().st_mtime, attributes=0),
        ]
        groups = verifier.build_groups([entries])
        cache.save()
        return [group.status for group in groups]

    assert run(4096) == ["confirmed"], "第一次扫描应确认重复"
    # 换了采样尺寸，缓存里的旧摘要必须失效，仍然要得出 confirmed
    assert run(1024) == ["confirmed"], "换 sample-size 后命中旧摘要，重复文件被漏报"


def test_suspect_group_counts_hardlinks(tmp_path):
    """--no-hash 的 suspect 组也必须识别硬链接，否则 savingsBytes 被算大。"""
    from datetime import datetime

    from dupfind.models import FileEntry, ScanStats
    from dupfind.verify import Verifier
    from dupfind.cache import HashCache

    body = content_bytes(SIZE, seed=21)
    a = write_file(tmp_path, "a.bin", data=body)
    b = tmp_path / "b.bin"
    try:
        os.link(a, b)
    except OSError as exc:
        pytest.skip(f"当前临时目录无法创建硬链接：{exc}")

    stats = ScanStats(root=str(tmp_path), started_at=datetime.now().astimezone())
    verifier = Verifier(sample_size=4096, threads=1, cache=HashCache(None), stats=stats, hashing=False)
    entries = [
        FileEntry(path=str(a), name="a.bin", size=SIZE, mtime=a.stat().st_mtime, attributes=0),
        FileEntry(path=str(b), name="b.bin", size=SIZE, mtime=b.stat().st_mtime, attributes=0),
    ]
    groups = verifier.build_groups([entries])
    assert len(groups) == 1 and groups[0].status == "suspect"
    assert groups[0].savings_bytes == 0, "硬链接被当成独立副本，可节省空间被算大"


def test_pipeline_suspect_group_counts_hardlinks(tmp_path):
    """扫描中断时的兜底分组同样要识别硬链接。"""
    from dupfind.pipeline import _suspect_group

    body = content_bytes(SIZE, seed=22)
    a = write_file(tmp_path, "a.bin", data=body)
    b = tmp_path / "b.bin"
    try:
        os.link(a, b)
    except OSError as exc:
        pytest.skip(f"当前临时目录无法创建硬链接：{exc}")

    entries = [
        _entry(a),
        _entry(b),
    ]
    for entry in entries:
        entry.file_id = None
        entry.allocated = None
    record = _suspect_group(entries)
    assert record.savings_bytes == 0, "兜底分组把硬链接算成了两份独立数据"


@pytest.mark.parametrize("group_id", ["abc", None, float("nan"), True])
def test_load_report_rejects_bad_group_id(tmp_path, group_id):
    """畸形 id 必须转成 ReportError（退出码 3），而不是抛 ValueError/TypeError 崩栈。"""
    from dupfind.report import ReportError, load_report

    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "scan": {"root": "D:\\", "partial": False},
                "groups": [
                    {
                        "id": group_id,
                        "status": "confirmed",
                        "size": SIZE,
                        "savingsBytes": SIZE,
                        "files": [{"path": "D:\a.bin"}, {"path": "D:\b.bin"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if group_id is None:
        # id 缺失时允许退回序号，这里测的是「显式给了 None」之外的三种坏值
        load_report(path)
        return
    with pytest.raises(ReportError):
        load_report(path)


@pytest.mark.parametrize(
    "patch",
    [
        {"version": True},
        {"version": 1.0},
        {"scan": {"root": "D:\\", "partial": "false"}},
        {"groups": [{"status": "confirmed", "size": True, "savingsBytes": 0, "files": [{"path": "D:\a.bin"}]}]},
    ],
)
def test_load_report_rejects_wrong_types(tmp_path, patch):
    """版本/partial/size 的类型必须严格：``True == 1``、字符串 "false" 都会导致误判。"""
    from dupfind.report import ReportError, load_report

    payload = {
        "version": 1,
        "scan": {"root": "D:\\", "partial": False},
        "groups": [],
    }
    payload.update(patch)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportError):
        load_report(path)


@pytest.mark.parametrize(
    "names",
    [
        ("x 2.txt", "x 3.txt"),
        ("a.txt", "a 2.txt"),
        ("10.pdf", "10 2.pdf"),
    ],
)
def test_identical_token_sets_still_need_short_name_check(names):
    """归一化后词元相同的文件也必须过短名校验，否则噪声名成为候选组。"""
    from dupfind.grouping import build_candidate_groups
    from dupfind.models import FileEntry
    from dupfind.normalize import FilenameNormalizer

    entries = [
        FileEntry(path="D:/x/" + name, name=name, size=SIZE, mtime=0.0, attributes=0) for name in names
    ]
    assert build_candidate_groups(entries, FilenameNormalizer()) == [], "噪声名被合成了候选组"


def test_normalize_strips_deep_marker_chains():
    """9 层叠加的副本标记也要剥干净（固定迭代轮数会留下残渣）。"""
    from dupfind.normalize import FilenameNormalizer

    normalizer = FilenameNormalizer()
    chain = "报告" + "".join(f" - 副本({index})" for index in range(2, 10)) + ".pdf"
    assert normalizer.normalize(chain) == "报告"


def test_format_size_carries_into_next_unit():
    """四舍五入后满 1024 必须进位，否则会出现 ``1024.0 KB`` 这种读数。"""
    from dupfind.units import format_size, parse_size

    assert format_size(1048575) == "1.0 MB"
    assert format_size(1073741823) == "1.0 GB"
    # format_size 的输出必须能被 parse_size 读回来
    assert parse_size(format_size(1024 ** 5)) == 1024 ** 5


def test_parse_size_keeps_integer_precision():
    """大整数不能被 float 抹掉精度。"""
    from dupfind.units import parse_size

    assert parse_size("9007199254740993B") == 9007199254740993
    with pytest.raises(ValueError):
        parse_size(-5)


# ---------------------------------------------------------------------------
# 6. 隔离区副本绝不能被选作保留项（否则 empty 会把唯一数据删光）
# ---------------------------------------------------------------------------


def test_plan_group_never_keeps_cluster_inside_trash(tmp_path):
    """活文件与隔离区里的副本同组时，必须保留活文件。

    否则活文件被移进隔离区，保留项本身也在隔离区，随后 empty 把两份一起删掉。
    """
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    trash = tmp_path / "_dupfind_trash"
    live = str(tmp_path / "docs" / "thesis.docx")
    trashed = str(trash / "C" / "backup" / "thesis.docx")

    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile(live, SIZE, "thesis", None, "0x1", False, False),
            # 路径排序上 "_dupfind_trash" 的 "_" 小于 "docs" 的 "d"，
            # 不修正的话 shortest 会选中隔离区那份
            ReportFile(trashed, SIZE, "thesis", None, "0x2", False, False),
        ],
    )
    keep, removals, problem = plan_group(group, "shortest", trash=str(trash))
    assert problem is None, problem
    assert keep[0].path == live, "隔离区里的副本被选作保留项，活文件会被移走"
    assert removals[0][0].path == trashed


def test_plan_group_refuses_group_entirely_inside_trash(tmp_path):
    """组内所有副本都在隔离区时，无可安全处置的文件，整组放弃。"""
    from dupfind.apply import plan_group
    from dupfind.report import ReportFile, ReportGroup

    trash = tmp_path / "_dupfind_trash"
    group = ReportGroup(
        id=1,
        status="confirmed",
        size=SIZE,
        savings_bytes=SIZE,
        files=[
            ReportFile(str(trash / "C" / "a" / "x.bin"), SIZE, "x", None, "0x1", False, False),
            ReportFile(str(trash / "C" / "b" / "x.bin"), SIZE, "x", None, "0x2", False, False),
        ],
    )
    keep, removals, problem = plan_group(group, "shortest", trash=str(trash))
    assert problem is not None and "隔离区" in problem


def test_trash_directory_is_excluded_from_scan(tmp_path):
    """默认排除规则必须跳过 _dupfind_trash，否则第二次扫描会把隔离区当重复源。"""
    from dupfind.config import Config
    from dupfind.exclude import Excluder

    excluder = Excluder(Config(), "D:\\")
    assert excluder.dir_reason(r"D:\_dupfind_trash", "_dupfind_trash") is not None
    assert excluder.dir_reason(r"D:\data\_dupfind_trash", "_dupfind_trash") is not None


def test_report_directory_is_excluded_from_scan(tmp_path):
    """Web 产出的 dupfind-reports 是工具自己的数据，不能被当成待去重文件。

    两份内容一致的报告会被报成一组重复文件；更糟的是处置时会移动报告本身，
    让用户随后的「加载报告」失败。
    """
    from dupfind.config import Config
    from dupfind.exclude import Excluder

    excluder = Excluder(Config(), "D:\\")
    assert excluder.dir_reason(r"D:\dupfind-reports", "dupfind-reports") is not None
    assert excluder.dir_reason(r"D:\proj\dupfind-reports", "dupfind-reports") is not None


def test_trash_target_detects_collision_on_long_path(tmp_path):
    """长路径（>260）下已存在的镜像目标也必须被检测到，不能静默覆盖旧文件。"""
    from dupfind.trash import TrashManager

    deep = "d" * 90
    relative = "\\".join([deep, deep, deep, "x.bin"])
    source = rf"{tmp_path.anchor}C\{relative}"
    assert len(source) > 260, "测试前提不成立：路径不够长"

    trash = tmp_path / "trash"
    manager = TrashManager(str(trash))
    target = manager.target_for(source)
    from dupfind.win32 import long_path

    os.makedirs(os.path.dirname(long_path(target)), exist_ok=True)
    with open(long_path(target), "w", encoding="utf-8") as fp:
        fp.write("OLD-ONLY-COPY")

    # 同名镜像目标已被占用，必须换一个名字
    fresh = TrashManager(str(trash))
    second = fresh.target_for(source)
    assert second != target, "长路径下未检测到已存在的目标，会覆盖旧文件"
    assert "dupfind-" in os.path.basename(second)


def test_file_reason_respects_ignore_subtree():
    """忽略「(根目录)」后，根下的直接文件也必须被扫描跳过。

    子目录由 dir_reason 拦截；扫描根本身不会调用 dir_reason，根下文件只能
    在 file_reason 里拦，否则视图隐藏了它们、重扫却仍会收进报告。
    """
    from dupfind.config import Config
    from dupfind.exclude import Excluder

    excluder = Excluder(Config(), r"D:\data", ignore_dirs=[r"D:\data"])
    assert excluder.file_reason(r"D:\data\a.jpg", "a.jpg", 0, 0) is not None
    assert excluder.file_reason(r"D:\other\a.jpg", "a.jpg", 0, 0) is None
