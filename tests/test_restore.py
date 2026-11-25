"""restore / empty 的行为测试。

重点覆盖四件事：崩溃后从 journal 恢复、幂等、冲突处理、清空时的安全护栏。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_builder import DEFAULT_SIZE, content_bytes  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = DEFAULT_SIZE


def _write_manifest(trash: Path, actions: list[dict]) -> Path:
    """按 apply 产出的格式写一份 manifest 快照。"""
    trash.mkdir(parents=True, exist_ok=True)
    path = trash / "manifest-20260101-000000.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "createdAt": "2026-01-01T00:00:00+08:00",
                "report": "report.json",
                "trash": str(trash),
                "actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _move_to_trash(source: Path, trash: Path) -> Path:
    """模拟 apply 的移动，返回隔离区内的目标路径。"""
    relative = source.relative_to(source.anchor)
    target = trash / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return target


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_moves_files_back(tmp_path):
    """还原后文件回到原位置，隔离区清空。"""
    from dupfind.restore import RestoreOptions, run_restore

    data = tmp_path / "data"
    data.mkdir()
    source = data / "报告.pdf"
    source.write_bytes(content_bytes(SIZE, seed=1))
    original_bytes = source.read_bytes()

    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 0
    assert source.is_file() and source.read_bytes() == original_bytes
    assert not target.exists()


def test_restore_dry_run_moves_nothing(tmp_path):
    """dry-run 不移动任何文件。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=2))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=True))
    assert not source.exists() and target.exists(), "dry-run 不应移动文件"


def test_restore_is_idempotent(tmp_path):
    """重复执行 restore 不应报错，也不应产生重复文件。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=3))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=False))
    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 1, "第二次执行没有可还原的动作"
    assert source.is_file()
    assert len(list(source.parent.iterdir())) == 1, "重复还原产生了多余文件"


def test_restore_recovers_from_journal_after_crash(tmp_path):
    """快照未写出（进程被杀）时，journal 仍能完整还原。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=4))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    # 只有 journal，没有快照 —— 模拟 save() 之前崩溃
    trash.mkdir(parents=True, exist_ok=True)
    journal = trash / "manifest-20260101-000000.json.journal"
    journal.write_text(
        json.dumps({"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"})
        + "\n",
        encoding="utf-8",
    )

    code = run_restore(journal, RestoreOptions(dry_run=False))
    assert code == 0
    assert source.is_file(), "journal 中的动作未被还原"


def test_restore_recovers_action_left_pending(tmp_path):
    """移动已发生、但 status 还停在 pending 时，也必须还原。

    apply 的顺序是「写 journal(pending) → 移动 → 标记 completed」。进程在移动
    之后、标记之前被杀，journal 里就只有 pending —— 跳过它文件会永远留在隔离区。
    """
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=15))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "pending"}],
    )

    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 0
    assert source.is_file(), "停在 pending 的动作被跳过了，文件卡在隔离区"


def test_restore_pending_without_move_is_noop(tmp_path):
    """pending 但移动根本没发生（原文件还在原位）：不应报错，也不应移动任何东西。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=16))
    trash = tmp_path / "trash"
    target = trash / "C" / "data" / "a.bin"  # 隔离区里并不存在

    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "pending"}],
    )
    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 1, "没有可还原的动作时应返回 1"
    assert source.is_file() and source.read_bytes() == content_bytes(SIZE, seed=16)


def test_restore_journal_tolerates_truncated_line(tmp_path):
    """崩溃可能留下半行 JSON，解析时应跳过而不是整份失败。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=5))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    trash.mkdir(parents=True, exist_ok=True)
    journal = trash / "manifest-20260101-000000.json.journal"
    journal.write_text(
        json.dumps({"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"})
        + "\n"
        + '{"groupId": 2, "src": "D:\\\\trunc',  # 半行
        encoding="utf-8",
    )

    code = run_restore(journal, RestoreOptions(dry_run=False))
    assert code == 0
    assert source.is_file()


def test_restore_conflict_skip_keeps_existing(tmp_path):
    """原位置已有文件时，skip 模式不动它。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=6))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    # 原位置被新文件占用
    source.write_bytes(b"NEW CONTENT")
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    code = run_restore(manifest, RestoreOptions(dry_run=False, on_conflict="skip"))
    assert code == 1, "冲突跳过属于「没事可做」，不是失败"
    assert source.read_bytes() == b"NEW CONTENT"
    assert target.exists(), "冲突时隔离区文件应保留"


def test_restore_failure_exit_code_is_2(tmp_path):
    """真正的失败（越界路径）退出码为 2，与「没事可做」的 1 区分开。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=12))
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"KEEP")

    trash = tmp_path / "trash"
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(victim), "size": SIZE, "status": "completed"}],
    )
    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 2, "失败应返回 2"


def test_restore_conflict_rename_preserves_both(tmp_path):
    """rename 模式把还原内容另存，两边都保留。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=7))
    original = source.read_bytes()
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    source.write_bytes(b"NEW CONTENT")
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=False, on_conflict="rename"))
    restored = list(source.parent.glob("a.restored-*"))
    assert restored, "未生成另存文件"
    assert restored[0].read_bytes() == original
    assert source.read_bytes() == b"NEW CONTENT"


def test_restore_overwrite_dry_run_does_not_delete(tmp_path):
    """overwrite + dry-run 绝不能删除已存在的原文件 —— 这是不可逆操作。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=9))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    source.write_bytes(b"NEW CONTENT")
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=True, on_conflict="overwrite"))
    assert source.read_bytes() == b"NEW CONTENT", "dry-run 删除了已存在的文件"
    assert target.exists(), "dry-run 移动了隔离区文件"


def test_restore_overwrite_replaces_existing(tmp_path):
    """overwrite 实际执行时用隔离区内容替换原位置文件。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=11))
    original = source.read_bytes()
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    source.write_bytes(b"NEW CONTENT")
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=False, on_conflict="overwrite"))
    assert source.read_bytes() == original
    assert not target.exists()


def test_empty_keeps_manifest_when_payload_fails(tmp_path):
    """清空 payload 失败时，manifest 仍应保留，否则还原记录丢失。"""
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload = trash / "C" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x")
    manifest = _write_manifest(trash, [])

    run_empty(str(trash), EmptyOptions(dry_run=False, keep_manifests=True))
    assert manifest.is_file()


def test_restore_rejects_dst_outside_trash(tmp_path):
    """dst 不在该 manifest 的 trash 内时拒绝，防止伪造 manifest 搬运任意文件。"""
    from dupfind.restore import RestoreOptions, run_restore

    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"IMPORTANT")
    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=8))

    trash = tmp_path / "trash"
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(victim), "size": SIZE, "status": "completed"}],
    )

    run_restore(manifest, RestoreOptions(dry_run=False))
    assert victim.exists() and victim.read_bytes() == b"IMPORTANT", "越界路径被搬运了"


def test_restore_directory_processes_all_manifests(tmp_path):
    """传目录时处理其中全部 manifest。"""
    from dupfind.restore import RestoreOptions, run_restore

    trash = tmp_path / "trash"
    sources = []
    actions = []
    for index in range(2):
        source = tmp_path / "data" / f"f{index}.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content_bytes(SIZE, seed=10 + index))
        target = _move_to_trash(source, trash)
        sources.append(source)
        actions.append(
            {"groupId": index, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}
        )
    _write_manifest(trash, actions)

    run_restore(trash, RestoreOptions(dry_run=False))
    assert all(source.is_file() for source in sources)


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


def test_empty_removes_files_and_keeps_manifests(tmp_path):
    """清空后隔离区文件消失，manifest 默认保留。"""
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload = trash / "C" / "data" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 100)
    manifest = _write_manifest(trash, [])

    code = run_empty(str(trash), EmptyOptions(dry_run=False, keep_manifests=True))
    assert code == 0
    assert not payload.exists()
    assert manifest.is_file(), "manifest 应默认保留"
    assert trash.is_dir(), "trash 根目录不应被删除"


def test_empty_purge_manifests_removes_them(tmp_path):
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload = trash / "C" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 100)
    manifest = _write_manifest(trash, [])

    run_empty(str(trash), EmptyOptions(dry_run=False, keep_manifests=False))
    assert not payload.exists()
    assert not manifest.exists()


def test_empty_dry_run_deletes_nothing(tmp_path):
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload = trash / "C" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 100)
    _write_manifest(trash, [])

    run_empty(str(trash), EmptyOptions(dry_run=True))
    assert payload.is_file(), "dry-run 不应删除文件"


def test_empty_refuses_directory_without_manifest(tmp_path):
    """没有 manifest 的目录不是隔离区 —— 用户很可能把资料目录写错了，必须拒绝。

    ``empty`` 是不可逆的，一旦照删就再也拿不回来。
    """
    from dupfind.restore import EmptyOptions, RestoreError, run_empty

    innocent = tmp_path / "照片"
    innocent.mkdir()
    photo = innocent / "旅行.jpg"
    photo.write_bytes(b"JPEGDATA")

    with pytest.raises(RestoreError, match="看起来不是隔离区"):
        run_empty(str(innocent), EmptyOptions(dry_run=False))
    assert photo.is_file(), "非隔离区目录里的文件被删了"


def test_empty_refuses_drive_root(tmp_path):
    """拒绝清空盘符根目录 —— 这是不可逆操作的最后一道护栏。"""
    from dupfind.restore import EmptyOptions, RestoreError, run_empty

    with pytest.raises(RestoreError, match="盘符根目录"):
        run_empty("D:\\", EmptyOptions(dry_run=False))


@pytest.mark.parametrize(
    "target",
    [
        "C:\\Windows",
        "C:\\Windows ",
        "C:\\PROGRA~1",
        "C:\\ProgramData",
    ],
)
def test_empty_refuses_system_directories(target):
    """empty 是不可逆操作，绝不能清空系统目录，也不能被等价写法绕过。"""
    from dupfind.restore import EmptyOptions, RestoreError, run_empty

    with pytest.raises(RestoreError, match="受保护|盘符根目录"):
        run_empty(target, EmptyOptions(dry_run=False))


def test_forged_trash_field_is_ignored(tmp_path):
    """manifest 里声明的 trash 字段不可信，必须以文件实际位置为准。

    否则伪造 trash="C:\\\\" 就能让任意路径通过「dst 必须在 trash 内」的校验，
    把系统文件搬到攻击者指定的位置。
    """
    from dupfind.restore import RestoreOptions, load_manifest, run_restore

    victim = tmp_path / "victim.dll"
    victim.write_bytes(b"SYSTEM IMPORTANT")
    src = tmp_path / "data" / "innocent.bin"
    src.parent.mkdir()
    trash = tmp_path / "trash"
    trash.mkdir()

    forged = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(src), "dst": str(victim), "size": 16, "status": "completed"}],
    )
    # 手工把 trash 字段改成盘符根目录
    data = json.loads(forged.read_text(encoding="utf-8"))
    data["trash"] = str(Path(tmp_path.anchor))
    forged.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    document = load_manifest(forged)
    assert document.trash == str(trash), "trash 字段不应被 manifest 内容覆盖"

    code = run_restore(forged, RestoreOptions(dry_run=False))
    assert code == 2
    assert victim.exists() and victim.read_bytes() == b"SYSTEM IMPORTANT", "受害者文件被搬走了"


def test_restore_refuses_protected_src(tmp_path):
    """伪造的 manifest 想把文件写进系统目录，必须拒绝。

    合法 manifest 里不会有 src 落在受保护路径的动作（apply 从不移动那里的
    文件），所以这条校验不影响正常还原。
    """
    from dupfind.restore import RestoreOptions, run_restore

    trash = tmp_path / "trash"
    payload = trash / "C" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x")
    manifest = _write_manifest(
        trash,
        [
            {
                "groupId": 1,
                "src": "C:\\Windows\\System32\\evil.dll",
                "dst": str(payload),
                "size": 1,
                "status": "completed",
            }
        ],
    )

    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 2, "写入系统目录的伪造动作未被拒绝"
    assert payload.exists(), "被拒绝时隔离区文件应保留"


def test_restore_missing_drive_does_not_crash(tmp_path):
    """原盘符已不存在时，该动作失败但整批还原不中断，隔离区文件保留。"""
    from dupfind.restore import RestoreOptions, run_restore

    trash = tmp_path / "trash"
    payload = trash / "Z" / "x" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 10)
    manifest = _write_manifest(
        trash,
        [
            {
                "groupId": 1,
                "src": "Z:\\nonexistent\\a.bin",
                "dst": str(payload),
                "size": 10,
                "status": "completed",
            }
        ],
    )

    code = run_restore(manifest, RestoreOptions(dry_run=False))
    assert code == 2, "失败应返回 2"
    assert payload.exists(), "失败时隔离区文件必须保留"


def test_restore_directory_finds_journal_only_manifest(tmp_path):
    """传目录时，只有 journal、没有快照的 manifest 也必须被发现并还原。

    apply 期间进程被强杀，快照还没写出 —— 这正是 journal 存在的意义。
    """
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=13))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)

    journal = trash / "manifest-20260101-000000.json.journal"
    journal.write_text(
        json.dumps({"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"})
        + "\n",
        encoding="utf-8",
    )

    code = run_restore(trash, RestoreOptions(dry_run=False))
    assert code == 0
    assert source.is_file(), "目录模式漏掉了只有 journal 的 manifest"


def test_empty_purge_keeps_manifests_when_payload_fails(tmp_path):
    """payload 删不掉时不能连 manifest 一起删 —— 那些文件还要靠它还原。"""
    import stat

    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload = trash / "C" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x")
    manifest = _write_manifest(trash, [])
    # 只读属性会让 os.unlink 在 Windows 上失败
    os.chmod(payload, stat.S_IREAD)

    code = run_empty(str(trash), EmptyOptions(dry_run=False, keep_manifests=False))
    assert code == 2
    assert manifest.is_file(), "删除失败时 manifest 被一并删掉了"
    assert payload.is_file(), "前提不成立：只读文件被删掉了，测试没覆盖到失败分支"


def test_restore_idempotent_when_journal_present(tmp_path):
    """快照写回后必须清掉 journal，否则 journal 的旧状态会盖掉「已还原」。"""
    from dupfind.restore import RestoreOptions, run_restore

    source = tmp_path / "data" / "a.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content_bytes(SIZE, seed=14))
    trash = tmp_path / "trash"
    target = _move_to_trash(source, trash)
    manifest = _write_manifest(
        trash,
        [{"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"}],
    )
    journal = trash / "manifest-20260101-000000.json.journal"
    journal.write_text(
        json.dumps({"groupId": 1, "src": str(source), "dst": str(target), "size": SIZE, "status": "completed"})
        + "\n",
        encoding="utf-8",
    )

    assert run_restore(manifest, RestoreOptions(dry_run=False)) == 0
    assert not journal.exists(), "写回快照后 journal 未清理，旧状态会覆盖已还原状态"
    # 第二次执行：不应再次「还原」一个已经不存在的隔离区文件
    assert run_restore(manifest, RestoreOptions(dry_run=False)) == 1


def test_empty_does_not_follow_junction(tmp_path):
    """隔离区里的 junction 不能被跟随，否则会删到隔离区之外的文件。"""
    import subprocess

    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "real.bin").write_bytes(b"x" * 10)
    _write_manifest(trash, [])

    victim = tmp_path / "victim"
    victim.mkdir()
    important = victim / "important.txt"
    important.write_text("KEEP ME", encoding="utf-8")

    link = trash / "link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(victim)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"无法创建 junction：{result.stderr.strip()}")

    run_empty(str(trash), EmptyOptions(dry_run=False))
    assert important.exists(), "junction 被跟随，删到了隔离区之外的文件"
    assert not (trash / "real.bin").exists(), "隔离区内的真实文件应被删除"


def test_empty_prune_does_not_touch_junction_target(tmp_path):
    """清理空目录时绝不能顺着 junction 删到外面去。

    ``os.walk`` 会跟随 junction，随后对 ``trash\\link\\sub`` 调 rmdir 删掉的
    其实是链接指向的外部真实目录 —— 即使 empty 已经跳过了链接本身。
    """
    import subprocess

    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    trash.mkdir()
    _write_manifest(trash, [])
    outside = tmp_path / "outside"
    empty_subdir = outside / "empty_sub"
    empty_subdir.mkdir(parents=True)

    link = trash / "link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"无法创建 junction：{result.stderr.strip()}")

    run_empty(str(trash), EmptyOptions(dry_run=False))
    assert empty_subdir.is_dir(), "顺着 junction 删掉了隔离区之外的目录"


def test_empty_prunes_empty_dirs(tmp_path):
    """清空后应删掉空的镜像目录，但保留 trash 根。"""
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    nested = trash / "C" / "deep" / "dir"
    nested.mkdir(parents=True)
    (nested / "a.bin").write_bytes(b"x")
    _write_manifest(trash, [])

    run_empty(str(trash), EmptyOptions(dry_run=False))
    assert not (trash / "C").exists(), "空目录未被清理"
    assert trash.is_dir()


# ---------------------------------------------------------------------------
# 数据丢失风险（审查发现）
# ---------------------------------------------------------------------------


def test_empty_refuses_nested_lookalike_manifest(tmp_path):
    r"""嵌套的 ``manifest-assets.json`` 是用户数据，不能当成「这是隔离区」的证据。

    否则 ``dupfind empty D:\MyProject`` 会删掉项目里除该文件以外的一切。
    """
    from dupfind.restore import EmptyOptions, RestoreError, run_empty

    project = tmp_path / "MyProject"
    assets = project / "assets"
    assets.mkdir(parents=True)
    (assets / "manifest-assets.json").write_text("{}", encoding="utf-8")
    thesis = project / "thesis-final.docx"
    thesis.write_bytes(b"MY THESIS")

    with pytest.raises(RestoreError, match="看起来不是隔离区"):
        run_empty(str(project), EmptyOptions(dry_run=False))
    assert thesis.is_file(), "真实目录里的文件被删了"


def test_empty_preserves_lookalike_manifest_inside_real_trash(tmp_path):
    """真隔离区里若有嵌套的同名文件，它是 payload，不是还原记录。"""
    from dupfind.restore import EmptyOptions, run_empty

    trash = tmp_path / "trash"
    payload_dir = trash / "C" / "proj" / "assets"
    payload_dir.mkdir(parents=True)
    lookalike = payload_dir / "manifest-assets.json"
    lookalike.write_text("{}", encoding="utf-8")
    _write_manifest(trash, [])

    code = run_empty(str(trash), EmptyOptions(dry_run=False, keep_manifests=True))
    assert code == 0
    assert not lookalike.exists(), "嵌套的同名文件被误当成 manifest 保留了"


def test_empty_refuses_junction_root(tmp_path):
    """trash 根是 junction 时，os.walk 会顺着它删到外面去，必须拒绝。"""
    import subprocess

    from dupfind.restore import EmptyOptions, RestoreError, run_empty

    real = tmp_path / "RealDocs"
    real.mkdir()
    keep = real / "family-photos.zip"
    keep.write_bytes(b"PHOTOS")
    _write_manifest(real, [])

    link = tmp_path / "trash-link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"无法创建 junction：{result.stderr.strip()}")

    with pytest.raises(RestoreError, match="junction"):
        run_empty(str(link), EmptyOptions(dry_run=False))
    assert keep.is_file(), "顺着 junction 删掉了隔离区之外的文件"
