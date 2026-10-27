"""Receiver side: negotiate, open parallel data channels, save resumably."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    APP_PROTOCOL_NAME,
    CONTROL_HANDSHAKE_TIMEOUT,
    DATA_CONNECT_TIMEOUT,
    DATA_READ_TIMEOUT,
    DEFAULT_CHUNK_SIZE,
    RESUME_DIR_NAME,
    RESUME_STATE_VERSION,
)
from .control import ControlChannel
from .errors import UserError
from .metrics import ProgressTracker
from .naming import (
    DestinationPlan,
    NamingError,
    plan_destinations,
    windows_long_path_warnings,
)
from .netutil import configure_socket
from .protocol import (
    DATA_HEADER,
    DATA_MAGIC,
    MAX_PAYLOAD,
    PeerClosed,
    ProtocolError,
    bytes_to_id,
    recv_control,
    recv_exact,
    send_control,
    validate_file_id,
    validate_manifest_entry,
)
from . import __version__

log = logging.getLogger(__name__)

BUSY_STATUSES = {"connecting", "review", "preparing", "starting", "transferring", "verifying"}
TERMINAL_STATUSES = {"completed", "error", "cancelled"}
MAX_PENDING_CHUNKS = 64
_MAX_STATE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RemoteFile:
    id: str
    rel_path: str
    size: int
    mtime_ns: int
    quick_hash: str
    mode: int | None = None
    dest_rel_path: str | None = None

    @property
    def destination_rel(self) -> str:
        return self.dest_rel_path or self.rel_path

    @property
    def renamed(self) -> bool:
        return self.destination_rel != self.rel_path

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "rel": self.rel_path,
            "dest_rel": self.destination_rel,
            "renamed": self.renamed,
            "size": self.size,
        }


@dataclass(slots=True)
class ResumeMatch:
    offsets: dict[str, int]
    done_ids: set[str]
    files: int = 0
    bytes: int = 0
    warnings: list[str] | None = None


def _state_path(root: Path, file_id: str) -> Path:
    return root / RESUME_DIR_NAME / "state" / f"{file_id}.json"


def _part_path(root: Path, file_id: str) -> Path:
    return root / RESUME_DIR_NAME / "parts" / f"{file_id}.part"


def _atomic_state_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
    os.replace(temporary, path)


def scan_resume(root: Path, files: list[RemoteFile]) -> ResumeMatch:
    """Match previous partial/completed records against a new manifest.

    A record is usable only when its path, size, mtime and quick fingerprint
    all match. Corrupt or unrelated sidecars are ignored rather than deleted.
    """

    offsets: dict[str, int] = {}
    done_ids: set[str] = set()
    warnings: list[str] = []
    state_dir = root / RESUME_DIR_NAME / "state"
    records: dict[str, dict[str, Any]] = {}
    if state_dir.is_dir():
        try:
            paths = list(state_dir.glob("*.json"))
        except OSError as exc:
            return ResumeMatch(offsets={}, done_ids=set(), warnings=[f"读取续传记录失败: {exc}"])
        for path in paths:
            try:
                if path.stat().st_size > _MAX_STATE_BYTES:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != RESUME_STATE_VERSION:
                    continue
                file_id = validate_file_id(data.get("id"))
                if path.name != f"{file_id}.json":
                    continue
                records[file_id] = data
            except (OSError, ValueError, json.JSONDecodeError, ProtocolError):
                continue

    for entry in files:
        record = records.get(entry.id)
        if record is None:
            continue
        if (
            record.get("path") != entry.rel_path
            or record.get("size") != entry.size
            or record.get("mtime_ns") != entry.mtime_ns
            or record.get("quick_hash") != entry.quick_hash
        ):
            continue
        status = record.get("status")
        if status == "done":
            destination = root.joinpath(*entry.destination_rel.split("/"))
            try:
                if destination.is_file() and destination.stat().st_size == entry.size:
                    offsets[entry.id] = entry.size
                    done_ids.add(entry.id)
            except OSError:
                pass
            continue
        if status != "partial":
            continue
        part = _part_path(root, entry.id)
        try:
            if not part.is_file():
                continue
            size = part.stat().st_size
        except OSError:
            continue
        if 0 < size <= entry.size:
            offsets[entry.id] = size

    resume_files = sum(1 for value in offsets.values() if value > 0)
    resume_bytes = sum(offsets.values())
    return ResumeMatch(
        offsets=offsets,
        done_ids=done_ids,
        files=resume_files,
        bytes=resume_bytes,
        warnings=warnings,
    )


class ReceiveFileWriter:
    """Writes one destination file, committing bytes strictly in order."""

    def __init__(
        self,
        entry: RemoteFile,
        root: Path,
        initial_offset: int,
        done_existing: bool,
    ) -> None:
        self.entry = entry
        self.root = root
        self.initial_offset = max(0, min(initial_offset, entry.size))
        self.expected = self.initial_offset
        self.done_existing = done_existing
        self.handle: Any = None
        self.pending: dict[int, bytes] = {}
        self.lock = threading.RLock()
        self.finalized = False
        self.state_written = False
        parts = entry.destination_rel.split("/")
        self.destination = root.joinpath(*parts)
        if not self.destination.is_relative_to(root):
            raise ProtocolError("目标路径越界")
        self.part_path = _part_path(root, entry.id)
        self.state_path = _state_path(root, entry.id)

    def write(self, offset: int, data: bytes) -> int:
        if not data:
            raise ProtocolError("收到空数据块")
        end = offset + len(data)
        if offset < 0 or end > self.entry.size:
            raise ProtocolError("数据块超出文件范围")
        with self.lock:
            if self.finalized:
                raise ProtocolError("文件已保存，不能再写入")
            if offset < self.expected:
                raise ProtocolError(f"数据块重叠: {self.entry.rel_path}")
            if offset == self.expected:
                self._ensure_open()
                written = self._consume_locked(offset, data)
                written += self._drain_pending_locked()
                self._close_if_complete_locked()
                return written
            self._ensure_open()
            for start, buffered in self.pending.items():
                other_end = start + len(buffered)
                if offset < other_end and end > start:
                    raise ProtocolError(f"数据块重叠: {self.entry.rel_path}")
            if len(self.pending) >= MAX_PENDING_CHUNKS:
                raise ProtocolError("数据块乱序过多，传输已中止")
            self.pending[offset] = bytes(data)
            return 0

    def _consume_locked(self, offset: int, data: bytes) -> int:
        if self.handle is None:
            raise ProtocolError("内部错误：文件尚未打开")
        self.handle.seek(offset)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = self.handle.write(view[written:])
            if not count:
                raise OSError(f"写入文件失败: {self.entry.rel_path}")
            written += count
        self.expected += len(data)
        return len(data)

    def _drain_pending_locked(self) -> int:
        written = 0
        while self.expected in self.pending:
            data = self.pending.pop(self.expected)
            written += self._consume_locked(self.expected, data)
        return written

    def _ensure_open(self) -> None:
        if self.handle is not None:
            return
        self.part_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_state("partial")
        if self.initial_offset == 0:
            self.handle = self.part_path.open("w+b", buffering=0)
        else:
            if not self.part_path.is_file():
                raise OSError(f"续传分片不存在: {self.entry.rel_path}")
            self.handle = self.part_path.open("r+b", buffering=0)
            actual = os.fstat(self.handle.fileno()).st_size
            if actual != self.initial_offset:
                self.handle.close()
                self.handle = None
                raise OSError(f"续传分片大小变化: {self.entry.rel_path}")

    def _write_state(self, status: str) -> None:
        payload = {
            "version": RESUME_STATE_VERSION,
            "id": self.entry.id,
            "path": self.entry.rel_path,
            "size": self.entry.size,
            "mtime_ns": self.entry.mtime_ns,
            "quick_hash": self.entry.quick_hash,
            "status": status,
        }
        try:
            _atomic_state_write(self.state_path, payload)
            self.state_written = True
        except OSError as exc:
            raise OSError(f"无法写入续传记录: {exc}") from exc

    def _close_if_complete_locked(self) -> None:
        if self.expected >= self.entry.size and self.handle is not None:
            self.handle.close()
            self.handle = None

    def close(self) -> None:
        with self.lock:
            if self.handle is not None:
                try:
                    self.handle.close()
                except OSError:
                    pass
                self.handle = None

    def finalize(self) -> bool:
        """Move the completed part into place. Returns True if already done."""

        with self.lock:
            if self.finalized:
                return True
            if self.expected != self.entry.size:
                raise OSError(
                    f"文件数据不完整: {self.entry.rel_path} "
                    f"({self.expected}/{self.entry.size} 字节)"
                )
            if self.pending:
                raise OSError(f"仍有乱序数据未写入: {self.entry.rel_path}")
            if self.done_existing:
                if self.destination.is_file() and self.destination.stat().st_size == self.entry.size:
                    self._apply_mode()
                    self.finalized = True
                    return True
                raise OSError(f"已标记完成的文件不存在: {self.entry.rel_path}")
            if self.handle is not None:
                # Should only happen for a resumed complete part or an empty
                # file; normal writes close the handle as soon as they finish.
                handle = self.handle
                self.handle = None
            elif self.part_path.is_file():
                handle = self.part_path.open("r+b", buffering=0)
            elif self.entry.size == 0:
                self.part_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.part_path.open("w+b", buffering=0)
            else:
                raise OSError(f"续传分片不存在: {self.entry.rel_path}")
            try:
                actual = os.fstat(handle.fileno()).st_size
                if actual != self.entry.size:
                    raise OSError(
                        f"保存前文件大小不匹配: {self.entry.rel_path} "
                        f"({actual}/{self.entry.size} 字节)"
                    )
                os.fsync(handle.fileno())
            finally:
                handle.close()
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self.part_path, self.destination)
            except OSError as exc:
                raise OSError(f"无法保存文件 {self.entry.rel_path}: {exc}") from exc
            try:
                os.utime(self.destination, ns=(self.entry.mtime_ns, self.entry.mtime_ns))
            except OSError:
                pass
            self._apply_mode()
            try:
                self._write_state("done")
            except OSError:
                # The file itself is safely in place; the done marker is only
                # an optimization if a later file fails in this same session.
                pass
            self.finalized = True
            return False

    def _apply_mode(self) -> None:
        if self.entry.mode is None or os.name == "nt":
            return
        try:
            os.chmod(self.destination, self.entry.mode & 0o777)
        except OSError as exc:
            log.debug("无法恢复文件权限 %s: %s", self.entry.rel_path, exc)


class ReceiveTransfer:
    """Owns per-file writers and data reader threads for one transfer."""

    def __init__(
        self,
        service: "ReceiveService",
        files: list[RemoteFile],
        offsets: dict[str, int],
        done_ids: set[str],
        save_dir: Path,
        *,
        generation: int,
        chunk_size: int,
    ) -> None:
        self.service = service
        self.files = files
        self.offsets = offsets
        self.save_dir = save_dir
        self.generation = generation
        self.chunk_size = chunk_size
        self.base_bytes = sum(offsets.get(item.id, 0) for item in files)
        self.total_bytes = sum(item.size for item in files)
        self.writers = {
            item.id: ReceiveFileWriter(
                item,
                save_dir,
                offsets.get(item.id, 0),
                item.id in done_ids,
            )
            for item in files
        }
        self.lock = threading.RLock()
        self.channels: list[socket.socket] = []
        self.readers_pending = 0
        self.all_sent = False
        self.error: str | None = None
        self.finalize_started = False
        self.received_new = 0
        self.started_at = time.monotonic()

    def add_channel(self, sock: socket.socket) -> None:
        with self.lock:
            self.channels.append(sock)

    def start_readers(self) -> None:
        with self.lock:
            channels = list(self.channels)
            self.readers_pending = len(channels)
        for index, sock in enumerate(channels):
            thread = threading.Thread(
                target=self.service._data_reader,
                args=(sock, self, index),
                name=f"receive-data-{index}",
                daemon=True,
            )
            thread.start()

    def add_received(self, amount: int) -> None:
        if amount <= 0:
            return
        with self.lock:
            self.received_new += amount
        self.service._update_receive_progress(self)

    def mark_reader_done(self) -> None:
        with self.lock:
            self.readers_pending = max(0, self.readers_pending - 1)
        self._maybe_finalize()

    def mark_all_sent(self) -> None:
        with self.lock:
            self.all_sent = True
        self._maybe_finalize()

    def _maybe_finalize(self) -> None:
        with self.lock:
            if self.error or not self.all_sent or self.readers_pending > 0:
                return
            if self.finalize_started:
                return
            self.finalize_started = True
        self.service._start_finalize(self)

    def fail(self, reason: str) -> None:
        with self.lock:
            if self.error is not None or self.finalize_started:
                return
            self.error = reason
            channels = list(self.channels)
        for sock in channels:
            _close_socket(sock)
        self.service._on_receive_transfer_error(self, reason)

    def abort(self, reason: str) -> None:
        with self.lock:
            if self.error is None:
                self.error = reason
            channels = list(self.channels)
        for sock in channels:
            _close_socket(sock)
        for writer in self.writers.values():
            writer.close()

    def close_channels(self) -> None:
        with self.lock:
            channels = list(self.channels)
        for sock in channels:
            _close_socket(sock)

    def connection_count(self) -> int:
        with self.lock:
            return len(self.channels)


class ReceiveService:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.status = "idle"
        self.error: str | None = None
        self.notice: str | None = None
        self.host = ""
        self.port = 0
        self._peer_ip = ""
        self._sender_name = ""
        self._session_id: str | None = None
        self._data_token: str | None = None
        self._data_ports: list[int] = []
        self._chunk_size = DEFAULT_CHUNK_SIZE
        self.manifest: list[RemoteFile] = []
        self._manifest_revision = 0
        self._save_dir: Path | None = None
        self._free_space: int | None = None
        self._resume = ResumeMatch(offsets={}, done_ids=set())
        self._path_adaptation: dict[str, Any] = {"renamed": 0, "examples": [], "warnings": []}
        self._path_warnings: list[str] = []
        self._proposed_offsets: dict[str, int] = {}
        self._done_ids: set[str] = set()
        self._completed_payload: dict[str, Any] | None = None
        self._progress = ProgressTracker()
        self._control: ControlChannel | None = None
        self._connecting_socket: socket.socket | None = None
        self._connect_thread: threading.Thread | None = None
        self._data_setup_thread: threading.Thread | None = None
        self._data_setup_started = False
        self._transfer: ReceiveTransfer | None = None
        self._generation = 0
        self._dir_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self.status in BUSY_STATUSES

    # -- public API ----------------------------------------------------
    def connect(self, host: str, port: int) -> None:
        host = (host or "").strip()
        if not host:
            raise UserError("请填写发送方主机地址")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise UserError("控制端口必须在 1–65535 之间")
        with self.lock:
            if self.busy:
                raise UserError("接收端正在处理连接，请先取消")
            self._generation += 1
            generation = self._generation
            old_control = self._control
            self._control = None
            self._close_connecting_locked()
            self.manifest = []
            self._manifest_revision += 1
            self.status = "connecting"
            self.error = None
            self.notice = "正在连接发送方…"
            self.host = host
            self.port = port
            self._peer_ip = ""
            self._sender_name = ""
            self._session_id = None
            self._data_token = None
            self._data_ports = []
            self._chunk_size = DEFAULT_CHUNK_SIZE
            self._proposed_offsets = {}
            self._done_ids = set()
            self._data_setup_started = False
            self._transfer = None
            self._completed_payload = None
            self._resume = ResumeMatch(offsets={}, done_ids=set())
            self._free_space = None
            self._path_adaptation = {"renamed": 0, "examples": [], "warnings": []}
            self._path_warnings = []
            self._progress = ProgressTracker()
            self._connect_thread = threading.Thread(
                target=self._connect_worker,
                args=(host, port, generation),
                name="receive-connect",
                daemon=True,
            )
        if old_control is not None:
            old_control.close("重新连接")
        self._connect_thread.start()

    def cancel(self) -> None:
        with self.lock:
            if not self.busy:
                if self.status in TERMINAL_STATUSES:
                    return
                raise UserError("当前没有可取消的接收任务")
            if self.status == "verifying":
                raise UserError("正在校验并保存文件，暂时不能取消")
            self._generation += 1
            status = self.status
            self.status = "cancelled"
            self.notice = "接收已取消，已收到的分片会保留以便续传"
            self.error = None
            channel = self._control
            self._control = None
            self._close_connecting_locked()
            transfer = self._transfer
        if channel is not None:
            channel.send({"type": "cancel"})
            channel.close("接收方取消")
        if transfer is not None:
            transfer.abort("接收方取消")
        if status == "connecting":
            self.notice = "已取消连接"

    def reset(self) -> None:
        with self.lock:
            if self.busy:
                raise UserError("请先取消当前接收任务")
            self._generation += 1
            channel = self._control
            self._control = None
            self._close_connecting_locked()
            transfer = self._transfer
            self._transfer = None
            self.status = "idle"
            self.error = None
            self.notice = None
            self._sender_name = ""
            self._peer_ip = ""
            self._session_id = None
            self._data_token = None
            self._data_ports = []
            self._chunk_size = DEFAULT_CHUNK_SIZE
            self.manifest = []
            self._manifest_revision += 1
            self._save_dir = None
            self._free_space = None
            self._resume = ResumeMatch(offsets={}, done_ids=set())
            self._proposed_offsets = {}
            self._done_ids = set()
            self._path_adaptation = {"renamed": 0, "examples": [], "warnings": []}
            self._path_warnings = []
            self._completed_payload = None
            self._progress = ProgressTracker()
            self._data_setup_started = False
        if transfer is not None:
            transfer.abort("接收端重置")
        if channel is not None:
            channel.close("接收端重置")

    def set_save_dir(self, path: str) -> dict[str, Any]:
        raw = (path or "").strip().strip('"')
        if not raw:
            raise UserError("请选择保存文件夹")
        with self.lock:
            if self.status != "review":
                raise UserError("当前不在接收确认阶段")
            manifest = list(self.manifest)
        root = Path(raw).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise UserError(f"保存文件夹不可用: {exc}") from exc
        if not root.is_dir():
            raise UserError("保存路径不是文件夹")
        if not os.access(root, os.W_OK):
            raise UserError("保存文件夹不可写")
        with self._dir_lock:
            resume = scan_resume(root, manifest)
            free = int(shutil.disk_usage(root).free)
            path_warnings = (
                windows_long_path_warnings(root, [item.destination_rel for item in manifest])
                if os.name == "nt"
                else []
            )
            with self.lock:
                if self.status != "review":
                    raise UserError("接收状态已变化，请重试")
                self._save_dir = root
                self._free_space = free
                self._resume = resume
                self._path_warnings = path_warnings
                self.error = None
                self.notice = "保存位置已确认"
                return self._resume_dict_locked()

    def start(self) -> None:
        with self.lock:
            if self.status != "review":
                raise UserError("当前不能开始接收")
            if self._save_dir is None:
                raise UserError("请先选择保存文件夹")
            if self._control is None:
                raise UserError("与发送方的连接已断开，请重新连接")
            self.status = "preparing"
            self.error = None
            self.notice = "正在核对续传位置…"
            generation = self._generation
            thread = threading.Thread(
                target=self._begin_transfer,
                args=(generation,),
                name="receive-prepare",
                daemon=True,
            )
        thread.start()

    # -- connection ----------------------------------------------------
    def _connect_worker(self, host: str, port: int, generation: int) -> None:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=DATA_CONNECT_TIMEOUT)
            configure_socket(sock)
            sock.settimeout(CONTROL_HANDSHAKE_TIMEOUT)
            with self.lock:
                if generation != self._generation or self.status != "connecting":
                    sock.close()
                    return
                self._connecting_socket = sock
            send_control(
                sock,
                {
                    "type": "hello",
                    "protocol": 1,
                    "app": APP_PROTOCOL_NAME,
                    "app_version": __version__,
                },
            )
            welcome = recv_control(sock)
            if welcome is None:
                raise ProtocolError("发送方关闭了连接")
            if welcome.get("type") == "error":
                raise ProtocolError(str(welcome.get("message") or "发送方拒绝连接"))
            parsed = self._parse_welcome(welcome)
            try:
                peer_ip = sock.getpeername()[0]
            except OSError:
                peer_ip = host

            with self.lock:
                if generation != self._generation or self.status != "connecting":
                    sock.close()
                    return
                self._connecting_socket = None
                self.host = host
                self.port = port
                self._peer_ip = peer_ip
                self._sender_name = parsed["sender_name"]
                self._session_id = parsed["session_id"]
                self._data_token = parsed["data_token"]
                self._data_ports = parsed["data_ports"]
                self._chunk_size = parsed["chunk_size"]
                self.manifest = parsed["files"]
                path_plan: DestinationPlan = parsed["path_plan"]
                self._path_adaptation = {
                    "renamed": path_plan.renamed_count,
                    "examples": path_plan.examples(),
                    "warnings": [],
                }
                self._path_warnings = []
                self._manifest_revision += 1
                self.status = "review"
                self.notice = "连接成功，请确认文件并选择保存位置"
                self.error = None
                self._progress = ProgressTracker()
                self._progress.reset(sum(item.size for item in self.manifest), 0, len(self.manifest))
                self._data_setup_started = False
                self._transfer = None
                channel = ControlChannel(
                    sock,
                    name="receive-control",
                    on_message=self._on_control_message,
                    on_close=self._on_control_closed,
                )
                self._control = channel
                current_manifest = list(self.manifest)
                save_dir = self._save_dir

            channel.start()
            if save_dir is not None:
                # Reuse a previously chosen save directory if it still exists.
                try:
                    resume = scan_resume(save_dir, current_manifest)
                    free = int(shutil.disk_usage(save_dir).free)
                    path_warnings = (
                        windows_long_path_warnings(
                            save_dir,
                            [item.destination_rel for item in current_manifest],
                        )
                        if os.name == "nt"
                        else []
                    )
                    with self.lock:
                        if self.status == "review":
                            self._resume = resume
                            self._free_space = free
                            self._path_warnings = path_warnings
                except OSError:
                    pass
        except (OSError, ProtocolError, ValueError) as exc:
            if sock is not None:
                _close_socket(sock)
            with self.lock:
                if generation == self._generation and self.status == "connecting":
                    self._connecting_socket = None
                    self.status = "error"
                    self.error = f"连接发送方失败: {exc}"
                    self.notice = None
        except Exception as exc:  # noqa: BLE001
            log.exception("接收连接线程异常")
            if sock is not None:
                _close_socket(sock)
            with self.lock:
                if generation == self._generation and self.status == "connecting":
                    self._connecting_socket = None
                    self.status = "error"
                    self.error = f"连接异常: {exc}"
                    self.notice = None

    def _parse_welcome(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("type") != "welcome":
            raise ProtocolError("发送方未发送文件清单")
        if message.get("protocol") != 1:
            raise ProtocolError("协议版本不兼容")
        session_id = message.get("session_id")
        token = message.get("data_token")
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolError("会话标识非法")
        if not isinstance(token, str) or not token:
            raise ProtocolError("数据令牌非法")
        ports = message.get("data_ports")
        if (
            not isinstance(ports, list)
            or not ports
            or len(ports) > 64
            or any(not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 65535) for p in ports)
            or len(set(ports)) != len(ports)
        ):
            raise ProtocolError("数据端口列表非法")
        chunk_size = message.get("chunk_size", DEFAULT_CHUNK_SIZE)
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not (64 * 1024 <= chunk_size <= 32 * 1024 * 1024):
            raise ProtocolError("数据块大小非法")
        manifest = message.get("manifest")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise ProtocolError("文件清单格式非法")
        raw_files = manifest["files"]
        if not raw_files:
            raise ProtocolError("发送方没有可接收的文件")
        validated: list[dict[str, Any]] = []
        ids: set[str] = set()
        paths: set[str] = set()
        for raw in raw_files:
            entry = validate_manifest_entry(raw)
            if entry["id"] in ids:
                raise ProtocolError("文件清单包含重复 ID")
            folded = entry["path"].casefold()
            if folded in paths:
                raise ProtocolError("文件清单包含重复路径")
            ids.add(entry["id"])
            paths.add(folded)
            validated.append(entry)
        try:
            path_plan = plan_destinations(
                [entry["path"] for entry in validated],
                windows=(os.name == "nt"),
            )
        except NamingError as exc:
            raise ProtocolError(str(exc)) from exc
        files = [
            RemoteFile(
                id=entry["id"],
                rel_path=entry["path"],
                size=entry["size"],
                mtime_ns=entry["mtime_ns"],
                quick_hash=entry["quick_hash"],
                mode=entry.get("mode"),
                dest_rel_path=path_plan.mapping[entry["path"]],
            )
            for entry in validated
        ]
        total = sum(item.size for item in files)
        declared = manifest.get("total_size")
        if declared is not None and declared != total:
            raise ProtocolError("文件清单总大小不一致")
        sender_name = message.get("sender_name")
        if not isinstance(sender_name, str):
            sender_name = ""
        return {
            "session_id": session_id,
            "data_token": token,
            "data_ports": list(ports),
            "chunk_size": chunk_size,
            "files": files,
            "path_plan": path_plan,
            "sender_name": sender_name,
        }

    # -- prepare / data channels --------------------------------------
    def _begin_transfer(self, generation: int) -> None:
        try:
            with self.lock:
                if generation != self._generation or self.status != "preparing":
                    return
                manifest = list(self.manifest)
                save_dir = self._save_dir
                channel = self._control
            if save_dir is None or channel is None:
                raise UserError("保存位置或连接已失效")
            with self._dir_lock:
                resume = scan_resume(save_dir, manifest)
                free = int(shutil.disk_usage(save_dir).free)
            base = resume.bytes
            needed = max(0, sum(item.size for item in manifest) - base)
            with self.lock:
                if generation != self._generation or self.status != "preparing":
                    return
                if needed > free:
                    self.status = "review"
                    self.error = (
                        f"保存磁盘空间不足：还需 {needed} 字节，"
                        f"可用 {free} 字节。请更换保存位置。"
                    )
                    self.notice = None
                    self._resume = resume
                    self._free_space = free
                    return
                self._resume = resume
                self._free_space = free
                self._proposed_offsets = {key: value for key, value in resume.offsets.items() if value > 0}
                self._done_ids = set(resume.done_ids)
                total = sum(item.size for item in manifest)
                self._progress = ProgressTracker()
                self._progress.reset(total, base, len(manifest))
                notice = "正在等待发送方确认续传位置…"
                self.notice = notice
                prepare = {
                    "type": "prepare",
                    "session_id": self._session_id,
                    "resume": dict(self._proposed_offsets),
                }
            if not channel.send(prepare):
                raise UserError("无法向发送方发送准备请求")
        except UserError as exc:
            self._abort_with_error(exc.message)
        except (OSError, ProtocolError) as exc:
            self._abort_with_error(f"准备接收失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("接收准备线程异常")
            self._abort_with_error(f"准备接收失败: {exc}")

    def _on_control_message(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "ready":
            self._handle_ready(message)
        elif msg_type == "started":
            self._handle_started()
        elif msg_type == "progress":
            # The receiver tracks committed bytes itself; sender progress is
            # informational and must not overwrite local notices.
            pass
        elif msg_type == "all_sent":
            with self.lock:
                transfer = self._transfer
            if transfer is not None:
                transfer.mark_all_sent()
        elif msg_type == "error":
            self._remote_error(str(message.get("message") or "发送方报告错误"))
        elif msg_type == "aborted":
            self._remote_error(str(message.get("message") or "发送方中止了传输"))
        elif msg_type == "done":
            pass
        else:
            log.debug("忽略未知接收控制消息: %s", msg_type)

    def _handle_ready(self, message: dict[str, Any]) -> None:
        with self.lock:
            if self.status != "preparing":
                raise ProtocolError("当前状态不接受 ready")
            proposed = dict(self._proposed_offsets)
            raw_accepted = message.get("accepted_resume") or {}
            if not isinstance(raw_accepted, dict):
                raise ProtocolError("续传确认格式非法")
            accepted: dict[str, int] = {}
            by_id = {item.id: item for item in self.manifest}
            for key, value in raw_accepted.items():
                if key not in by_id or not isinstance(value, int) or isinstance(value, bool):
                    raise ProtocolError("续传确认包含非法条目")
                if value < 0 or value > by_id[key].size:
                    raise ProtocolError("续传确认偏移超出范围")
                if value:
                    accepted[key] = value
            if accepted != proposed:
                raise ProtocolError("发送方与接收方的续传位置不一致")
            total = sum(item.size for item in self.manifest)
            new_bytes = message.get("new_bytes")
            if isinstance(new_bytes, int) and not isinstance(new_bytes, bool):
                if new_bytes != total - sum(accepted.values()):
                    raise ProtocolError("发送方的新增字节数不一致")
            files = list(self.manifest)
            save_dir = self._save_dir
            if save_dir is None:
                raise UserError("保存位置丢失")
            transfer = ReceiveTransfer(
                self,
                files,
                accepted,
                set(self._done_ids),
                save_dir,
                generation=self._generation,
                chunk_size=self._chunk_size,
            )
            self._transfer = transfer
            self.status = "starting"
            self.notice = "正在建立并行数据通道…"
            self._progress.reset(total, sum(accepted.values()), len(files))
            channel = self._control
            generation = self._generation
        if channel is None or not channel.send({"type": "start"}):
            self._abort_with_error("无法通知发送方开始传输")
            return
        timer = threading.Timer(30.0, self._started_timeout, args=(generation, transfer))
        timer.daemon = True
        timer.start()

    def _started_timeout(self, generation: int, transfer: ReceiveTransfer) -> None:
        with self.lock:
            stale = (
                generation == self._generation
                and transfer is self._transfer
                and self.status == "starting"
            )
        if stale:
            self._abort_with_error("发送方没有建立数据通道")

    def _handle_started(self) -> None:
        with self.lock:
            if self.status != "starting":
                if self.status == "transferring":
                    return
                raise ProtocolError("当前状态不接受数据通道启动")
            if self._data_setup_started:
                return
            self._data_setup_started = True
            generation = self._generation
            transfer = self._transfer
        if transfer is None:
            self._abort_with_error("传输上下文丢失")
            return
        thread = threading.Thread(
            target=self._setup_data_channels,
            args=(generation, transfer),
            name="receive-data-setup",
            daemon=True,
        )
        self._data_setup_thread = thread
        thread.start()

    def _setup_data_channels(self, generation: int, transfer: ReceiveTransfer) -> None:
        ports = list(self._data_ports)
        host = self._peer_ip or self.host
        token = self._data_token or ""
        results: list[tuple[int, socket.socket | None, str]] = []
        futures: dict[concurrent.futures.Future[socket.socket], int] = {}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(ports)))
        try:
            futures = {
                executor.submit(self._open_data_channel, host, port, token, index): index
                for index, port in enumerate(ports)
            }
            concurrent.futures.wait(
                futures,
                timeout=DATA_CONNECT_TIMEOUT * 2 + 5,
            )
            for future, index in futures.items():
                if not future.done():
                    # Its socket-level timeouts will end it; collecting it
                    # below after shutdown avoids losing a late success.
                    continue
                if future.cancelled():
                    continue
                try:
                    sock = future.result()
                except (OSError, ProtocolError, ValueError) as exc:
                    results.append((index, None, str(exc)))
                except Exception as exc:  # noqa: BLE001
                    results.append((index, None, str(exc)))
                else:
                    results.append((index, sock, ""))
        except Exception as exc:  # noqa: BLE001
            log.exception("建立数据连接时异常")
            results.append((-1, None, str(exc)))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        # Collect any success that settled while the executor was shutting down.
        collected = {index for index, _sock, _err in results}
        for future, index in futures.items():
            if index in collected or future.cancelled():
                continue
            if future.done():
                try:
                    sock = future.result()
                except Exception as exc:  # noqa: BLE001
                    results.append((index, None, str(exc)))
                else:
                    results.append((index, sock, ""))

        with self.lock:
            still_current = (
                generation == self._generation
                and self.status == "starting"
                and transfer is self._transfer
            )
            channel = self._control
        sockets = [sock for _index, sock, _err in results if sock is not None]
        if not still_current or channel is None:
            for sock in sockets:
                _close_socket(sock)
            return
        if not sockets:
            errors = "; ".join(error for _index, _sock, error in results if error)
            self._abort_with_error(f"无法建立数据连接: {errors or '连接全部失败'}")
            return
        for index, sock, _error in sorted(results, key=lambda item: item[0]):
            if sock is None:
                continue
            transfer.add_channel(sock)
        with self.lock:
            if generation != self._generation or self.status != "starting":
                for sock in sockets:
                    _close_socket(sock)
                return
            total = sum(item.size for item in self.manifest)
            base = transfer.base_bytes
            self.status = "transferring"
            self.notice = None
            self._progress.reset(total, base, len(self.manifest))
        transfer.start_readers()
        failed = len(ports) - len(sockets)
        if failed:
            with self.lock:
                self.notice = f"有 {failed} 条数据通道建立失败，将使用 {len(sockets)} 条通道"
        if not channel.send(
            {
                "type": "data_ready",
                "connected": len(sockets),
                "failed": failed,
            }
        ):
            self._abort_with_error("无法启用数据通道")

    def _open_data_channel(self, host: str, port: int, token: str, index: int) -> socket.socket:
        sock = socket.create_connection((host, port), timeout=DATA_CONNECT_TIMEOUT)
        try:
            configure_socket(sock)
            sock.settimeout(DATA_CONNECT_TIMEOUT)
            send_control(sock, {"type": "data_hello", "token": token, "index": index})
            ack = recv_control(sock)
            if ack is None or ack.get("type") != "data_ok":
                raise ProtocolError("数据端口握手被拒绝")
            sock.settimeout(DATA_READ_TIMEOUT)
            return sock
        except Exception:
            _close_socket(sock)
            raise

    # -- incoming binary data -----------------------------------------
    def _data_reader(self, sock: socket.socket, transfer: ReceiveTransfer, index: int) -> None:
        reason: str | None = None
        try:
            while True:
                header = recv_exact(sock, DATA_HEADER.size)
                if header is None:
                    break
                magic, raw_id, offset, length, checksum = DATA_HEADER.unpack(header)
                if magic != DATA_MAGIC:
                    raise ProtocolError("数据帧标识错误")
                if length <= 0 or length > min(MAX_PAYLOAD, transfer.chunk_size):
                    raise ProtocolError("数据帧长度非法")
                file_id = bytes_to_id(raw_id)
                payload = recv_exact(sock, length)
                if payload is None:
                    raise PeerClosed("数据帧不完整")
                if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    raise ProtocolError("数据块校验失败")
                writer = transfer.writers.get(file_id)
                if writer is None:
                    raise ProtocolError("收到未知文件的数据")
                written = writer.write(offset, payload)
                transfer.add_received(written)
        except (OSError, ProtocolError, ValueError) as exc:
            reason = f"数据通道 {index + 1} 中断: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("数据读取线程异常")
            reason = f"数据通道 {index + 1} 异常: {exc}"
        finally:
            _close_socket(sock)
            if reason:
                transfer.fail(reason)
            transfer.mark_reader_done()

    def _update_receive_progress(self, transfer: ReceiveTransfer) -> None:
        with self.lock:
            if self._transfer is not transfer or self.status not in {"transferring", "verifying"}:
                return
            with transfer.lock:
                received = transfer.received_new
            self._progress.new_bytes = min(self._progress.total - self._progress.base, received)

    # -- completion ----------------------------------------------------
    def _start_finalize(self, transfer: ReceiveTransfer) -> None:
        with self.lock:
            if self._transfer is not transfer or self.status != "transferring":
                return
            self.status = "verifying"
            self.notice = "正在校验并保存文件…"
            self.error = None
            self._progress.finish()
            channel = self._control
        if channel is not None:
            channel.send({"type": "saving", "message": "接收方正在校验并保存文件"})
        thread = threading.Thread(
            target=self._finalize_worker,
            args=(transfer,),
            name="receive-finalize",
            daemon=True,
        )
        thread.start()

    def _finalize_worker(self, transfer: ReceiveTransfer) -> None:
        errors: list[str] = []
        finalized = 0
        for entry in transfer.files:
            writer = transfer.writers[entry.id]
            try:
                writer.finalize()
                finalized += 1
            except (OSError, ProtocolError) as exc:
                errors.append(str(exc))
        with self.lock:
            if self._transfer is not transfer:
                return
            channel = self._control
            if errors:
                self.status = "error"
                summary = errors[0]
                if len(errors) > 1:
                    summary += f"（另有 {len(errors) - 1} 个文件失败）"
                self.error = f"有 {len(errors)} 个文件保存失败：{summary}"
                self.notice = None
                ok = False
                payload = {
                    "type": "completed",
                    "ok": False,
                    "error": self.error,
                    "files": finalized,
                }
            else:
                self.status = "completed"
                self.error = None
                self.notice = "全部文件已校验并保存"
                ok = True
                self._progress.finish()
                total_elapsed = float(self._progress.snapshot()["elapsed"])
                self._completed_payload = {
                    "files": len(transfer.files),
                    "bytes": transfer.total_bytes,
                    "elapsed": total_elapsed,
                    "save_dir": str(transfer.save_dir),
                    "resumed_files": self._resume.files,
                    "resumed_bytes": self._resume.bytes,
                }
                payload = {
                    "type": "completed",
                    "ok": True,
                    "files": len(transfer.files),
                    "bytes": transfer.total_bytes,
                    "elapsed": round(total_elapsed, 3),
                }
                self._control = None
            self._transfer = None
        if errors:
            # Keep sidecars/parts for a future resume; mark done records for
            # files that did save successfully already exist.
            pass
        else:
            self._cleanup_states(transfer.save_dir, transfer.files)
        if channel is not None:
            try:
                channel.send(payload)
            finally:
                channel.close("接收完成" if ok else "接收失败")

    def _cleanup_states(self, root: Path, files: list[RemoteFile]) -> None:
        for entry in files:
            for path in (_state_path(root, entry.id), _part_path(root, entry.id)):
                try:
                    path.unlink()
                except OSError:
                    pass
        for directory in (
            root / RESUME_DIR_NAME / "state",
            root / RESUME_DIR_NAME / "parts",
            root / RESUME_DIR_NAME,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    # -- errors / disconnects -----------------------------------------
    def _on_receive_transfer_error(self, transfer: ReceiveTransfer, reason: str) -> None:
        with self.lock:
            if self._transfer is not transfer or self.status in {"error", "cancelled", "completed"}:
                return
            self.status = "error"
            self.error = reason
            self.notice = None
            channel = self._control
        if channel is not None:
            channel.send({"type": "error", "message": reason})
            channel.close("接收失败")
        transfer.abort(reason)

    def _remote_error(self, reason: str) -> None:
        with self.lock:
            if self.status in TERMINAL_STATUSES:
                return
            self.status = "error"
            self.error = reason
            self.notice = None
            channel = self._control
            transfer = self._transfer
        if transfer is not None:
            transfer.abort(reason)
        if channel is not None:
            channel.close("发送方中止")

    def _abort_with_error(self, reason: str) -> None:
        with self.lock:
            if self.status in TERMINAL_STATUSES:
                return
            self.status = "error"
            self.error = reason
            self.notice = None
            channel = self._control
            self._control = None
            transfer = self._transfer
        if transfer is not None:
            transfer.abort(reason)
        if channel is not None:
            channel.send({"type": "error", "message": reason})
            channel.close("接收错误")

    def _on_control_closed(self, channel: ControlChannel, reason: str) -> None:
        with self.lock:
            if channel is not self._control:
                return
            self._control = None
            current = self.status
            if current in TERMINAL_STATUSES:
                return
            if current == "verifying":
                self.notice = "发送方已断开，正在完成本地保存…"
                return
            self.status = "error"
            self.error = f"与发送方的连接已断开: {reason}"
            self.notice = None
            transfer = self._transfer
        if transfer is not None:
            transfer.abort("连接断开")

    # -- utilities -----------------------------------------------------
    def _path_adaptation_state_locked(self) -> dict[str, Any]:
        payload = dict(self._path_adaptation)
        payload["warnings"] = list(self._path_warnings)
        return payload

    def _resume_dict_locked(self) -> dict[str, Any]:
        needed = 0
        if self._save_dir is not None:
            total = sum(item.size for item in self.manifest)
            needed = max(0, total - self._resume.bytes)
        space_ok = self._free_space is None or needed <= self._free_space
        return {
            "files": self._resume.files,
            "bytes": self._resume.bytes,
            "warnings": list(self._resume.warnings or []),
            "needed_space": needed,
            "free_space": self._free_space,
            "space_ok": space_ok,
        }

    def _close_connecting_locked(self) -> None:
        sock = self._connecting_socket
        self._connecting_socket = None
        if sock is not None:
            _close_socket(sock)

    def files_page(self, offset: int, limit: int) -> tuple[list[dict[str, object]], int]:
        with self.lock:
            total = len(self.manifest)
            start = max(0, offset)
            end = min(total, start + max(0, limit))
            items = [item.public() for item in self.manifest[start:end]]
            return items, total

    def state(self) -> dict[str, Any]:
        with self.lock:
            manifest_total = sum(item.size for item in self.manifest)
            resume = self._resume_dict_locked()
            transfer = self._transfer
            connections = transfer.connection_count() if transfer is not None else 0
            progress = self._progress.snapshot(connections=connections)
            can_start = (
                self.status == "review"
                and self._save_dir is not None
                and self._control is not None
                and bool(self.manifest)
                and bool(resume.get("space_ok"))
            )
            peer = None
            if self._sender_name:
                peer = {
                    "host": self.host,
                    "port": self.port,
                    "name": self._sender_name,
                    "address": self._peer_ip,
                }
            return {
                "status": self.status,
                "busy": self.status in BUSY_STATUSES,
                "error": self.error,
                "notice": self.notice,
                "peer": peer,
                "host": self.host,
                "port": self.port,
                "save_dir": str(self._save_dir) if self._save_dir else None,
                "free_space": self._free_space,
                "can_start": can_start,
                "manifest": {
                    "count": len(self.manifest),
                    "total_size": manifest_total,
                    "revision": self._manifest_revision,
                },
                "files_count": len(self.manifest),
                "resume": resume,
                "path_adaptation": self._path_adaptation_state_locked(),
                "progress": progress,
                "completed": self._completed_payload,
                "data_ports": list(self._data_ports),
            }


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
