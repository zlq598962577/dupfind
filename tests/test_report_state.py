"""报告处置状态：状态文件、身份校验、manifest 兜底与还原同步。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dupfind.report import load_report
from dupfind.report_state import (
    ReportStateError,
    ReportStateStore,
    load_effective_state,
    record_apply_result,
    report_identity,
    scan_manifest_states,
    set_manual_group_state,
    state_path_for,
    sync_after_restore,
)
from dupfind.trash import Manifest

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
    report = write_report(
        tmp_path,
        [[files[0], files[1]], [files[2], files[3]]],
    )
    return {"root": root, "files": files, "report": report, "trash": tmp_path / "trash"}


def test_record_and_load_state(scan):
    document = load_report(scan["report"])
    assert record_apply_result(
        document, {1: ("handled", 1, SIZE)}, manifest=r"D:\trash\manifest-1.json"
    )
    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.identity_ok is True
    assert state.state_of(1) == "handled"
    assert state.state_of(2) == "pending"
    assert state.skip_ids() == frozenset({1})
    record = state.groups[1]
    assert record.moved_files == 1
    assert record.moved_bytes == SIZE
    assert record.manifest == r"D:\trash\manifest-1.json"


def test_partial_groups_stay_disposable(scan):
    """部分处置的组还要继续清理剩下的副本，不能从待处理里消失。"""
    document = load_report(scan["report"])
    record_apply_result(document, {1: ("partial", 1, SIZE)})
    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "partial"
    assert 1 not in state.skip_ids()


def test_state_invalidated_when_report_is_rescanned(scan):
    document = load_report(scan["report"])
    record_apply_result(document, {1: ("handled", 1, SIZE)})

    payload = json.loads(scan["report"].read_text(encoding="utf-8"))
    payload["scan"]["startedAt"] = "2026-09-09T09:09:09+08:00"
    scan["report"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    state = load_effective_state(load_report(scan["report"]), trash=str(scan["trash"]))
    assert state.identity_ok is False
    assert state.groups == {}
    assert state.skip_ids() == frozenset()


def test_corrupt_state_file_is_treated_as_empty(scan):
    document = load_report(scan["report"])
    state_path_for(scan["report"]).write_text("{not json", encoding="utf-8")
    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.identity_ok is True
    assert state.groups == {}


def test_identity_requires_same_scan_shape(tmp_path):
    document_identity = {
        "root": r"D:\data",
        "startedAt": "2026-01-01T00:00:00+08:00",
        "filesScanned": 4,
        "groups": 2,
    }
    store = ReportStateStore(tmp_path / "report.json")
    # 不存在的状态文件 + 新身份 → 视为有效（尚无状态）
    assert store.load(document_identity) is True


def test_unknown_group_state_is_rejected(tmp_path):
    store = ReportStateStore(tmp_path / "report.json")
    with pytest.raises(ReportStateError):
        store.set_group(1, "bogus")


def test_manifest_backfill_rebuilds_handled_groups(scan):
    trash = scan["trash"]
    trash.mkdir()
    manifest = Manifest(str(trash), str(scan["report"]))
    done = manifest.add(
        group_id=1, source=str(scan["files"][1]), target=str(trash / "C" / "a.pdf"), size=SIZE
    )
    manifest.mark(done, status="completed")
    pending = manifest.add(
        group_id=2, source=str(scan["files"][3]), target=str(trash / "C" / "b.zip"), size=SIZE
    )
    assert pending["status"] == "pending"
    manifest.save()
    manifest.close()

    states, matched = scan_manifest_states(scan["report"], str(trash))
    assert matched == 1
    assert set(states) == {1}, "pending 动作不能算作已处置"
    assert states[1].state == "handled"
    assert states[1].moved_files == 1

    # 没有状态文件时，视图仍能从 manifest 得到「已处置」
    state = load_effective_state(load_report(scan["report"]), trash=str(trash))
    assert state.state_of(1) == "handled"
    assert state.manifest_groups == 1


def test_manual_ignore_and_pending(scan):
    document = load_report(scan["report"])
    assert set_manual_group_state(document, 1, "ignored") is True
    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "ignored"
    assert state.skip_ids() == frozenset({1})

    assert set_manual_group_state(document, 1, None) is True
    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "pending"


def test_manual_state_rebuilds_state_file_from_another_scan(scan):
    """旧状态文件属于另一份扫描：必须以当前身份重建，否则永远 409。"""
    document = load_report(scan["report"])
    stale = state_path_for(scan["report"])
    stale.write_text(
        json.dumps(
            {
                "version": 1,
                "identity": {
                    "root": r"Z:\\",
                    "startedAt": "2000-01-01T00:00:00+08:00",
                    "filesScanned": 0,
                    "groups": 0,
                },
                "groups": {"1": {"state": "handled", "movedFiles": 9}},
            }
        ),
        encoding="utf-8",
    )
    assert set_manual_group_state(document, 1, "ignored") is True

    saved = json.loads(stale.read_text(encoding="utf-8"))
    assert saved["identity"]["root"] == str(Path(scan["report"]).parent)
    state = load_effective_state(document, include_manifests=False)
    assert state.identity_ok is True
    assert state.state_of(1) == "ignored"


def test_restore_clears_handled_but_keeps_ignored(scan):
    document = load_report(scan["report"])
    record_apply_result(document, {2: ("handled", 1, SIZE)})
    set_manual_group_state(document, 1, "ignored")

    sync_after_restore(scan["report"], str(scan["trash"]))

    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "ignored", "手动忽略不能被还原流程抹掉"
    assert state.state_of(2) == "pending", "还原后该组必须回到待处理"


def test_manual_pending_overrides_manifest_backfill(scan):
    """显式恢复待处理要写墓碑：manifest 兜底不能立刻把它补回 handled。"""
    trash = scan["trash"]
    trash.mkdir()
    manifest = Manifest(str(trash), str(scan["report"]))
    done = manifest.add(
        group_id=1, source=str(scan["files"][1]), target=str(trash / "C" / "a.pdf"), size=SIZE
    )
    manifest.mark(done, status="completed")
    manifest.save()
    manifest.close()

    document = load_report(scan["report"])
    assert load_effective_state(document, trash=str(trash)).state_of(1) == "handled"

    assert set_manual_group_state(document, 1, None) is True
    state = load_effective_state(document, trash=str(trash))
    assert state.state_of(1) == "pending", "墓碑必须压制 manifest 回填"
    assert 1 not in state.skip_ids()


def test_sync_after_restore_keeps_records_from_other_trash(scan, tmp_path):
    """还原一个隔离区不能抹掉另一个隔离区的处置记录。"""
    other = tmp_path / "other-trash"
    other.mkdir()
    document = load_report(scan["report"])
    record_apply_result(
        document, {2: ("handled", 1, SIZE)}, manifest=str(other / "manifest-1.json")
    )
    set_manual_group_state(document, 1, "ignored")

    scan["trash"].mkdir()
    sync_after_restore(scan["report"], str(scan["trash"]))

    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "ignored", "手动忽略不能被还原流程抹掉"
    assert state.state_of(2) == "handled", "其他隔离区的记录不能丢"


def test_bad_state_file_version_keeps_identity(scan):
    """版本不符的状态文件被覆盖时，不能写出 identity:null 把文件永久锁死。"""
    document = load_report(scan["report"])
    state_path_for(scan["report"]).write_text('{"version": 2, "groups": {}}', encoding="utf-8")

    assert set_manual_group_state(document, 1, "ignored") is True
    saved = json.loads(state_path_for(scan["report"]).read_text(encoding="utf-8"))
    assert saved["identity"] is not None
    assert saved["identity"]["root"] == str(Path(scan["report"]).parent)
    assert load_effective_state(document, include_manifests=False).identity_ok is True


def test_partial_retry_accumulates_moved_files(scan):
    """partial 组重试成功后，卡片上的「已移动」应累加而不是倒退。"""
    document = load_report(scan["report"])
    record_apply_result(document, {1: ("partial", 1, SIZE)})
    record_apply_result(document, {1: ("handled", 2, SIZE * 2)})

    state = load_effective_state(document, trash=str(scan["trash"]))
    assert state.state_of(1) == "handled"
    assert state.groups[1].moved_files == 3
    assert state.groups[1].moved_bytes == SIZE * 3


def test_record_with_no_results_is_a_noop(scan):
    document = load_report(scan["report"])
    assert record_apply_result(document, {}) is True
    assert not state_path_for(scan["report"]).exists()


def test_report_identity_uses_scan_metadata(scan):
    identity = report_identity(load_report(scan["report"]))
    assert identity["root"] == str(Path(scan["report"]).parent)
    assert identity["groups"] == 2
    assert identity["filesScanned"] == 4
