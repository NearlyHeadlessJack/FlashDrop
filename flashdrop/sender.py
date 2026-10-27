"""Sender side: listen on a control port plus parallel data ports."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any

from .catalog import CatalogError, ManifestFile, SenderCatalog, prepare_manifest
from .config import (
    APP_PROTOCOL_NAME,
    CONTROL_HANDSHAKE_TIMEOUT,
    DATA_CONNECT_TIMEOUT,
    DEFAULT_CHUNK_SIZE,
    MAX_CONNECTIONS,
    MIN_CONNECTIONS,
    PROGRESS_INTERVAL,
)
from .control import ControlChannel
from .errors import UserError
from .metrics import ProgressTracker
from .netutil import configure_socket, display_addresses, listener, open_data_listeners
from .protocol import (
    DATA_HEADER,
    DATA_MAGIC,
    ProtocolError,
    format_socket_peer,
    id_to_bytes,
    recv_control,
    send_control,
)
from . import __version__

log = logging.getLogger(__name__)

BUSY_STATUSES = {"preparing", "listening", "negotiating", "starting", "transferring", "finishing"}
TERMINAL_STATUSES = {"completed", "error", "stopped", "cancelled"}


@dataclass(frozen=True, slots=True)
class PlannedFile:
    id: str
    source: str
    rel_path: str
    size: int
    mtime_ns: int
    quick_hash: str
    resume_offset: int

    @property
    def id_bytes(self) -> bytes:
        return id_to_bytes(self.id)


@dataclass(frozen=True, slots=True)
class ChunkJob:
    file: PlannedFile
    offset: int
    length: int


class SendTransfer:
    """Coordinates chunk scheduling and sender-side data connections."""

    def __init__(self, service: "SendService", plan: list[PlannedFile], token: str) -> None:
        self.service = service
        self.plan = plan
        self.token = token
        self.chunk_size = DEFAULT_CHUNK_SIZE
        self._lock = threading.RLock()
        self._channel_ready = threading.Condition(self._lock)
        self._channels: list[socket.socket] = []
        self._cursor_index = 0
        self._cursor_offset = 0
        self._sent = 0
        self._error: str | None = None
        self._workers: list[threading.Thread] = []
        self._supervisor: threading.Thread | None = None
        self._started = False
        self._inflight = 0
        self._window = 4
        self.abort_event = threading.Event()
        self.accept_stop = threading.Event()
        self.finished = threading.Event()
        self.started_at = time.monotonic()
        self.connected = 0

    # -- setup ---------------------------------------------------------
    def register_data_channel(self, sock: socket.socket) -> bool:
        with self._lock:
            if self.abort_event.is_set() or self._started:
                return False
            self._channels.append(sock)
            self.connected = len(self._channels)
            self._channel_ready.notify_all()
            return True

    def unregister_data_channel(self, sock: socket.socket) -> None:
        with self._lock:
            if sock in self._channels:
                self._channels.remove(sock)
                self.connected = len(self._channels)
            self._channel_ready.notify_all()

    def mark_data_ready(self) -> None:
        self.accept_stop.set()

    def wait_channels(self, expected: int, timeout: float = 3.0) -> int:
        deadline = time.monotonic() + timeout
        with self._lock:
            while len(self._channels) < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._channel_ready.wait(remaining)
            return len(self._channels)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            channels = list(self._channels)
            # Bound the number of chunks in-flight. This keeps receiver-side
            # reordering memory bounded when one data connection is slower
            # than the others, without serializing the whole transfer.
            self._window = max(len(channels), min(64, len(channels) * 4))
            self.started_at = time.monotonic()
        if not channels:
            self._fail("没有可用的数据连接")
            return
        for index, sock in enumerate(channels):
            worker = threading.Thread(
                target=self._worker,
                args=(sock, index),
                name=f"send-data-{index}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()
        self._supervisor = threading.Thread(target=self._supervise, name="send-data-supervisor", daemon=True)
        self._supervisor.start()

    # -- scheduling ----------------------------------------------------
    def next_job(self) -> ChunkJob | None:
        with self._lock:
            while (
                not self.abort_event.is_set()
                and self._inflight >= self._window
            ):
                self._channel_ready.wait(0.5)
            if self.abort_event.is_set():
                return None
            if self._cursor_index < len(self.plan):
                first = self.plan[self._cursor_index]
                if self._cursor_offset < first.resume_offset:
                    self._cursor_offset = first.resume_offset
            while self._cursor_index < len(self.plan):
                current = self.plan[self._cursor_index]
                if self._cursor_offset >= current.size:
                    self._cursor_index += 1
                    if self._cursor_index < len(self.plan):
                        self._cursor_offset = self.plan[self._cursor_index].resume_offset
                    else:
                        self._cursor_offset = 0
                    continue
                offset = self._cursor_offset
                length = min(self.chunk_size, current.size - offset)
                self._cursor_offset += length
                self._inflight += 1
                return ChunkJob(file=current, offset=offset, length=length)
            return None

    def job_finished(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._channel_ready.notify_all()

    def _worker(self, sock: socket.socket, index: int) -> None:
        current_id: str | None = None
        handle = None
        try:
            sock.settimeout(None)
            while not self.abort_event.is_set():
                job = self.next_job()
                if job is None:
                    break
                try:
                    if job.file.id != current_id:
                        if handle is not None:
                            handle.close()
                            handle = None
                        self._check_source(job.file)
                        handle = open(job.file.source, "rb")
                        current_id = job.file.id
                    assert handle is not None
                    handle.seek(job.offset)
                    data = handle.read(job.length)
                    if len(data) != job.length:
                        raise OSError(f"源文件读取不完整: {job.file.rel_path}")
                    frame = DATA_HEADER.pack(
                        DATA_MAGIC,
                        job.file.id_bytes,
                        job.offset,
                        len(data),
                        zlib.crc32(data) & 0xFFFFFFFF,
                    )
                    sock.sendall(frame + data)
                    with self._lock:
                        self._sent += len(data)
                finally:
                    self.job_finished()
        except (OSError, ValueError) as exc:
            if not self.abort_event.is_set():
                self._fail(f"数据连接 {index + 1} 中断: {exc}")
        finally:
            try:
                handle.close()  # type: ignore[union-attr]
            except (AttributeError, OSError):
                pass
            self._close_socket(sock)

    @staticmethod
    def _check_source(item: PlannedFile) -> None:
        stat = os.stat(item.source)
        if stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
            raise OSError(f"源文件在传输期间发生变化: {item.rel_path}")

    def _supervise(self) -> None:
        for worker in self._workers:
            worker.join()
        # Workers close their own sockets; shutting down again is harmless.
        with self._lock:
            channels = list(self._channels)
            error = self._error
        if error is None:
            # A source edited in place while it was being read can still have
            # the original size/mtime at open time. Re-check every file that
            # contributed new bytes before telling the receiver it is done.
            for item in self.plan:
                if item.resume_offset >= item.size:
                    continue
                try:
                    stat = os.stat(item.source)
                    if stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
                        error = f"源文件在传输期间发生变化: {item.rel_path}"
                        break
                except OSError as exc:
                    error = f"无法再次读取源文件 {item.rel_path}: {exc}"
                    break
        for sock in channels:
            self._close_socket(sock)
        self.finished.set()
        if error:
            self.service._on_transfer_error(error)
        else:
            self.service._on_all_sent()

    def sent(self) -> int:
        with self._lock:
            return self._sent

    def error(self) -> str | None:
        with self._lock:
            return self._error

    def connections(self) -> int:
        with self._lock:
            return len(self._channels)

    def _fail(self, reason: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = reason
            channels = list(self._channels)
            self._channel_ready.notify_all()
        self.abort_event.set()
        self.accept_stop.set()
        for sock in channels:
            self._close_socket(sock)

    def abort(self, reason: str) -> None:
        self._fail(reason)

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class SendService:
    """Owns the listening lifecycle and exactly one active receiver session."""

    def __init__(self, catalog: SenderCatalog) -> None:
        self.catalog = catalog
        self.lock = threading.RLock()
        self.status = "idle"
        self.error: str | None = None
        self.notice: str | None = None
        self.peer: str | None = None
        self.manifest: list[ManifestFile] = []
        self.plan: list[PlannedFile] = []
        self.notice_warnings: list[str] = []
        self._requested_port = 0
        self._requested_connections = 0
        self._bound_port: int | None = None
        self._data_ports: list[int] = []
        self._addresses: list[str] = []
        self._session_id: str | None = None
        self._data_token: str | None = None
        self._resume_files = 0
        self._resume_bytes = 0
        self._connections_used = 0
        self._completed_payload: dict[str, Any] | None = None
        self._control: ControlChannel | None = None
        self._control_listener: socket.socket | None = None
        self._data_listeners: list[socket.socket] = []
        self._data_accept_threads: list[threading.Thread] = []
        self._transfer: SendTransfer | None = None
        self._run_thread: threading.Thread | None = None
        self._progress_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._close_reason = ""
        self._progress = ProgressTracker()
        self._manifest_total = 0

    # -- public lifecycle ---------------------------------------------
    @property
    def busy(self) -> bool:
        return self.status in BUSY_STATUSES

    def start(self, port: int, connections: int) -> None:
        files = self.catalog.snapshot()
        if not files:
            raise UserError("请先选择要发送的文件")
        if not isinstance(port, int) or isinstance(port, bool) or not (0 <= port <= 65535):
            raise UserError("控制端口必须在 0–65535 之间")
        if not isinstance(connections, int) or not (MIN_CONNECTIONS <= connections <= MAX_CONNECTIONS):
            raise UserError(f"并行连接数必须在 {MIN_CONNECTIONS}–{MAX_CONNECTIONS} 之间")
        with self.lock:
            if self.busy:
                raise UserError("发送服务已在运行，请先停止")
            if self._run_thread is not None and self._run_thread.is_alive():
                raise UserError("上一轮发送正在收尾，请稍后重试")
            self.status = "preparing"
            self.error = None
            self.notice = None
            self.peer = None
            self.manifest = []
            self.plan = []
            self._requested_port = port
            self._requested_connections = connections
            self._bound_port = None
            self._data_ports = []
            self._addresses = []
            self._session_id = None
            self._data_token = None
            self._resume_files = 0
            self._resume_bytes = 0
            self._connections_used = 0
            self._completed_payload = None
            self._manifest_total = 0
            self._progress = ProgressTracker()
            self._stop_event = threading.Event()
            self._close_reason = ""
            self._control = None
            self._transfer = None
            self._control_listener = None
            self._data_listeners = []
            self._data_accept_threads = []
            self._run_thread = threading.Thread(
                target=self._run,
                args=(files,),
                name="send-service",
                daemon=True,
            )
            self._run_thread.start()

    def stop(self) -> None:
        with self.lock:
            if self.status in {"idle", "stopped"}:
                return
            if self.status not in TERMINAL_STATUSES:
                self.status = "stopped"
                self.notice = "发送已停止"
            channel = self._control
            transfer = self._transfer
            listener_sock = self._control_listener
            self._stop_event.set()
            self._control = None
        if channel is not None:
            channel.send({"type": "aborted", "message": "发送方已停止"})
            channel.close("发送方已停止")
        if transfer is not None:
            transfer.abort("发送方已停止")
        self._close_socket(listener_sock)
        if self.status in {"stopped"}:
            self._close_data_listeners()

    def reset(self) -> None:
        with self.lock:
            if self.busy:
                raise UserError("传输进行中，不能重置")
            run_thread = self._run_thread
        if run_thread is not None and run_thread.is_alive():
            run_thread.join(2.5)
        with self.lock:
            if run_thread is not None and run_thread.is_alive():
                raise UserError("发送服务正在收尾，请稍后重试")
            leftover = [self._control_listener, *self._data_listeners]
            self._control_listener = None
            self._data_listeners = []
            self.status = "idle"
            self.error = None
            self.notice = None
            self.peer = None
            self.manifest = []
            self.plan = []
            self._bound_port = None
            self._data_ports = []
            self._addresses = []
            self._session_id = None
            self._data_token = None
            self._resume_files = 0
            self._resume_bytes = 0
            self._connections_used = 0
            self._completed_payload = None
            self._manifest_total = 0
            self._progress = ProgressTracker()
            self._stop_event.set()
            self._control = None
            self._transfer = None
        for sock in leftover:
            self._close_socket(sock)

    def add_paths(self, paths: list[str]) -> Any:
        with self.lock:
            if self.busy:
                raise UserError("请先停止当前的发送服务再修改文件")
            return self.catalog.add_paths(paths)

    def remove(self, file_id: str) -> bool:
        with self.lock:
            if self.busy:
                raise UserError("请先停止当前的发送服务再修改文件")
            return self.catalog.remove(file_id)

    def clear(self) -> None:
        with self.lock:
            if self.busy:
                raise UserError("请先停止当前的发送服务再修改文件")
            self.catalog.clear()

    # -- service thread -------------------------------------------------
    def _run(self, files: list[Any]) -> None:
        try:
            manifest = prepare_manifest(files)
            if self._stop_event.is_set():
                return
            with self.lock:
                self.manifest = manifest
                self._manifest_total = sum(item.size for item in manifest)
                self.notice_warnings = self.catalog.stats().warnings

            try:
                control_sock = listener("0.0.0.0", self._requested_port)
            except OSError as exc:
                target = "自动分配端口" if self._requested_port == 0 else f"控制端口 {self._requested_port}"
                raise OSError(f"无法监听{target}：{exc}") from exc
            actual_port = int(control_sock.getsockname()[1])
            try:
                data_socks, data_ports = open_data_listeners(
                    "0.0.0.0",
                    self._requested_connections,
                    preferred_start=actual_port + 1,
                    reserved_ports={actual_port},
                )
            except Exception as exc:
                self._close_socket(control_sock)
                if isinstance(exc, OSError):
                    raise OSError(f"无法分配数据端口：{exc}") from exc
                raise
            with self.lock:
                if self._stop_event.is_set():
                    cancelled_start = True
                else:
                    cancelled_start = False
                    self._control_listener = control_sock
                    self._data_listeners = data_socks
                    self._bound_port = actual_port
                    self._data_ports = data_ports
                    self._addresses = display_addresses()
                    self.status = "listening"
                    self.notice = "正在等待接收方连接"
                    self.error = None
            if cancelled_start:
                self._close_socket(control_sock)
                for sock in data_socks:
                    self._close_socket(sock)
                return
            log.info(
                "发送端已就绪，控制端口 %s，数据端口 %s",
                actual_port,
                ",".join(str(p) for p in data_ports),
            )
        except (CatalogError, OSError, ValueError) as exc:
            self._set_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - service boundary
            log.exception("发送端准备失败")
            self._set_error(f"发送端启动失败: {exc}")
            return

        control_sock = self._control_listener
        assert control_sock is not None
        try:
            control_sock.settimeout(0.5)
            while not self._stop_event.is_set():
                try:
                    conn, peer = control_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with self.lock:
                    already_busy = self._control is not None
                    current_status = self.status
                if already_busy or current_status not in {"listening"}:
                    self._close_socket(conn)
                    continue
                self._handle_control_connection(conn, peer)
                with self.lock:
                    if self.status in TERMINAL_STATUSES or self._stop_event.is_set():
                        break
        finally:
            self._cleanup_after_run()

    def _handle_control_connection(self, conn: socket.socket, peer: Any) -> None:
        peer_text = format_socket_peer(peer)
        channel: ControlChannel | None = None
        try:
            conn.settimeout(CONTROL_HANDSHAKE_TIMEOUT)
            hello = recv_control(conn)
            if hello is None or hello.get("type") != "hello":
                self._close_socket(conn)
                return
            if hello.get("protocol") not in (None, 1):
                send_control(conn, {"type": "error", "message": "协议版本不兼容"})
                self._close_socket(conn)
                return

            with self.lock:
                if self.status != "listening" or self._control is not None:
                    self._close_socket(conn)
                    return
                self._session_id = secrets.token_hex(16)
                self._data_token = secrets.token_hex(16)
                self.peer = peer_text
                self.status = "negotiating"
                self.notice = "接收方已连接，正在等待确认保存位置"
                welcome = self._build_welcome_locked(peer_text)
                channel = ControlChannel(
                    conn,
                    name="send-control",
                    on_message=self._on_control_message,
                    on_close=self._on_control_closed,
                )
                self._control = channel

            configure_socket(conn)
            if not channel.send(welcome):
                channel.close("无法发送清单")
                self._after_control_closed("无法发送文件清单")
                return
            channel.start()
            channel.wait_closed()

            with self.lock:
                close_reason = self._close_reason
            self._after_control_closed(close_reason)
        except (OSError, ProtocolError) as exc:
            log.warning("发送控制握手失败 (%s): %s", peer_text, exc)
            self._close_socket(conn)
            if channel is not None:
                channel.close("握手失败")
                with self.lock:
                    current = channel is self._control
                if current:
                    self._after_control_closed(f"握手失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("发送控制会话异常")
            if channel is not None:
                channel.close("控制连接异常")
                with self.lock:
                    current = channel is self._control
                if current:
                    self._after_control_closed(f"控制连接异常: {exc}")
            else:
                self._set_error(f"控制连接异常: {exc}")

    def _build_welcome_locked(self, peer_text: str) -> dict[str, Any]:
        entries = [item.manifest_entry() for item in self.manifest]
        return {
            "type": "welcome",
            "protocol": 1,
            "app": APP_PROTOCOL_NAME,
            "app_version": __version__,
            "session_id": self._session_id,
            "data_token": self._data_token,
            "data_ports": list(self._data_ports),
            "connections": len(self._data_ports),
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "sender_name": socket.gethostname(),
            "peer": peer_text,
            "manifest": {
                "count": len(entries),
                "total_size": sum(int(item["size"]) for item in entries),
                "files": entries,
            },
        }

    def _after_control_closed(self, reason: str) -> None:
        channel = self._control
        with self.lock:
            self._control = None
            current = self.status
            transfer = self._transfer
            if current == "completed":
                self.peer = None
                self._session_id = None
                self._data_token = None
                return
            if self._stop_event.is_set() or current in {"stopped", "error", "cancelled"}:
                self._session_id = None
                self._data_token = None
                return
            if current == "negotiating":
                self.status = "listening"
                self.notice = "接收方已断开，继续等待新的连接"
                self.peer = None
                self._session_id = None
                self._data_token = None
                self.plan = []
                self._resume_files = 0
                self._resume_bytes = 0
                self._progress = ProgressTracker()
                self._progress.reset(self._manifest_total, 0, len(self.manifest))
                self._cleanup_transfer_locked()
                return
            if current == "finishing":
                self.status = "error"
                self.error = "接收方在确认保存结果前断开了连接"
            elif current == "starting":
                self.status = "error"
                self.error = f"接收方在建立数据连接时断开: {reason}"
            elif current == "transferring":
                self.status = "error"
                self.error = f"传输过程中连接中断: {reason}"
            else:
                self.status = "error"
                self.error = f"连接意外断开: {reason}"
            self.notice = None
            self._session_id = None
            self._data_token = None
            if transfer is not None:
                transfer.abort(self.error or "连接断开")
            self._cleanup_transfer_locked()
            self._close_data_listeners_locked_safe()

    def _on_control_message(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "prepare":
            self._handle_prepare(message)
        elif msg_type == "start":
            self._handle_start()
        elif msg_type == "data_ready":
            self._handle_data_ready(message)
        elif msg_type == "cancel":
            self._handle_cancel()
        elif msg_type == "completed":
            self._handle_completed(message)
        elif msg_type == "error":
            self._set_error(str(message.get("message") or "接收方报告错误"))
        elif msg_type == "done":
            pass
        else:
            log.debug("忽略未知发送控制消息: %s", msg_type)

    def _handle_prepare(self, message: dict[str, Any]) -> None:
        with self.lock:
            if self.status != "negotiating":
                raise ProtocolError("当前状态不接受准备消息")
            if message.get("session_id") != self._session_id:
                raise ProtocolError("会话标识不匹配")
            raw_resume = message.get("resume") or {}
            if not isinstance(raw_resume, dict):
                raise ProtocolError("续传信息格式错误")
            by_id = {item.id: item for item in self.manifest}
            accepted: dict[str, int] = {}
            for key, value in raw_resume.items():
                if key not in by_id:
                    raise ProtocolError("续传文件不在清单中")
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ProtocolError("续传偏移非法")
                item = by_id[key]
                if value < 0 or value > item.size:
                    raise ProtocolError("续传偏移超出文件范围")
                if value:
                    accepted[key] = value
            plan = [
                PlannedFile(
                    id=item.id,
                    source=item.source,
                    rel_path=item.rel_path,
                    size=item.size,
                    mtime_ns=item.mtime_ns,
                    quick_hash=item.quick_hash,
                    resume_offset=accepted.get(item.id, 0),
                )
                for item in self.manifest
            ]
            base = sum(item.resume_offset for item in plan)
            self.plan = plan
            self._resume_files = sum(1 for item in plan if item.resume_offset > 0)
            self._resume_bytes = base
            self._progress = ProgressTracker()
            self._progress.reset(self._manifest_total, base, len(plan))
            self.notice = "已收到接收方的保存位置，等待对方开始"
            ready = {
                "type": "ready",
                "accepted_resume": accepted,
                "base_bytes": base,
                "new_bytes": max(0, self._manifest_total - base),
                "files": len(plan),
                "total_size": self._manifest_total,
            }
        channel = self._control
        if channel is not None:
            channel.send(ready)

    def _handle_start(self) -> None:
        with self.lock:
            if self.status != "negotiating" or not self.plan:
                raise ProtocolError("尚未准备好接收传输计划")
            self.status = "starting"
            self.notice = "正在建立并行数据通道"
            self._progress.reset(self._manifest_total, self._resume_bytes, len(self.plan))
            token = self._data_token or ""
            transfer = SendTransfer(self, list(self.plan), token)
            self._transfer = transfer
            data_ports = list(self._data_ports)
        self._start_data_acceptors(transfer, token)
        channel = self._control
        if channel is None or not channel.send(
            {
                "type": "started",
                "data_ports": data_ports,
                "connections": len(data_ports),
                "chunk_size": DEFAULT_CHUNK_SIZE,
            }
        ):
            self._on_transfer_error("无法发送启动确认")

    def _handle_data_ready(self, message: dict[str, Any]) -> None:
        connected = message.get("connected")
        if not isinstance(connected, int) or isinstance(connected, bool) or connected < 0:
            raise ProtocolError("数据连接数量非法")
        with self.lock:
            transfer = self._transfer
            if self.status != "starting" or transfer is None:
                raise ProtocolError("当前状态不接受数据连接确认")
        transfer.mark_data_ready()
        actual = transfer.wait_channels(connected, timeout=2.0)
        if actual <= 0:
            self._on_transfer_error("没有可用的数据连接")
            return
        with self.lock:
            if self.status != "starting":
                return
            self.status = "transferring"
            self.notice = None
            self._connections_used = actual
            self._progress.reset(self._manifest_total, self._resume_bytes, len(self.plan))
        transfer.start()
        progress_thread = threading.Thread(
            target=self._progress_loop,
            args=(transfer,),
            name="send-progress",
            daemon=True,
        )
        self._progress_thread = progress_thread
        progress_thread.start()

    def _start_data_acceptors(self, transfer: SendTransfer, token: str) -> None:
        with self.lock:
            sockets = list(self._data_listeners)
        threads: list[threading.Thread] = []
        for index, listener_sock in enumerate(sockets):
            thread = threading.Thread(
                target=self._accept_data_connection,
                args=(listener_sock, index, token, transfer),
                name=f"send-data-accept-{index}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        with self.lock:
            self._data_accept_threads = threads

    def _accept_data_connection(
        self,
        listener_sock: socket.socket,
        index: int,
        token: str,
        transfer: SendTransfer,
    ) -> None:
        listener_sock.settimeout(0.5)
        while not transfer.accept_stop.is_set() and not self._stop_event.is_set():
            try:
                conn, _peer = listener_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if transfer.accept_stop.is_set() or transfer.abort_event.is_set():
                self._close_socket(conn)
                return
            registered = False
            try:
                conn.settimeout(DATA_CONNECT_TIMEOUT)
                hello = recv_control(conn)
                if (
                    hello is None
                    or hello.get("type") != "data_hello"
                    or hello.get("token") != token
                ):
                    self._close_socket(conn)
                    continue
                conn.settimeout(None)
                configure_socket(conn)
                registered = transfer.register_data_channel(conn)
                if not registered:
                    self._close_socket(conn)
                    return
                send_control(conn, {"type": "data_ok", "index": index})
                return
            except (OSError, ProtocolError):
                if registered:
                    transfer.unregister_data_channel(conn)
                self._close_socket(conn)

    def _progress_loop(self, transfer: SendTransfer) -> None:
        while not transfer.finished.wait(PROGRESS_INTERVAL):
            with self.lock:
                if self.status != "transferring":
                    return
                sent = transfer.sent()
                self._progress.new_bytes = min(
                    max(0, self._manifest_total - self._resume_bytes),
                    sent,
                )
                progress = self._progress.snapshot(connections=transfer.connections())
                channel = self._control
            if channel is None:
                return
            channel.send({"type": "progress", **progress})
        # Finished: publish one final sample; all_sent is sent by the callback.

    def _on_transfer_error(self, reason: str) -> None:
        with self.lock:
            if self.status in {"completed", "error", "stopped", "cancelled"}:
                return
            self.status = "error"
            self.error = reason
            self.notice = None
            channel = self._control
            transfer = self._transfer
        if transfer is not None:
            transfer.abort(reason)
        if channel is not None:
            channel.send({"type": "error", "message": reason})
            channel.close("传输失败")
        self._close_data_listeners()

    def _on_all_sent(self) -> None:
        with self.lock:
            if self.status != "transferring":
                return
            self.status = "finishing"
            self.notice = "所有数据已发送，正在等待对方校验并保存"
            self._progress.finish()
            transfer = self._transfer
            if transfer is not None:
                self._connections_used = transfer.connections()
            payload = {
                "type": "all_sent",
                "sent": self._progress.new_bytes,
                "total": self._progress.total,
                "elapsed": self._progress.snapshot()["elapsed"],
            }
            channel = self._control
        if channel is not None:
            channel.send(payload)

    def _handle_cancel(self) -> None:
        transfer: SendTransfer | None = None
        with self.lock:
            if self.status == "negotiating":
                self.notice = "接收方取消了本次传输，继续等待连接"
                channel = self._control
                cancelled = False
            elif self.status in {"starting", "transferring", "finishing"}:
                self.status = "cancelled"
                self.notice = "接收方取消了传输"
                self.error = None
                channel = self._control
                transfer = self._transfer
                cancelled = True
            else:
                return
        if cancelled and transfer is not None:
            transfer.abort("接收方取消")
        if channel is not None:
            channel.close("接收方取消")

    def _handle_completed(self, message: dict[str, Any]) -> None:
        ok = bool(message.get("ok"))
        with self.lock:
            if self.status != "finishing":
                return
            if not ok:
                self.status = "error"
                self.error = str(message.get("error") or "接收方保存文件失败")
                self.notice = None
            else:
                self.status = "completed"
                self.notice = "全部文件已由接收方校验并保存"
                self.error = None
                self._progress.finish()
                self._completed_payload = {
                    "files": int(message.get("files", len(self.manifest))),
                    "bytes": int(message.get("bytes", self._manifest_total)),
                    "elapsed": float(message.get("elapsed", 0.0)),
                    "resumed_files": self._resume_files,
                    "resumed_bytes": self._resume_bytes,
                }
            channel = self._control
            self.peer = None
        if channel is not None:
            channel.send({"type": "done"})
            channel.close("完成")

    # -- cleanup ------------------------------------------------------
    def _set_error(self, message: str) -> None:
        with self.lock:
            if self.status in {"completed"}:
                return
            self.status = "error"
            self.error = message
            self.notice = None
            channel = self._control
            transfer = self._transfer
        if transfer is not None:
            transfer.abort(message)
        if channel is not None:
            channel.close("发生错误")
        self._close_data_listeners()

    def _on_control_closed(self, channel: ControlChannel, reason: str) -> None:
        with self.lock:
            if channel is not self._control:
                return
            self._close_reason = reason
            transfer = self._transfer
        if transfer is not None:
            transfer.abort(reason)

    def _cleanup_after_run(self) -> None:
        with self.lock:
            channel = self._control
            self._control = None
            transfer = self._transfer
            if self.status in BUSY_STATUSES and not self._stop_event.is_set():
                self.status = "error"
                self.error = self.error or self._close_reason or "发送服务意外结束"
            elif self._stop_event.is_set() and self.status not in TERMINAL_STATUSES:
                self.status = "stopped"
                self.notice = "发送已停止"
            listeners = [self._control_listener, *self._data_listeners]
            self._control_listener = None
            self._data_listeners = []
            self._data_accept_threads = []
            self._session_id = None
            self._data_token = None
        if transfer is not None:
            transfer.abort("发送服务结束")
        if channel is not None:
            channel.close("发送服务结束")
        for sock in listeners:
            self._close_socket(sock)

    def _cleanup_transfer_locked(self) -> None:
        self._transfer = None
        self._connections_used = 0

    def _close_data_listeners(self) -> None:
        with self.lock:
            sockets = list(self._data_listeners)
        for sock in sockets:
            self._close_socket(sock)
        with self.lock:
            self._data_listeners = []

    def _close_data_listeners_locked_safe(self) -> None:
        # Called from the control session while the accept thread will exit on
        # its own; closing now releases the ports promptly for a retry.
        with self.lock:
            sockets = list(self._data_listeners)
        for sock in sockets:
            self._close_socket(sock)
        with self.lock:
            self._data_listeners = []

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -- state --------------------------------------------------------
    def state(self) -> dict[str, Any]:
        catalog_stats = self.catalog.stats()
        with self.lock:
            status = self.status
            manifest_count = len(self.manifest)
            if manifest_count:
                count = manifest_count
                total = self._manifest_total
            else:
                count = catalog_stats.count
                total = catalog_stats.total_size
            listen = None
            if self._bound_port is not None:
                listen = {
                    "port": self._bound_port,
                    "addresses": list(self._addresses),
                    "data_ports": list(self._data_ports),
                    "connections": self._requested_connections,
                }
            transfer = self._transfer
            connections = transfer.connections() if transfer is not None else self._connections_used
            progress = self._progress.snapshot(connections=connections)
            payload = {
                "status": status,
                "busy": status in BUSY_STATUSES,
                "can_modify": status not in BUSY_STATUSES,
                "error": self.error,
                "notice": self.notice,
                "peer": self.peer,
                "warnings": list(self.notice_warnings),
                "listen": listen,
                "files": {
                    "count": count,
                    "total_size": total,
                    "revision": catalog_stats.revision,
                },
                "resume": {
                    "files": self._resume_files,
                    "bytes": self._resume_bytes,
                },
                "progress": progress,
                "completed": self._completed_payload,
            }
        return payload

    def files_page(self, offset: int, limit: int) -> tuple[list[dict[str, object]], int]:
        if self.manifest and self.status != "idle":
            with self.lock:
                start = max(0, offset)
                end = min(len(self.manifest), start + max(0, limit))
                items = [
                    {
                        "id": item.id,
                        "rel": item.rel_path,
                        "path": item.source,
                        "size": item.size,
                    }
                    for item in self.manifest[start:end]
                ]
            return items, len(self.manifest)
        return self.catalog.page(offset, limit)
