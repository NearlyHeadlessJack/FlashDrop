"""Local file selection, directory expansion, and manifest fingerprints."""

from __future__ import annotations

import hashlib
import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .config import QUICK_HASH_SAMPLE, RESUME_DIR_NAME
from .protocol import file_id_for, make_manifest_entry


class CatalogError(Exception):
    """A user-facing selection or fingerprint error."""


@dataclass(frozen=True, slots=True)
class LocalFile:
    id: str
    source: str
    rel_path: str
    size: int
    mtime_ns: int

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "rel": self.rel_path,
            "path": self.source,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ManifestFile:
    id: str
    source: str
    rel_path: str
    size: int
    mtime_ns: int
    quick_hash: str

    def manifest_entry(self) -> dict[str, object]:
        return make_manifest_entry(
            file_id=self.id,
            rel_path=self.rel_path,
            size=self.size,
            mtime_ns=self.mtime_ns,
            quick_hash=self.quick_hash,
        )


@dataclass(slots=True)
class AddResult:
    added: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CatalogStats:
    count: int
    total_size: int
    revision: int
    warnings: list[str]


def _canonical(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _top_name(rel_path: str) -> str:
    return rel_path.replace("\\", "/").split("/", 1)[0].casefold()


def _reserved_top(name: str) -> bool:
    return name.casefold() == RESUME_DIR_NAME.casefold()


def _unique_top_name(desired: str, used: set[str]) -> str:
    base = desired or "shared"
    candidate = base
    index = 2
    while candidate.casefold() in used or _reserved_top(candidate):
        candidate = f"{base} ({index})"
        index += 1
    return candidate


def _unique_file_name(name: str, used_rel: set[str], used_top: set[str]) -> str:
    stem, suffix = os.path.splitext(name)
    candidate = name
    index = 2
    while (
        candidate.casefold() in used_top
        or candidate.casefold() in used_rel
        or _reserved_top(candidate)
    ):
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def _clean_warning(path: str, exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return f"{path}: {text}"


def _read_quick_hash(path: Path, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"flashdrop-qh1\x00")
    digest.update(struct.pack(">QQ", size, mtime_ns & 0xFFFFFFFFFFFFFFFF))
    with path.open("rb") as handle:
        head = handle.read(min(size, QUICK_HASH_SAMPLE))
        digest.update(head)
        if size > QUICK_HASH_SAMPLE:
            offset = max(size - QUICK_HASH_SAMPLE, QUICK_HASH_SAMPLE)
            handle.seek(offset)
            digest.update(handle.read(min(QUICK_HASH_SAMPLE, size - offset)))
    return digest.hexdigest()[:40]


def fingerprint_file(path: str | os.PathLike[str]) -> tuple[int, int, str]:
    """Return ``(size, mtime_ns, quick_hash)`` for a source file.

    The sample includes both ends of large files. It is intentionally cheap:
    it detects normal edits between sessions without reading multi-gigabyte
    files before transfer. Full-file cryptographic hashing would halve
    throughput on storage-bound links.
    """

    file_path = Path(path)
    try:
        before = file_path.stat()
        if not file_path.is_file():
            raise CatalogError(f"不是普通文件: {file_path}")
        quick = _read_quick_hash(file_path, before.st_size, before.st_mtime_ns)
        after = file_path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            before = after
            quick = _read_quick_hash(file_path, before.st_size, before.st_mtime_ns)
            after = file_path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise CatalogError(f"文件在读取指纹时发生变化，请重新选择: {file_path}")
        return int(after.st_size), int(after.st_mtime_ns), quick
    except FileNotFoundError as exc:
        raise CatalogError(f"文件不存在: {file_path}") from exc
    except OSError as exc:
        raise CatalogError(f"无法读取文件 {file_path}: {exc}") from exc


def prepare_manifest(files: Iterable[LocalFile]) -> list[ManifestFile]:
    prepared: list[ManifestFile] = []
    for item in files:
        size, mtime_ns, quick = fingerprint_file(item.source)
        prepared.append(
            ManifestFile(
                id=item.id,
                source=item.source,
                rel_path=item.rel_path,
                size=size,
                mtime_ns=mtime_ns,
                quick_hash=quick,
            )
        )
    return prepared


class SenderCatalog:
    """Thread-safe ordered list of files selected for transfer."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mutation_lock = threading.Lock()
        self._files: list[LocalFile] = []
        self._sources: set[str] = set()
        self._revision = 0
        self._warnings: list[str] = []

    def stats(self) -> CatalogStats:
        with self._lock:
            return CatalogStats(
                count=len(self._files),
                total_size=sum(item.size for item in self._files),
                revision=self._revision,
                warnings=list(self._warnings),
            )

    def snapshot(self) -> list[LocalFile]:
        with self._lock:
            return list(self._files)

    def page(self, offset: int, limit: int) -> tuple[list[dict[str, object]], int]:
        with self._lock:
            total = len(self._files)
            start = max(0, offset)
            end = min(total, start + max(0, limit))
            return [item.public() for item in self._files[start:end]], total

    def add_paths(self, paths: Sequence[str]) -> AddResult:
        """Expand files and directories, skipping already-selected sources."""

        cleaned = [str(Path(raw.strip().strip('"')).expanduser()) for raw in paths if raw.strip()]
        if not cleaned:
            return AddResult()

        with self._mutation_lock:
            with self._lock:
                existing_sources = set(self._sources)
                existing_rels = {item.rel_path.casefold() for item in self._files}
                used_top = {_top_name(item.rel_path) for item in self._files}

            new_files: list[LocalFile] = []
            pending_sources: set[str] = set()
            warnings: list[str] = []
            skipped = 0

            for raw_path in cleaned:
                path = Path(raw_path)
                try:
                    is_file = path.is_file()
                    is_dir = path.is_dir()
                except OSError as exc:
                    warnings.append(_clean_warning(raw_path, exc))
                    continue

                path = Path(os.path.abspath(os.fspath(path)))
                if is_file:
                    if not self._plan_single_file(
                        path,
                        existing_sources=existing_sources,
                        pending_sources=pending_sources,
                        existing_rels=existing_rels,
                        used_top=used_top,
                        new_files=new_files,
                        warnings=warnings,
                    ):
                        skipped += 1
                elif is_dir:
                    before = len(new_files)
                    skipped += self._plan_directory(
                        path,
                        existing_sources=existing_sources,
                        pending_sources=pending_sources,
                        used_top=used_top,
                        new_files=new_files,
                        warnings=warnings,
                    )
                    if len(new_files) == before:
                        warnings.append(f"{path}: 没有发现可添加的新文件")
                else:
                    warnings.append(f"{path}: 路径不存在或不是文件/文件夹")

            # Compute identities and stats, then commit.
            known_ids = {item.id for item in self._files}
            committed: list[LocalFile] = []
            for candidate in new_files:
                if candidate.id in known_ids:
                    # A path casefold collision is extremely unlikely after the
                    # unique-name pass, but never silently corrupt a manifest.
                    warnings.append(f"{candidate.rel_path}: 文件标识冲突，已跳过")
                    continue
                known_ids.add(candidate.id)
                committed.append(candidate)

            with self._lock:
                if committed:
                    self._files.extend(committed)
                    self._sources.update(_canonical(item.source) for item in committed)
                    self._revision += 1
                self._warnings = warnings[:20]
                return AddResult(
                    added=len(committed),
                    skipped=skipped,
                    warnings=list(warnings)[:20],
                )

    def _plan_single_file(
        self,
        path: Path,
        *,
        existing_sources: set[str],
        pending_sources: set[str],
        existing_rels: set[str],
        used_top: set[str],
        new_files: list[LocalFile],
        warnings: list[str],
    ) -> bool:
        canonical = _canonical(path)
        if canonical in existing_sources or canonical in pending_sources:
            warnings.append(f"{path}: 已在发送列表中")
            return False
        try:
            stat = path.stat()
        except OSError as exc:
            warnings.append(_clean_warning(str(path), exc))
            return False
        name = path.name or "file"
        rel_name = _unique_file_name(name, existing_rels, used_top)
        used_top.add(rel_name.casefold())
        existing_rels.add(rel_name.casefold())
        pending_sources.add(canonical)
        new_files.append(
            LocalFile(
                id=file_id_for(rel_name),
                source=str(path),
                rel_path=rel_name,
                size=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
            )
        )
        return True

    def _plan_directory(
        self,
        root: Path,
        *,
        existing_sources: set[str],
        pending_sources: set[str],
        used_top: set[str],
        new_files: list[LocalFile],
        warnings: list[str],
    ) -> int:
        candidates: list[Path] = []

        def onerror(exc: OSError) -> None:
            warnings.append(_clean_warning(getattr(exc, "filename", str(root)), exc))

        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
            dirnames[:] = sorted(
                (
                    name
                    for name in dirnames
                    if name.casefold() != RESUME_DIR_NAME.casefold()
                    and not os.path.islink(os.path.join(current, name))
                ),
                key=str.casefold,
            )
            filenames.sort(key=str.casefold)
            for name in filenames:
                path = Path(current) / name
                canonical = _canonical(path)
                if canonical in existing_sources or canonical in pending_sources:
                    continue
                candidates.append(path)

        if not candidates:
            return 0

        desired_root = root.name or "shared-folder"
        root_name = _unique_top_name(desired_root, used_top)
        used_top.add(root_name.casefold())

        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
            except (OSError, ValueError) as exc:
                warnings.append(_clean_warning(str(path), exc))
                continue
            rel_path = f"{root_name}/{relative}"
            new_files.append(
                LocalFile(
                    id=file_id_for(rel_path),
                    source=str(path),
                    rel_path=rel_path,
                    size=int(stat.st_size),
                    mtime_ns=int(stat.st_mtime_ns),
                )
            )
            pending_sources.add(_canonical(path))
        return 0

    def remove(self, file_id: str) -> bool:
        with self._mutation_lock:
            with self._lock:
                for index, item in enumerate(self._files):
                    if item.id == file_id:
                        removed = self._files.pop(index)
                        self._sources.discard(_canonical(removed.source))
                        self._revision += 1
                        return True
        return False

    def clear(self) -> None:
        with self._mutation_lock:
            with self._lock:
                if not self._files and not self._warnings:
                    return
                self._files.clear()
                self._sources.clear()
                self._warnings.clear()
                self._revision += 1
