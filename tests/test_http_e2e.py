from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from flashdrop.app import FlashDropApp
from flashdrop.web import FlashDropWebServer
from tests.support import collect_files, http_json, http_text, make_tree, wait_until, workspace_temp


class HttpEndToEndTests(unittest.TestCase):
    def _start_app(self) -> tuple[FlashDropApp, FlashDropWebServer]:
        app = FlashDropApp()
        server = FlashDropWebServer(app, host="127.0.0.1", port=0)
        server.start()
        self.addCleanup(app.shutdown)
        self.addCleanup(server.stop)
        return app, server

    def _state(self, server: FlashDropWebServer) -> dict:
        return http_json(f"{server.url}api/state")

    def test_http_full_transfer(self) -> None:
        base = workspace_temp(self)
        source = base / "源文件"
        make_tree(
            source,
            {
                "a.txt": "HTTP API 测试\n".encode(),
                "sub/b.bin": os.urandom(2_500_000),
                "sub/empty": b"",
            },
        )
        destination = base / "目标"
        destination.mkdir()

        sender_app, sender_server = self._start_app()
        receiver_app, receiver_server = self._start_app()

        state = self._state(sender_server)
        self.assertEqual(state["mode"], "home")
        http_json(f"{sender_server.url}api/mode", "POST", {"mode": "send"})
        added = http_json(f"{sender_server.url}api/send/add", "POST", {"paths": str(source)})
        self.assertEqual(added["added"], 3)

        sender_state = self._state(sender_server)
        self.assertEqual(sender_state["send"]["files"]["count"], 3)
        files = http_json(f"{sender_server.url}api/send/files?offset=0&limit=10")
        self.assertEqual(files["total"], 3)
        self.assertEqual(len(files["files"]), 3)

        http_json(
            f"{sender_server.url}api/send/start",
            "POST",
            {"port": 0, "connections": 3},
        )
        wait_until(
            lambda: self._state(sender_server)["send"]["status"] == "listening",
            label="HTTP 发送端监听",
            diagnostics=lambda: json.dumps(self._state(sender_server), ensure_ascii=False),
        )
        listening = self._state(sender_server)["send"]["listen"]
        self.assertIsNotNone(listening)
        self.assertEqual(len(listening["data_ports"]), 3)

        http_json(f"{receiver_server.url}api/mode", "POST", {"mode": "receive"})
        http_json(
            f"{receiver_server.url}api/receive/connect",
            "POST",
            {"host": "127.0.0.1", "port": listening["port"]},
        )
        wait_until(
            lambda: self._state(receiver_server)["receive"]["status"] == "review",
            label="HTTP 接收端进入确认",
            diagnostics=lambda: json.dumps(self._state(receiver_server), ensure_ascii=False),
        )
        receiver_state = self._state(receiver_server)
        self.assertEqual(receiver_state["receive"]["manifest"]["count"], 3)
        self.assertIsNone(receiver_state["receive"]["save_dir"])
        self.assertFalse(receiver_state["receive"]["can_start"])

        result = http_json(
            f"{receiver_server.url}api/receive/set-dir",
            "POST",
            {"path": str(destination)},
        )
        self.assertTrue(result["resume"]["space_ok"])
        self.assertTrue(self._state(receiver_server)["receive"]["can_start"])

        receiver_files = http_json(f"{receiver_server.url}api/receive/files?offset=0&limit=2")
        self.assertEqual(receiver_files["total"], 3)
        self.assertEqual(len(receiver_files["files"]), 2)

        http_json(f"{receiver_server.url}api/receive/start", "POST", {})
        wait_until(
            lambda: self._state(receiver_server)["receive"]["status"] in {"completed", "error"},
            label="HTTP 接收结束",
            timeout=30,
            diagnostics=lambda: json.dumps(self._state(receiver_server), ensure_ascii=False),
        )
        final_receive = self._state(receiver_server)["receive"]
        self.assertEqual(final_receive["status"], "completed", final_receive.get("error"))
        wait_until(
            lambda: self._state(sender_server)["send"]["status"] == "completed",
            label="HTTP 发送完成",
            timeout=10,
            diagnostics=lambda: json.dumps(self._state(sender_server), ensure_ascii=False),
        )
        expected = {f"{source.name}/{rel}" for rel in collect_files(source)}
        actual = set(collect_files(destination))
        self.assertEqual(actual, expected)

    def test_static_ui_and_assets(self) -> None:
        _, server = self._start_app()
        html = http_text(server.url)
        self.assertIn("FlashDrop", html)
        self.assertIn("我要发送", html)
        self.assertIn("我要接收", html)
        self.assertIn('id="send-file-list"', html)
        js = http_text(f"{server.url}app.js")
        self.assertIn("/api/send/start", js)
        css = http_text(f"{server.url}style.css")
        self.assertIn("--accent", css)

    def test_mode_and_validation_errors(self) -> None:
        _, server = self._start_app()
        with self.assertRaisesRegex(AssertionError, "模式"):
            http_json(f"{server.url}api/send/add", "POST", {"paths": "C:\\nope"})
        http_json(f"{server.url}api/mode", "POST", {"mode": "send"})
        with self.assertRaisesRegex(AssertionError, "文件"):
            http_json(f"{server.url}api/send/start", "POST", {"port": 45870, "connections": 4})
        base = workspace_temp(self)
        sample = base / "sample.txt"
        sample.write_text("sample", encoding="utf-8")
        http_json(f"{server.url}api/send/add", "POST", {"paths": str(sample)})
        with self.assertRaisesRegex(AssertionError, "端口"):
            http_json(f"{server.url}api/send/start", "POST", {"port": 70000, "connections": 4})
        html = http_text(server.url)
        self.assertTrue(html.startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
