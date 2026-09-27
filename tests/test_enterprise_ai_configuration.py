# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scenarios.windows._library.enterprise_ai.configuration import (
    CONDITIONS, Configuration, corpus_manifest, finite_number, validate_remote_dut,
)


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_control_matrix_matches_protocol(self):
        expected = {
            "A": (True, False, False), "B": (True, True, False),
            "C": (False, False, True), "D": (True, False, True),
            "E": (True, True, True), "F": (False, True, True),
        }
        self.assertEqual(CONDITIONS, expected)
        for name, flags in expected.items():
            configuration = Configuration.from_values({
                "condition": name, "corpus_dir": "approved-corpus", "semantic_ready_value": "7",
            })
            self.assertEqual((configuration.foreground, configuration.stress, configuration.ai), flags)

    def test_no_model_or_query_substitute(self):
        for protocol in ("query", "model"):
            for condition in ("A", "D"):
                with self.assertRaisesRegex(ValueError, "gated"):
                    Configuration.from_values({"condition": condition, "protocol": protocol})

    def test_indexing_has_no_assumed_ready_status_or_corpus(self):
        with self.assertRaisesRegex(ValueError, "corpus_dir"):
            Configuration.from_values({"condition": "C"})
        with self.assertRaisesRegex(ValueError, "semantic_ready_value"):
            Configuration.from_values({"condition": "C", "corpus_dir": "approved"})

    def test_numeric_input_bounds_and_nonfinite(self):
        for bad in ("NaN", "inf", "-inf", "not-a-number", True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                finite_number(bad, "budget", 1, 10)
        for values in (
            {"condition": "Z"}, {"measurement_seconds": "0"},
            {"stress_workers": "1.5"}, {"stress_duty_cycle": "1.1"},
            {"validation_mode": "success"}, {"configure_utc": "yes"},
            {"required_pts": "PT_123"}, {"validation_mode": "strict", "required_pts": ""},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Configuration.from_values(values)

    def test_remote_dut_consent_and_host_guards(self):
        for consent, platform, host, local in (
            ("0", "Windows", "192.0.2.30", "0"),
            ("1", "Windows", "127.0.0.1", "0"),
            ("1", "Windows", "::1", "0"),
            ("1", "Windows", "localhost", "0"),
            ("1", "Windows", "0.0.0.0", "0"),
            ("1", "macOS", "192.0.2.30", "0"),
            ("1", "Windows", "192.0.2.30", "1"),
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_remote_dut(consent, platform, host, local)
        validate_remote_dut("1", "Windows", "192.0.2.30", "0")

    def test_corpus_manifest_determinism_and_real_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z.txt").write_text("Synthetic fixture: mountain photographs.", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "a.txt").write_text("Synthetic fixture: quarterly costs.", encoding="utf-8")
            first = corpus_manifest(root)
            self.assertEqual(first, corpus_manifest(root))
            self.assertEqual(first["file_count"], 2)
            self.assertEqual(first["files"][0]["relative_path"], r"nested\a.txt")
            (root / "z.txt").write_text("Changed synthetic fixture", encoding="utf-8")
            self.assertNotEqual(first["sha256"], corpus_manifest(root)["sha256"])

    def test_empty_and_oversize_corpus_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least one"):
                corpus_manifest(directory)
            Path(directory, "data.txt").write_text("abc", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safety limit"):
                corpus_manifest(directory, max_bytes=2)
            with self.assertRaisesRegex(ValueError, "safety limit"):
                corpus_manifest(directory, max_files=0)

    def test_corpus_reparse_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "file.txt").write_text("fixture")
            real_lstat = Path.lstat
            def lstat(item):
                if item.name == "file.txt":
                    real = real_lstat(item)
                    return SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0x400)
                return real_lstat(item)
            with mock.patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ValueError, "reparse"):
                corpus_manifest(directory)


class ActionCopyTests(unittest.TestCase):
    def test_copied_foreground_schedule_has_only_documented_changes(self):
        original = json.loads((ROOT / "scenarios/windows/enterprise_collab/enterprise_collab.json").read_text())
        actual = json.loads((ROOT / "scenarios/windows/enterprise_ai/enterprise_ai.json").read_text())
        def retain(nodes):
            result = []
            skip_end = False
            for item in nodes:
                if skip_end:
                    self.assertEqual(item["type"], "End If")
                    skip_end = False
                    continue
                if item["type"] == "If" and item.get("left_term") in ("[perf_run]", "[background_onedrive_copy]"):
                    skip_end = True
                    continue
                if item["type"] == "Set Default" and item.get("name") in ("[perf_run]", "[background_onedrive_copy]"):
                    continue
                item = dict(item)
                if item["type"] == "Set Default" and item.get("name") in (
                    "[simple_office_launch]", "[file_explorer]", "[snipping_tool]", "[settings_app]",
                ):
                    item["value"] = "1"
                if item["type"] == "Information":
                    item["description"] = "Enterprise AI foreground actions, adapted from Enterprise Collaborator."
                if "children" in item:
                    item["children"] = retain(item["children"])
                result.append(item)
            return result
        self.assertEqual(actual, retain(original))

    def test_transitive_include_code_and_image_references_exist(self):
        start = ROOT / "scenarios/windows/enterprise_ai/enterprise_ai.json"
        visited = set()
        def inspect(filename):
            filename = filename.resolve()
            if filename in visited:
                return
            visited.add(filename)
            self.assertTrue(filename.is_file(), str(filename))
            def walk(nodes):
                for node in nodes:
                    if node.get("enabled") is False:
                        continue
                    if node["type"] == "Include":
                        rel = Path(*node["include_path"].split("\\"))
                        parent = filename.parent / rel
                        if not parent.exists():
                            parent = ROOT / rel
                        inspect(parent / (parent.name + ".json"))
                    if node["type"] != "Capture":
                        for item in node.get("file_name", []):
                            self.assertTrue((filename.parent / item).exists(), f"{filename}: {item}")
                    walk(node.get("children", []))
            walk(json.loads(filename.read_text(encoding="utf-8-sig")))
        inspect(start)
        self.assertGreater(len(visited), 10)


if __name__ == "__main__":
    unittest.main()
