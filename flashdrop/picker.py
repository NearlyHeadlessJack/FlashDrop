"""Standalone native file/folder picker.

Run this module in a fresh process so GUI toolkits have a proper main thread.
On macOS it first uses the built-in Standard Additions dialog through
``osascript`` (no Tk dependency). If that is unavailable it falls back to Tk;
Windows/Linux go straight to Tk. The app reads one JSON object from stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

_PROMPTS = {
    "files": "选择要发送的文件",
    "folder": "选择要发送的文件夹",
    "save-dir": "选择保存到的文件夹",
}


def _jxa_source(kind: str) -> str:
    """Build the JavaScript for Automation dialog script.

    The JSON string is returned as the final expression. osascript prints the
    string; the Python side also accepts a double-JSON-encoded result.
    """

    prompt = json.dumps(_PROMPTS[kind], ensure_ascii=False)
    if kind == "files":
        choose = (
            "app.chooseFile({"
            f"withPrompt: {prompt}, "
            "multipleSelectionsAllowed: true"
            "})"
        )
    else:
        choose = f"app.chooseFolder({{withPrompt: {prompt}}})"
    return f"""
(function () {{
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;
  function asPath(item) {{
    try {{ return item.toString(); }} catch (error) {{ return String(item); }}
  }}
  try {{
    var picked = {choose};
    if (!picked) {{
      return JSON.stringify({{paths: []}});
    }}
    var list = Array.isArray(picked) ? picked : [picked];
    return JSON.stringify({{paths: list.map(asPath)}});
  }} catch (error) {{
    var message = String(error);
    if ((error && error.errorNumber === -128) || message.indexOf("-128") >= 0) {{
      return JSON.stringify({{paths: []}});
    }}
    return JSON.stringify({{paths: [], error: message}});
  }}
}})();
"""


def _parse_jxa_output(stdout: str) -> dict[str, Any]:
    """Parse osascript output, tolerating a double-encoded JSON string."""

    text = (stdout or "").strip()
    if not text:
        raise ValueError("osascript 没有返回结果")

    def decode(value: str) -> Any:
        return json.loads(value)

    parsed: Any
    try:
        parsed = decode(text)
    except json.JSONDecodeError:
        # Be forgiving if osascript prepends a diagnostic line to stdout.
        match = re.search(r"\{.*\}\s*$", text, re.DOTALL)
        if not match:
            raise
        parsed = decode(match.group(0))

    if isinstance(parsed, str):
        parsed = decode(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("选择器返回格式错误")
    paths = parsed.get("paths")
    if not isinstance(paths, list):
        raise ValueError("选择器没有返回路径列表")
    cleaned = [str(path) for path in paths if str(path)]
    return {"paths": cleaned, "error": parsed.get("error")}


def _macos_pick(kind: str) -> list[str]:
    completed = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", _jxa_source(kind)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or "").strip()
        if "-128" in details or "User canceled" in details:
            return []
        raise OSError(details or f"osascript 退出码 {completed.returncode}")
    payload = _parse_jxa_output(completed.stdout or "")
    if payload.get("error"):
        raise OSError(str(payload["error"]))
    return list(payload["paths"])


def _tk_pick(kind: str) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.update_idletasks()
    try:
        if kind == "files":
            selected = filedialog.askopenfilenames(
                parent=root,
                title=_PROMPTS[kind],
            )
            return [str(path) for path in selected]
        selected = filedialog.askdirectory(
            parent=root,
            title=_PROMPTS[kind],
            mustexist=True,
        )
        return [str(selected)] if selected else []
    finally:
        root.destroy()


def _write_result(payload: dict[str, Any]) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["files", "folder", "save-dir"], required=True)
    args = parser.parse_args()

    errors: list[str] = []
    paths: list[str] | None = None

    if sys.platform == "darwin":
        try:
            paths = _macos_pick(args.kind)
        except Exception as exc:  # noqa: BLE001 - fall back to Tk once
            errors.append(f"macOS 原生选择器不可用: {exc}")

    if paths is None:
        try:
            paths = _tk_pick(args.kind)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
            errors.append(f"Tk 文件选择窗口不可用: {exc}")

    if paths is None:
        _write_result(
            {
                "paths": [],
                "error": "；".join(errors) or "无法打开系统文件选择窗口",
            }
        )
        return 2

    # A successfully opened dialog returning an empty list means the user
    # canceled; that is not an error.
    _write_result({"paths": paths})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
