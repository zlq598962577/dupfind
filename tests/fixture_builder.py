"""测试夹具生成器：在临时目录中构造 dupfind 验收场景所需的文件树。

所有夹具都是确定性的（相同 size+seed 生成完全相同的字节），便于断言哈希与内容。
默认文件大小 2048 字节，大于 scan 默认的 --min-size 1KB。
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

#: 默认夹具文件大小（> 1KB，确保默认 --min-size 不会把它们过滤掉）
DEFAULT_SIZE = 2048


class HardlinkNotSupported(RuntimeError):
    """当前文件系统/临时目录无法创建硬链接。"""


def content_bytes(size: int = DEFAULT_SIZE, seed: int = 0) -> bytes:
    """生成确定性内容；相同 (size, seed) 完全一致，不同 seed 从头部就不同。"""
    if size <= 0:
        return b""
    header = f"df:{seed:06d}\n".encode("ascii")
    if size <= len(header):
        return header[:size]
    pattern = bytes(((seed * 31 + i) % 256) for i in range(256))
    body_len = size - len(header)
    repeated = (pattern * (body_len // 256 + 1))[:body_len]
    return header + repeated


def _rel(directory: str, filename: str) -> str:
    return str(Path(directory) / filename) if directory else filename


def write_file(
    root: Path,
    relative: str,
    *,
    size: int = DEFAULT_SIZE,
    seed: int = 0,
    data: bytes | None = None,
    readonly: bool = False,
    mtime: float | None = None,
) -> Path:
    """在 root/relative 写入文件（自动建父目录）。"""
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content_bytes(size, seed) if data is None else data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    if readonly:
        os.chmod(path, stat.S_IREAD)
    return path


def make_copy_pair(
    root: Path,
    *,
    directory: str = "",
    stem: str = "报告",
    ext: str = ".pdf",
    copy_suffix: str = "-副本",
    size: int = DEFAULT_SIZE,
    seed: int = 0,
) -> tuple[Path, Path]:
    """同大小同内容的“原文件 + 副本”对，名字满足副本包含关系。"""
    base = write_file(root, _rel(directory, f"{stem}{ext}"), size=size, seed=seed)
    dup = write_file(
        root, _rel(directory, f"{stem}{copy_suffix}{ext}"), size=size, seed=seed
    )
    return base, dup


def make_same_size_different_content_pair(
    root: Path,
    *,
    directory: str = "",
    stem: str = "会议纪要",
    ext: str = ".docx",
    other_suffix: str = "-终版",
    size: int = DEFAULT_SIZE,
    seed_a: int = 0,
    seed_b: int = 1,
) -> tuple[Path, Path]:
    """同大小、名字包含、但内容不同的文件对（应被哈希排除）。"""
    first = write_file(root, _rel(directory, f"{stem}{ext}"), size=size, seed=seed_a)
    second = write_file(
        root, _rel(directory, f"{stem}{other_suffix}{ext}"), size=size, seed=seed_b
    )
    return first, second


def make_zero_byte_pair(
    root: Path,
    *,
    directory: str = "",
    stem: str = "empty",
    ext: str = ".txt",
    copy_suffix: str = "-副本",
) -> tuple[Path, Path]:
    """两个 0 字节的副本关系文件（必须不参与分组）。"""
    base = write_file(root, _rel(directory, f"{stem}{ext}"), size=0)
    dup = write_file(root, _rel(directory, f"{stem}{copy_suffix}{ext}"), size=0)
    return base, dup


def make_img_pair(
    root: Path,
    *,
    directory: str = "",
    size: int = DEFAULT_SIZE,
    seed: int = 0,
) -> tuple[Path, Path]:
    """img1.jpg / img12.jpg：无分隔符、词元非子集，默认不作候选。"""
    first = write_file(root, _rel(directory, "img1.jpg"), size=size, seed=seed)
    second = write_file(root, _rel(directory, "img12.jpg"), size=size, seed=seed)
    return first, second


def populate_excluded_tree(root: Path, *, size: int = DEFAULT_SIZE, seed: int = 0) -> dict[str, tuple[Path, Path]]:
    """构造“正常目录 + 黑名单目录”的重复对，返回各目录下的文件对。"""
    return {
        "keep": make_copy_pair(root, directory="keep", stem="报告单", ext=".pdf", size=size, seed=seed),
        ".git": make_copy_pair(root, directory=".git", stem="配置档", ext=".txt", size=size, seed=seed + 1),
        "node_modules": make_copy_pair(root, directory="node_modules", stem="bundle", ext=".js", size=size, seed=seed + 2),
    }


def populate_permission_tree(root: Path, *, size: int = DEFAULT_SIZE) -> dict[str, object]:
    """构造“可访问目录 + 待加 ACL 拒绝目录”的重复对。"""
    return {
        "visible": make_copy_pair(root, directory="visible", stem="公开资料", ext=".txt", size=size, seed=0),
        "locked": make_copy_pair(root, directory="locked", stem="受限资料", ext=".txt", size=size, seed=1),
        "locked_dir": Path(root) / "locked",
    }


def make_hardlink_pair(
    root: Path,
    *,
    directory: str = "",
    stem: str = "data",
    ext: str = ".bin",
    link_suffix: str = "-副本",
    size: int = DEFAULT_SIZE,
    seed: int = 0,
) -> tuple[Path, Path]:
    """创建同一文件的硬链接对；不支持时抛出 HardlinkNotSupported。"""
    source = write_file(root, _rel(directory, f"{stem}{ext}"), size=size, seed=seed)
    link = Path(root) / _rel(directory, f"{stem}{link_suffix}{ext}")
    try:
        os.link(source, link)
    except (OSError, NotImplementedError) as exc:  # 非 NTFS/权限受限等
        raise HardlinkNotSupported(f"{exc}") from exc
    return source, link


def make_readonly(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def make_writable(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def iter_files(root: Path) -> list[Path]:
    """递归列出 root 下的普通文件（跳过符号链接）。"""
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """记录文件树快照（相对路径 -> size/mtime_ns/sha256），用于断言“未被修改”。"""
    result: dict[str, tuple[int, int, str]] = {}
    for path in iter_files(root):
        st = path.stat()
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "<unreadable>"
        key = str(path.relative_to(root)).replace("\\", "/")
        result[key] = (st.st_size, st.st_mtime_ns, digest)
    return result
