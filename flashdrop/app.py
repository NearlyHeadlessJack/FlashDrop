"""Application facade used by the local HTTP API."""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .catalog import AddResult, SenderCatalog
from .config import DEFAULT_CONNECTIONS, DEFAULT_CONTROL_PORT
from .errors import UserError
from .receiver import ReceiveService
from .sender import SendService

log = logging.getLogger(__name__)

MODES = {"home", "send", "receive"}


class FlashDropApp:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.mode = "home"
        self.catalog = SenderCatalog()
        self.sender = SendService(self.catalog)
        self.receiver = ReceiveService()

    # -- mode ----------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise UserError("未知的模式")
        with self.lock:
            if mode == self.mode:
                return
            if self.sender.busy or self.receiver.busy:
                raise UserError("当前有传输任务正在进行，请先停止或完成")
            self.mode = mode

    def state(self) -> dict[str, Any]:
        with self.lock:
            mode = self.mode
        return {
            "app": "FlashDrop",
            "version": __version__,
            "mode": mode,
            "busy": bool(self.sender.busy or self.receiver.busy),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            "defaults": {
                "control_port": DEFAULT_CONTROL_PORT,
                "connections": DEFAULT_CONNECTIONS,
            },
            "send": self.sender.state(),
            "receive": self.receiver.state(),
        }

    # -- sender --------------------------------------------------------
    def _require_mode(self, mode: str) -> None:
        with self.lock:
            if self.mode != mode:
                raise UserError("当前模式不匹配，请刷新页面")

    def sender_add_paths(self, paths: list[str]) -> AddResult:
        self._require_mode("send")
        return self.sender.add_paths(paths)

    def sender_remove(self, file_id: str) -> bool:
        self._require_mode("send")
        return self.sender.remove(file_id)

    def sender_clear(self) -> None:
        self._require_mode("send")
        self.sender.clear()

    def sender_start(self, port: int, connections: int) -> None:
        self._require_mode("send")
        self.sender.start(port, connections)

    def sender_stop(self) -> None:
        self._require_mode("send")
        self.sender.stop()

    def sender_reset(self) -> None:
        self._require_mode("send")
        self.sender.reset()

    def sender_files(self, offset: int, limit: int) -> dict[str, Any]:
        self._require_mode("send")
        files, total = self.sender.files_page(offset, limit)
        stats = self.catalog.stats()
        return {
            "files": files,
            "total": total,
            "offset": offset,
            "limit": limit,
            "revision": stats.revision,
        }

    # -- receiver ------------------------------------------------------
    def receiver_connect(self, host: str, port: int) -> None:
        self._require_mode("receive")
        self.receiver.connect(host, port)

    def receiver_set_dir(self, path: str) -> dict[str, Any]:
        self._require_mode("receive")
        return self.receiver.set_save_dir(path)

    def receiver_start(self) -> None:
        self._require_mode("receive")
        self.receiver.start()

    def receiver_cancel(self) -> None:
        self._require_mode("receive")
        self.receiver.cancel()

    def receiver_reset(self) -> None:
        self._require_mode("receive")
        self.receiver.reset()

    def receiver_files(self, offset: int, limit: int) -> dict[str, Any]:
        self._require_mode("receive")
        files, total = self.receiver.files_page(offset, limit)
        return {
            "files": files,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    # -- native picker -------------------------------------------------
    def pick(self, kind: str) -> list[str]:
        if kind not in {"files", "folder", "save-dir"}:
            raise UserError("未知的选择类型")
        script = Path(__file__).with_name("picker.py")
        command = [sys.executable, str(script), "--kind", kind]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                creationflags=creationflags,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UserError("文件选择窗口等待超时") from exc
        except OSError as exc:
            raise UserError(f"无法启动文件选择窗口: {exc}") from exc

        payload: dict[str, Any] = {}
        for line in reversed((completed.stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if completed.returncode != 0:
            message = payload.get("error") or (completed.stderr or "").strip() or "文件选择窗口异常退出"
            raise UserError(f"{message}。也可以使用“手动输入路径”。")
        paths = payload.get("paths")
        if not isinstance(paths, list):
            raise UserError("文件选择窗口没有返回有效路径")
        return [str(path) for path in paths if path]

    # -- lifecycle -----------------------------------------------------
    def shutdown(self) -> None:
        try:
            self.sender.stop()
        except Exception:  # noqa: BLE001
            log.exception("关闭发送服务失败")
        try:
            self.receiver.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.receiver.reset()
        except Exception:  # noqa: BLE001
            pass
