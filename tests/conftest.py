"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from harness import WORKSPACE_ROOT, discover_cli, DupfindCli  # noqa: E402


@pytest.fixture(scope="session")
def workspace_root() -> Path:
    return WORKSPACE_ROOT


@pytest.fixture(scope="session")
def cli(workspace_root: Path) -> DupfindCli:
    """返回可运行的 dupfind 入口；实现未就绪时跳过全部黑盒用例。"""
    found = discover_cli(workspace_root)
    if found is None:
        pytest.skip(
            "未找到可运行的 dupfind CLI（已尝试 DUPFIND_BIN、python -m dupfind、"
            "src/dupfind/{__main__,main,cli}.py、PATH 中的 dupfind）。"
            "实现就绪后直接重跑；或将 DUPFIND_BIN 指向入口。"
        )
    return found


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """每个用例独立的数据根目录。"""
    root = tmp_path / "data"
    root.mkdir()
    return root
