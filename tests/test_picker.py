from __future__ import annotations

import json
import unittest

from flashdrop import picker


class PickerTests(unittest.TestCase):
    def test_jxa_script_contains_multi_selection_and_folder_dialogs(self) -> None:
        files_script = picker._jxa_source("files")
        self.assertIn("chooseFile", files_script)
        self.assertIn("multipleSelectionsAllowed: true", files_script)
        self.assertIn("JSON.stringify", files_script)
        self.assertIn("chooseFolder", picker._jxa_source("folder"))
        self.assertIn("chooseFolder", picker._jxa_source("save-dir"))

    def test_parse_plain_json_output(self) -> None:
        payload = picker._parse_jxa_output(json.dumps({"paths": ["/Users/a/文件.txt"]}))
        self.assertEqual(payload["paths"], ["/Users/a/文件.txt"])

    def test_parse_double_encoded_json_output(self) -> None:
        inner = json.dumps({"paths": ["/Users/a/项目.zip"]}, ensure_ascii=False)
        payload = picker._parse_jxa_output(json.dumps(inner, ensure_ascii=False))
        self.assertEqual(payload["paths"], ["/Users/a/项目.zip"])

    def test_parse_error_payload(self) -> None:
        payload = picker._parse_jxa_output(
            json.dumps({"paths": [], "error": "Not authorized to send Apple events."})
        )
        self.assertIn("authorized", payload["error"])

    def test_parse_empty_or_malformed(self) -> None:
        with self.assertRaises(ValueError):
            picker._parse_jxa_output("")
        with self.assertRaises(ValueError):
            picker._parse_jxa_output("not json")


if __name__ == "__main__":
    unittest.main()
