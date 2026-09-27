# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""EC-derived foreground actions with a single measured, isolated AI lifecycle."""

import logging
import json
import ipaddress
import os
import socket

import core.app_scenario
import core.call_rpc as rpc
from core.parameters import Params
from scenarios.windows._library.enterprise_ai.configuration import (
    Configuration, DEFAULTS, validate_remote_dut,
)
from scenarios.windows._library.enterprise_ai.lifecycle import Controller
from . import default_params


class EnterpriseAI(core.app_scenario.Scenario):
    module = "enterprise_ai"
    prep_version = "1"
    default_params.run()

    def __init__(self, *args, **kwargs):
        values = {name: Params.get(self.module, name) for name in DEFAULTS}
        self.ai_config = Configuration.from_values(values)
        validate_remote_dut(
            values["dedicated_dut"], Params.get("global", "platform"),
            Params.get("global", "dut_ip"), Params.get("global", "local_execution"),
        )
        def addresses(host):
            result = set()
            for item in socket.getaddrinfo(host, None):
                address = ipaddress.ip_address(item[4][0])
                result.add(getattr(address, "ipv4_mapped", None) or address)
            return result
        target_addresses = addresses(Params.get("global", "dut_ip"))
        local_addresses = addresses(socket.gethostname())
        if target_addresses & local_addresses:
            raise ValueError("enterprise_ai refuses to run a workload against its own host address.")
        if Params.get("global", "training_mode") != "0" or Params.get("global", "collection_enabled") != "1":
            raise ValueError("enterprise_ai requires collection_enabled=1 and training_mode=0.")
        for legacy in ("background_onedrive_copy", "perf_run"):
            if Params.get_raw(self.module, legacy) not in (None, "", "0"):
                raise ValueError(f"{legacy} is not supported by enterprise_ai; use its scoped UTC and stress configuration.")
        tools = Params.get("global", "tools").split()
        incompatible = set(tools) - {"run_report", "enterprise_ai_metrics"}
        if incompatible:
            raise ValueError(
                "The initial light-capture protocol requires global:tools=run_report. "
                "Extra tool(s) need separate overhead/state validation: " + ", ".join(sorted(incompatible))
            )
        self._parameter_overrides = []
        self._override("global", "tools", "+enterprise_ai_metrics")
        self._override("global", "web_replay_run", "1")
        self._override("global", "phase_reporting", "1")
        self._override("global", "trace_filemode", "1")
        self.prep_scenarios = (
            ["edge_install", "web_prep", "teams_install", "office_install", "productivity_prep"]
            if self.ai_config.foreground else []
        )
        self.ai_controller = None
        self.actions = None
        self._foreground_started = False
        self._foreground_cleaned = False
        self._base_teardown_done = False
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            self._restore_overrides()
            raise
        for tool in self.tool_instances:
            if type(tool).__module__ == "tools.run_report":
                tool.files = "run_info.csv study_vars.csv *ConfigPre.csv *ConfigPost.csv enterprise_ai_summary.csv"
        self.addCleanup(self._restore_overrides)
        self.addCleanup(self._cleanup)
        self.ai_controller = Controller(self, self.ai_config, self._ai_marker)

    def _override(self, section, name, value):
        previous = Params.overrides.get(section, {}).get(name)
        self._parameter_overrides.append((section, name, previous))
        Params.setOverride(section, name, value)

    def _restore_overrides(self):
        for section, name, previous in reversed(self._parameter_overrides):
            if previous is None:
                Params.overrides.get(section, {}).pop(name, None)
            else:
                Params.overrides[section][name] = previous
        self._parameter_overrides.clear()

    def _ai_marker(self, text):
        response = rpc.plugin_call(self.dut_ip, self.rpc_port, "InputInject", "EventTag", text)
        try:
            result = json.loads(response)
        except (TypeError, ValueError) as error:
            raise RuntimeError("InputInject marker call did not return JSON.") from error
        if not isinstance(result, dict) or "error" in result or "result" not in result:
            raise RuntimeError(f"InputInject rejected the measurement marker: {response}")
        logging.info("enterprise_ai marker: %s", text)

    def _phase(self, name):
        phase = self._find_next_type(name, json=self.actions)
        if phase is None:
            raise RuntimeError(f"The enterprise_ai action JSON is missing its {name} phase.")
        result = self.run_actions(phase["children"])
        if result != 0:
            raise RuntimeError(f"The {name} phase did not complete successfully (result={result}).")

    def setUp(self):
        try:
            if self.tool_failure_reason:
                raise RuntimeError(self.tool_failure_reason)
            for tool in self.tool_instances:
                if getattr(tool, "module", None) == "enterprise_ai_metrics":
                    tool.preflight(self)
            self.actions = self.load_action_json(os.path.join(os.path.dirname(__file__), "enterprise_ai.json"))
            self.ai_controller.prepare()
            if self.ai_config.foreground:
                self._foreground_started = True
                self._phase("Setup")
            super().setUp()
        except Exception as error:
            self.ai_controller.fail(error)
            raise

    def process_action(self, action):
        controller = self.ai_controller
        if controller and controller.measurement_start is not None and not controller.measurement_ended:
            if controller.clock() > controller.measurement_start + self.ai_config.measurement_seconds:
                raise RuntimeError("Foreground workload exceeded the fixed measurement window.")
        return super().process_action(action)

    def runTest(self):
        try:
            self.ai_controller.begin()
            if self.ai_config.foreground:
                self._phase("Run Test")
            self.ai_controller.finish_window()
        except Exception as error:
            self.ai_controller.fail(error)
            raise
        finally:
            try:
                self.ai_controller.end_measurement()
            finally:
                self.ai_controller.stop_work()

    def finish_environment(self):
        """Called after core WPR stop and before metrics/report callbacks."""
        if self.trace_started:
            raise RuntimeError("UTC state cannot be restored until the core trace has stopped.")
        self.ai_controller.restore_and_collect()

    def tearDown(self):
        try:
            self.ai_controller.stop_work()
            self._cleanup_foreground()
            self.ai_controller.collect_before_trace_stop()
        except Exception as error:
            self.ai_controller.fail(error)
            raise
        finally:
            try:
                super().tearDown()
            finally:
                self._base_teardown_done = True
                self._cleanup()

    def _cleanup_foreground(self):
        if self._foreground_started and not self._foreground_cleaned:
            self._phase("Teardown")
            self._foreground_cleaned = True

    def _cleanup(self):
        controller = self.ai_controller
        if controller is None:
            return
        errors = []
        for operation in (controller.end_measurement, controller.stop_work, self._cleanup_foreground):
            try:
                operation()
            except Exception as error:
                logging.error("enterprise_ai cleanup: %s", error)
                errors.append(str(error))
        if errors:
            controller.manifest["cleanup_errors"].extend(errors)
            controller.fail(RuntimeError("; ".join(errors)))
        if self.trace_started and not self._base_teardown_done:
            try:
                core.app_scenario.Scenario.tearDown(self)
            except Exception as error:
                logging.error("enterprise_ai emergency trace finalization: %s", error)
                errors.append(str(error))
            finally:
                self._base_teardown_done = True
        if not self.trace_started:
            try:
                self.finish_environment()
            except Exception as error:
                logging.error("enterprise_ai environment restoration: %s", error)
                errors.append(str(error))
        if errors:
            failure = RuntimeError("enterprise_ai cleanup incomplete: " + "; ".join(errors))
            controller.fail(failure)
            raise failure

    def kill(self):
        self._cleanup()
