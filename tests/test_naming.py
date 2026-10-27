from __future__ import annotations

import unicodedata
import unittest

from flashdrop.naming import (
    NamingError,
    plan_destinations,
    windows_long_path_warnings,
    windows_safe_component,
)


class NamingTests(unittest.TestCase):
    def test_clean_names_are_preserved(self) -> None:
        plan = plan_destinations(["资料/报告.pdf", "照片/夏日.jpg"], windows=True)
        self.assertEqual(plan.renamed_count, 0)
        self.assertEqual(plan.mapping["资料/报告.pdf"], "资料/报告.pdf")

    def test_invalid_windows_characters_are_adapted_deterministically(self) -> None:
        source = "项目/报告:终稿?.pdf"
        first = plan_destinations([source], windows=True)
        second = plan_destinations([source], windows=True)
        target = first.mapping[source]
        self.assertEqual(target, second.mapping[source])
        self.assertNotEqual(target, source)
        self.assertNotIn(":", target)
        self.assertNotIn("?", target)
        self.assertTrue(target.endswith(".pdf"))
        self.assertRegex(target, r"~[0-9a-f]{8}")
        self.assertEqual(first.renamed_count, 1)

    def test_reserved_and_trailing_names_are_adapted(self) -> None:
        sources = ["CON.txt", "aux", "trailing. ", "normal.txt"]
        plan = plan_destinations(sources, windows=True)
        self.assertEqual(len(set(plan.mapping.values())), len(sources))
        self.assertNotEqual(plan.mapping["CON.txt"], "CON.txt")
        self.assertNotEqual(plan.mapping["aux"], "aux")
        for target in plan.mapping.values():
            for part in target.split("/"):
                self.assertFalse(part.endswith((" ", ".")))
                self.assertNotIn(part.casefold().split(".", 1)[0], {"con", "prn", "aux", "nul"})

    def test_backslash_and_control_characters_are_adapted(self) -> None:
        plan = plan_destinations(["folder/a?b.txt", "folder/CON", "folder/a\x01b"], windows=True)
        for target in plan.mapping.values():
            self.assertNotIn("\\", target)
            self.assertNotIn("?", target)
            self.assertNotIn("\x01", target)

    def test_nfc_collision_is_rejected_instead_of_overwriting(self) -> None:
        # In normal sender manifests this is already prevented, but a malformed
        # or future peer must not be allowed to overwrite the same Windows path.
        nfc = unicodedata.normalize("NFC", "café.txt")
        nfd = unicodedata.normalize("NFD", "café.txt")
        if nfc == nfd:
            self.skipTest("platform strings are already normalized")
        with self.assertRaises(NamingError):
            plan_destinations([nfc, nfd], windows=True)

    def test_shared_source_directory_keeps_one_target_directory(self) -> None:
        plan = plan_destinations(["a?/one.txt", "a?/two.txt"], windows=True)
        targets = list(plan.mapping.values())
        self.assertEqual(len({target.rsplit("/", 1)[0] for target in targets}), 1)
        self.assertTrue(all(target.endswith(".txt") for target in targets))

    def test_component_length_is_bounded(self) -> None:
        safe, changed = windows_safe_component("a" * 400 + ".txt", context="x")
        self.assertTrue(changed)
        self.assertLessEqual(len(safe.encode("utf-16-le")) // 2, 180)

    def test_long_target_paths_generate_a_warning(self) -> None:
        warnings = windows_long_path_warnings("C:\\save", ["a" * 300])
        self.assertEqual(len(warnings), 1)
        self.assertIn("路径", warnings[0])
        self.assertEqual(windows_long_path_warnings("C:\\save", ["short.txt"]), [])

    def test_non_windows_is_identity(self) -> None:
        plan = plan_destinations(["CON.txt", "a:b"], windows=False)
        self.assertEqual(plan.mapping["CON.txt"], "CON.txt")
        self.assertEqual(plan.renamed_count, 0)


if __name__ == "__main__":
    unittest.main()
