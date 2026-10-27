"""Standalone native file/folder picker.

Run this module in a fresh process so Tk has a proper main thread on every
platform. The app reads a single JSON object from stdout.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["files", "folder", "save-dir"], required=True)
    args = parser.parse_args()

    result: dict[str, object]
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.update_idletasks()
        if args.kind == "files":
            selected = filedialog.askopenfilenames(
                parent=root,
                title="选择要发送的文件",
            )
            paths = [str(path) for path in selected]
        elif args.kind == "folder":
            selected = filedialog.askdirectory(
                parent=root,
                title="选择要发送的文件夹",
                mustexist=True,
            )
            paths = [str(selected)] if selected else []
        else:
            selected = filedialog.askdirectory(
                parent=root,
                title="选择保存到的文件夹",
                mustexist=True,
            )
            paths = [str(selected)] if selected else []
        root.destroy()
        result = {"paths": paths}
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
        result = {"error": f"无法打开系统文件选择窗口: {exc}", "paths": []}
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False))
        return 2

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
