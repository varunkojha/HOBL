# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

from scenarios.windows._library.enterprise_ai.configuration import DEFAULTS


ROOT = Path(__file__).resolve().parents[1]


class FakeParams:
    values = {}
    overrides = {}

    @classmethod
    def get(cls, section, name):
        if name in cls.overrides.get(section, {}):
            return cls.overrides[section][name]
        if (section, name) in cls.values:
            return cls.values[section, name]
        if section == "enterprise_ai" and name in DEFAULTS:
            return DEFAULTS[name][0]
        return ""

    @classmethod
    def get_raw(cls, section, name):
        return cls.values.get((section, name))

    @classmethod
    def setOverride(cls, section, name, value):
        if value.startswith("+"):
            value = " ".join(dict.fromkeys((cls.get(section, name) + " " + value[1:]).split()))
        cls.overrides.setdefault(section, {})[name] = value


class FakeScenario:
    initialized = 0

    def __init__(self, *args, **kwargs):
        type(self).initialized += 1
        self.events = []
        self.cleanups = []
        self.trace_started = False
        self.tool_failure_reason = None
        self.tool_instances = []
        self.action_result = 0
        self.dut_ip = "192.0.2.30"
        self.rpc_port = 8000

    def addCleanup(self, callback):
        self.cleanups.append(callback)

    def load_action_json(self, path):
        return [{"type": name, "children": [{"fixture_phase": name}]} for name in ("Setup", "Run Test", "Teardown")]

    def _find_next_type(self, name, json):
        return next((node for node in json if node["type"] == name), None)

    def run_actions(self, children):
        self.events.append(children[0]["fixture_phase"])
        return self.action_result

    def setUp(self):
        self.events.append("trace_start")
        self.trace_started = True

    def tearDown(self):
        self.events.append("trace_stop")
        self.trace_started = False
        self.finish_environment()

    def process_action(self, action):
        self.events.append(action)
        return 0


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        FakeParams.values = {
            ("enterprise_ai", "dedicated_dut"): "1", ("enterprise_ai", "condition"): "A",
            ("global", "platform"): "Windows", ("global", "dut_ip"): "192.0.2.30",
            ("global", "local_execution"): "0", ("global", "collection_enabled"): "1",
            ("global", "training_mode"): "0", ("global", "tools"): "run_report",
        }
        FakeParams.overrides = {}
        package_name = "_enterprise_ai_test_package"
        package = types.ModuleType(package_name)
        package.__path__ = []
        default_params = types.ModuleType(package_name + ".default_params")
        default_params.run = lambda: None
        core = types.ModuleType("core")
        core.app_scenario = types.ModuleType("core.app_scenario")
        core.app_scenario.Scenario = FakeScenario
        core.parameters = types.ModuleType("core.parameters")
        core.parameters.Params = FakeParams
        core.call_rpc = types.ModuleType("core.call_rpc")
        core.call_rpc.plugin_call = mock.Mock(return_value=json.dumps({"result": True}))
        self.rpc = core.call_rpc
        modules = {
            package_name: package, package_name + ".default_params": default_params,
            "core": core, "core.app_scenario": core.app_scenario,
            "core.parameters": core.parameters, "core.call_rpc": core.call_rpc,
        }
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        file = ROOT / "scenarios" / "windows" / "enterprise_ai" / "enterprise_ai.py"
        spec = importlib.util.spec_from_file_location(package_name + ".enterprise_ai", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.controller = mock.Mock()
        self.controller.manifest = {"cleanup_errors": []}
        self.controller.measurement_start = None
        self.controller.measurement_ended = False
        controller_patch = mock.patch.object(module, "Controller", return_value=self.controller)
        controller_patch.start()
        self.addCleanup(controller_patch.stop)
        addresses = mock.patch.object(socket, "getaddrinfo", side_effect=lambda host, *_: [
            (socket.AF_INET, 0, 0, "", ("192.0.2.30" if host == "192.0.2.30" else "192.0.2.10", 0)),
        ])
        addresses.start()
        self.addCleanup(addresses.stop)

    def test_base_copy_trace_and_restoration_order(self):
        scenario = self.module.EnterpriseAI()
        self.controller.prepare.side_effect = lambda: scenario.events.append("prepare")
        self.controller.restore_and_collect.side_effect = lambda: scenario.events.append("restore_and_collect")
        scenario.setUp()
        scenario.runTest()
        scenario.tearDown()
        self.assertLess(scenario.events.index("prepare"), scenario.events.index("Setup"))
        self.assertLess(scenario.events.index("Setup"), scenario.events.index("trace_start"))
        self.assertLess(scenario.events.index("trace_start"), scenario.events.index("Run Test"))
        self.assertLess(scenario.events.index("trace_stop"), scenario.events.index("restore_and_collect"))
        self.assertLess(scenario.events.index("Teardown"), scenario.events.index("trace_stop"))
        self.controller.begin.assert_called_once()
        self.controller.finish_window.assert_called_once()
        self.assertIn("enterprise_ai_metrics", FakeParams.get("global", "tools"))
        self.assertEqual(FakeParams.get("global", "trace_filemode"), "1")
        scenario._restore_overrides()
        self.assertEqual(FakeParams.get("global", "tools"), "run_report")

    def test_no_consent_fails_before_base_initialization(self):
        FakeParams.values["enterprise_ai", "dedicated_dut"] = "0"
        with mock.patch.object(FakeScenario, "__init__") as initializer:
            with self.assertRaisesRegex(ValueError, "dedicated_dut"):
                self.module.EnterpriseAI()
            initializer.assert_not_called()

    def test_host_alias_and_mapped_loopback_rejected(self):
        with mock.patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, 0, 0, "", ("192.0.2.10", 0)),
        ]), self.assertRaisesRegex(ValueError, "own host"):
            self.module.EnterpriseAI()
        FakeParams.values["global", "dut_ip"] = "::ffff:127.0.0.1"
        with self.assertRaisesRegex(ValueError, "Loopback"):
            self.module.EnterpriseAI()

    def test_extra_tool_is_not_silently_enabled_or_removed(self):
        FakeParams.values["global", "tools"] = "power_light run_report"
        with self.assertRaisesRegex(ValueError, "Extra tool"):
            self.module.EnterpriseAI()
        self.assertEqual(FakeParams.get("global", "tools"), "power_light run_report")

    def test_legacy_network_flag_is_rejected(self):
        FakeParams.values["enterprise_ai", "background_onedrive_copy"] = "1"
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.module.EnterpriseAI()

    def test_failed_action_return_cannot_be_success(self):
        scenario = self.module.EnterpriseAI()
        scenario.setUp()
        scenario.action_result = 1
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            scenario.runTest()
        self.controller.fail.assert_called_once()
        self.controller.stop_work.assert_called()

    def test_setup_failure_registers_restoration_cleanup(self):
        scenario = self.module.EnterpriseAI()
        self.controller.prepare.side_effect = RuntimeError("synthetic prepare failure")
        with self.assertRaisesRegex(RuntimeError, "prepare failure"):
            scenario.setUp()
        self.controller.fail.assert_called_once()
        for cleanup in reversed(scenario.cleanups):
            cleanup()
        self.controller.restore_and_collect.assert_called()

    def test_host_preflight_failure_precedes_dut_preparation(self):
        scenario = self.module.EnterpriseAI()
        tool = mock.Mock(module="enterprise_ai_metrics")
        tool.preflight.side_effect = RuntimeError("synthetic missing host decoder")
        scenario.tool_instances = [tool]
        with self.assertRaisesRegex(RuntimeError, "missing host decoder"):
            scenario.setUp()
        self.controller.prepare.assert_not_called()

    def test_active_trace_blocks_early_restoration(self):
        scenario = self.module.EnterpriseAI()
        scenario.trace_started = True
        with self.assertRaisesRegex(RuntimeError, "trace has stopped"):
            scenario.finish_environment()
        self.controller.restore_and_collect.assert_not_called()

    def test_foreground_cleanup_failure_is_recorded_before_trace_reporting(self):
        scenario = self.module.EnterpriseAI()
        scenario.setUp()
        scenario.action_result = 1
        self.controller.fail.side_effect = lambda error: scenario.events.append("failure_recorded")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
            scenario.tearDown()
        self.assertLess(scenario.events.index("failure_recorded"), scenario.events.index("trace_stop"))
        self.assertTrue(self.controller.manifest["cleanup_errors"])

    def test_marker_rpc_error_is_not_hidden(self):
        scenario = self.module.EnterpriseAI()
        for response in ('{"error": "synthetic failure"}', "bad response"):
            self.rpc.plugin_call.return_value = response
            with self.assertRaises(RuntimeError):
                scenario._ai_marker("synthetic marker")

    def test_timeout_cleans_only_through_owned_controller(self):
        scenario = self.module.EnterpriseAI()
        scenario.kill()
        self.controller.stop_work.assert_called()
        self.controller.restore_and_collect.assert_called()
        self.assertNotIn("Teardown", scenario.events)


if __name__ == "__main__":
    unittest.main()
