"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

from . import __version__
from .app import FlashDropApp
from .config import DEFAULT_UI_PORT
from .web import FlashDropWebServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashdrop",
        description="FlashDrop — 局域网点对点文件快传",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_UI_PORT,
        help=f"本地 WebUI 端口（默认 {DEFAULT_UI_PORT}；被占用时自动改用空闲端口）",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"FlashDrop {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = FlashDropApp()
    server = FlashDropWebServer(app, host="127.0.0.1", port=args.port)
    try:
        url = server.start()
    except OSError:
        # A different local tool may own the conventional port. The WebUI is
        # only served on loopback, so falling back to an ephemeral port is safe.
        logging.getLogger(__name__).info("端口 %s 被占用，改用空闲端口", args.port)
        server = FlashDropWebServer(app, host="127.0.0.1", port=0)
        url = server.start()

    print("", flush=True)
    print(f"  FlashDrop {__version__}  —  局域网点对点快传", flush=True)
    print(f"  本机控制台: {url}", flush=True)
    print("  请保持此窗口开启；按 Ctrl+C 退出。", flush=True)
    print("", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    stop_event = threading.Event()
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n正在退出 FlashDrop…")
    finally:
        app.shutdown()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
