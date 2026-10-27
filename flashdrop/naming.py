"""Cross-platform filename normalization and Windows destination planning.

The wire format uses POSIX separators. Senders may legitimately have filename
characters that Windows cannot represent; the Windows receiver maps those names
to deterministic, readable alternatives instead of failing halfway through a
transfer. Other platforms keep the source names.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MAX_WINDOWS_COMPONENT_UNITS = 180
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])$", re.IGNORECASE)
_WINDOWS_RESERVED = {"conin$", "conout$", "clock$"}
_HASH_TAG = re.compile(r"~[0-9a-f]{8}$")


class NamingError(Exception):
    """A destination naming plan cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class DestinationPlan:
    mapping: dict[str, str]
    renamed: tuple[tuple[str, str], ...]

    @property
    def renamed_count(self) -> int:
        return len(self.renamed)

    def examples(self, limit: int = 3) -> list[dict[str, str]]:
        return [{"from": source, "to": target} for source, target in self.renamed[:limit]]


def normalize_nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _utf16_units(value: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _truncate_utf16(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


def _windows_reserved(stem: str) -> bool:
    candidate = stem.split(".", 1)[0].strip(" .").casefold()
    return candidate in _WINDOWS_RESERVED or bool(_WINDOWS_DEVICE.fullmatch(candidate))


def _add_hash(name: str, digest: str) -> str:
    stem, extension = os.path.splitext(name)
    if not stem:
        stem = name
        extension = ""
    if not extension or len(extension) > 24:
        stem = name
        extension = ""
    tag = f"~{digest}"
    room = MAX_WINDOWS_COMPONENT_UNITS - _utf16_units(tag) - _utf16_units(extension)
    stem = _truncate_utf16(stem, max(1, room)).rstrip(" .")
    if not stem:
        stem = "_"
    candidate = f"{stem}{tag}{extension}"
    # The suffix is evidence that this is an adapted name, not a source name.
    return candidate


def windows_safe_component(component: str, *, context: str) -> tuple[str, bool]:
    """Return ``(safe_component, changed)`` for a Windows filename component.

    The suffix hash makes adaptation stable across manifest order and resume
    sessions. It is derived from the source component and its source parent.
    """

    normalized = normalize_nfc(component)
    changed = False
    safe = _WINDOWS_FORBIDDEN.sub("_", normalized)
    safe = safe.rstrip(" .")
    if not safe or safe in {".", ".."}:
        safe = "_"
        changed = True
    if _windows_reserved(safe):
        safe = f"_{safe}"
        changed = True
    if safe != normalized:
        changed = True
    if _utf16_units(safe) > MAX_WINDOWS_COMPONENT_UNITS:
        changed = True
    if not changed:
        # Guard against a clean name that merely ends in our hash syntax and
        # would be ambiguous when explaining adapted paths to the user.
        if _HASH_TAG.search(os.path.splitext(safe)[0]):
            changed = True
        else:
            return safe, False

    digest = hashlib.blake2b(
        f"{context}|{normalized}|flashdrop-win-v1".encode("utf-8", "surrogatepass"),
        digest_size=4,
    ).hexdigest()
    return _add_hash(safe, digest), True


def plan_destinations(paths: Sequence[str], *, windows: bool) -> DestinationPlan:
    """Map source-relative paths to deterministic destination-relative paths.

    Windows adaptation is intentionally conservative: invalid characters are
    replaced, reserved/trailing-dot/trailing-space names are tagged, and overly
    long components are shortened. Clean names on non-Windows targets are left
    untouched. If two source paths would still collide after adaptation, the
    receiver refuses the manifest rather than overwriting a file.
    """

    if not windows:
        return DestinationPlan(mapping={path: path for path in paths}, renamed=())

    mapping: dict[str, str] = {}
    renamed: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    directory_sources: dict[str, str] = {}
    for source in paths:
        parts = source.split("/")
        target_parts: list[str] = []
        source_prefix = ""
        for index, component in enumerate(parts):
            source_prefix = f"{source_prefix}/{component}" if source_prefix else component
            context = source_prefix
            safe, _changed = windows_safe_component(component, context=context)
            if not safe or safe in {".", ".."}:
                safe = "_"
            target_parts.append(safe)
            if index < len(parts) - 1:
                target_dir = "/".join(target_parts)
                target_key = normalize_nfc(target_dir).casefold()
                other_source = directory_sources.get(target_key)
                if other_source is not None and other_source != source_prefix:
                    raise NamingError(
                        "Windows 目录名适配冲突："
                        f"「{other_source}」和「{source_prefix}」都会变成「{target_dir}」。"
                        "请先重命名其中一个目录，或把目录打包成 zip 后传输。"
                    )
                directory_sources[target_key] = source_prefix
        target = "/".join(target_parts)
        if target != source:
            renamed.append((source, target))
        key = normalize_nfc(target).casefold()
        if key in seen:
            other = seen[key]
            raise NamingError(
                "Windows 目标路径冲突："
                f"「{other}」和「{source}」都会保存为「{target}」。"
                "请先重命名源文件，或把文件夹打包成 zip 后传输。"
            )
        seen[key] = source
        mapping[source] = target

    # A destination may not be both a file and a directory in another path.
    for target in mapping.values():
        parts = target.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if normalize_nfc(ancestor).casefold() in seen:
                raise NamingError(
                    "Windows 目标路径冲突：某个文件名与另一个文件的目录名相同："
                    f"「{ancestor}」。请把该文件夹打包成 zip 后传输。"
                )
    return DestinationPlan(mapping=mapping, renamed=tuple(renamed))


def windows_long_path_warnings(root: "os.PathLike[str] | str", relative_paths: Iterable[str], limit: int = 240) -> list[str]:
    """Warn about targets that may exceed the default Windows MAX_PATH.

    This is advisory: systems with LongPathsEnabled can create longer paths,
    and we do not want to reject an otherwise valid transfer.
    """

    root_path = os.fspath(root)
    too_long = 0
    for relative in relative_paths:
        target = os.path.join(root_path, *relative.split("/"))
        if len(target) > limit:
            too_long += 1
    if not too_long:
        return []
    return [
        f"{too_long} 个目标路径超过 {limit} 个字符；"
        "Windows 未启用长路径时可能保存失败，建议缩短保存目录或先打包文件。"
    ]
