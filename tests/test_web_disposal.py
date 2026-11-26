"""Web 处置闭环：预览聚合、后台任务、四个处置接口。

这些用例走的是真实的文件系统（不是 mock）：apply 会校验大小/修改时间、
复核内容、写 manifest，所以报告必须指向真实存在的文件，否则测到的是
「一切都被跳过」而不是正常路径。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")

SIZE = 4096


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _write_file(path: Path, size: int = SIZE, fill: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * size)[:size])
    return path


def _report_entry(path: Path, *, file_id: str, size: int = SIZE) -> dict:
    return {
        "path": str(path),
        "normalizedName": path.stem,
        "mtime": _iso_mtime(path),
        "fileId": file_id,
        "shared": False,
        "unstable": False,
        "sizeOnDisk": size,
    }


def _iso_mtime(path: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _write_report(
    tmp_path: Path,
    groups: list[list[Path]],
    *,
    size: int = SIZE,
    partial: bool = False,
    status: str = "confirmed",
) -> Path:
    payload = {
        "version": 1,
        "scan": {
            "root": str(tmp_path),
            "startedAt": "2026-01-01T00:00:00+08:00",
            "durationMs": 1,
            "filesScanned": sum(len(group) for group in groups),
            "excludedDirs": 0,
            "excludedFiles": 0,
            "accessDenied": 0,
            "errors": 0,
            "partial": partial,
        },
        "groups": [
            {
                "id": index,
                "status": status,
                "size": size,
                "savingsBytes": size * (len(group) - 1),
                "files": [
                    _report_entry(path, file_id=f"0x{index:08X}{item:016X}", size=size)
                    for item, path in enumerate(group)
                ],
            }
            for index, group in enumerate(groups, start=1)
        ],
    }
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return report


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """两组合格的确认重复文件：组 1 三份、组 2 两份。"""
    root = tmp_path / "data"
    root.mkdir()
    for name in ("报告.pdf", "报告 - 副本.pdf", "报告 - 副本(2).pdf"):
        _write_file(root / "docs" / name)
    for name in ("照片.zip", "照片 - 副本.zip"):
        _write_file(root / "pics" / name)
    return root


@pytest.fixture
def report_path(tmp_path: Path, data: Path) -> Path:
    return _write_report(
        tmp_path,
        [
            [
                data / "docs" / "报告.pdf",
                data / "docs" / "报告 - 副本.pdf",
                data / "docs" / "报告 - 副本(2).pdf",
            ],
            [data / "pics" / "照片.zip", data / "pics" / "照片 - 副本.zip"],
        ],
    )


def _options(trash: Path):
    from dupfind.apply import ApplyOptions

    return ApplyOptions(trash_dir=str(trash), keep_mode="shortest", dry_run=False, assume_yes=True)


def _wait(client, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/disposal/{task_id}")
        assert body.status_code == 200, body.text
        task = body.json()
        if task["state"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError("处置任务超时未结束")


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from dupfind.web.disposal import DisposalTaskManager
    from dupfind.web.server import create_app
    from dupfind.web.tasks import ScanTaskManager

    manager = ScanTaskManager(tmp_path / "reports")
    app = create_app(manager=manager, disposal=DisposalTaskManager())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolated_default_trash(monkeypatch, tmp_path, request):
    """预览会扫描 <报告盘符>:\\_dupfind_trash；测试统一指到 tmp_path。

    否则宿主机器上的历史隔离区会改变 unscoped/otherReport 计数，测试不再
    hermetic（在临时目录位于 D 盘、而 D:\\_dupfind_trash 有残留时必现）。
    专门验证 default_trash_for 本身的用例不受影响。
    """
    if request.node.name == "test_default_trash_for_uses_drive_root":
        return
    monkeypatch.setattr(
        "dupfind.web.disposal.default_trash_for",
        lambda root: str(tmp_path / "default-trash"),
    )


# ---------------------------------------------------------------------------
# build_preview
# ---------------------------------------------------------------------------


def test_preview_aggregates_directories_and_extensions(tmp_path, report_path):
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    trash = tmp_path / "trash"
    preview = build_preview(load_report(report_path), _options(trash)).to_dict()

    assert preview["groupCount"] == 2
    assert preview["fileCount"] == 3, "两组共移走 3 个文件（各留一份）"
    assert preview["freedBytes"] == SIZE * 3
    assert preview["requiresConfirmation"] is False

    directories = {item["directory"]: item for item in preview["byDirectory"]}
    # 目录按「盘符 + 末尾两层」聚合，中间用 … 省略
    assert directories[f"{tmp_path.drive}\\…\\data\\docs"]["files"] == 2
    assert directories[f"{tmp_path.drive}\\…\\data\\pics"]["files"] == 1

    extensions = {item["extension"]: item["files"] for item in preview["byExtension"]}
    assert extensions[".pdf"] == 2
    assert extensions[".zip"] == 1


def test_preview_respects_group_ids(tmp_path, report_path):
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    preview = build_preview(load_report(report_path), _options(tmp_path / "trash"), [2]).to_dict()
    assert preview["groupCount"] == 1
    assert preview["fileCount"] == 1
    assert preview["freedBytes"] == SIZE


def test_preview_reports_skipped_non_confirmed_groups(tmp_path, data):
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    report = _write_report(
        tmp_path, [[data / "docs" / "报告.pdf", data / "docs" / "报告 - 副本.pdf"]], status="suspect"
    )
    preview = build_preview(load_report(report), _options(tmp_path / "trash")).to_dict()
    assert preview["groupCount"] == 0
    assert preview["skipped"] == [{"groupId": 1, "reason": "非确认组"}]


def test_preview_requires_confirmation_above_threshold(tmp_path):
    """超过 20 个文件必须二次确认 —— 与 CLI 的 DELETE 确认同等对待。"""
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    root = tmp_path / "many"
    root.mkdir()
    paths = [_write_file(root / f"dup-{index}.bin") for index in range(22)]
    report = _write_report(tmp_path, [paths])
    preview = build_preview(load_report(report), _options(tmp_path / "trash")).to_dict()
    assert preview["fileCount"] == 21
    assert preview["requiresConfirmation"] is True


def test_preview_rejects_partial_report(tmp_path, data):
    from dupfind.apply import ExecutionError
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    report = _write_report(
        tmp_path, [[data / "docs" / "报告.pdf", data / "docs" / "报告 - 副本.pdf"]], partial=True
    )
    with pytest.raises(ExecutionError, match="partial"):
        build_preview(load_report(report), _options(tmp_path / "trash"))


def test_preview_never_keeps_a_trashed_copy(tmp_path, data):
    """隔离区里的副本绝不能当选保留项，否则 empty 会把唯一的数据删掉。"""
    from dupfind.report import load_report
    from dupfind.web.disposal import build_preview

    trash = tmp_path / "trash"
    trashed = _write_file(trash / "C" / "old" / "报告 - 副本.pdf")
    live = data / "docs" / "报告.pdf"
    # 「报告.pdf」比「报告 - 副本.pdf」短，默认 shortest 会选中隔离区那份
    report = _write_report(tmp_path, [[live, trashed]])
    preview = build_preview(load_report(report), _options(trash)).to_dict()
    assert preview["groupCount"] == 1
    assert preview["byDirectory"], "应保留活副本、移走隔离区里的那份"


# ---------------------------------------------------------------------------
# 任务管理器
# ---------------------------------------------------------------------------


def test_task_manager_apply_runs_and_keeps_stdout_quiet(tmp_path, report_path, capsys):
    """apply 的逐组明细是给 CLI 的；Web 下必须收进缓冲区，不能写进服务端日志。"""
    from dupfind.report import load_report
    from dupfind.web.disposal import DisposalTaskManager

    manager = DisposalTaskManager()
    task = manager.start_apply(load_report(report_path), _options(tmp_path / "trash"), None)

    deadline = time.monotonic() + 20
    while task.state == "running" and time.monotonic() < deadline:
        time.sleep(0.05)

    assert task.state == "succeeded", task.error
    assert task.moved_files == 3
    assert task.moved_bytes == SIZE * 3
    assert task.failed_files == 0
    assert task.processed_groups == 2 and task.total_groups == 2
    assert "[移动]" not in capsys.readouterr().out


def test_task_manager_apply_with_nothing_to_do(tmp_path, data):
    from dupfind.report import load_report
    from dupfind.web.disposal import DisposalTaskManager

    report = _write_report(
        tmp_path, [[data / "docs" / "报告.pdf", data / "docs" / "报告 - 副本.pdf"]], status="suspect"
    )
    manager = DisposalTaskManager()
    task = manager.start_apply(load_report(report), _options(tmp_path / "trash"), None)

    deadline = time.monotonic() + 10
    while task.state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.state == "succeeded"
    assert task.message == "没有可处置的确认组"
    assert task.moved_files == 0


def test_task_manager_records_failure(tmp_path, report_path):
    """报告里的文件在执行前被删掉 → 全部跳过，任务仍要正常收尾。"""
    from dupfind.report import load_report
    from dupfind.web.disposal import DisposalTaskManager

    document = load_report(report_path)
    for group in document.groups:
        for item in group.files:
            os.unlink(item.path)

    manager = DisposalTaskManager()
    task = manager.start_apply(document, _options(tmp_path / "trash"), None)
    deadline = time.monotonic() + 10
    while task.state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.state == "succeeded"
    assert task.moved_files == 0
    assert task.skipped_files == 3
    assert manager.last() is task


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


def test_apply_preview_endpoint(client, report_path, tmp_path):
    response = client.post(
        "/api/apply/preview",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(tmp_path / "trash")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fileCount"] == 3
    assert body["keepMode"] == "shortest"
    assert body["dryRun"] is True


def test_apply_endpoint_moves_files_and_reports_progress(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    response = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert response.status_code == 200, response.text

    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["kind"] == "apply"
    assert task["movedFiles"] == 3
    assert task["processedGroups"] == task["totalGroups"] == 2

    assert (data / "docs" / "报告.pdf").is_file(), "保留项不能被移动"
    assert not (data / "docs" / "报告 - 副本.pdf").exists()
    assert list(trash.glob("manifest-*.json")), "应留下还原记录"


def test_apply_endpoint_requires_confirm_above_threshold(client, tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    paths = [_write_file(root / f"dup-{index}.bin") for index in range(22)]
    report = _write_report(tmp_path, [paths])

    response = client.post(
        "/api/apply",
        json={"reportPath": str(report), "keep": "shortest", "trash": str(tmp_path / "trash")},
    )
    assert response.status_code == 409, response.text
    assert "需要确认" in response.json()["detail"]
    assert not (tmp_path / "trash").exists(), "未确认时不得开始移动"


def test_apply_endpoint_rejects_empty_selection(client, report_path, tmp_path):
    response = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "groupIds": [], "trash": str(tmp_path / "trash")},
    )
    assert response.status_code == 400
    assert "没有可处置的文件" in response.json()["detail"]


def test_apply_endpoint_rejects_bad_keep_mode(client, report_path, tmp_path):
    response = client.post(
        "/api/apply/preview",
        json={"reportPath": str(report_path), "keep": "oldest", "trash": str(tmp_path / "trash")},
    )
    assert response.status_code == 400


def test_restore_endpoint_brings_files_back(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    applied = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    assert _wait(client, applied.json()["id"])["state"] == "succeeded"
    assert not (data / "docs" / "报告 - 副本.pdf").exists()

    restored = client.post("/api/restore", json={"trash": str(trash), "dryRun": False})
    assert restored.status_code == 200, restored.text
    task = _wait(client, restored.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["kind"] == "restore"

    assert (data / "docs" / "报告 - 副本.pdf").is_file()
    assert (data / "docs" / "报告 - 副本(2).pdf").is_file()
    assert (data / "pics" / "照片 - 副本.zip").is_file()


def test_restore_endpoint_rejects_missing_trash(client, tmp_path):
    response = client.post("/api/restore", json={"trash": str(tmp_path / "nope")})
    assert response.status_code == 404


def test_empty_endpoint_requires_confirm(client, report_path, tmp_path):
    trash = tmp_path / "trash"
    applied = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    assert _wait(client, applied.json()["id"])["state"] == "succeeded"

    refused = client.post("/api/empty", json={"trash": str(trash)})
    assert refused.status_code == 409
    assert any(path.is_file() for path in trash.rglob("*")), "未确认时不得删除任何文件"

    accepted = client.post("/api/empty", json={"trash": str(trash), "confirm": True})
    assert accepted.status_code == 200, accepted.text
    task = _wait(client, accepted.json()["id"])
    assert task["state"] == "succeeded", task
    assert not any(path.is_file() for path in trash.rglob("*") if path.suffix != ".json")


def test_empty_endpoint_reports_which_file_failed(client, report_path, tmp_path):
    """清空失败时要把具体文件和原因带回前端，只说「部分失败」用户无从处理。"""
    from dupfind.win32 import close_handle, open_exclusive

    trash = tmp_path / "trash"
    applied = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    assert _wait(client, applied.json()["id"])["state"] == "succeeded"

    payload_file = next(path for path in trash.rglob("*") if path.is_file() and path.suffix != ".json")
    handle = open_exclusive(str(payload_file))
    try:
        response = client.post("/api/empty", json={"trash": str(trash), "confirm": True})
        assert response.status_code == 200, response.text
        task = _wait(client, response.json()["id"])
    finally:
        close_handle(handle)

    assert task["state"] == "failed"
    assert payload_file.name in task["error"], "失败原因没有指明具体文件"


def test_disposal_state_endpoint_404(client):
    assert client.get("/api/disposal/nope").status_code == 404
    assert client.get("/api/disposal").status_code == 404


def test_meta_exposes_last_disposal(client, report_path, tmp_path):
    trash = tmp_path / "trash"
    applied = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    _wait(client, applied.json()["id"])

    meta = client.get("/api/meta")
    assert meta.status_code == 200
    assert meta.json()["lastDisposal"]["kind"] == "apply"


# ---------------------------------------------------------------------------
# 路径校验
# ---------------------------------------------------------------------------


def test_validate_trash_path_rejects_relative():
    from dupfind.restore import RestoreError
    from dupfind.web.disposal import validate_trash_path

    with pytest.raises(RestoreError):
        validate_trash_path("relative\\trash")


def test_default_trash_for_uses_drive_root(tmp_path):
    from dupfind.web.disposal import default_trash_for

    assert default_trash_for(str(tmp_path)).endswith("_dupfind_trash")


def test_concurrent_manifests_never_share_a_name(tmp_path):
    """同一秒内并发的 apply 必须各拿一个 manifest 名字。

    用 exists 探测再创建会撞名：两个任务共用一份快照，后保存者覆盖先保存者
    的 actions，共享的 journal 又被先完成者删掉 —— 已移走的文件从此没有还原
    记录，只剩被 empty 永久删除一条路。
    """
    import threading

    from dupfind.trash import Manifest

    trash = tmp_path / "trash"
    trash.mkdir()
    paths: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        manifest = Manifest(str(trash), "report.json")
        manifest.add(group_id=1, source=r"D:\a.txt", target=r"D:\t\a.txt", size=1)
        manifest.save()
        with lock:
            paths.append(manifest.path)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(paths) == 12
    assert len(set(paths)) == 12, "并发任务拿到了同名 manifest"
    assert len(list(trash.glob("manifest-*.json"))) == 12


def test_manifest_with_no_actions_leaves_nothing_behind(tmp_path):
    """占名用的空 journal 必须清掉，否则 restore 会多出一份空 manifest。"""
    from dupfind.trash import Manifest

    trash = tmp_path / "trash"
    trash.mkdir()
    manifest = Manifest(str(trash), "report.json")
    manifest.close()

    assert list(trash.iterdir()) == [], "空 manifest 占位文件没有被清理"


def test_trash_lock_blocks_second_task_on_same_trash(client, report_path, tmp_path):
    """同一隔离区不能同时跑两个任务（另一个可能是 CLI 或第二个 web 实例）。"""
    trash = tmp_path / "trash"
    first = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "newest", "trash": str(trash), "confirm": True},
    )
    # 第一个可能已经结束；只有在它仍在跑时才要求 409
    if _wait(client, first.json()["id"])["state"] == "running":
        assert second.status_code == 409
    else:
        assert second.status_code in (200, 409)


def test_trash_lock_is_cross_process(tmp_path):
    """锁必须是跨进程的独占句柄，不能用内存里的 threading.Lock。"""
    from dupfind.web.disposal import TrashBusy, TrashLock

    trash = tmp_path / "trash"
    trash.mkdir()
    first = TrashLock(str(trash))
    first.acquire()
    try:
        with pytest.raises(TrashBusy):
            TrashLock(str(trash)).acquire()
    finally:
        first.release()

    # 释放后可以再次获取
    second = TrashLock(str(trash))
    second.acquire()
    second.release()


def test_trash_lock_file_is_outside_the_trash(tmp_path):
    """锁文件必须在隔离区外面，否则 empty 会把它当 payload 删掉。"""
    from dupfind.web.disposal import TrashLock

    trash = tmp_path / "trash"
    trash.mkdir()
    lock = TrashLock(str(trash))
    lock.acquire()
    try:
        assert not any(path.is_file() for path in trash.rglob("*")), "锁文件落在了隔离区里"
    finally:
        lock.release()


def test_apply_reports_moved_files_even_when_it_fails_midway(tmp_path, report_path, monkeypatch):
    """中途抛异常时，已经移动的文件数必须如实上报。

    报成 0 会让用户以为什么都没发生，而文件其实躺在隔离区，随后 empty 就
    把它们永久删掉了。
    """
    from dupfind.apply import ExecutionError
    from dupfind.report import load_report
    from dupfind.web.disposal import DisposalTaskManager

    document = load_report(report_path)
    manager = DisposalTaskManager()

    import dupfind.web.disposal as disposal_module

    real_execute = disposal_module.execute_plan

    def boom(document, plan, options, **kwargs):
        # 真的先移动一个文件，再模拟「manifest 写入失败」这类中断
        stats = kwargs["stats"]
        stats.moved_files = 2
        stats.moved_bytes = SIZE * 2
        raise ExecutionError("manifest 写入失败")

    monkeypatch.setattr(disposal_module, "execute_plan", boom)
    task = manager.start_apply(document, _options(tmp_path / "trash"), None)

    deadline = time.monotonic() + 10
    while task.state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.state == "failed"
    assert task.moved_files == 2, "失败时丢失了已移动的文件数"
    assert real_execute is not boom


# ---------------------------------------------------------------------------
# 还原范围：预览 / 按组还原 / 输入校验
# ---------------------------------------------------------------------------


def _apply_all(client, report_path: Path, trash: Path) -> dict:
    response = client.post(
        "/api/apply",
        json={"reportPath": str(report_path), "keep": "shortest", "trash": str(trash), "confirm": True},
    )
    assert response.status_code == 200, response.text
    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    return task


def _preview(client, report_path: Path, group_ids: list[int] | None = None):
    params = [("reportPath", str(report_path))]
    for group_id in group_ids or []:
        params.append(("groupIds", str(group_id)))
    return client.get("/api/restore/preview", params=params)


def test_restore_preview_lists_disposed_groups(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    response = _preview(client, report_path)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["identityOk"] is True
    assert preview["totalFiles"] == 3, "两组共移走 3 个文件"
    assert preview["totalBytes"] == SIZE * 3
    assert {item["groupId"] for item in preview["groups"]} == {1, 2}
    usable = [item for item in preview["trashes"] if item["files"] > 0]
    assert len(usable) == 1
    assert os.path.normcase(usable[0]["path"]) == os.path.normcase(str(trash))


def test_restore_preview_group_filter(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    response = _preview(client, report_path, [1])
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["totalFiles"] == 2, "组 1 只移走 2 个文件"
    assert [item["groupId"] for item in preview["groups"]] == [1]


def test_restore_group_endpoint_only_restores_selected_group(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    response = client.post(
        "/api/restore",
        json={
            "trash": str(trash),
            "reportPath": str(report_path),
            "groupIds": [1],
            "dryRun": False,
        },
    )
    assert response.status_code == 200, response.text
    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["restoredFiles"] == 2
    assert task["reportPath"] == str(report_path)

    assert (data / "docs" / "报告 - 副本.pdf").is_file()
    assert (data / "docs" / "报告 - 副本(2).pdf").is_file()
    assert not (data / "pics" / "照片 - 副本.zip").exists(), "未选中的组被还原了"
    assert any(
        path.name == "照片 - 副本.zip" for path in trash.rglob("*") if path.is_file()
    ), "未选中的组应留在隔离区"

    view = client.get("/api/report", params={"path": str(report_path)}).json()
    states = {group["id"]: group["disposalState"] for group in view["groups"]}
    assert states[1] == "pending", "组级还原后该组应回到待处理"
    assert states[2] == "handled", "未还原的组必须保持已处置"


def test_restore_group_endpoint_requires_report_path(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    response = client.post(
        "/api/restore", json={"trash": str(trash), "groupIds": [1], "dryRun": False}
    )
    assert response.status_code == 400
    assert "reportPath" in response.json()["detail"]


def test_restore_group_endpoint_rejects_unknown_group(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    response = client.post(
        "/api/restore",
        json={
            "trash": str(trash),
            "reportPath": str(report_path),
            "groupIds": [999],
            "dryRun": False,
        },
    )
    assert response.status_code == 409


def test_restore_endpoint_rejects_file_as_trash(client, tmp_path):
    fake = tmp_path / "not-a-trash.bin"
    fake.write_bytes(b"x")
    response = client.post("/api/restore", json={"trash": str(fake), "dryRun": False})
    assert response.status_code == 404


def test_restore_preview_rejects_rescanned_report(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["scan"]["startedAt"] = "2026-09-09T09:09:09+08:00"
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = _preview(client, report_path)
    assert response.status_code == 409, response.text
    assert "重新扫描" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 审查回归：journal-only、旧组号、报告级范围、幂等收尾
# ---------------------------------------------------------------------------


def test_restore_preview_reports_journal_only_as_unscoped(client, report_path, data, tmp_path):
    """只剩 journal 的崩溃记录没有 report 字段，预览应归为「无法归属」而非不可读。

    这类记录不在 Web 的报告级/组级范围里（无从判断属于哪份报告），但
    ``dupfind restore <trash>`` 的整区还原仍能处理，必须提示用户走 CLI。
    """
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    journal = Path(str(manifest) + ".journal")
    journal.write_text(
        "\n".join(json.dumps(action, ensure_ascii=False) for action in payload["actions"]) + "\n",
        encoding="utf-8",
    )
    manifest.unlink()

    response = _preview(client, report_path)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["totalFiles"] == 0
    assert preview["unscopedManifests"] == 1
    assert preview["totalUnreadable"] == 0, "journal-only 记录被误报为不可读"

    # CLI 整区还原仍能把它捞回来
    from dupfind.restore import RestoreOptions, run_restore

    assert run_restore(trash, RestoreOptions(dry_run=False)) == 0
    assert (data / "docs" / "报告 - 副本.pdf").is_file()


def test_restore_preview_ignores_stale_group_from_state(client, report_path, data, tmp_path):
    """状态里残留的旧组号不属于当前报告时不能索引报错。"""
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    state_path = Path(str(report_path) + ".state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["groups"]["999"] = {
        "state": "handled",
        "handledAt": "",
        "movedFiles": 1,
        "movedBytes": SIZE,
        "manifest": str(next(trash.glob("manifest-*.json"))),
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    response = _preview(client, report_path)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert 999 in preview["staleGroupIds"]
    assert 999 not in preview["groupIds"]
    assert preview["totalFiles"] == 3


def test_restore_report_level_skips_ignored_group(client, report_path, data, tmp_path):
    """报告级还原只能动 still handled/partial 的组，不能把 ignored 组的文件搬回来。"""
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    ignored = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 1, "state": "ignored"},
    )
    assert ignored.status_code == 200, ignored.text

    response = client.post(
        "/api/restore",
        json={"trash": str(trash), "reportPath": str(report_path), "dryRun": False},
    )
    assert response.status_code == 200, response.text
    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["restoredFiles"] == 1, "只应还原仍为 handled 的组 2"
    assert not (data / "docs" / "报告 - 副本.pdf").exists(), "ignored 组的文件被还原了"
    assert (data / "pics" / "照片 - 副本.zip").is_file()


def test_restore_report_level_skips_unsafe_group(client, report_path, data, tmp_path):
    """预览标记 unsafe 的组不能被报告级执行还原。"""
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for action in payload["actions"]:
        if action["groupId"] == 1:
            action["src"] = str(tmp_path / "outside" / "moved.pdf")
            break
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    preview = _preview(client, report_path).json()
    assert preview["unsafeGroupIds"] == [1]

    response = client.post(
        "/api/restore",
        json={"trash": str(trash), "reportPath": str(report_path), "dryRun": False},
    )
    assert response.status_code == 200, response.text
    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["restoredFiles"] == 1
    assert any(
        path.name == "报告 - 副本.pdf" for path in trash.rglob("*") if path.is_file()
    ), "unsafe 组的文件被还原了"


def test_restore_group_endpoint_rejects_unsafe_group(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for action in payload["actions"]:
        if action["groupId"] == 1:
            action["src"] = str(tmp_path / "outside" / "moved.pdf")
            break
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = client.post(
        "/api/restore",
        json={
            "trash": str(trash),
            "reportPath": str(report_path),
            "groupIds": [1],
            "dryRun": False,
        },
    )
    assert response.status_code == 409


def test_restore_group_endpoint_rejects_unverifiable_manifest(
    client, report_path, data, tmp_path, monkeypatch
):
    """超过读取上限的 manifest 无法校验组归属，必须拒绝而不是静默跳过。"""
    import dupfind.web.disposal as disposal_module

    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    monkeypatch.setattr(disposal_module, "MAX_PREVIEW_MANIFEST_BYTES", 1)

    response = client.post(
        "/api/restore",
        json={
            "trash": str(trash),
            "reportPath": str(report_path),
            "groupIds": [1],
            "dryRun": False,
        },
    )
    assert response.status_code == 409


def test_restore_report_level_repairs_already_restored_groups(client, report_path, data, tmp_path):
    """文件已回到原位、manifest 仍 completed 时，Web 必须能完成状态收尾。"""
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)

    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for action in payload["actions"]:
        os.replace(action["dst"], action["src"])  # 模拟上次还原后崩溃

    preview = _preview(client, report_path).json()
    assert preview["totalFiles"] == 0
    assert preview["totalAlready"] == 3
    assert preview["missingGroupIds"] == [], "已在原处的组不能被提示成「没有找到」"

    response = client.post(
        "/api/restore",
        json={"trash": str(trash), "reportPath": str(report_path), "dryRun": False},
    )
    assert response.status_code == 200, response.text
    task = _wait(client, response.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["alreadyRestored"] == 3
    assert task["stateSynced"] is True

    view = client.get("/api/report", params={"path": str(report_path)}).json()
    assert all(group["disposalState"] == "pending" for group in view["groups"])


def test_restore_preview_accepts_custom_trash_when_state_is_lost(
    client, report_path, data, tmp_path
):
    """状态文件丢失时，预览需要一个实际隔离区才能从 manifest 回填。"""
    trash = tmp_path / "custom-trash"
    _apply_all(client, report_path, trash)
    Path(str(report_path) + ".state.json").unlink()

    without = _preview(client, report_path).json()
    assert without["totalFiles"] == 0

    with_trash = client.get(
        "/api/restore/preview",
        params=[("reportPath", str(report_path)), ("trash", str(trash))],
    )
    assert with_trash.status_code == 200, with_trash.text
    assert with_trash.json()["totalFiles"] == 3


def test_restore_empty_report_path_is_rejected(client, tmp_path):
    trash = tmp_path / "trash"
    trash.mkdir()
    response = client.post(
        "/api/restore", json={"trash": str(trash), "reportPath": "", "dryRun": False}
    )
    assert response.status_code == 422


def test_restore_preview_reports_cross_trash_group(client, report_path, data, tmp_path):
    """同一组在第二个隔离区也有动作时，预览必须标出 crossTrash。"""
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    second = tmp_path / "default-trash"
    second.mkdir()
    actions = []
    for action in payload["actions"]:
        if action["groupId"] != 1:
            continue
        source = Path(action["dst"])
        if not source.is_file():
            continue
        drive, rest = os.path.splitdrive(action["src"])
        target = second / drive[0].upper() / rest.lstrip("\\")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        actions.append(
            {
                "groupId": 1,
                "src": action["src"],
                "dst": str(target),
                "size": action["size"],
                "status": "completed",
            }
        )
    (second / "manifest-20260101-000100.json").write_text(
        json.dumps(
            {
                "version": 1,
                "createdAt": "",
                "report": str(report_path),
                "trash": str(second),
                "actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    preview = _preview(client, report_path).json()
    assert 1 in preview["crossTrashGroupIds"]
    assert preview["totalFiles"] == 5, "两个隔离区各计一遍组 1，加上组 2"


def test_restore_preview_truncated_when_manifest_limit_exceeded(
    client, report_path, data, tmp_path, monkeypatch
):
    import dupfind.web.disposal as disposal_module

    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    source_manifest = next(trash.glob("manifest-*.json"))
    for index in range(2):
        (trash / f"manifest-20260101-00020{index}.json").write_text(
            source_manifest.read_text(encoding="utf-8"), encoding="utf-8"
        )
    monkeypatch.setattr(disposal_module, "MAX_PREVIEW_MANIFESTS", 1)

    preview = _preview(client, report_path).json()
    assert preview["truncated"] is True


def test_restore_preview_flags_rejected_action(client, report_path, data, tmp_path):
    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    manifest = next(trash.glob("manifest-*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for action in payload["actions"]:
        if action["groupId"] == 1:
            action["dst"] = str(trash / "wrong-batch" / "moved.pdf")
            break
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    preview = _preview(client, report_path).json()
    assert preview["totalRejected"] == 1
    assert preview["totalFiles"] == 2, "被拒绝的动作不能计入「将还原」"


def test_restore_report_level_rejects_unverifiable_manifest(
    client, report_path, data, tmp_path, monkeypatch
):
    import dupfind.web.disposal as disposal_module

    trash = tmp_path / "trash"
    _apply_all(client, report_path, trash)
    monkeypatch.setattr(disposal_module, "MAX_PREVIEW_MANIFEST_BYTES", 1)

    response = client.post(
        "/api/restore",
        json={"trash": str(trash), "reportPath": str(report_path), "dryRun": False},
    )
    assert response.status_code == 409


def test_restore_legacy_whole_trash_rejects_overwrite(client, tmp_path):
    trash = tmp_path / "trash"
    trash.mkdir()
    response = client.post(
        "/api/restore",
        json={"trash": str(trash), "onConflict": "overwrite", "dryRun": False},
    )
    assert response.status_code == 400


def _short_path(path: Path) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 32768)
    return buffer.value if length else ""


def test_trash_lock_canonicalizes_short_names(tmp_path):
    """同一隔离区的 8.3 短名写法必须命中同一把锁。"""
    from dupfind.web.disposal import TrashBusy, TrashLock

    trash = tmp_path / "trash"
    trash.mkdir()
    short = _short_path(trash)
    if not short or os.path.normcase(short) == os.path.normcase(str(trash)):
        pytest.skip("该卷未启用 8.3 短名")

    first = TrashLock(str(trash))
    first.acquire()
    try:
        with pytest.raises(TrashBusy):
            TrashLock(short).acquire()
    finally:
        first.release()


def test_capture_is_thread_local(tmp_path):
    """捕获必须是线程局部的：redirect_stdout 会把全局 stdout 换成 StringIO。"""
    import io
    import threading

    from dupfind.console import capture, say

    real_stdout = __import__("sys").stdout
    result: dict[str, str] = {}

    def worker() -> None:
        buffer = io.StringIO()
        with capture() as captured:
            say("hidden")
        result["captured"] = captured.getvalue()
        buffer.close()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert result["captured"].strip() == "hidden"
    assert __import__("sys").stdout is real_stdout, "全局 stdout 被替换了"


def test_report_view_exposes_removal_counts(tmp_path, report_path):
    """前端 action bar 要用每种策略下真实的移动文件数，不能靠 files.length-1 猜。"""
    from dupfind.report import load_report
    from dupfind.web.report_view import build_view

    view = build_view(load_report(report_path))
    for group in view["groups"]:
        counts = group["removalCounts"]
        assert set(counts) == {"shortest", "newest"}
        assert all(value >= 1 for value in counts.values())


def test_volumes_lists_only_local_drives_quickly(tmp_path):
    """盘符列表必须走 GetDriveTypeW：断开的映射网络盘在 isdir 上会卡 20 秒+。"""
    import time

    from dupfind.web.tasks import ScanTaskManager

    manager = ScanTaskManager(tmp_path / "reports")
    started = time.monotonic()
    volumes = manager.volumes()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"列盘符耗时 {elapsed:.1f}s，疑似在探测网络盘"
    assert all(volume[1:] == ":\\" for volume in volumes)
    assert "C:\\" in volumes
