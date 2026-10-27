from __future__ import annotations

import os
import unittest
from pathlib import Path

from flashdrop.catalog import SenderCatalog, prepare_manifest
from tests.support import collect_files, make_tree, workspace_temp


class CatalogTests(unittest.TestCase):
    def test_add_folder_and_deduplicate(self) -> None:
        base = workspace_temp(self)
        source = base / "素材"
        make_tree(
            source,
            {
                "a.txt": b"a",
                "nested/b.bin": b"b" * 10,
                "nested/.flashdrop/internal.part": b"must-not-transfer",
                "empty.dat": b"",
            },
        )
        catalog = SenderCatalog()
        result = catalog.add_paths([str(source), str(source / "a.txt")])
        self.assertEqual(result.added, 3)
        stats = catalog.stats()
        self.assertEqual(stats.count, 3)
        self.assertEqual(stats.total_size, 11)
        files, total = catalog.page(0, 10)
        self.assertEqual(total, 3)
        self.assertTrue(all(not item["rel"].startswith(".flashdrop") for item in files))
        self.assertTrue(any("已在发送列表中" in warning or "没有发现" in warning for warning in result.warnings))

        # Adding the same folder again adds nothing.
        second = catalog.add_paths([str(source)])
        self.assertEqual(second.added, 0)

    def test_duplicate_basenames_become_unique(self) -> None:
        base = workspace_temp(self)
        first = base / "one" / "same.txt"
        second = base / "two" / "same.txt"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        catalog = SenderCatalog()
        result = catalog.add_paths([str(first), str(second)])
        self.assertEqual(result.added, 2)
        files, _ = catalog.page(0, 10)
        rels = sorted(item["rel"] for item in files)
        self.assertEqual(rels, ["same (2).txt", "same.txt"])
        self.assertEqual(len({item["id"] for item in files}), 2)

    def test_two_folders_same_name_get_suffix(self) -> None:
        base = workspace_temp(self)
        for parent in ("a", "b"):
            make_tree(base / parent / "photos", {"x.jpg": parent.encode()})
        catalog = SenderCatalog()
        result = catalog.add_paths([str(base / "a" / "photos"), str(base / "b" / "photos")])
        self.assertEqual(result.added, 2)
        files, _ = catalog.page(0, 10)
        rels = sorted(item["rel"] for item in files)
        self.assertEqual(rels, ["photos (2)/x.jpg", "photos/x.jpg"])

    def test_prepare_manifest_fingerprint_and_size(self) -> None:
        base = workspace_temp(self)
        source = base / "f.bin"
        source.write_bytes(b"hello")
        catalog = SenderCatalog()
        catalog.add_paths([str(source)])
        manifest = prepare_manifest(catalog.snapshot())
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0].size, 5)
        self.assertEqual(len(manifest[0].quick_hash), 40)

    def test_remove_and_clear(self) -> None:
        base = workspace_temp(self)
        make_tree(base / "src", {"a": b"1", "b": b"2"})
        catalog = SenderCatalog()
        catalog.add_paths([str(base / "src")])
        files, _ = catalog.page(0, 10)
        first_id = files[0]["id"]
        self.assertTrue(catalog.remove(first_id))
        self.assertEqual(catalog.stats().count, 1)
        catalog.clear()
        self.assertEqual(catalog.stats().count, 0)
        self.assertEqual(catalog.stats().revision, 3)

    def test_collect_files_ignores_state_dir(self) -> None:
        base = workspace_temp(self)
        make_tree(base, {"a": b"1", ".flashdrop/state/x": b"2"})
        self.assertEqual(collect_files(base), {"a": b"1"})


if __name__ == "__main__":
    unittest.main()
