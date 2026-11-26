"""FastAPI 应用：单页静态资源 + 扫描/浏览/处置 API。"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..apply import ApplyOptions, ExecutionError
from ..config import ConfigError, load_config
from ..ignore import IgnoreError, IgnoreList, default_ignore_path, update_ignore
from ..report import ReportError, load_report
from ..report_state import load_effective_state, set_manual_group_state
from ..restore import CONFLICT_MODES, EmptyOptions, RestoreError, RestoreOptions
from ..units import parse_size
from .disposal import (
    CONFIRM_THRESHOLD_BYTES,
    CONFIRM_THRESHOLD_FILES,
    CONFIRM_THRESHOLD_GROUPS,
    DisposalTaskManager,
    TrashBusy,
    build_preview,
    default_trash_for,
    validate_trash_path,
)
from .report_view import build_view
from .tasks import ScanTaskManager

STATIC_DIR = Path(__file__).parent / "static"
MAX_REPORT_BYTES = 256 * 1024 * 1024
KEEP_MODES = ("shortest", "newest")


class ScanPayload(BaseModel):
    root: str = Field(..., min_length=1, description="扫描根路径，如 F:\\")
    minSize: str = "1KB"
    noHash: bool = False
    contentOnly: bool = False
    maxDepth: int | None = None


class PreviewPayload(BaseModel):
    """预览请求：勾选的组 + 保留策略 + 隔离区。"""

    reportPath: str = Field(..., min_length=1)
    groupIds: list[int] | None = None
    keep: str = "shortest"
    trash: str = ""
    minSavings: str = "0"
    verifyContent: bool = True


class ApplyPayload(PreviewPayload):
    """执行请求：在预览的基础上要求 ``confirm`` 明确确认。"""

    confirm: bool = False


class RestorePayload(BaseModel):
    trash: str = Field(..., min_length=1)
    onConflict: str = "skip"
    dryRun: bool = False


class EmptyPayload(BaseModel):
    trash: str = Field(..., min_length=1)
    purgeManifests: bool = False
    confirm: bool = False


class IgnorePayload(BaseModel):
    """加入忽略清单的目录（子树语义）。"""

    path: str = Field(..., min_length=1)


class GroupStatePayload(BaseModel):
    """手动标记某组为已忽略，或恢复为待处理。"""

    reportPath: str = Field(..., min_length=1)
    groupId: int
    state: str = "ignored"


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


def _apply_options(payload: PreviewPayload) -> ApplyOptions:
    """把请求体转成 ApplyOptions，顺便校验参数。"""
    keep = payload.keep.lower()
    if keep not in KEEP_MODES:
        raise HTTPException(status_code=400, detail=f"保留策略必须是 {', '.join(KEEP_MODES)} 之一")
    try:
        min_savings = parse_size(payload.minSavings or "0")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"min-savings 无法解析: {exc}") from exc
    return ApplyOptions(
        trash_dir=payload.trash or "",
        keep_mode=keep,
        dry_run=True,
        assume_yes=True,
        min_savings=min_savings,
        verify_content=payload.verifyContent,
    )


def _resolve_trash(payload: PreviewPayload, document) -> str:
    trash = payload.trash.strip() or default_trash_for(document.root)
    try:
        return validate_trash_path(trash)
    except RestoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(
    manager: ScanTaskManager | None = None,
    disposal: DisposalTaskManager | None = None,
    ignore_path: Path | str | None = None,
) -> FastAPI:
    manager = manager or ScanTaskManager(Path.cwd() / "dupfind-reports")
    disposal = disposal or DisposalTaskManager()
    app = FastAPI(title="dupfind web", version=__version__, docs_url=None, redoc_url=None)
    app.state.manager = manager
    app.state.disposal = disposal
    app.state.ignore_path = Path(ignore_path) if ignore_path is not None else default_ignore_path()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _load_ignore() -> IgnoreList:
        try:
            return IgnoreList.load(app.state.ignore_path)
        except IgnoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
            "keepModes": list(KEEP_MODES),
            "confirmThreshold": {
                "files": CONFIRM_THRESHOLD_FILES,
                "bytes": CONFIRM_THRESHOLD_BYTES,
                "groups": CONFIRM_THRESHOLD_GROUPS,
            },
            "ignorePath": str(app.state.ignore_path),
            "lastDisposal": disposal.last().snapshot() if disposal.last() else None,
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
            ignore_dirs=_load_ignore().directories,
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
        ignore = _load_ignore()
        state = load_effective_state(document)
        return build_view(document, ignore=ignore, state=state)

    # --- 处置闭环 ---------------------------------------------------

    def _load_document(raw: str):
        report_path = _validate_report_path(raw)
        try:
            document = load_report(report_path)
        except ReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return document

    def _prepare_options(payload: PreviewPayload, document) -> ApplyOptions:
        """统一的处置参数：忽略清单与「已处置组」跳过规则在这里注入。

        预览与执行必须用同一份 options，否则会出现「预览说 100 组、执行说
        80 组」的偏差。
        """
        options = _apply_options(payload)
        options.trash_dir = _resolve_trash(payload, document)
        options.ignore = _load_ignore()
        options.skip_group_ids = load_effective_state(
            document, trash=options.trash_dir
        ).skip_ids()
        return options

    @app.post("/api/apply/preview")
    def apply_preview(payload: PreviewPayload) -> dict[str, object]:
        """执行前预览：不写任何文件。"""
        document = _load_document(payload.reportPath)
        options = _prepare_options(payload, document)
        try:
            preview = build_preview(document, options, payload.groupIds)
        except ExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return preview.to_dict()

    @app.post("/api/apply")
    def apply_start(payload: ApplyPayload) -> dict[str, object]:
        document = _load_document(payload.reportPath)
        options = _prepare_options(payload, document)
        options.verify_content = payload.verifyContent
        options.dry_run = False

        try:
            preview = build_preview(document, options, payload.groupIds)
        except ExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if preview.fileCount == 0:
            raise HTTPException(status_code=400, detail="没有可处置的文件")
        # 规模超过阈值时必须显式确认 —— 与 CLI 的 DELETE 二次确认同等对待
        if preview.requiresConfirmation and not payload.confirm:
            raise HTTPException(
                status_code=409,
                detail=f"本次将移动 {preview.fileCount} 个文件、释放 {preview.freedText}，需要确认后再执行",
            )
        try:
            task = disposal.start_apply(document, options, payload.groupIds)
        except TrashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return task.snapshot()

    @app.get("/api/disposal/{task_id}")
    def disposal_state(task_id: str) -> dict[str, object]:
        task = disposal.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task.snapshot()

    @app.post("/api/disposal/{task_id}/cancel")
    def cancel_disposal(task_id: str) -> dict[str, object]:
        """停止 apply；在下一个组边界生效，不会把一组文件拆成两半。"""
        task = disposal.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"cancelled": disposal.cancel(task_id), "task": task.snapshot()}

    @app.get("/api/disposal")
    def disposal_last() -> dict[str, object]:
        task = disposal.last()
        if task is None:
            raise HTTPException(status_code=404, detail="还没有处置任务")
        return task.snapshot()

    @app.post("/api/restore")
    def restore_start(payload: RestorePayload) -> dict[str, object]:
        mode = payload.onConflict.lower()
        if mode not in CONFLICT_MODES:
            raise HTTPException(status_code=400, detail=f"--on-conflict 必须是 {', '.join(CONFLICT_MODES)} 之一")
        try:
            target = validate_trash_path(payload.trash)
        except RestoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail=f"隔离区不存在: {target}")
        try:
            task = disposal.start_restore(target, RestoreOptions(dry_run=payload.dryRun, on_conflict=mode))
        except TrashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return task.snapshot()

    @app.post("/api/empty")
    def empty_start(payload: EmptyPayload) -> dict[str, object]:
        try:
            target = validate_trash_path(payload.trash)
        except RestoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not os.path.isdir(target):
            raise HTTPException(status_code=404, detail=f"隔离区不存在: {target}")
        # 清空是不可逆操作，必须显式确认
        if not payload.confirm:
            raise HTTPException(status_code=409, detail="清空隔离区不可恢复，需要确认后再执行")
        try:
            task = disposal.start_empty(
                target, EmptyOptions(dry_run=False, keep_manifests=not payload.purgeManifests)
            )
        except TrashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return task.snapshot()

    # --- 忽略清单与处置状态 -----------------------------------------

    @app.get("/api/ignore")
    def read_ignore() -> dict[str, object]:
        return {"path": str(app.state.ignore_path), "directories": _load_ignore().directories}

    @app.post("/api/ignore")
    def add_ignore(payload: IgnorePayload) -> dict[str, object]:
        try:
            ignore = update_ignore(app.state.ignore_path, add=payload.path)
        except IgnoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"path": str(ignore.path), "directories": ignore.directories}

    @app.delete("/api/ignore")
    def remove_ignore(path: str = Query(..., min_length=1)) -> dict[str, object]:
        try:
            ignore = update_ignore(app.state.ignore_path, remove=path)
        except IgnoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"path": str(ignore.path), "directories": ignore.directories}

    @app.post("/api/report/state")
    def set_group_state(payload: GroupStatePayload) -> dict[str, object]:
        """手动把一组标为已忽略，或恢复为待处理。"""
        state = payload.state.strip().lower()
        if state not in ("ignored", "pending"):
            raise HTTPException(status_code=400, detail="状态必须是 ignored 或 pending")
        document = _load_document(payload.reportPath)
        if not any(group.id == payload.groupId for group in document.groups):
            raise HTTPException(status_code=404, detail=f"报告中不存在第 {payload.groupId} 组")
        try:
            ok = set_manual_group_state(
                document, payload.groupId, None if state == "pending" else state
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法写入处置状态: {exc}") from exc
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="报告已被重新扫描，旧状态不再适用；请重新加载报告后再操作",
            )
        return {"groupId": payload.groupId, "state": state}

    @app.get("/api/config")
    def read_config() -> dict[str, object]:
        """暴露当前工作目录的配置摘要，让前端能提示「哪些目录会被跳过」。"""
        try:
            config = load_config(None)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "source": str(config.source) if config.source else None,
            "anyLevelDirs": list(config.any_level_dirs),
            "systemDirs": list(config.system_dirs),
            "excludeExtensions": list(config.exclude_extensions),
        }

    return app
