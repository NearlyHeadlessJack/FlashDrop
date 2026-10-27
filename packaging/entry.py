"""PyInstaller entry script; behaves like ``python -m flashdrop``."""

import sys

from flashdrop.launcher import run

if __name__ == "__main__":
    sys.exit(run())
