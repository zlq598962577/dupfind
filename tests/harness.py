"""dupfind 黑盒测试工具：CLI 发现/调用、报告读取、trash 与 manifest 断言。

CLI 发现顺序（任一探测成功即用）：
1. 环境变量 ``DUPFIND_BIN``（可执行文件，或 .py 脚本）
2. ``python -m dupfind``（支持 src/ 布局与扁平布局）
3. 常见入口脚本：src/dupfind/__main__.py、src/dupfind/main.py、src/dupfind/cli.py 等
4. PATH 中的 ``dupfind`` 控制台脚本

apply 的非 dry-run 开关在规格中没有显式命名，这里默认约定 ``--no-dry-run``；
实现若采用其他拼写，可用环境变量 ``DUPFIND_COMMIT_FLAGS`` 覆盖（空格分隔）。
"""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

#: apply 关闭 dry-run 的附加参数（规格未显式命名，默认按 Typer 的 --dry-run/--no-dry-run 约定）
COMMIT_FLAGS: tuple[str, ...] = tuple(
    os.environ.get("DUPFIND_COMMIT_FLAGS", "--no-dry-run").split()
)


@dataclass(frozen=True)
class CliResult:
    """一次 CLI 调用的结果。"""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    def debug(self, limit: int = 3000) -> str:
        cmd = " ".join(self.command)
        return (
            f"$ {cmd}\n"
            f"exit={self.returncode}\n"
            f"--- stdout ---\n{self.stdout[-limit:]}\n"
            f"--- stderr ---\n{self.stderr[-limit:]}"
        )


@dataclass(frozen=True)
class DupfindCli:
    """已发现的 dupfind 入口。"""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    origin: str

    def run(self, *args: Any, timeout: int = 180, stdin: str = "") -> CliResult:
        full = (*self.command, *(str(a) for a in args))
        try:
            proc = subprocess.run(
                list(full),
                cwd=str(self.cwd),
                env=self.env,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"dupfind 命令超时（{timeout}s，可能进入了交互等待）：{' '.join(full)}"
            ) from exc
        return CliResult(full, proc.returncode, proc.stdout or "", proc.stderr or "")


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _with_pythonpath(*paths: Path | str) -> dict[str, str]:
    env = _base_env()
    parts = [str(p) for p in paths if p]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _probe(candidate: DupfindCli) -> bool:
    try:
        result = candidate.run("--help", timeout=30)
    except (OSError, AssertionError):
        return False
    if result.returncode != 0:
        return False
    text = result.output.lower()
    return bool(text.strip()) and ("usage" in text or "dupfind" in text or "options" in text)


def discover_cli(root: Path = WORKSPACE_ROOT) -> DupfindCli | None:
    """探测可运行的 dupfind CLI；未就绪返回 None（测试将 skip）。"""
    root = Path(root).resolve()
    src = root / "src"
    candidates: list[DupfindCli] = []

    bin_env = os.environ.get("DUPFIND_BIN", "").strip()
    if bin_env:
        bin_path = Path(bin_env)
        command = (
            (sys.executable, str(bin_path))
            if bin_path.suffix.lower() in {".py", ".pyw"}
            else (str(bin_path),)
        )
        candidates.append(
            DupfindCli(
                command, root, _with_pythonpath(src, root), f"DUPFIND_BIN={bin_env}"
            )
        )

    candidates.append(
        DupfindCli(
            (sys.executable, "-m", "dupfind"),
            root,
            _with_pythonpath(src, root),
            "python -m dupfind（src 布局）",
        )
    )
    candidates.append(
        DupfindCli(
            (sys.executable, "-m", "dupfind"),
            root,
            _with_pythonpath(root),
            "python -m dupfind（扁平布局）",
        )
    )
    for relative in (
        "src/dupfind/__main__.py",
        "src/dupfind/main.py",
        "src/dupfind/cli.py",
        "src/main.py",
        "dupfind.py",
    ):
        entry = root / relative
        if entry.is_file():
            candidates.append(
                DupfindCli(
                    (sys.executable, str(entry)),
                    root,
                    _with_pythonpath(src, root),
                    f"python {relative}",
                )
            )

    console = shutil.which("dupfind")
    if console:
        candidates.append(
            DupfindCli((console,), root, _base_env(), "PATH 中的 dupfind 控制台脚本")
        )

    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if candidate.command in seen:
            continue
        seen.add(candidate.command)
        if _probe(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# 命令封装
# ---------------------------------------------------------------------------


def scan(
    cli: DupfindCli,
    root: Path,
    out: Path,
    *flags: Any,
    timeout: int = 180,
    stdin: str = "",
) -> CliResult:
    return cli.run(
        "scan", str(root), "--out", str(out), *flags, timeout=timeout, stdin=stdin
    )


def apply_report(
    cli: DupfindCli,
    report: Path,
    *flags: Any,
    timeout: int = 180,
    stdin: str = "",
) -> CliResult:
    return cli.run(
        "apply", str(report), *flags, timeout=timeout, stdin=stdin
    )


# ---------------------------------------------------------------------------
# 报告断言
# ---------------------------------------------------------------------------


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"报告应为 JSON 对象: {path}"
    assert data.get("version") == 1, f"报告 version 必须为 1，实际 {data.get('version')!r}"
    assert "scan" in data and "groups" in data, "报告缺少 scan/groups 字段"
    return data


def win_norm(path: Path | str) -> str:
    """Windows 路径比较用的规范化形式（大小写不敏感、统一分隔符）。"""
    return ntpath.normcase(ntpath.normpath(str(path)))


def group_paths(group: dict[str, Any]) -> list[str]:
    return [str(item.get("path", "")) for item in group.get("files", [])]


def paths_in_report(report: dict[str, Any]) -> list[str]:
    return [str(item["path"]) for group in report.get("groups", []) for item in group.get("files", [])]


def find_groups_with_paths(report: dict[str, Any], *paths: Path | str) -> list[dict[str, Any]]:
    """返回包含任一给定路径的分组。"""
    targets = {win_norm(p) for p in paths}
    found = []
    for group in report.get("groups", []):
        if targets & {win_norm(p) for p in group_paths(group)}:
            found.append(group)
    return found


def total_savings(report: dict[str, Any]) -> int:
    return sum(int(group.get("savingsBytes", 0)) for group in report.get("groups", []))


def assert_sorted_by_savings(report: dict[str, Any]) -> None:
    savings = [int(group.get("savingsBytes", 0)) for group in report.get("groups", [])]
    assert savings == sorted(savings, reverse=True), f"groups 未按 savingsBytes 降序: {savings}"


def assert_no_path_escape(report: dict[str, Any]) -> None:
    """报告中的路径必须是绝对路径，且任何片段都不能是 ``..``。"""
    scan_meta = report.get("scan", {}) or {}
    paths = ([scan_meta["root"]] if scan_meta.get("root") else []) + paths_in_report(report)
    for raw in paths:
        text = str(raw).replace("/", "\\")
        assert text, "报告中出现空路径"
        assert ntpath.isabs(text), f"报告路径应为绝对路径: {text}"
        assert ".." not in text.split("\\"), f"报告路径包含 .. 逃逸片段: {text}"


# ---------------------------------------------------------------------------
# trash / manifest 断言
# ---------------------------------------------------------------------------


def path_forms(path: Path | str) -> set[str]:
    """同一路径的多种可接受写法（绝对、去掉盘符冒号、仅相对尾部）。"""
    normalized = ntpath.normpath(str(path))
    forms = {win_norm(normalized), normalized.replace("/", "\\").lower()}
    drive, tail = ntpath.splitdrive(normalized)
    token = drive.rstrip(":\\/")
    parts = [part for part in tail.replace("/", "\\").split("\\") if part]
    if token:
        forms.add("\\".join([token] + parts).lower())
        forms.add("\\".join([token + ":"] + parts).lower())
    forms.add("\\".join(parts).lower())
    return {form for form in forms if form}


def find_mirrored_file(trash: Path, source: Path | str) -> Path | None:
    """按 <trash>/<盘符>/<原相对路径> 规则查找被移动的文件。"""
    trash = Path(trash)
    if not trash.exists():
        return None
    expected = path_forms(source)
    for candidate in sorted(p for p in trash.rglob("*") if p.is_file()):
        relative = str(candidate.relative_to(trash)).replace("/", "\\").lower()
        if relative in expected:
            return candidate
    return None


def files_under(root: Path) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def find_manifests(trash: Path) -> list[Path]:
    trash = Path(trash)
    if not trash.exists():
        return []
    return sorted(trash.rglob("manifest-*.json"))


def iter_json_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_json_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from iter_json_strings(value)


def _normalized_json_strings(path: Path) -> set[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    result: set[str] = set()
    for text in iter_json_strings(data):
        if not text.strip():
            continue
        result.add(text.replace("/", "\\").lower())
        result.add(win_norm(text))
        result.add(ntpath.normpath(text).replace("/", "\\").lower())
    return result


def manifest_covers(manifests: list[Path], source: Path | str, destination: Path | str) -> bool:
    """manifest 中同时出现源路径与目标路径，则视为记录了该动作。"""
    want_source = path_forms(source) | {win_norm(source), str(source).replace("/", "\\").lower()}
    want_destination = path_forms(destination)
    source_seen = destination_seen = False
    for manifest in manifests:
        strings = _normalized_json_strings(manifest)
        if want_source & strings:
            source_seen = True
        if want_destination & strings:
            destination_seen = True
    return source_seen and destination_seen
