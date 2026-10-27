"""Thread-safe JSON control channel with liveness heartbeats."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable

from .config import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
from .protocol import ProtocolError, encode_control, recv_control

log = logging.getLogger(__name__)


class ControlChannel:
    """Owns one connected socket and dispatches non-heartbeat messages.

    ``on_close`` is invoked exactly once, including when a send from another
    thread fails. Callbacks must be fast; long operations belong on worker
    threads.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        name: str,
        on_message: Callable[[dict[str, Any]], None],
        on_close: Callable[["ControlChannel", str], None],
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
    ) -> None:
        self.sock = sock
        self.name = name
        self._on_message = on_message
        self._on_close = on_close
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._last_rx = time.monotonic()
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self.closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name=f"{name}-reader", daemon=True)
        self._heartbeat: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.sock.settimeout(None)
        except OSError:
            pass
        self._reader.start()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.name}-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()

    def send(self, message: dict[str, Any]) -> bool:
        if self._closed:
            return False
        frame = encode_control(message)
        try:
            with self._send_lock:
                if self._closed:
                    return False
                self.sock.sendall(frame)
            return True
        except (OSError, ProtocolError) as exc:
            if not isinstance(exc, ProtocolError):
                self._finish(f"发送失败: {exc}")
            return False

    def close(self, reason: str = "已关闭") -> None:
        self._finish(reason)

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self.closed.wait(timeout)

    def _read_loop(self) -> None:
        reason = "连接已关闭"
        try:
            while not self._closed:
                message = recv_control(self.sock)
                if message is None:
                    reason = "对端已断开连接"
                    break
                self._last_rx = time.monotonic()
                msg_type = message.get("type")
                if msg_type == "ping":
                    self.send({"type": "pong", "t": message.get("t", time.time())})
                    continue
                if msg_type == "pong":
                    continue
                try:
                    self._on_message(message)
                except Exception as exc:  # noqa: BLE001 - boundary for remote input
                    log.exception("控制消息处理失败 (%s): %r", self.name, message.get("type"))
                    reason = f"处理控制消息失败: {exc}"
                    break
        except (OSError, ProtocolError) as exc:
            reason = f"控制连接中断: {exc}"
        finally:
            self._finish(reason)

    def _heartbeat_loop(self) -> None:
        while not self._closed:
            if self.closed.wait(self._heartbeat_interval):
                return
            if self._closed:
                return
            if time.monotonic() - self._last_rx > self._heartbeat_timeout:
                self._finish("心跳超时")
                return
            if not self.send({"type": "ping", "t": time.time()}):
                return

    def _finish(self, reason: str) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed.set()
        try:
            self._on_close(self, reason)
        except Exception:  # noqa: BLE001
            log.exception("控制连接关闭回调失败 (%s)", self.name)
