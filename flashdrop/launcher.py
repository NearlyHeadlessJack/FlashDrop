"""Single entry point shared by ``python -m flashdrop`` and the frozen build.

A PyInstaller one-file executable cannot spawn a Python interpreter to run
``picker.py`` as a script, so the packaged build relaunches itself with
``--kind`` and dispatches to the standalone picker here.
"""

from __future__ import annotations

import sys


def run() -> int:
    if sys.argv[1:2] == ["--kind"]:
        from . import picker

        return picker.main()

    from .cli import main

    return main()
