"""Local-only HTTP server for the FlashDrop WebUI and JSON API."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .app import FlashDropApp
from .errors import UserError

log = logging.getLogger(__name__)
MAX_REQUEST_BODY = 4 * 1024 * 1024
STATIC_DIR = Path(__file__).with_name("web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class FlashDropHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: FlashDropApp) -> None:
        self.app = app
        super().__init__(address, FlashDropRequestHandler)


class FlashDropRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FlashDrop"
    sys_version = ""

    @property
    def app(self) -> FlashDropApp:
        server = self.server
        assert isinstance(server, FlashDropHTTPServer)
        return server.app

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/api/"):
                self._handle_get_api(parsed.path, parse_qs(parsed.query))
                return
            self._serve_static(parsed.path)
        except UserError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": exc.message, "code": exc.code})
        except Exception:  # noqa: BLE001
            log.exception("处理 GET 请求失败: %s", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务器内部错误"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                return
            body = self._read_json_body()
            self._handle_post_api(parsed.path, body)
        except UserError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": exc.message, "code": exc.code})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体不是合法 JSON"})
        except Exception:  # noqa: BLE001
            log.exception("处理 POST 请求失败: %s", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务器内部错误"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- API -----------------------------------------------------------
    def _handle_get_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.app.state())
            return
        if path == "/api/send/files":
            offset, limit = _page_args(query)
            self._send_json(HTTPStatus.OK, self.app.sender_files(offset, limit))
            return
        if path == "/api/receive/files":
            offset, limit = _page_args(query)
            self._send_json(HTTPStatus.OK, self.app.receiver_files(offset, limit))
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def _handle_post_api(self, path: str, body: dict[str, Any]) -> None:
        route = POST_ROUTES.get(path)
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        result = route(self, body)
        self._send_json(HTTPStatus.OK, result or {"ok": True})

    def _post_mode(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.set_mode(str(body.get("mode") or ""))
        return {"ok": True, "state": self.app.state()}

    def _post_send_add(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("paths")
        if isinstance(raw, str):
            paths = [line for line in raw.splitlines() if line.strip()]
        elif isinstance(raw, list):
            paths = [str(item) for item in raw if str(item).strip()]
        else:
            raise UserError("请提供要添加的文件路径")
        if not paths:
            raise UserError("请提供要添加的文件路径")
        result = self.app.sender_add_paths(paths)
        return _add_result(result)

    def _post_send_remove(self, body: dict[str, Any]) -> dict[str, Any]:
        file_id = body.get("id")
        if not isinstance(file_id, str):
            raise UserError("缺少文件 ID")
        removed = self.app.sender_remove(file_id)
        if not removed:
            raise UserError("文件不在发送列表中")
        return {"ok": True}

    def _post_send_clear(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.sender_clear()
        return {"ok": True}

    def _post_send_start(self, body: dict[str, Any]) -> dict[str, Any]:
        port = _as_int(body.get("port"), "控制端口")
        connections = _as_int(body.get("connections"), "并行连接数")
        self.app.sender_start(port, connections)
        return {"ok": True}

    def _post_send_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.sender_stop()
        return {"ok": True}

    def _post_send_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.sender_reset()
        return {"ok": True}

    def _post_send_pick_files(self, body: dict[str, Any]) -> dict[str, Any]:
        paths = self._pick_paths("files")
        if not paths:
            return {"ok": True, "canceled": True, "added": 0}
        result = self.app.sender_add_paths(paths)
        payload = _add_result(result)
        payload["canceled"] = False
        return payload

    def _post_send_pick_folder(self, body: dict[str, Any]) -> dict[str, Any]:
        paths = self._pick_paths("folder")
        if not paths:
            return {"ok": True, "canceled": True, "added": 0}
        result = self.app.sender_add_paths(paths)
        payload = _add_result(result)
        payload["canceled"] = False
        return payload

    def _post_receive_connect(self, body: dict[str, Any]) -> dict[str, Any]:
        host = body.get("host")
        if not isinstance(host, str):
            raise UserError("请填写发送方主机地址")
        port = _as_int(body.get("port"), "控制端口")
        self.app.receiver_connect(host, port)
        return {"ok": True}

    def _post_receive_set_dir(self, body: dict[str, Any]) -> dict[str, Any]:
        path = body.get("path")
        if not isinstance(path, str):
            raise UserError("请提供保存文件夹路径")
        result = self.app.receiver_set_dir(path)
        return {"ok": True, "resume": result}

    def _post_receive_choose_dir(self, body: dict[str, Any]) -> dict[str, Any]:
        paths = self._pick_paths("save-dir")
        if not paths:
            return {"ok": True, "canceled": True}
        result = self.app.receiver_set_dir(paths[0])
        return {"ok": True, "canceled": False, "path": paths[0], "resume": result}

    def _post_receive_start(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.receiver_start()
        return {"ok": True}

    def _post_receive_cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.receiver_cancel()
        return {"ok": True}

    def _post_receive_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        self.app.receiver_reset()
        return {"ok": True}

    def _pick_paths(self, kind: str) -> list[str]:
        # Mode and busy checks are delegated to the picker-using action
        # endpoints through FlashDropApp's public methods. Picking itself is
        # harmless, but the mode must be right before a dialog is launched.
        if kind == "save-dir":
            if self.app.mode != "receive":
                raise UserError("当前模式不匹配")
            # "review" is deliberately reported as busy so mode switches and
            # editing stay blocked, but it is exactly the stage in which the
            # receiver must be able to choose its save directory.
            if self.app.receiver.status != "review":
                raise UserError("请先连接发送方并进入文件确认页面，再选择保存文件夹")
        else:
            if self.app.mode != "send":
                raise UserError("当前模式不匹配")
            if self.app.sender.busy:
                raise UserError("发送端正在忙，暂时不能选择文件")
        return self.app.pick(kind)

    # -- response/static helpers --------------------------------------
    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise UserError("请求长度不合法") from exc
        if length < 0 or length > MAX_REQUEST_BODY:
            raise UserError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UserError("请求体不是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise UserError("请求体必须是对象")
        return parsed

    def _serve_static(self, path: str) -> None:
        item = STATIC_FILES.get(path)
        if item is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})
            return
        filename, content_type = item
        file_path = STATIC_DIR / filename
        try:
            payload = file_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "界面资源缺失"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )


def _add_result(result: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "added": int(getattr(result, "added", 0)),
        "skipped": int(getattr(result, "skipped", 0)),
        "warnings": list(getattr(result, "warnings", []) or []),
    }


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise UserError(f"{label}必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise UserError(f"{label}必须是整数")


def _page_args(query: dict[str, list[str]]) -> tuple[int, int]:
    def parse(name: str, default: int, minimum: int, maximum: int) -> int:
        values = query.get(name)
        if not values:
            return default
        try:
            number = int(values[0])
        except ValueError:
            return default
        return max(minimum, min(maximum, number))

    return parse("offset", 0, 0, 2**31), parse("limit", 200, 1, 500)


POST_ROUTES: dict[str, Callable[[FlashDropRequestHandler, dict[str, Any]], dict[str, Any] | None]] = {
    "/api/mode": FlashDropRequestHandler._post_mode,
    "/api/send/add": FlashDropRequestHandler._post_send_add,
    "/api/send/remove": FlashDropRequestHandler._post_send_remove,
    "/api/send/clear": FlashDropRequestHandler._post_send_clear,
    "/api/send/start": FlashDropRequestHandler._post_send_start,
    "/api/send/stop": FlashDropRequestHandler._post_send_stop,
    "/api/send/reset": FlashDropRequestHandler._post_send_reset,
    "/api/send/pick-files": FlashDropRequestHandler._post_send_pick_files,
    "/api/send/pick-folder": FlashDropRequestHandler._post_send_pick_folder,
    "/api/receive/connect": FlashDropRequestHandler._post_receive_connect,
    "/api/receive/set-dir": FlashDropRequestHandler._post_receive_set_dir,
    "/api/receive/choose-dir": FlashDropRequestHandler._post_receive_choose_dir,
    "/api/receive/start": FlashDropRequestHandler._post_receive_start,
    "/api/receive/cancel": FlashDropRequestHandler._post_receive_cancel,
    "/api/receive/reset": FlashDropRequestHandler._post_receive_reset,
}


class FlashDropWebServer:
    def __init__(self, app: FlashDropApp, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.httpd: FlashDropHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/"

    def start(self) -> str:
        self.httpd = FlashDropHTTPServer((self.host, self.port), self.app)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="flashdrop-web", daemon=True)
        self.thread.start()
        return self.url

    def serve_forever(self) -> None:
        if self.httpd is None:
            self.start()
        assert self.httpd is not None
        self.httpd.serve_forever()

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=3)
            self.thread = None
