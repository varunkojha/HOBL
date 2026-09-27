# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scenarios.windows._library.enterprise_ai.configuration import Configuration
from scenarios.windows._library.enterprise_ai.lifecycle import Controller, invoke_script, powershell_literal


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.events = []
        self.scenario = mock.Mock()
        self.scenario.dut_exec_path = r"R:\hobl_bin"
        self.scenario.dut_data_path = r"R:\hobl_data"
        self.scenario.result_dir = self.temporary.name
        self.scenario.resolve.side_effect = lambda value: str(ROOT / Path(*value.split("\\")))
        self.clock = Clock()

    def controller(self, **values):
        cfg = Configuration.from_values({"measurement_seconds": "30", "settle_seconds": "2", **values})
        result = Controller(self.scenario, cfg, lambda tag: self.events.append(tag), self.clock, self.clock.sleep)
        result.script = mock.Mock(side_effect=lambda file, action, **kw: self.response(result, file, action, **kw))
        return result

    def response(self, controller, file, action, **kwargs):
        self.events.append((file, action))
        status = {
            "Initialize": "initialized", "Prepare": "prepared", "Restore": "restored",
            "Collect": "collected", "Release": "released", "Stop": "stopped", "Status": "ready",
        }.get(action)
        if action == "Start":
            status = "ready" if file == "stress_control.ps1" else "submitted"
        response = {"schema_version": 1, "run_id": controller.run_id, "status": status}
        if file == "native_indexing.ps1" and action == "Start":
            response["documents_submitted"] = controller.manifest["corpus"]["file_count"]
        return response

    def prepare(self, controller):
        with mock.patch("scenarios.windows._library.enterprise_ai.lifecycle.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, stdout="synthetic-source-sha\n")):
            with mock.patch("scenarios.windows._library.enterprise_ai.lifecycle.invoke_script"):
                controller.prepare()

    def test_baseline_does_not_launch_ai_or_stress(self):
        controller = self.controller(condition="A")
        self.prepare(controller)
        controller.begin()
        controller.finish_window()
        controller.stop_work()
        self.events.append("core_trace_stopped")
        controller.restore_and_collect()
        self.assertFalse(any("native_indexing" in str(e) or "stress_control" in str(e) for e in self.events))
        begin = f"enterprise_ai:{controller.run_id}:measurement_begin"
        end = f"enterprise_ai:{controller.run_id}:measurement_end"
        self.assertLess(self.events.index(begin), self.events.index(end))
        self.assertLess(self.events.index("core_trace_stopped"), self.events.index(("utc_control.ps1", "Restore")))
        manifest = json.loads(Path(self.temporary.name, "enterprise_ai_run.json").read_text())
        self.assertEqual(manifest["host_observation_seconds"], 30)
        self.assertFalse(manifest["semantic_completion_verified"])
        self.assertTrue(controller.cleaned)

    def test_index_trigger_is_inside_measurement_and_cleanup_after_end(self):
        corpus = Path(self.temporary.name, "approved")
        corpus.mkdir()
        (corpus / "fixture.txt").write_text("Synthetic indexing lifecycle fixture.")
        controller = self.controller(condition="D", corpus_dir=str(corpus), semantic_ready_value="7")
        self.prepare(controller)
        self.assertNotIn(("native_indexing.ps1", "Start"), self.events)
        controller.begin()
        controller.finish_window()
        controller.stop_work()
        self.assertLess(self.events.index(f"enterprise_ai:{controller.run_id}:measurement_begin"),
                        self.events.index(("native_indexing.ps1", "Start")))
        self.assertLess(self.events.index(f"enterprise_ai:{controller.run_id}:measurement_end"),
                        self.events.index(("native_indexing.ps1", "Stop")))
        self.assertEqual(controller.manifest["indexing"]["documents_submitted"], 1)

    def test_failed_preparation_keeps_cleanup_obligation(self):
        controller = self.controller(condition="A", configure_utc="1")
        original = controller.script.side_effect
        def fail(file, action, **kwargs):
            if file == "utc_control.ps1" and action == "Prepare":
                raise RuntimeError("synthetic partial apply")
            return original(file, action, **kwargs)
        controller.script.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "partial apply"):
            self.prepare(controller)
        self.assertTrue(controller.utc_attempted)
        controller.restore_and_collect()
        self.assertIn(("utc_control.ps1", "Restore"), self.events)

    def test_restore_failure_keeps_exclusive_lease_and_reports_error(self):
        controller = self.controller(condition="A")
        self.prepare(controller)
        original = controller.script.side_effect
        def fail(file, action, **kwargs):
            if action == "Restore":
                raise RuntimeError("synthetic restore error")
            return original(file, action, **kwargs)
        controller.script.side_effect = fail
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "recovery required"):
            controller.restore_and_collect()
        self.assertNotIn(("workspace.ps1", "Release"), self.events)
        self.assertFalse(controller.cleaned)
        self.assertTrue(controller.manifest["cleanup_errors"])

    def test_overrun_is_not_an_accepted_shorter_or_longer_sample(self):
        controller = self.controller(condition="A")
        self.prepare(controller)
        controller.begin()
        self.clock.sleep(31)
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            controller.finish_window()

    def test_end_marker_is_idempotent(self):
        controller = self.controller(condition="A")
        self.prepare(controller)
        controller.begin()
        controller.end_measurement()
        controller.end_measurement()
        self.assertEqual(self.events.count(f"enterprise_ai:{controller.run_id}:measurement_end"), 1)

    def test_foreign_run_or_invalid_script_output_rejected(self):
        controller = self.controller()
        controller.script = Controller.script.__get__(controller)
        for output in ("not JSON", json.dumps({"schema_version": 1, "run_id": "another-run"})):
            self.scenario._call.return_value = output
            with self.assertRaises(RuntimeError):
                controller.script("workspace.ps1", "Initialize")

    def test_stop_failure_is_not_silently_successful(self):
        controller = self.controller(condition="B")
        controller.initialized = True
        controller.stress_attempted = True
        controller.script.side_effect = RuntimeError("synthetic child termination failure")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            controller.stop_work()
        self.assertTrue(controller.stress_attempted)

    def test_dut_paths_derive_from_drive_not_host(self):
        controller = self.controller()
        self.assertTrue(controller.run_dir.startswith("R:\\hobl_bin\\enterprise_ai_resources\\runs\\"))
        self.assertTrue(controller.raw_dir.startswith("R:\\hobl_data\\enterprise_ai_raw\\"))

    def test_encoded_powershell_preserves_quoted_paths(self):
        self.scenario._call.return_value = "{}"
        invoke_script(self.scenario, r"R:\tools\owner's script.ps1", Path=r"R:\data\O'Brien", Action="Prepare")
        args = self.scenario._call.call_args.args[0][1]
        decoded = base64.b64decode(args.split()[-1]).decode("utf-16le")
        self.assertIn("owner''s script.ps1", decoded)
        self.assertIn("O''Brien", decoded)
        self.assertNotIn("-Command", args)
        with self.assertRaises(ValueError):
            powershell_literal("bad\0argument")


if __name__ == "__main__":
    unittest.main()
