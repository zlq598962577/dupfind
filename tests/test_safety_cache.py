"""CanonicalCache 的目录规约：隔离区判定不能因为尾分隔符或 junction 失配。"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="dupfind 仅支持 Windows")


def test_canonical_directory_matches_canonical(tmp_path):
    from dupfind.safety import CanonicalCache, canonical

    trash = tmp_path / "trash"
    trash.mkdir()
    cache = CanonicalCache()
    expected = canonical(str(trash))
    for variant in (str(trash), str(trash) + "\\", str(trash).replace("\\", "/") + "/"):
        assert cache.canonical_directory(variant) == expected


def test_is_in_trash_with_trailing_separator(tmp_path):
    """trash 带尾分隔符时 commonpath 会去掉尾分隔符，比较不能失配。"""
    from dupfind.apply import _is_in_trash
    from dupfind.safety import CanonicalCache

    trash = tmp_path / "trash"
    inside = trash / "C" / "a.bin"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    cache = CanonicalCache()

    assert _is_in_trash(str(inside), str(trash) + "\\", cache) is True
    assert _is_in_trash(str(tmp_path / "outside.bin"), str(trash) + "\\", cache) is False


def test_canonical_directory_resolves_junction_leaf(tmp_path):
    """隔离区目录本身是 junction 时必须解析到真实目标，否则隔离区副本
    不会被排除，可能被选为「保留」而把唯一活文件移走。"""
    real = tmp_path / "real-trash"
    real.mkdir()
    link = tmp_path / "trash-link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"无法创建 junction：{result.stderr.strip()}")

    from dupfind.safety import CanonicalCache, canonical

    assert CanonicalCache().canonical_directory(str(link)) == canonical(str(real))
