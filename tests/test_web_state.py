"""Web 视图的分类 / 忽略清单 / 处置状态与取消接口。"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from report_fixtures import SIZE, write_file, write_report

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    for name in ("报告.pdf", "报告 - 副本.pdf", "报告 - 副本(2).pdf"):
        write_file(root / "docs" / name)
    for name in ("照片.zip", "照片 - 副本.zip"):
        write_file(root / "pics" / name)
    return root


@pytest.fixture
def report_path(tmp_path: Path, data: Path) -> Path:
    return write_report(
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


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from dupfind.web.disposal import DisposalTaskManager
    from dupfind.web.server import create_app
    from dupfind.web.tasks import ScanTaskManager

    app = create_app(
        manager=ScanTaskManager(tmp_path / "reports"),
        disposal=DisposalTaskManager(),
        # 必须指到临时目录：默认清单在工作目录，测试会污染仓库根目录
        ignore_path=tmp_path / "dupfind-ignore.json",
    )
    with TestClient(app) as test_client:
        yield test_client


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


def _view(client, report_path: Path) -> dict:
    response = client.get("/api/report", params={"path": str(report_path)})
    assert response.status_code == 200, response.text
    return response.json()


def _preview(client, report_path: Path, tmp_path: Path, **extra) -> dict:
    payload = {
        "reportPath": str(report_path),
        "keep": "shortest",
        "trash": str(tmp_path / "trash"),
    }
    payload.update(extra)
    response = client.post("/api/apply/preview", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def test_view_exposes_categories_labels_and_state(client, report_path):
    view = _view(client, report_path)
    assert view["summary"]["groups"] == 2
    assert view["summary"]["userGroups"] == 2
    assert view["summary"]["hiddenByTypeGroups"] == 0
    assert view["categoryLabels"]["program"] == "程序"
    assert "image" in view["userCategories"]
    assert {group["category"] for group in view["groups"]} == {"document", "archive"}
    assert {item["dir"] for item in view["topDirs"]} == {"data"}
    assert view["state"]["identityOk"] is True
    assert all(group["disposalState"] == "pending" for group in view["groups"])


def test_meta_exposes_ignore_path(client, tmp_path):
    meta = client.get("/api/meta").json()
    assert meta["ignorePath"] == str(tmp_path / "dupfind-ignore.json")


# ---------------------------------------------------------------------------
# 忽略清单
# ---------------------------------------------------------------------------


def test_ignore_endpoint_hides_files_and_excludes_them_from_disposal(
    client, report_path, data, tmp_path
):
    added = client.post("/api/ignore", json={"path": str(data / "docs")})
    assert added.status_code == 200, added.text
    assert added.json()["directories"] == [str(data / "docs")]

    view = _view(client, report_path)
    assert view["summary"]["groups"] == 1
    assert view["summary"]["ignoredGroups"] == 1
    assert view["summary"]["ignoredFiles"] == 3
    assert [group["id"] for group in view["groups"]] == [2]
    assert view["ignore"]["directories"][0]["path"] == str(data / "docs")
    assert view["ignore"]["directories"][0]["groups"] == 1

    preview = _preview(client, report_path, tmp_path, groupIds=[1, 2])
    assert preview["groupCount"] == 1
    assert {"reason": "组内文件位于忽略目录", "groups": 1} in preview["skippedByReason"]

    removed = client.delete("/api/ignore", params={"path": str(data / "docs")})
    assert removed.status_code == 200
    assert removed.json()["directories"] == []
    assert _view(client, report_path)["summary"]["groups"] == 2


def test_ignore_endpoint_rejects_drive_root(client):
    response = client.post("/api/ignore", json={"path": "C:\\"})
    assert response.status_code == 400


def test_ignore_endpoint_rejects_relative_path(client):
    response = client.post("/api/ignore", json={"path": "relative\\dir"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 手动状态
# ---------------------------------------------------------------------------


def test_report_state_endpoint_marks_group_ignored(client, report_path, tmp_path):
    response = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 1, "state": "ignored"},
    )
    assert response.status_code == 200, response.text

    view = _view(client, report_path)
    by_id = {group["id"]: group for group in view["groups"]}
    assert by_id[1]["disposalState"] == "ignored"
    assert view["summary"]["disposal"]["ignored"] == 1
    assert view["summary"]["savingsBytes"] == SIZE, "已忽略的组不再计入可节省空间"

    preview = _preview(client, report_path, tmp_path, groupIds=[1])
    assert preview["groupCount"] == 0
    assert preview["skippedByReason"] == [{"reason": "已处置或已忽略", "groups": 1}]

    restored = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 1, "state": "pending"},
    )
    assert restored.status_code == 200
    assert {group["id"]: group for group in _view(client, report_path)["groups"]}[1]["disposalState"] == "pending"


def test_report_state_rejects_unknown_group(client, report_path):
    response = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 999, "state": "ignored"},
    )
    assert response.status_code == 404


def test_report_state_rejects_unknown_state(client, report_path):
    response = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 1, "state": "handled"},
    )
    assert response.status_code == 400


def test_report_state_rebuilds_state_file_from_another_scan(client, report_path):
    """旧扫描留下的状态文件不能让人永久 409：标记应覆盖为新身份。"""
    from dupfind.report_state import state_path_for

    state_path_for(report_path).write_text(
        json.dumps(
            {
                "version": 1,
                "identity": {
                    "root": r"Z:\\",
                    "startedAt": "2000-01-01T00:00:00+08:00",
                    "filesScanned": 0,
                    "groups": 0,
                },
                "groups": {},
            }
        ),
        encoding="utf-8",
    )
    response = client.post(
        "/api/report/state",
        json={"reportPath": str(report_path), "groupId": 1, "state": "ignored"},
    )
    assert response.status_code == 200, response.text

    view = _view(client, report_path)
    assert view["state"]["identityOk"] is True
    by_id = {group["id"]: group for group in view["groups"]}
    assert by_id[1]["disposalState"] == "ignored"


# ---------------------------------------------------------------------------
# 处置后状态回写
# ---------------------------------------------------------------------------


def test_apply_marks_handled_and_refreshed_view_keeps_it(client, report_path, tmp_path):
    started = client.post(
        "/api/apply",
        json={
            "reportPath": str(report_path),
            "groupIds": [1],
            "keep": "shortest",
            "trash": str(tmp_path / "trash"),
            "confirm": True,
        },
    )
    assert started.status_code == 200, started.text
    task = _wait(client, started.json()["id"])
    assert task["state"] == "succeeded", task
    assert task["stateWritten"] is True
    assert task["handledGroupIds"] == [1]
    assert task["partialGroupIds"] == []

    view = _view(client, report_path)
    by_id = {group["id"]: group for group in view["groups"]}
    assert by_id[1]["disposalState"] == "handled"
    assert by_id[1]["disposal"]["movedFiles"] == 2
    assert by_id[2]["disposalState"] == "pending"
    assert view["summary"]["disposal"]["handled"] == 1
    assert view["summary"]["disposal"]["pending"] == 1
    assert view["summary"]["savingsBytes"] == SIZE, "已处置的组不再计入可节省空间"
    # 目录面板必须与顶部汇总同口径，否则清完一个目录后它还显示几 GB 可节省
    assert {item["dir"]: item["savingsBytes"] for item in view["topDirs"]} == {"data": SIZE}

    preview = _preview(client, report_path, tmp_path, groupIds=[1])
    assert preview["groupCount"] == 0
    assert preview["skippedByReason"] == [{"reason": "已处置或已忽略", "groups": 1}]


def test_cancel_endpoint_stops_a_running_apply(client, report_path, tmp_path, monkeypatch):
    """停止只在组边界生效；任务状态必须变成 cancelled 并如实收尾。"""
    import dupfind.web.disposal as disposal_module

    started = threading.Event()

    def blocking_execute(document, plan, options, **kwargs):
        started.set()
        stop = kwargs["should_stop"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not stop():
            time.sleep(0.01)
        stats = kwargs["stats"]
        if stop():
            stats.cancelled = True
        return stats

    monkeypatch.setattr(disposal_module, "execute_plan", blocking_execute)

    response = client.post(
        "/api/apply",
        json={
            "reportPath": str(report_path),
            "keep": "shortest",
            "trash": str(tmp_path / "trash"),
            "confirm": True,
        },
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    assert started.wait(5), "后台任务没有开始"

    cancelled = client.post(f"/api/disposal/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True

    task = _wait(client, task_id)
    assert task["state"] == "cancelled"
    assert task["message"] and "已停止" in task["message"]

    again = client.post(f"/api/disposal/{task_id}/cancel")
    assert again.status_code == 200
    assert again.json()["cancelled"] is False


def test_preview_reports_missing_and_estimated_fields(client, report_path, tmp_path):
    preview = _preview(client, report_path, tmp_path, verifyContent=False)
    assert preview["missingFiles"] == 0
    assert "estimatedSeconds" in preview
    assert preview["skippedByReason"] == []
