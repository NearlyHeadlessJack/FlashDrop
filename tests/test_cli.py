"""Tests for the CLI entry point's console handling."""

from __future__ import annotations

import contextlib
import io
import unittest

from flashdrop import cli


class StdioTests(unittest.TestCase):
    def test_redirected_stream_is_switched_to_utf8(self) -> None:
        # cp1252 is what an English Windows uses for a redirected stdout; it
        # cannot encode the Chinese banner.
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")
        with contextlib.redirect_stdout(stream):
            cli._prepare_stdio()
            print("局域网点对点快传")
            stream.flush()
        self.assertEqual(buffer.getvalue().decode("utf-8").strip(), "局域网点对点快传")

    def test_odd_streams_do_not_raise(self) -> None:
        class Dummy:
            pass

        original = (cli.sys.stdout, cli.sys.stderr)
        cli.sys.stdout, cli.sys.stderr = Dummy(), None
        try:
            cli._prepare_stdio()  # must not raise
        finally:
            cli.sys.stdout, cli.sys.stderr = original


if __name__ == "__main__":
    unittest.main()
