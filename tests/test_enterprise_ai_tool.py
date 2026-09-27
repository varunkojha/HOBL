# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from scenarios.windows._library.enterprise_ai.configuration import file_hash


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "a" * 32
INPUT_GUID = "150a3791-7792-452d-858c-15d647ecb48f"


def synthetic_events():
    def event(second, phase):
        return (
            f'<Event><System><Provider Guid="{{{INPUT_GUID}}}"/>'
            f'<TimeCreated SystemTime="2026-09-27T00:00:{second:02d}Z"/></System>'
            f'<EventData><Data Name="Tag">enterprise_ai:{RUN_ID}:{phase}</Data></EventData></Event>'
        )
    return "<Events>" + event(0, "measurement_begin") + event(30, "measurement_end") + "</Events>"


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        core = types.ModuleType("core")
        core.app_scenario = types.ModuleType("core.app_scenario")
        core.app_scenario.Scenario = object
        core.parameters = types.ModuleType("core.parameters")
        class Params:
            providers = ""
            validator = ""
            @classmethod
            def setDefault(cls, *args, **kwargs):
                pass
            @classmethod
            def get(cls, *args):
                return cls.validator
            @classmethod
            def getCalculated(cls, *args):
                return cls.providers
            @classmethod
            def setCalculated(cls, name, value):
                cls.providers = value
        self.params = Params
        core.parameters.Params = Params
        patcher = mock.patch.dict(sys.modules, {
            "core": core, "core.app_scenario": core.app_scenario, "core.parameters": core.parameters,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location("_enterprise_ai_tool_fixture", ROOT / "tools" / "enterprise_ai_metrics.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.scenario = mock.Mock()
        self.scenario.result_dir = str(self.root)
        self.scenario.testname = "synthetic"
        self.scenario.ai_controller.run_id = RUN_ID
        self.scenario.resolve.side_effect = lambda name: str(self.root / name.split("\\")[-1])
        self.manifest_file = self.root / "StressUtcPerftrack.xml"
        self.manifest_file.write_text(
            '<scenarios><scenario scenarioname="PT_8805_Word_fixture" ptscenarioname="Word"/></scenarios>',
            encoding="utf-8",
        )
        for name in ("PerfParser.exe", "PerfParser.dll", "PerfParser.deps.json",
                     "PerfParser.runtimeconfig.json", "DisableAllUploads.json"):
            (self.root / name).write_text("Synthetic prerequisite, never executed.", encoding="utf-8")
        (self.root / "synthetic.etl").write_bytes(b"SYNTHETIC FIXTURE - NOT AN ETL")
        self.run = {
            "schema_version": 1, "run_id": RUN_ID, "status": "collected",
            "condition": "A", "protocol": "indexing", "foreground": True, "stress": False, "ai": False,
            "config": {"measurement_seconds": 30, "validation_mode": "capture", "required_pts": ["8805"]},
            "dut": {"architecture": "X64"}, "cleanup_errors": [], "semantic_completion_verified": False,
            "utc_manifest_sha256": file_hash(self.manifest_file),
        }
        self.save_run()
        self.which = mock.patch.object(self.module.shutil, "which", return_value="synthetic-tracerpt.exe")
        self.which.start()
        self.addCleanup(self.which.stop)
        self.tool = self.module.Tool()
        self.tool.initCallback(self.scenario)
        self.decoder = mock.patch.object(self.module, "run_decoder", side_effect=self.decode)
        self.decoder_mock = self.decoder.start()
        self.addCleanup(self.decoder.stop)

    def save_run(self):
        (self.root / "enterprise_ai_run.json").write_text(json.dumps(self.run), encoding="utf-8")

    def decode(self, arguments, log_path, timeout=1800):
        Path(log_path).write_text("Synthetic decoder fixture; no executable was run.")
        if arguments[0] == "synthetic-tracerpt.exe":
            output = Path(arguments[arguments.index("-o") + 1])
            output.write_text(synthetic_events(), encoding="utf-8")
        else:
            Path(arguments[-1]).write_text("Scenario,Metric,Duration\nPT_8805_Word_fixture,Word,12.5\n", encoding="utf-8")

    def test_capture_report_is_explicitly_inconclusive_and_raw_is_separate(self):
        with self.assertLogs(level="WARNING"):
            self.tool.dataReadyCallback()
        self.scenario.finish_environment.assert_called_once()
        quality = json.loads((self.root / "enterprise_ai_quality.json").read_text())
        self.assertEqual(quality["status"], "inconclusive")
        self.assertEqual(quality["trace_loss_status"], "unknown")
        with (self.root / "enterprise_ai_summary.csv").open(newline="") as stream:
            summary = dict(csv.reader(stream))
        self.assertEqual(summary["scenario_runtime"], "30.0")
        self.assertEqual(summary["validation_status"], "inconclusive")
        self.assertNotIn("Duration", summary)
        raw = self.root / "enterprise_ai_raw" / RUN_ID
        self.assertTrue((raw / "PerfMetrics_raw.csv").exists())
        self.assertTrue((raw / "PerfMetrics.csv").exists())
        self.assertTrue((raw / "events.xml").exists())
        self.assertFalse((self.root / "PerfMetrics_raw.csv").exists())

    def test_strict_without_evidence_fails_and_retains_diagnostics(self):
        self.run["config"]["validation_mode"] = "strict"
        self.save_run()
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(ValueError, "quality gates"):
            self.tool.dataReadyCallback()
        quality = json.loads((self.root / "enterprise_ai_quality.json").read_text())
        self.assertEqual(quality["status"], "invalid")

    def test_failed_decoder_writes_machine_readable_failure_not_raw_as_summary(self):
        self.decoder_mock.side_effect = subprocess.TimeoutExpired("synthetic-decoder", 1)
        with self.assertLogs(level="ERROR"), self.assertRaises(subprocess.TimeoutExpired):
            self.tool.dataReadyCallback()
        quality = json.loads((self.root / "enterprise_ai_quality.json").read_text())
        self.assertEqual(quality["run_id"], RUN_ID)
        self.assertEqual(quality["status"], "invalid")
        self.assertIn("timed out", quality["reasons"][0])

    def test_manifest_and_run_identity_cannot_be_reused(self):
        self.run["run_id"] = "../foreign"
        self.save_run()
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, "invalid run_id"):
            self.tool.dataReadyCallback()
        self.decoder_mock.assert_not_called()

    def test_capture_manifest_is_bound_to_the_extraction_manifest(self):
        self.manifest_file.write_text("<changed/>")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, "manifest changed"):
            self.tool.dataReadyCallback()
        self.decoder_mock.assert_not_called()

    def test_extra_profile_is_rejected_not_silently_combined(self):
        self.params.providers = "power_light.wprp"
        with self.assertRaisesRegex(ValueError, "extra providers"):
            self.tool.initCallback(self.scenario)

    def test_missing_host_parser_prerequisite_fails_preflight(self):
        (self.root / "PerfParser.dll").unlink()
        with self.assertRaisesRegex(ValueError, "artifact is missing"):
            self.tool.preflight(self.scenario)
        self.decoder_mock.assert_not_called()

    def test_provider_setting_is_scoped_and_restored(self):
        self.assertEqual(self.params.providers, "GTPLight_EnterpriseAI.wprp")
        self.tool.cleanup()
        self.assertEqual(self.params.providers, "")

    def test_decoder_never_invokes_a_shell(self):
        self.decoder.stop()
        with mock.patch.object(self.module.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout="fixture", stderr="")) as invoke:
            arguments = [r"R:\approved tools\parser.exe", r"R:\trace data\input.etl"]
            self.module.run_decoder(arguments, self.root / "safe.log")
            self.assertEqual(invoke.call_args.args[0], arguments)
            self.assertFalse(invoke.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
