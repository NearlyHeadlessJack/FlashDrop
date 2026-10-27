"""Shared constants and small configuration helpers."""

from __future__ import annotations

PROTOCOL_VERSION = 1
APP_PROTOCOL_NAME = "flashdrop"

DEFAULT_CONTROL_PORT = 45870
DEFAULT_CONNECTIONS = 4
MIN_CONNECTIONS = 1
MAX_CONNECTIONS = 8
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024

# Control frames are JSON objects prefixed by a 4-byte big-endian length.
MAX_CONTROL_FRAME = 128 * 1024 * 1024
CONTROL_HANDSHAKE_TIMEOUT = 12.0
DATA_CONNECT_TIMEOUT = 10.0
DATA_SETUP_GRACE = 5.0
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 45.0
DATA_READ_TIMEOUT = 120.0
PROGRESS_INTERVAL = 1.0
QUICK_HASH_SAMPLE = 64 * 1024

RESUME_DIR_NAME = ".flashdrop"
RESUME_STATE_VERSION = 1

DEFAULT_UI_PORT = 8765

# Public page/status text is rendered by the frontend. Keep backend errors
# concise and in Chinese so they can be shown directly in the toast area.
