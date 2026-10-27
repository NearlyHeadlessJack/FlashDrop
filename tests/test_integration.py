from __future__ import annotations

import json
import os
import stat
import time
import unittest
from pathlib import Path

from flashdrop.catalog import prepare_manifest
from flashdrop.config import DEFAULT_CHUNK_SIZE, RESUME_STATE_VERSION
from flashdrop.protocol import file_id_for
from flashdrop.receiver import ReceiveService, scan_resume
from tests.support import TransferPair, collect_files, make_tree, wait_until, workspace_temp


class IntegrationTests(unittest.TestCase):
    def _make_sources(self, base: Path) -> Path:
        source = base / "source folder"
        make_tree(
            source,
            {
                "hello.txt": "你好，FlashDrop\n".encode(),
                "nested/empty.bin": b"",
                "nested/random.bin": os.urandom(5 * 1024 * 1024 + 713),
                "nested/deep/data.bin": bytes(range(256)) * 4096,
            },
        )
        (source / "nested" / ".flashdrop").mkdir()
        (source / "nested" / ".flashdrop" / "ignored.txt").write_text("no", encoding="utf-8")
        return source

    def test_end_to_end_multiport_and_nested_tree(self) -> None:
        base = workspace_temp(self)
        source = self._make_sources(base)
        destination = base / "dest"
        destination.mkdir()
        pair = TransferPair(source, destination, connections=4)
        self.addCleanup(pair.close)
        pair.start_to_review()

        self.assertEqual(pair.receiver.state()["manifest"]["count"], 4)
        self.assertEqual(pair.receiver.state()["save_dir"], None)
        pair.receiver.set_save_dir(str(destination))
        preview = pair.receiver.state()["resume"]
        self.assertEqual(preview["files"], 0)
        pair.receiver.start()

        wait_until(
            lambda: pair.receiver.status in {"completed", "error"},
            label="接收完成",
            timeout=30,
            diagnostics=lambda: f"receiver={pair.receiver.state()}; sender={pair.sender.state()}",
        )
        self.assertEqual(pair.receiver.status, "completed", pair.receiver.error)
        wait_until(lambda: pair.sender.status in {"completed", "error"}, label="发送完成", timeout=10)
        self.assertEqual(pair.sender.status, "completed", pair.sender.error)

        expected_root = source.name
        expected = {
            f"{expected_root}/{rel}": data
            for rel, data in collect_files(source).items()
            if ".flashdrop" not in rel.split("/")
        }
        actual = collect_files(destination)
        self.assertEqual(set(actual), set(expected))
        for rel, data in expected.items():
            self.assertEqual(actual[rel], data, rel)

        receiver_state = pair.receiver.state()
        sender_state = pair.sender.state()
        self.assertEqual(receiver_state["progress"]["done"], receiver_state["progress"]["total"])
        self.assertEqual(sender_state["progress"]["done"], sender_state["progress"]["total"])
        self.assertEqual(receiver_state["completed"]["files"], 4)
        self.assertFalse((destination / ".flashdrop").exists())

    def test_resume_from_existing_partial(self) -> None:
        base = workspace_temp(self)
        source = base / "big-source"
        source.mkdir()
        data = os.urandom(10_000_000)
        (source / "big.bin").write_bytes(data)
        destination = base / "dest"
        destination.mkdir()

        pair = TransferPair(source, destination, connections=3)
        self.addCleanup(pair.close)
        manifest = prepare_manifest(pair.catalog.snapshot())
        self.assertEqual(len(manifest), 1)
        item = manifest[0]
        prefix = data[: 4 * 1024 * 1024]
        state_dir = destination / ".flashdrop" / "state"
        parts_dir = destination / ".flashdrop" / "parts"
        state_dir.mkdir(parents=True)
        parts_dir.mkdir(parents=True)
        (parts_dir / f"{item.id}.part").write_bytes(prefix)
        (state_dir / f"{item.id}.json").write_text(
            json.dumps(
                {
                    "version": RESUME_STATE_VERSION,
                    "id": item.id,
                    "path": item.rel_path,
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                    "quick_hash": item.quick_hash,
                    "status": "partial",
                }
            ),
            encoding="utf-8",
        )

        pair.start_to_review()
        pair.receiver.set_save_dir(str(destination))
        preview = pair.receiver.state()["resume"]
        self.assertEqual(preview["files"], 1)
        self.assertEqual(preview["bytes"], len(prefix))
        self.assertEqual(preview["needed_space"], len(data) - len(prefix))
        pair.receiver.start()
        wait_until(
            lambda: pair.receiver.status in {"completed", "error"},
            label="续传完成",
            timeout=30,
            diagnostics=lambda: f"receiver={pair.receiver.state()}; sender={pair.sender.state()}",
        )
        self.assertEqual(pair.receiver.status, "completed", pair.receiver.error)
        wait_until(lambda: pair.sender.status == "completed", label="发送端确认续传完成", timeout=10)
        final_path = destination / source.name / "big.bin"
        self.assertEqual(final_path.read_bytes(), data)
        self.assertFalse((destination / ".flashdrop").exists())
        sender_state = pair.sender.state()
        self.assertEqual(sender_state["resume"]["files"], 1)
        self.assertEqual(sender_state["resume"]["bytes"], len(prefix))
        self.assertEqual(sender_state["progress"]["new_bytes"], len(data) - len(prefix))

    def test_receiver_cancel_in_review_returns_sender_to_listening(self) -> None:
        base = workspace_temp(self)
        source = base / "retry-source"
        make_tree(source, {"a.txt": b"retry me"})
        destination = base / "dest"
        destination.mkdir()
        pair = TransferPair(source, destination, connections=2)
        self.addCleanup(pair.close)
        pair.start_to_review()
        pair.receiver.cancel()
        wait_until(lambda: pair.sender.status == "listening", label="发送端回到监听", timeout=10)
        port = pair.sender.state()["listen"]["port"]
        pair.receiver.reset()
        pair.receiver.connect("127.0.0.1", port)
        wait_until(lambda: pair.receiver.status == "review", label="第二次确认", timeout=10)
        pair.complete()
        self.assertEqual(
            (destination / source.name / "a.txt").read_bytes(),
            b"retry me",
        )

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX executable mode")
    def test_posix_executable_mode_survives_end_to_end(self) -> None:
        base = workspace_temp(self)
        source = base / "scripts"
        source.mkdir()
        script = source / "run.sh"
        script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        os.chmod(script, 0o750)
        destination = base / "dest"
        destination.mkdir()
        pair = TransferPair(source, destination, connections=2)
        self.addCleanup(pair.close)
        pair.start_to_review()
        pair.complete()
        target = destination / source.name / "run.sh"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)

    def test_windows_manifest_path_adaptation_preview(self) -> None:
        service = ReceiveService()
        path = "mac folder/报告:终稿?.pdf"
        message = {
            "type": "welcome",
            "protocol": 1,
            "session_id": "a" * 32,
            "data_token": "b" * 32,
            "data_ports": [45871],
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "sender_name": "Mac",
            "manifest": {
                "count": 1,
                "total_size": 3,
                "files": [
                    {
                        "id": file_id_for(path),
                        "path": path,
                        "size": 3,
                        "mtime_ns": 123,
                        "quick_hash": "q" * 40,
                    }
                ],
            },
        }
        parsed = service._parse_welcome(message)  # noqa: SLF001 - focused contract test
        item = parsed["files"][0]
        if os.name == "nt":
            self.assertNotEqual(item.destination_rel, item.rel_path)
            self.assertNotIn(":", item.destination_rel)
            self.assertNotIn("?", item.destination_rel)
            self.assertEqual(parsed["path_plan"].renamed_count, 1)
        else:
            self.assertEqual(item.destination_rel, item.rel_path)
            self.assertEqual(parsed["path_plan"].renamed_count, 0)
        public = item.public()
        self.assertEqual(public["dest_rel"], item.destination_rel)
        self.assertIn("renamed", public)

    def test_scan_resume_ignores_bad_or_unrelated_records(self) -> None:
        base = workspace_temp(self)
        source = base / "src"
        source.mkdir()
        (source / "a.bin").write_bytes(b"a" * 100)
        destination = base / "dest"
        destination.mkdir()

        from flashdrop.catalog import SenderCatalog

        catalog = SenderCatalog()
        catalog.add_paths([str(source)])
        manifest = prepare_manifest(catalog.snapshot())
        item = manifest[0]
        from flashdrop.receiver import RemoteFile

        remote = RemoteFile(item.id, item.rel_path, item.size, item.mtime_ns, item.quick_hash)
        state_dir = destination / ".flashdrop" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / f"{item.id}.json").write_text(
            json.dumps(
                {
                    "version": RESUME_STATE_VERSION,
                    "id": item.id,
                    "path": item.rel_path,
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                    "quick_hash": "wrong",
                    "status": "partial",
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "broken.json").write_text("{not json", encoding="utf-8")
        match = scan_resume(destination, [remote])
        self.assertEqual(match.files, 0)
        self.assertEqual(match.bytes, 0)


if __name__ == "__main__":
    unittest.main()
