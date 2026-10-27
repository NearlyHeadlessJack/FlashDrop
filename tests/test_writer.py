from __future__ import annotations

import os
import stat
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

    def test_writer_uses_adapted_destination_path(self) -> None:
        base = workspace_temp(self)
        save = base / "save"
        save.mkdir()
        entry = RemoteFile(
            id=file_id_for("mac/报告:终稿?.pdf"),
            rel_path="mac/报告:终稿?.pdf",
            dest_rel_path="mac/报告_终稿_~1234abcd.pdf",
            size=2,
            mtime_ns=1,
            quick_hash="q" * 40,
        )
        writer = ReceiveFileWriter(entry, save, 0, done_existing=False)
        writer.write(0, b"ok")
        writer.finalize()
        self.assertEqual(
            (save / "mac" / "报告_终稿_~1234abcd.pdf").read_bytes(),
            b"ok",
        )
        self.assertFalse((save / "mac" / "报告:终稿?.pdf").exists())

    def test_finalize_rejects_incomplete_file(self) -> None:
        base = workspace_temp(self)
        writer, _entry, _save = self._writer(base, b"abcdefgh")
        writer.write(0, b"abcd")
        with self.assertRaises(OSError):
            writer.finalize()
        writer.close()

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX executable mode")
    def test_posix_mode_is_applied_on_finalize(self) -> None:
        base = workspace_temp(self)
        save = base / "save"
        save.mkdir()
        entry = RemoteFile(
            id=file_id_for("scripts/run.sh"),
            rel_path="scripts/run.sh",
            size=4,
            mtime_ns=1_700_000_000_000_000_000,
            quick_hash="q" * 40,
            mode=0o755,
        )
        probe = save / "mode-probe"
        probe.write_bytes(b"")
        os.chmod(probe, 0o755)
        if stat.S_IMODE(probe.stat().st_mode) != 0o755:
            self.skipTest("filesystem does not preserve POSIX permission bits")
        writer = ReceiveFileWriter(entry, save, 0, done_existing=False)
        writer.write(0, b"#!/\n")
        writer.finalize()
        target = save / "scripts" / "run.sh"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
