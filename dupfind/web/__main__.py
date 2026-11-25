"""启动入口：``python -m dupfind.web --port 8123``。"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
PORT_SCAN_RANGE = 20


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def select_port(host: str, preferred: int) -> int | None:
    for offset in range(PORT_SCAN_RANGE):
        candidate = preferred + offset
        if port_available(host, candidate):
            if offset:
                print(f"端口 {preferred} 已被占用，改用 {candidate}（不会影响已有服务）。")
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dupfind-web", description="dupfind 本地可视化界面（扫描、处置与还原）"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址，默认 {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--report-dir", default="", help="扫描报告目录，默认工作目录下 dupfind-reports")
    args = parser.parse_args(argv)

    try:
        import uvicorn  # noqa: PLC0415

        from .server import create_app  # noqa: PLC0415
        from .tasks import ScanTaskManager  # noqa: PLC0415
    except ImportError as exc:
        print(f"缺少 Web 依赖: {exc}\n请先安装: pip install 'dupfind[web]'", file=sys.stderr)
        return 2

    port = select_port(args.host, args.port)
    if port is None:
        print(f"端口 {args.port}-{args.port + PORT_SCAN_RANGE - 1} 均不可用，请用 --port 指定其他端口。", file=sys.stderr)
        return 2

    report_dir = Path(args.report_dir) if args.report_dir else Path.cwd() / "dupfind-reports"
    app = create_app(ScanTaskManager(report_dir))
    print(f"dupfind web 启动中：http://{args.host}:{port}")
    print(f"报告目录：{report_dir}")
    print("处置的文件先移入 <盘符>:\\_dupfind_trash，清空隔离区后才真正释放空间。")
    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
