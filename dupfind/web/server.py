"""FastAPI 应用：单页静态资源 + 只读 JSON API。"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..report import ReportError, load_report
from ..units import parse_size
from .report_view import build_view
from .tasks import ScanTaskManager

STATIC_DIR = Path(__file__).parent / "static"
MAX_REPORT_BYTES = 256 * 1024 * 1024


class ScanPayload(BaseModel):
    root: str = Field(..., min_length=1, description="扫描根路径，如 F:\\")
    minSize: str = "1KB"
    noHash: bool = False
    contentOnly: bool = False
    maxDepth: int | None = None


def _validate_root(root: str) -> str:
    absolute = os.path.abspath(root)
    drive, _ = ntpath.splitdrive(absolute)
    if not drive or not drive.endswith(":") or not os.path.isdir(absolute):
        raise HTTPException(status_code=400, detail=f"根路径必须是存在的本地盘符路径: {root}")
    return absolute


def _validate_report_path(raw: str) -> Path:
    path = Path(os.path.abspath(raw))
    if path.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="报告必须是 .json 文件")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"报告不存在: {path}")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise HTTPException(status_code=400, detail="报告超过 256MB，拒绝加载")
    return path


def create_app(manager: ScanTaskManager | None = None) -> FastAPI:
    manager = manager or ScanTaskManager(Path.cwd() / "dupfind-reports")
    app = FastAPI(title="dupfind web", version=__version__, docs_url=None, redoc_url=None)
    app.state.manager = manager
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/meta")
    def meta() -> dict[str, object]:
        return {
            "version": __version__,
            "volumes": manager.volumes(),
            "reportDir": str(manager.report_dir),
            "lastReport": manager.last_report(),
            "defaults": {"minSize": "1KB", "noHash": False, "contentOnly": False},
            "keepModes": ["shortest", "newest"],
        }

    @app.post("/api/scan")
    def start_scan(payload: ScanPayload) -> dict[str, object]:
        root = _validate_root(payload.root)
        try:
            min_size = parse_size(payload.minSize)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"min-size 无法解析: {exc}") from exc
        if min_size < 0:
            raise HTTPException(status_code=400, detail="min-size 不能为负")
        if payload.maxDepth is not None and payload.maxDepth < 0:
            raise HTTPException(status_code=400, detail="max-depth 不能为负")
        task = manager.start(
            root=root,
            min_size=min_size,
            no_hash=payload.noHash,
            content_only=payload.contentOnly,
            max_depth=payload.maxDepth,
        )
        return task.snapshot()

    @app.get("/api/scan/{task_id}")
    def scan_state(task_id: str) -> dict[str, object]:
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task.snapshot()

    @app.post("/api/scan/{task_id}/cancel")
    def cancel_scan(task_id: str) -> dict[str, object]:
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        cancelled = manager.cancel(task_id)
        return {"cancelled": cancelled, "task": task.snapshot()}

    @app.get("/api/report")
    def read_report(path: str = Query(..., min_length=1)) -> dict[str, object]:
        report_path = _validate_report_path(path)
        try:
            document = load_report(report_path)
        except ReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return build_view(document)

    return app
