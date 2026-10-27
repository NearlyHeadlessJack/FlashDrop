from __future__ import annotations

import socket
import struct
import threading
import unicodedata
import unittest

from flashdrop.protocol import (
    DATA_HEADER,
    DATA_MAGIC,
    ProtocolError,
    encode_control,
    file_id_for,
    make_manifest_entry,
    recv_control,
    safe_transfer_path,
    validate_manifest_entry,
)


class ProtocolTests(unittest.TestCase):
    def test_file_id_is_stable_and_normalized(self) -> None:
        first = file_id_for("folder/file.bin")
        self.assertEqual(len(first), 32)
        # IDs are case-folded so case-insensitive destination filesystems
        # cannot receive two colliding entries.
        self.assertEqual(first, file_id_for("folder/FILE.bin"))
        nfd = unicodedata.normalize("NFD", "café.txt")
        nfc = unicodedata.normalize("NFC", "café.txt")
        self.assertEqual(file_id_for(nfd), file_id_for(nfc))
        # A backslash is a literal macOS filename character, not a separator.
        self.assertNotEqual(file_id_for("folder\\file.bin"), file_id_for("folder/file.bin"))
        self.assertNotEqual(first, file_id_for("folder/other.bin"))

    def test_safe_transfer_path_accepts_nested(self) -> None:
        self.assertEqual(safe_transfer_path("a/b/c.txt"), "a/b/c.txt")
        # Backslashes are legal POSIX filename characters. The Windows
        # receiver adapts them to safe names; a macOS receiver preserves them.
        self.assertEqual(safe_transfer_path("a\\b\\c.txt"), "a\\b\\c.txt")
        self.assertEqual(safe_transfer_path("./a//b.txt"), "a/b.txt")
        nfd = unicodedata.normalize("NFD", "café.txt")
        self.assertEqual(safe_transfer_path(nfd), "café.txt")

    def test_safe_transfer_path_rejects_escape_and_reserved(self) -> None:
        bad = [
            "",
            "/etc/passwd",
            "\\server\\share",
            "../secret",
            "a/../../secret",
            "C:\\Windows\\system.ini",
            ".flashdrop",
            ".flashdrop/state/x.json",
            "a\x00b",
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    safe_transfer_path(value)

    def test_data_header_layout(self) -> None:
        raw_id = bytes.fromhex(file_id_for("x"))
        header = DATA_HEADER.pack(DATA_MAGIC, raw_id, 123456789, 4 * 1024 * 1024, 0xDEADBEEF)
        magic, parsed_id, offset, length, crc = DATA_HEADER.unpack(header)
        self.assertEqual(magic, DATA_MAGIC)
        self.assertEqual(parsed_id, raw_id)
        self.assertEqual(offset, 123456789)
        self.assertEqual(length, 4 * 1024 * 1024)
        self.assertEqual(crc, 0xDEADBEEF)
        self.assertEqual(DATA_HEADER.size, 36)

    def test_control_roundtrip_over_socketpair(self) -> None:
        left, right = socket.socketpair()
        received: list[dict] = []
        try:
            payload = {"type": "hello", "text": "你好", "n": 42}
            thread = threading.Thread(target=lambda: received.append(recv_control(right)))
            thread.start()
            left.sendall(encode_control(payload))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(received, [payload])
        finally:
            left.close()
            right.close()

    def test_optional_posix_mode_roundtrip(self) -> None:
        entry = make_manifest_entry(
            file_id=file_id_for("script.sh"),
            rel_path="script.sh",
            size=10,
            mtime_ns=123,
            quick_hash="q" * 40,
            mode=0o755,
        )
        self.assertEqual(entry["mode"], 0o755)
        self.assertEqual(validate_manifest_entry(entry)["mode"], 0o755)
        without_mode = dict(entry)
        without_mode.pop("mode")
        self.assertIsNone(validate_manifest_entry(without_mode)["mode"])
        for invalid in (0o1000, 0o10000, -1):
            with self.subTest(mode=invalid):
                with self.assertRaises(ProtocolError):
                    validate_manifest_entry(dict(entry, mode=invalid))

    def test_control_rejects_bad_length(self) -> None:
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!I", 0))
            with self.assertRaises(ProtocolError):
                recv_control(right)
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
