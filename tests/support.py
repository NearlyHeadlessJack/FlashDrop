from __future__ import annotations

import json
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from flashdrop.catalog import SenderCatalog
from flashdrop.receiver import ReceiveService
from flashdrop.sender import SendService

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def workspace_temp(test_case: unittest.TestCase) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{test_case.__class__.__name__}-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    test_case.addCleanup(shutil.rmtree, path, True)
    return path


def wait_until(
    predicate: Callable[[], Any],
    *,
    label: str,
    timeout: float = 20.0,
    diagnostics: Callable[[], str] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    detail = diagnostics() if diagnostics else ""
    raise AssertionError(f"等待超时：{label}{('；' + detail) if detail else ''}")


def make_tree(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def collect_files(root: Path, skip_resume: bool = True) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if skip_resume and rel.split("/", 1)[0] == ".flashdrop":
            continue
        result[rel] = path.read_bytes()
    return result


class TransferPair:
    def __init__(self, source: Path, destination: Path, connections: int = 4) -> None:
        self.source = source
        self.destination = destination
        self.connections = connections
        self.catalog = SenderCatalog()
        self.sender = SendService(self.catalog)
        self.receiver = ReceiveService()
        result = self.catalog.add_paths([str(source)])
        if not result.added:
            raise AssertionError(f"测试文件未被加入发送列表: {result}")

    def start_to_review(self) -> None:
        self.sender.start(0, self.connections)
        wait_until(
            lambda: self.sender.status == "listening",
            label="发送端进入监听",
            diagnostics=lambda: f"sender={self.sender.state()}",
        )
        listening = self.sender.state()["listen"]
        if not listening:
            raise AssertionError("发送端没有监听信息")
        self.receiver.connect("127.0.0.1", listening["port"])
        wait_until(
            lambda: self.receiver.status in {"review", "error"},
            label="接收端完成连接",
            diagnostics=lambda: f"receiver={self.receiver.state()}",
        )
        if self.receiver.status != "review":
            raise AssertionError(f"接收端连接失败: {self.receiver.state()}")

    def complete(self, timeout: float = 30.0) -> None:
        self.receiver.set_save_dir(str(self.destination))
        self.receiver.start()
        wait_until(
            lambda: self.receiver.status in {"completed", "error"},
            label="接收端结束",
            timeout=timeout,
            diagnostics=lambda: f"receiver={self.receiver.state()}; sender={self.sender.state()}",
        )
        if self.receiver.status != "completed":
            raise AssertionError(f"接收失败: {self.receiver.error}")
        wait_until(
            lambda: self.sender.status in {"completed", "error"},
            label="发送端结束",
            timeout=10,
            diagnostics=lambda: f"sender={self.sender.state()}",
        )
        if self.sender.status != "completed":
            raise AssertionError(f"发送端未确认完成: {self.sender.error}")

    def close(self) -> None:
        try:
            self.sender.stop()
        except Exception:
            pass
        try:
            self.receiver.cancel()
        except Exception:
            pass
        try:
            self.receiver.reset()
        except Exception:
            pass


def http_json(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 8) -> Any:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        finally:
            exc.close()
        raise AssertionError(f"HTTP {exc.code}: {raw}") from exc


def http_text(url: str, timeout: float = 8) -> str:
    with urlrequest.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")
