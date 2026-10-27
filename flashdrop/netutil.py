"""Socket and address helpers."""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable

from .protocol import format_socket_peer


def listener(host: str, port: int, backlog: int = 8) -> socket.socket:
    """Create a listening TCP socket without platform-specific address reuse."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: do not silently share a listener with another process.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        sock.listen(backlog)
    except OSError:
        sock.close()
        raise
    return sock


def configure_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def open_data_listeners(
    host: str,
    count: int,
    *,
    preferred_start: int,
    reserved_ports: Iterable[int] = (),
) -> tuple[list[socket.socket], list[int]]:
    """Open ``count`` data listeners, scanning for free ports.

    All sockets are returned open; callers own them. Ports need not be
    consecutive. The scan is bounded to one pass over the valid port range.
    """

    reserved = set(reserved_ports)
    if not 1 <= count <= 128:
        raise ValueError("数据端口数量不合法")

    candidates = list(range(1024, 65536))
    start = preferred_start if 1024 <= preferred_start <= 65535 else 1024
    candidates = candidates[candidates.index(start) :] + candidates[: candidates.index(start)]

    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for port in candidates:
            if len(sockets) == count:
                break
            if port in reserved:
                continue
            try:
                sock = listener(host, port, backlog=4)
            except OSError:
                continue
            sockets.append(sock)
            ports.append(port)
    except Exception:
        for sock in sockets:
            sock.close()
        raise

    if len(sockets) != count:
        for sock in sockets:
            sock.close()
        raise OSError("没有足够的空闲端口用于数据连接")
    return sockets, ports


def _socket_ipv4_addresses() -> list[str]:
    found: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return
        if ip.version != 4 or ip.is_unspecified or ip.is_loopback or ip.is_multicast:
            return
        if value not in found:
            found.append(value)

    # UDP connect does not send packets. It only asks the OS which source
    # address it would use for an outbound route.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass

    def sort_key(value: str) -> tuple[int, str]:
        try:
            private_rank = 0 if ipaddress.ip_address(value).is_private else 1
        except ValueError:
            private_rank = 2
        return (private_rank, value)

    return sorted(found, key=sort_key)


def display_addresses() -> list[str]:
    """Addresses a receiver can use, primary first, loopback last."""

    addresses = _socket_ipv4_addresses()
    addresses.append("127.0.0.1")
    return addresses


def peer_label(peer: object) -> str:
    return format_socket_peer(peer)
