from __future__ import annotations

import unittest
from pathlib import Path

from flashdrop.protocol import ProtocolError, file_id_for
from flashdrop.receiver import ReceiveFileWriter, RemoteFile
from tests.support import workspace_temp


class ReceiveWriterTests(unittest.TestCase):
    def _writer(self, base: Path, data: bytes, initial: int = 0) -> tuple[ReceiveFileWriter, RemoteFile, Path]:
        save = base / "save"
        save.mkdir()
        entry = RemoteFile(
            id=file_id_for("a/test.bin"),
            rel_path="a/test.bin",
            size=len(data),
            mtime_ns=1_700_000_000_000_000_000,
            quick_hash="q" * 40,
        )
        writer = ReceiveFileWriter(entry, save, initial, done_existing=False)
        if initial:
            part = save / ".flashdrop" / "parts" / f"{entry.id}.part"
            part.parent.mkdir(parents=True)
            part.write_bytes(data[:initial])
        return writer, entry, save

    def test_out_of_order_chunks_are_committed_in_order(self) -> None:
        base = workspace_temp(self)
        data = b"0123456789abcdef"
        writer, entry, save = self._writer(base, data)
        # Deliver the tail first, then the head. The writer must buffer the
        # tail and only expose a contiguous file.
        self.assertEqual(writer.write(8, data[8:]), 0)
        self.assertEqual(writer.write(0, data[:8]), 16)
        writer.finalize()
        self.assertEqual((save / entry.rel_path).read_bytes(), data)
        self.assertFalse((save / ".flashdrop" / "parts" / f"{entry.id}.part").exists())

    def test_overlapping_chunk_is_rejected(self) -> None:
        base = workspace_temp(self)
        writer, _entry, _save = self._writer(base, b"abcdefgh")
        writer.write(0, b"abcd")
        with self.assertRaises(ProtocolError):
            writer.write(3, b"defgh")
        writer.close()

    def test_finalize_rejects_incomplete_file(self) -> None:
        base = workspace_temp(self)
        writer, _entry, _save = self._writer(base, b"abcdefgh")
        writer.write(0, b"abcd")
        with self.assertRaises(OSError):
            writer.finalize()
        writer.close()


if __name__ == "__main__":
    unittest.main()
