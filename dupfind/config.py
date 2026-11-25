"""配置：内置默认值 + 可选 dupfind.toml 覆盖。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .units import parse_size

CONFIG_FILENAME = "dupfind.toml"

SYSTEM_ROOT_DIRS: tuple[str, ...] = (
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "$Recycle.Bin",
    "System Volume Information",
    "Recovery",
    "PerfLogs",
)

ANY_LEVEL_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".gradle",
    ".m2",
    "Docker",
    "DockerDesktopWSL",
    # dupfind 自己的隔离区绝不能参与扫描：否则「活副本」和「隔离区里的副本」
    # 会被当成一组，--keep 可能选中隔离区那份，随后 empty 把两份一起删掉。
    "_dupfind_trash",
)

EXCLUDE_EXTENSIONS: tuple[str, ...] = (
    ".dll",
    ".exe",
    ".sys",
    ".msi",
    ".cab",
    ".mui",
    ".pdb",
    ".tmp",
    ".log",
    ".etl",
    ".dmp",
)

SYSTEM_ROOT_FILES: tuple[str, ...] = (
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "DumpStack.log.tmp",
)

COPY_MARKERS: tuple[str, ...] = (
    "副本",
    "副本(2)",
    "复制",
    "(1)",
    "（1）",
    " - 副本",
    "copy",
    "- copy",
    "copy(2)",
    "(2)",
    "_1",
)


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class Config:
    system_dirs: list[str] = field(default_factory=lambda: list(SYSTEM_ROOT_DIRS))
    any_level_dirs: list[str] = field(default_factory=lambda: list(ANY_LEVEL_DIRS))
    exclude_extensions: list[str] = field(default_factory=lambda: list(EXCLUDE_EXTENSIONS))
    root_files: list[str] = field(default_factory=lambda: list(SYSTEM_ROOT_FILES))
    globs: list[str] = field(default_factory=list)
    copy_markers: list[str] = field(default_factory=lambda: list(COPY_MARKERS))
    min_size: int = 1024
    sample_size: int = 4096
    threads: int = 0  # 0 = 自动（默认 4）
    page_size: int = 10
    keep_mode: str = "ask"
    source: Path | None = None


def default_config() -> Config:
    return Config()


def _as_str_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or all(isinstance(item, str) for item in value):
        return [str(item) for item in value] if isinstance(value, list) else []
    raise ConfigError(f"配置项 {key} 必须是字符串数组")


def _as_size(value: Any, key: str) -> int:
    """配置里的大小写成 ``-5`` / ``"abc"`` 时要给出配置错误，而不是崩栈。"""
    try:
        return parse_size(str(value))
    except ValueError as exc:
        raise ConfigError(f"配置项 {key}: {exc}") from exc


def _as_int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是整数: {value!r}") from exc


def load_config(explicit: Path | None = None) -> Config:
    path = explicit
    if path is None:
        candidate = Path.cwd() / CONFIG_FILENAME
        if candidate.is_file():
            path = candidate
    config = default_config()
    if path is None:
        return config
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc

    exclude = data.get("exclude", {})
    files = data.get("files", {})
    scan = data.get("scan", {})
    apply_cfg = data.get("apply", {})
    for section, name in ((exclude, "exclude"), (files, "files"), (scan, "scan"), (apply_cfg, "apply")):
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] 必须是表")

    if "systemDirs" in exclude:
        config.system_dirs = _as_str_list(exclude["systemDirs"], "exclude.systemDirs")
    if "anyLevelDirs" in exclude:
        config.any_level_dirs = _as_str_list(exclude["anyLevelDirs"], "exclude.anyLevelDirs")
    if "extensions" in exclude:
        extensions = _as_str_list(exclude["extensions"], "exclude.extensions")
        config.exclude_extensions = [item if item.startswith(".") else f".{item}" for item in extensions]
    if "rootFiles" in exclude:
        config.root_files = _as_str_list(exclude["rootFiles"], "exclude.rootFiles")
    if "globs" in exclude:
        config.globs = _as_str_list(exclude["globs"], "exclude.globs")
    if "copyMarkers" in files:
        config.copy_markers = _as_str_list(files["copyMarkers"], "files.copyMarkers")
    if "minSize" in scan:
        config.min_size = _as_size(scan["minSize"], "scan.minSize")
    if "sampleSize" in scan:
        config.sample_size = _as_size(scan["sampleSize"], "scan.sampleSize")
    if "threads" in scan:
        config.threads = _as_int(scan["threads"], "scan.threads")
    if "page" in apply_cfg:
        config.page_size = _as_int(apply_cfg["page"], "apply.page")
    if "keep" in apply_cfg:
        config.keep_mode = str(apply_cfg["keep"])

    if config.sample_size < 1:
        raise ConfigError("scan.sampleSize 必须大于 0")
    if config.threads < 0:
        raise ConfigError("scan.threads 不能为负")
    if config.page_size < 1:
        raise ConfigError("apply.page 必须大于 0")

    config.source = path
    return config


def resolve_config_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.abspath(value))
