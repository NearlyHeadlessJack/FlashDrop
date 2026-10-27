"""Wire framing, control helpers, and path validation.

The control channel is a sequence of length-prefixed UTF-8 JSON objects.  The
data channels carry fixed binary headers followed by raw file bytes.  Both
directions use TCP; the application-level frames make state transitions and
partial writes unambiguous.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import struct
from typing import Any

from .config import MAX_CONTROL_FRAME, RESUME_DIR_NAME
from .naming import normalize_nfc

CONTROL_LENGTH = struct.Struct("!I")
DATA_MAGIC = b"FDX1"
# magic, 16-byte file id, absolute file offset, payload length, crc32
DATA_HEADER = struct.Struct("!4s16sQII")
MAX_PAYLOAD = 32 * 1024 * 1024

_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class ProtocolError(Exception):
    """Raised for malformed or unexpected protocol traffic."""


class PeerClosed(ProtocolError):
    """Raised when a peer closes the socket in the middle of a frame."""


def file_id_for(rel_path: str) -> str:
    """Stable file identity derived from the NFC transfer-relative path.

    Wire paths are NFC-normalized so macOS NFD names and Windows NFC names
    resolve to the same identity. Only ``/`` is a separator.
    """

    normalized = normalize_nfc(rel_path).casefold()
    return hashlib.blake2b(normalized.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def id_to_bytes(file_id: str) -> bytes:
    try:
        raw = bytes.fromhex(file_id)
    except ValueError as exc:  # pragma: no cover - guarded by validation
        raise ProtocolError("非法文件 ID") from exc
    if len(raw) != 16:
        raise ProtocolError("非法文件 ID 长度")
    return raw


def bytes_to_id(raw: bytes) -> str:
    return raw.hex()


def encode_control(message: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("控制消息无法序列化") from exc
    if len(payload) > MAX_CONTROL_FRAME:
        raise ProtocolError("控制消息过大")
    return CONTROL_LENGTH.pack(len(payload)) + payload


def recv_exact(sock: socket.socket, count: int) -> bytes | None:
    """Read exactly ``count`` bytes.

    Returns ``None`` only when the peer cleanly closes before the first byte.
    A close in the middle of a frame raises :class:`PeerClosed`.
    """

    if count < 0:
        raise ProtocolError("非法的读取长度")
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        try:
            chunk = sock.recv(remaining)
        except (ConnectionError, OSError) as exc:
            raise PeerClosed(f"连接读取失败: {exc}") from exc
        if not chunk:
            if remaining == count:
                return None
            raise PeerClosed("连接在数据帧中途关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_control(sock: socket.socket, message: dict[str, Any], lock: Any = None) -> None:
    frame = encode_control(message)
    if lock is None:
        sock.sendall(frame)
        return
    with lock:
        sock.sendall(frame)


def recv_control(sock: socket.socket) -> dict[str, Any] | None:
    header = recv_exact(sock, CONTROL_LENGTH.size)
    if header is None:
        return None
    (length,) = CONTROL_LENGTH.unpack(header)
    if length == 0 or length > MAX_CONTROL_FRAME:
        raise ProtocolError("非法控制帧长度")
    payload = recv_exact(sock, length)
    if payload is None:
        raise PeerClosed("控制帧不完整")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("控制消息不是合法 JSON") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("控制消息缺少 type")
    return message


def validate_file_id(value: Any) -> str:
    if not isinstance(value, str) or not _HEX_ID.fullmatch(value):
        raise ProtocolError("非法文件 ID")
    return value


def safe_transfer_path(raw: Any) -> str:
    """Validate a sender-provided POSIX-relative path.

    Only ``/`` separates components. A backslash is an ordinary filename
    character on macOS/Linux and is translated later if the receiver is
    Windows. The receiver never trusts that the path is safe: absolute paths,
    drive-relative paths, NUL, and ``..`` are rejected.
    """

    if not isinstance(raw, str):
        raise ProtocolError("文件路径缺失")
    if not raw or "\x00" in raw:
        raise ProtocolError("文件路径为空或包含非法字符")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ProtocolError("不允许绝对路径")
    if re.match(r"^[A-Za-z]:", raw):
        raise ProtocolError("不允许盘符路径")
    clean: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ProtocolError("文件路径不能包含 ..")
        clean.append(normalize_nfc(part))
    if not clean:
        raise ProtocolError("文件路径为空")
    if clean[0].casefold() == RESUME_DIR_NAME.casefold():
        raise ProtocolError("文件路径不能使用程序保留目录名")
    return "/".join(clean)


def make_manifest_entry(
    *,
    file_id: str,
    rel_path: str,
    size: int,
    mtime_ns: int,
    quick_hash: str,
    mode: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": file_id,
        "path": rel_path,
        "size": int(size),
        "mtime_ns": int(mtime_ns),
        "quick_hash": quick_hash,
    }
    if mode is not None:
        entry["mode"] = int(mode)
    return entry


def validate_manifest_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ProtocolError("清单条目格式错误")
    file_id = validate_file_id(entry.get("id"))
    path = safe_transfer_path(entry.get("path"))
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProtocolError("文件大小非法")
    mtime_ns = entry.get("mtime_ns")
    if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool):
        raise ProtocolError("文件时间非法")
    quick_hash = entry.get("quick_hash")
    if not isinstance(quick_hash, str) or not quick_hash:
        raise ProtocolError("文件指纹缺失")
    mode = entry.get("mode")
    if mode is not None:
        if (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or not (0 <= mode <= 0o777)
        ):
            raise ProtocolError("文件权限位非法")
    return {
        "id": file_id,
        "path": path,
        "size": size,
        "mtime_ns": mtime_ns,
        "quick_hash": quick_hash,
        "mode": mode,
    }


def format_socket_peer(peer: Any) -> str:
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)
