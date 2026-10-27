# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the single-file FlashDrop build.

Used by .github/workflows/release.yml on Windows, macOS arm64 and macOS x64.
The bundled ``flashdrop/web`` directory is what ``flashdrop.web._static_dir``
looks for under ``sys._MEIPASS`` at runtime.
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "flashdrop" / "web"), "flashdrop/web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "pydoc_data"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="flashdrop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
