# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""SAFE automated tests: no child processes, load, prep, services, WPR, or DUT calls.

Run with a test Python: python -B -m unittest discover -s tests -p
test_enterprise_ai_stress.py. All process/clock/Windows interactions are fake.
These tests neither install dependencies nor create files outside the repository.
"""

import contextlib
import copy
from dataclasses import FrozenInstanceError
import importlib.util
import io
import json
import ntpath
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "scenarios" / "windows" / "_library" / "enterprise_ai"
SPEC = importlib.util.spec_from_file_location("enterprise_ai_stress_under_test", LIBRARY / "stress_worker.py")
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)

RUN_ID = "0123456789abcdef0123456789abcdef"
RUN_DIRECTORY = str(ROOT / "test_runs" / RUN_ID)
CONFIG_PATH = str(Path(RUN_DIRECTORY) / "stress_config.json")
VENV_PYTHON = str(ROOT / "fake_venv" / "Scripts" / "python.exe")
BASE_PYTHON = str(ROOT / "fake_pyenv" / "python.exe")


def configuration(**overrides):
    document = {
        "schema_version": 1, "run_id": RUN_ID, "workers": 2,
        "duty_cycle": 0.4, "max_seconds": 60.0, "sample_interval_seconds": 0.1,
    }
    document.update(overrides)
    return document


def identity(process_id, executable=BASE_PYTHON):
    return {"pid": process_id, "create_time": str(133000000000000000 + process_id),
            "executable": executable}


def owner_manifest():
    return {
        "schema_version": 1, "run_id": RUN_ID, "workers": 2,
        "run_directory": RUN_DIRECTORY, "config_path": CONFIG_PATH,
        "config_sha256": "a" * 64, "worker_path": str(LIBRARY / "stress_worker.py"),
        "venv_python": VENV_PYTHON, "base_python": BASE_PYTHON,
        "launcher": identity(40, VENV_PYTHON),
    }


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Negative wait")
        self.value = round(self.value + seconds, 9)


class FakeEvent:
    def __init__(self, clock):
        self.clock = clock
        self.value = False

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def wait(self, timeout):
        if not self.value:
            self.clock.sleep(timeout)
        return self.value


class FakeReceiver:
    def __init__(self, context, index):
        self.context = context
        self.index = index
        self.closed = False
        self.received = False

    def poll(self):
        return self.context.send_readiness and not self.received

    def recv(self):
        process = self.context.processes[self.index]
        if not process.started:
            raise AssertionError("Readiness must come from a started child")
        self.received = True
        return {"kind": "ready", "run_id": RUN_ID, "worker_id": self.index,
                "parent_pid": 42, **identity(process.pid)}

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, context, *, target, args, name, daemon):
        self.context = context
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.pid = None
        self.alive = False
        self.started = False
        self.closed = False
        self.terminated = False
        self.join_timeouts = []
        self.return_code = 0

    @property
    def exitcode(self):
        return None if self.alive else self.return_code

    def start(self):
        self.started = True
        self.pid = 100 + self.args[0].worker_id
        self.alive = True
        # Intentionally never call target: no real stress or multiprocessing.

    def is_alive(self):
        if self.context.exit_after_release and self.context.events[1].is_set():
            self.alive = False
        return self.alive

    def join(self, timeout):
        self.join_timeouts.append(timeout)
        if self.context.graceful and self.context.events[0].is_set():
            self.alive = False
        elif self.alive:
            self.context.clock.sleep(timeout)

    def terminate(self):
        self.terminated = True
        self.return_code = 1
        self.alive = False

    def close(self):
        if self.alive:
            raise AssertionError("Cannot close a live process")
        self.closed = True


class FakeContext:
    def __init__(self, clock):
        self.clock = clock
        self.processes = []
        self.receivers = []
        self.senders = []
        self.events = []
        self.send_readiness = True
        self.graceful = True
        self.exit_after_release = False

    def Event(self):
        event = FakeEvent(self.clock)
        self.events.append(event)
        return event

    def Pipe(self, duplex):
        if duplex:
            raise AssertionError("Readiness channel must be one-way")
        receiver = FakeReceiver(self, len(self.receivers))
        sender = Mock()
        self.receivers.append(receiver)
        self.senders.append(sender)
        return receiver, sender

    def Process(self, **kwargs):
        process = FakeProcess(self, **kwargs)
        self.processes.append(process)
        return process


class FakeProbe:
    def __init__(self, clock):
        self.clock = clock
        self.primed = False
        self.fail_metrics = False
        self.reuse_after = None

    def identity(self, process_id):
        result = identity(process_id)
        if self.reuse_after is not None and self.clock() >= self.reuse_after:
            result["create_time"] = str(int(result["create_time"]) + 999)
        return result

    def prime_metrics(self):
        self.primed = True

    def sample(self):
        if not self.primed:
            raise AssertionError("Metrics were not primed")
        if self.fail_metrics:
            raise worker.StressError("Measured system counters are unavailable")
        return {"cpu_percent": 83.5, "available_memory_bytes": 12_000, "committed_bytes": 34_000}


class FakeFiles:
    def __init__(self, clock):
        self.clock = clock
        self.stop_at = 0.25
        self.states = []
        self.rows = []
        self.sample_file_closed = False
        self.on_publish = None

    def stop_requested(self):
        return self.stop_at is not None and self.clock() >= self.stop_at

    def write_state(self, state):
        if self.on_publish:
            self.on_publish(state)
        if state["status"] in ("stopped", "failed") and not self.sample_file_closed:
            raise AssertionError("Terminal state must follow sample handle close")
        self.states.append(copy.deepcopy(state))

    @contextlib.contextmanager
    def samples(self):
        try:
            yield self.rows.append
        finally:
            self.sample_file_closed = True


class Harness:
    def __init__(self, **config):
        self.clock = FakeClock()
        self.context = FakeContext(self.clock)
        self.probe = FakeProbe(self.clock)
        self.files = FakeFiles(self.clock)
        self.config = worker.validate_config(configuration(**config), logical_cpus=8)

    def run(self):
        # Guard the real entry points even if an implementation accidentally
        # stops honoring dependency injection.
        with patch.object(worker.mp, "get_context", side_effect=AssertionError("Real multiprocessing forbidden")), \
             patch.object(worker, "WindowsProbe", side_effect=AssertionError("Native process/metrics calls forbidden")), \
             patch.object(worker, "run_duty_cycle", side_effect=AssertionError("Real load forbidden")), \
             patch.object(worker, "arithmetic_batch", side_effect=AssertionError("Real load forbidden")):
            return worker.run_controller(
                self.config, RUN_DIRECTORY, {**identity(42), "parent_pid": 40},
                owner_manifest(), self.probe, self.context, self.files,
                clock=self.clock, sleep=self.clock.sleep)


class ConfigurationTests(unittest.TestCase):
    def test_valid_exact_config(self):
        config = worker.validate_config(configuration(), logical_cpus=8)
        self.assertEqual(config.workers, 2)
        self.assertEqual(config.duty_cycle, 0.4)
        self.assertEqual(config.schema_version, 1)

    def test_parent_maximum_budget_fits_within_the_hard_safety_ceiling(self):
        budget = 7200 + 600 + 300
        config = worker.validate_config(configuration(max_seconds=budget), logical_cpus=8)
        self.assertEqual(config.max_seconds, 8100.0)
        self.assertGreaterEqual(worker.MAX_SECONDS, budget)
        at_limit = worker.validate_config(configuration(max_seconds=worker.MAX_SECONDS), logical_cpus=8)
        self.assertEqual(at_limit.max_seconds, 10800.0)

    def test_rejects_invalid_or_unbounded_fields(self):
        cases = [
            {"schema_version": True}, {"schema_version": 2}, {"run_id": RUN_ID.upper()},
            {"run_id": "../foreign"}, {"run_id": "a" * 31}, {"workers": True},
            {"workers": 0}, {"workers": -1}, {"workers": 9}, {"workers": 1.5},
            {"duty_cycle": True}, {"duty_cycle": 0}, {"duty_cycle": 1.01},
            {"duty_cycle": float("nan")}, {"max_seconds": 0},
            {"max_seconds": worker.MAX_SECONDS + 1}, {"max_seconds": float("inf")},
            {"sample_interval_seconds": 0}, {"sample_interval_seconds": "1"},
            {"sample_interval_seconds": float("nan")}, {"extra_setting": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_config(configuration(**changes), logical_cpus=8)

    def test_missing_fields_and_cpu_count_fail(self):
        document = configuration()
        del document["workers"]
        with self.assertRaises(ValueError):
            worker.validate_config(document, logical_cpus=8)
        for count in (0, -1, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                worker.validate_config(configuration(), logical_cpus=count)
        with patch.object(worker.os, "cpu_count", return_value=None), self.assertRaises(ValueError):
            worker.validate_config(configuration())

    def test_duplicate_json_keys_are_rejected_and_bom_is_supported(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            worker.decode_json(b'{"schema_version": 1, "schema_version": 2}')
        raw = b"\xef\xbb\xbf" + json.dumps(configuration()).encode("utf-8")
        self.assertEqual(worker.decode_json(raw), configuration())

    def test_absolute_scoped_windows_paths(self):
        run = "R:\\hobl_data\\enterprise_ai\\" + RUN_ID
        worker.validate_scope(run + "\\stress_config.json", run, RUN_ID, paths=ntpath)
        cases = [
            ("relative.json", run, RUN_ID),
            ("R:stress_config.json", run, RUN_ID),
            ("\\" + RUN_ID + "\\stress_config.json", "\\" + RUN_ID, RUN_ID),
            (run + "\\stress_config.json", "relative\\" + RUN_ID, RUN_ID),
            ("R:\\other\\stress_config.json", run, RUN_ID),
            (run + "\\..\\stress_config.json", run, RUN_ID),
            (run + "\\stress_state.json", run, RUN_ID),
            (run + "\\stress_child_0.log", run, RUN_ID),
            (run + "\\stress_config.json", run + "wrong", RUN_ID),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(worker.OwnershipError):
                worker.validate_scope(*args, paths=ntpath)

    def test_parent_resource_run_directory_contract(self):
        run = "R:\\hobl_bin\\enterprise_ai_resources\\runs\\" + RUN_ID
        worker.validate_scope(run + "\\stress_config.json", run, RUN_ID, paths=ntpath)
        self.assertNotIn("owner.json", worker.RESERVED_FILES)
        self.assertNotIn("prep.log", worker.RESERVED_FILES)
        self.assertIn("stress_owner.json", worker.RESERVED_FILES)

    def test_invalid_cli_never_constructs_a_process_or_native_probe(self):
        with patch.object(worker, "load_config", side_effect=ValueError("invalid")), \
             patch.object(worker.mp, "get_context") as context, \
             patch.object(worker, "WindowsProbe") as probe, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worker.main(["--config", CONFIG_PATH, "--run-directory", RUN_DIRECTORY]), 1)
            context.assert_not_called()
            probe.assert_not_called()


class OwnershipTests(unittest.TestCase):
    def test_exact_identity_and_pid_reuse(self):
        expected = identity(42)
        worker.validate_identity(expected, dict(expected))
        for changes in ({"pid": 43}, {"create_time": "999"}, {"executable": VENV_PYTHON}):
            with self.subTest(changes=changes), self.assertRaises(worker.OwnershipError):
                worker.validate_identity(expected, {**expected, **changes})
        with self.assertRaises(worker.OwnershipError):
            worker.validate_identity({**expected, "create_time": 133000000000000042}, expected)

    def test_launcher_and_venv_redirector_are_explicitly_owned(self):
        config = worker.validate_config(configuration(), logical_cpus=8)
        owner = owner_manifest()
        probe = Mock()
        probe.identity.return_value = owner["launcher"]
        for controller, parent in ((owner["launcher"], 1), (identity(42), 40)):
            worker.validate_owner(owner, config, CONFIG_PATH, RUN_DIRECTORY, "a" * 64,
                                  probe, controller, parent)

    def test_foreign_owner_config_and_redirector_fail(self):
        config = worker.validate_config(configuration(), logical_cpus=8)
        owner = owner_manifest()
        probe = Mock()
        probe.identity.return_value = owner["launcher"]
        for changes in ({"run_id": "b" * 32}, {"config_sha256": "b" * 64},
                        {"worker_path": str(ROOT / "foreign.py")}, {"workers": 3},
                        {"schema_version": True}, {"run_directory": str(ROOT)}):
            with self.subTest(changes=changes), self.assertRaises(worker.OwnershipError):
                worker.validate_owner({**owner, **changes}, config, CONFIG_PATH, RUN_DIRECTORY,
                                      "a" * 64, probe, identity(42), 40)
        with self.assertRaises(worker.OwnershipError):
            worker.validate_owner(owner, config, CONFIG_PATH, RUN_DIRECTORY, "a" * 64,
                                  probe, identity(42), 999)

    def test_owner_wait_is_bounded_without_launching(self):
        clock = FakeClock()
        with patch.object(worker, "ensure_plain_path"), \
             patch.object(Path, "is_file", return_value=False), \
             self.assertRaisesRegex(worker.OwnershipError, "no workers were started"):
            worker.await_owner(Path(RUN_DIRECTORY), clock=clock, sleep=clock.sleep)
        self.assertLessEqual(clock(), worker.OWNER_WAIT_SECONDS + worker.POLL_SECONDS)


class MetricsTests(unittest.TestCase):
    def test_measured_total_cpu_is_normalized_not_per_core(self):
        self.assertEqual(worker.cpu_percent((10, 60, 30), (35, 110, 80)), 75.0)
        self.assertEqual(worker.cpu_percent((0, 0, 0), (0, 6400, 6400)), 100.0)
        self.assertEqual(worker.cpu_percent((0, 0, 0), (6400, 6400, 0)), 0.0)

    def test_unavailable_or_invalid_counters_fail_instead_of_fabricating_zero(self):
        for previous, current in (((1, 2, 3), (1, 2, 3)),
                                  ((1, 2, 3), (0, 3, 4)),
                                  ((0, 0, 0), (100, 10, 10))):
            with self.subTest(current=current), self.assertRaises(worker.StressError):
                worker.cpu_percent(previous, current)

    def test_native_probe_does_not_fall_back_to_fake_metrics(self):
        with patch.object(worker.os, "name", "not_windows"), self.assertRaisesRegex(worker.StressError, "required"):
            worker.WindowsProbe()


class JobSafetyTests(unittest.TestCase):
    def make_job(self, active_processes):
        job = object.__new__(worker.WindowsJob)
        job.handle = 123
        job.probe = Mock()

        def query(handle, info_class, accounting, size, returned):
            accounting._obj.active_processes = active_processes
            return 1

        job.probe.kernel.QueryInformationJobObject.side_effect = query
        return job

    def test_job_is_disarmed_only_after_all_children_have_exited(self):
        job = self.make_job(1)
        self.assertTrue(job.finish())
        self.assertIsNone(job.handle)
        job.probe.kernel.SetInformationJobObject.assert_called_once()
        job.probe.kernel.CloseHandle.assert_called_once_with(123)

    def test_live_descendants_keep_kill_on_close_armed(self):
        job = self.make_job(2)
        self.assertFalse(job.finish())
        self.assertEqual(job.handle, 123)
        job.probe.kernel.SetInformationJobObject.assert_not_called()
        job.probe.kernel.CloseHandle.assert_not_called()
        job.abort()
        job.probe.kernel.TerminateJobObject.assert_called_once_with(123, 1)

    def test_main_closes_controller_logs_before_aborting_an_owned_job(self):
        closed = []
        files = Mock()

        @contextlib.contextmanager
        def logs():
            try:
                yield
            finally:
                closed.append(True)

        files.logs.side_effect = logs
        probe = Mock()
        probe.identity.side_effect = lambda pid: identity(pid, VENV_PYTHON if pid == 40 else BASE_PYTHON)
        job = Mock()
        job.handle = 123
        job.finish.return_value = False
        job.abort.side_effect = lambda: self.assertEqual(closed, [True])
        config = worker.validate_config(configuration(), logical_cpus=8)
        state = {"status": "stopped", "error": None}
        with patch.object(worker, "load_config", return_value=(config, "a" * 64)), \
             patch.object(worker, "RunFiles", return_value=files), \
             patch.object(worker, "await_owner", return_value=owner_manifest()), \
             patch.object(worker, "WindowsProbe", return_value=probe), \
             patch.object(worker, "WindowsJob", return_value=job), \
             patch.object(worker, "run_controller", return_value=(0, state)), \
             patch.object(worker.mp, "get_context", return_value=Mock()), \
             patch.object(worker.os, "getpid", return_value=42), \
             patch.object(worker.os, "getppid", return_value=40):
            code = worker.main(["--config", CONFIG_PATH, "--run-directory", RUN_DIRECTORY])
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        job.abort.assert_called_once()
        self.assertIn("descendants", state["error"])


class ControllerTests(unittest.TestCase):
    def test_spawn_configuration_is_explicit_immutable_and_per_worker(self):
        harness = Harness(duty_cycle=0.65)
        result, state = harness.run()
        self.assertEqual(result, 0)
        self.assertEqual(state["status"], "stopped")
        for index, process in enumerate(harness.context.processes):
            self.assertIs(process.target, worker.child_main)
            self.assertFalse(process.daemon)
            config = process.args[0]
            self.assertEqual(config.worker_id, index)
            self.assertEqual(config.run_id, RUN_ID)
            self.assertEqual(config.duty_cycle, 0.65)
            with self.assertRaises(FrozenInstanceError):
                config.duty_cycle = 0.1
        self.assertIsNot(harness.context.processes[0].args[0], harness.context.processes[1].args[0])

    def test_readiness_requires_all_actual_children_and_measured_samples(self):
        harness = Harness()

        def check_ready(state):
            if state["status"] == "ready":
                self.assertTrue(all(process.started for process in harness.context.processes))
                self.assertTrue(all(receiver.received for receiver in harness.context.receivers))
                self.assertTrue(harness.context.events[1].is_set())
                self.assertEqual(len(state["children"]), harness.config.workers)
                self.assertGreater(len(harness.files.rows), 0)

        harness.files.on_publish = check_ready
        code, state = harness.run()
        self.assertEqual(code, 0)
        self.assertEqual(state["child_pids"], [100, 101])
        self.assertTrue(all(row["cpu_percent"] == 83.5 for row in harness.files.rows))
        self.assertTrue(all(set(row) == {"elapsed_s", "cpu_percent", "available_memory_bytes", "committed_bytes"}
                            for row in harness.files.rows))
        self.assertTrue(harness.files.sample_file_closed)
        self.assertTrue(all(process.closed and not process.alive for process in harness.context.processes))
        self.assertTrue(all(receiver.closed for receiver in harness.context.receivers))
        for sender in harness.context.senders:
            sender.close.assert_called_once()

    def test_readiness_timeout_never_reports_ready_and_cleans_children(self):
        harness = Harness()
        harness.context.send_readiness = False
        harness.files.stop_at = None
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("readiness timed out", state["error"])
        self.assertNotIn("ready", [state["status"] for state in harness.files.states])
        self.assertTrue(all(process.closed for process in harness.context.processes))
        self.assertLessEqual(harness.clock(), worker.STARTUP_SECONDS + worker.POLL_SECONDS)

    def test_maximum_runtime_is_failure_not_success(self):
        harness = Harness(max_seconds=0.2)
        harness.files.stop_at = None
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("max_seconds expired", state["error"])
        self.assertEqual(state["status"], "failed")
        self.assertTrue(all(not process.alive for process in harness.context.processes))

    def test_long_healthy_foreground_needs_no_parent_status_polling(self):
        for measurement_seconds, settle_seconds in ((1800, 60), (7200, 600)):
            with self.subTest(measurement_seconds=measurement_seconds):
                harness = Harness(max_seconds=measurement_seconds + settle_seconds + 300,
                                  sample_interval_seconds=1.0)
                harness.files.stop_at = measurement_seconds
                code, state = harness.run()
                self.assertEqual(code, 0)
                self.assertEqual(state["status"], "stopped")
                self.assertIsNone(state["error"])
                self.assertEqual(state["elapsed_s"], measurement_seconds)
                self.assertEqual(len(harness.files.rows), measurement_seconds)
                self.assertEqual(harness.files.rows[-1]["elapsed_s"], measurement_seconds - 1)
                self.assertTrue(any(item["status"] == "ready" for item in harness.files.states))
                self.assertTrue(all(process.closed and not process.alive for process in harness.context.processes))

    def test_stop_during_readiness_does_not_release_workers(self):
        harness = Harness()
        harness.context.send_readiness = False
        harness.files.stop_at = 0.1
        code, state = harness.run()
        self.assertEqual(code, 0)
        self.assertEqual(state["status"], "stopped")
        self.assertFalse(harness.context.events[1].is_set())
        self.assertNotIn("ready", [state["status"] for state in harness.files.states])

    def test_metrics_failure_is_explicit_and_stops_children(self):
        harness = Harness()
        harness.probe.fail_metrics = True
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("counters are unavailable", state["error"])
        self.assertFalse(harness.context.events[1].is_set())
        self.assertTrue(all(process.closed for process in harness.context.processes))

    def test_metrics_delay_past_safety_deadline_cannot_release_workers(self):
        harness = Harness(max_seconds=0.1)
        sample = harness.probe.sample

        def delayed_sample():
            harness.clock.sleep(0.2)
            return sample()

        harness.probe.sample = delayed_sample
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("expired before readiness", state["error"])
        self.assertFalse(harness.context.events[1].is_set())

    def test_nonzero_child_exit_during_stop_is_not_hidden_as_success(self):
        harness = Harness()

        def mark_exit(state):
            if state["status"] == "ready":
                harness.context.processes[0].return_code = 7

        harness.files.on_publish = mark_exit
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("exited with code 7", state["error"])
        self.assertEqual(state["status"], "failed")

    def test_unexpected_child_exit_fails(self):
        harness = Harness()
        harness.context.exit_after_release = True
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("exited unexpectedly", state["error"])
        self.assertTrue(all(process.closed for process in harness.context.processes))

    def test_forced_termination_is_recorded_as_failure_with_one_shared_deadline(self):
        harness = Harness()
        harness.context.graceful = False
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("Forced termination", state["error"])
        self.assertTrue(all(process.terminated and process.closed for process in harness.context.processes))
        self.assertLessEqual(harness.clock(), harness.files.stop_at + worker.STOP_SECONDS + worker.POLL_SECONDS)

    def test_pid_reuse_never_terminates_a_foreign_process(self):
        harness = Harness()
        harness.context.graceful = False
        harness.probe.reuse_after = 0.5
        code, state = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("reused", state["error"])
        self.assertTrue(all(not process.terminated for process in harness.context.processes))
        self.assertIn("owned job remains armed", state["error"])

    def test_forged_child_readiness_is_rejected(self):
        clock = FakeClock()
        process = Mock()
        process.is_alive.return_value = True
        receiver = Mock()
        receiver.poll.return_value = True
        receiver.recv.return_value = {"kind": "ready", "run_id": RUN_ID, "worker_id": 0,
                                     "parent_pid": 42, **identity(999)}
        with self.assertRaises(worker.OwnershipError):
            worker.wait_for_readiness([process], [receiver], [identity(100)], RUN_ID, 42,
                                      1, lambda: False, clock=clock, sleep=clock.sleep)

    def test_child_not_alive_is_not_ready_even_if_pipe_has_a_message(self):
        process = Mock()
        process.is_alive.return_value = False
        receiver = Mock()
        with self.assertRaisesRegex(worker.StressError, "before readiness"):
            worker.wait_for_readiness([process], [receiver], [identity(100)], RUN_ID, 42,
                                      1, lambda: False, clock=lambda: 0)
        receiver.recv.assert_not_called()

    def test_duty_cycle_uses_fixed_timing_without_computing_real_load(self):
        clock = FakeClock()
        stop = FakeEvent(clock)
        calls = []

        def fake_compute(value):
            calls.append(clock())
            clock.sleep(0.01)
            if clock() >= 0.23:
                stop.set()
            return value

        child = worker.ChildConfig(RUN_ID, 0, 0.4, 1)
        worker.run_duty_cycle(child, stop, clock=clock, compute=fake_compute)
        starts = [0] + [index for index in range(1, len(calls))
                        if calls[index] - calls[index - 1] > 0.02]
        self.assertEqual([calls[index] for index in starts], [0.0, 0.1, 0.2])
        for start, next_start in zip(starts, starts[1:]):
            busy_span = calls[next_start - 1] + 0.01 - calls[start]
            self.assertGreaterEqual(busy_span + 1e-9, 0.04)
            self.assertLessEqual(busy_span, 0.05 + 1e-9)


class PowerShellSafetyContractTests(unittest.TestCase):
    """Source contracts, not claims of live DUT/PowerShell integration coverage."""

    def test_prep_preserves_shared_toolchains_and_uses_process_local_selection(self):
        text = (LIBRARY / "prep.ps1").read_text(encoding="utf-8")
        self.assertIn("$installedVersions -notcontains $pythonVersion", text)
        self.assertIn("$env:PYENV_VERSION = $pythonVersion", text)
        self.assertIn("$env:PYENV_VERSION = $savedVersion", text)
        self.assertIn("& $pyenvExecutable which python", text)
        self.assertNotRegex(text, r"(?im)^\s*&.*\binstall\b.*\s-f(?:\s|$)")
        self.assertNotRegex(text, r"(?im)^\s*&.*\bpyenv\w*\s+(?:global|local)\b")
        self.assertNotIn("pip install", text)
        self.assertNotIn("Get-Command python", text)
        self.assertNotIn("Invoke-WebRequest", text)
        self.assertNotIn("SetEnvironmentVariable", text)
        self.assertNotIn("Remove-Item", text)

    def test_controller_uses_scoped_stop_and_exact_process_handles(self):
        text = (LIBRARY / "stress_control.ps1").read_text(encoding="utf-8")
        self.assertIn("-PassThru", text)
        self.assertIn("ToFileTimeUtc()", text)
        self.assertIn("GetProcessById", text)
        self.assertIn("$entry.Process.Kill()", text)
        self.assertIn("$entry.Process.WaitForExit($remaining)", text)
        self.assertIn("New-StopSignal", text)
        self.assertIn("'stress.stop'", text)
        self.assertIn("[IO.FileShare]::None", text)
        self.assertIn("Forced termination was required", text)
        self.assertNotRegex(text, r"(?i)Stop-Process\s+-Name|Get-Process\s+-Name|taskkill|ShellExecute")
        for line in text.splitlines():
            if "Get-CimInstance -ClassName Win32_Process " in line:
                self.assertIn("-Filter ", line)
                self.assertRegex(line, r"(?:ParentProcessId|ProcessId) = \$")

    def test_scripts_have_no_hardcoded_working_drive_or_unrelated_actions(self):
        for name in ("prep.ps1", "stress_control.ps1"):
            text = (LIBRARY / name).read_text(encoding="utf-8")
            self.assertIn("Split-Path -Qualifier $PSScriptRoot", text)
            self.assertNotRegex(text, r"(?i)[A-Z]:\\(?:hobl_|opencv)")
            self.assertNotRegex(text, r"(?im)^\s*(?:wpr|net\s+(?:start|stop)|Set-Service|Restart-Service)\b")

    def test_mock_block_requires_a_noninteractive_no_profile_process(self):
        self.assertIn("[Environment]::GetCommandLineArgs() -notcontains '-NoProfile'", POWERSHELL_MOCK_TESTS)
        self.assertIn("[Environment]::GetCommandLineArgs() -notcontains '-NonInteractive'", POWERSHELL_MOCK_TESTS)
        self.assertLess(POWERSHELL_MOCK_TESTS.index("GetCommandLineArgs"),
                        POWERSHELL_MOCK_TESTS.index("$ErrorActionPreference"))


# Run this block ONLY in a disposable pwsh -NoProfile -NonInteractive child.
# Never invoke/dot-source it or use New-Module in a shared integrated terminal:
# anonymous module function exports can outlive the call and shadow user commands.
# It loads only function ASTs, NOT the controller body; all effects are mocked.
POWERSHELL_MOCK_TESTS = r"""
if ([Environment]::GetCommandLineArgs() -notcontains '-NoProfile' -or
    [Environment]::GetCommandLineArgs() -notcontains '-NonInteractive') {
    throw ' ERROR - Mock tests require a disposable pwsh -NoProfile -NonInteractive child process.'
}
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$controlPath = Join-Path $root 'scenarios\windows\_library\enterprise_ai\stress_control.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($controlPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Controller syntax errors.' }
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }

function Start-Process { throw 'FORBIDDEN: real Start-Process in mocked safety tests' }
function Stop-Process { throw 'FORBIDDEN: real Stop-Process in mocked safety tests' }
function Get-CimInstance { throw 'FORBIDDEN: real process discovery in mocked safety tests' }
function Assert-PlainPath {}
function Assert-ConfigUnchanged {}
function Add-OwnedHandle { param($Identity, $ParentPid) Assert-IdentityRecord $Identity }
function Find-OwnedDescendants {}
function Read-RunJson { return $script:fixtureState }
function Write-RunJson { param($Name, $Value) $script:publishedState = $Value }
function New-StopSignal { $script:signalSent = $true }
function Get-ProcessIdentity {
    param($Process)
    if ($script:reusePid -eq $Process.Identity.pid) {
        return [pscustomobject]@{ pid = $Process.Identity.pid; create_time = '999999'; executable = $Process.Identity.executable }
    }
    return $Process.Identity
}
function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Mock safety test failed: $Message" }
}
function New-FakeProcess {
    param($Identity, [bool]$Exited)
    $fake = [pscustomobject]@{ Identity = $Identity; HasExited = $Exited; Killed = $false; Waited = $false }
    $fake | Add-Member ScriptMethod Kill { $this.Killed = $true; $this.HasExited = $true }
    $fake | Add-Member ScriptMethod WaitForExit { param($Milliseconds) $this.Waited = $true; return $this.HasExited }
    return $fake
}
$RunId = '0123456789abcdef0123456789abcdef'
$RunDirectory = Join-Path $root ('mock_run\' + $RunId)
$ConfigPath = Join-Path $RunDirectory 'stress_config.json'
$workerPath = $controlPath.Replace('stress_control.ps1', 'stress_worker.py')
$venvPython = Join-Path $root 'mock_venv\Scripts\python.exe'
$basePython = Join-Path $root 'mock_pyenv\python.exe'
$owner = [pscustomobject]@{
    schema_version = 1; run_id = $RunId; workers = 1; run_directory = $RunDirectory
    worker_path = $workerPath; config_path = $ConfigPath; config_sha256 = ('a' * 64)
    venv_python = $venvPython; base_python = $basePython
    launcher = [pscustomobject]@{ pid = 101; create_time = '1001'; executable = $venvPython }
}
$script:testOwner = $owner
$child = [pscustomobject]@{ pid = 102; create_time = '1002'; executable = $basePython; worker_id = 0; parent_pid = 101 }
function Reset-Fixture {
    param([string]$Status, [bool]$Exited)
    $script:fixtureState = [pscustomobject]@{
        schema_version = 1; run_id = $RunId; status = $Status
        controller_pid = 101; controller_create_time = '1001'; controller_executable = $venvPython
        controller_parent_pid = 0; children = @($child); child_pids = @(102); error = $null; elapsed_s = 1.0
    }
    $script:handles = @{
        '101' = [pscustomobject]@{ Identity = $owner.launcher; Process = (New-FakeProcess $owner.launcher $Exited) }
        '102' = [pscustomobject]@{ Identity = $child; Process = (New-FakeProcess $child $Exited) }
    }
    $script:publishedState = $null
    $script:signalSent = $false
    $script:reusePid = 0
}
$graceSeconds = 0
$killSeconds = 0

Reset-Fixture 'ready' $false
$actual = Stop-OwnedRun $owner
Assert-Test ($actual.status -eq 'failed' -and $actual.error -match 'Forced termination') 'forced stop must be durable failure'
Assert-Test $signalSent 'stop must signal before fallback'
Assert-Test ($handles['101'].Process.Killed -and $handles['102'].Process.Killed) 'only fake owned processes should be killed'
Assert-Test ($handles['101'].Process.Waited -and $handles['102'].Process.Waited) 'all owned processes must be joined'
Assert-Test ($publishedState.status -eq 'failed') 'forced failure must be persisted'
Write-Output 'PASS mocked PowerShell forced-stop/owned-join contract'

Reset-Fixture 'stopped' $true
$script:fixtureState.elapsed_s = 1800.0
$actual = Stop-OwnedRun $owner
Assert-Test ($actual.status -eq 'stopped') 'graceful terminal state must be preserved'
Assert-Test ($actual.elapsed_s -eq 1800.0) 'a healthy long run must not require recent Status polling'
Assert-Test (-not $handles['101'].Process.Killed -and -not $handles['102'].Process.Killed) 'graceful stop must not kill'
Write-Output 'PASS mocked PowerShell healthy 30-minute graceful-stop contract'

Reset-Fixture 'failed' $true
$script:fixtureState.error = 'Earlier worker failure'
$actual = Stop-OwnedRun $owner
Assert-Test ($actual.status -eq 'failed' -and $actual.error -eq 'Earlier worker failure') 'Stop must not erase an earlier failure'
Assert-Test (-not $handles['101'].Process.Killed -and -not $handles['102'].Process.Killed) 'an already failed stopped run must not kill'
Write-Output 'PASS mocked PowerShell prior-failure preservation'

Reset-Fixture 'ready' $false
function New-StopSignal { throw 'Mock stop-file write failure' }
$actual = Stop-OwnedRun $owner
Assert-Test ($actual.status -eq 'failed' -and $actual.error -match 'Could not signal graceful stop') 'signal failure must be explicit'
Assert-Test ($handles['101'].Process.Killed -and $handles['102'].Process.Killed) 'signal failure must still clean up owned processes'
function New-StopSignal { $script:signalSent = $true }
Write-Output 'PASS mocked PowerShell stop-signal failure cleanup'

Reset-Fixture 'ready' $false
$script:reusePid = 102
$actual = Stop-OwnedRun $owner
Assert-Test ($actual.status -eq 'failed' -and $actual.error -match 'ownership changed') 'PID reuse must fail explicitly'
Assert-Test (-not $handles['102'].Process.Killed) 'a foreign reused PID must never be killed'
Write-Output 'PASS mocked PowerShell PID-reuse refusal'

Reset-Fixture 'ready' $false
$script:fixtureState.children[0].parent_pid = 999
$rejected = $false
try { Assert-State $owner $fixtureState } catch { $rejected = $true }
Assert-Test $rejected 'foreign child ancestry must be rejected'
$script:fixtureState.children[0].parent_pid = 101
Write-Output 'PASS mocked PowerShell state ownership validation'

function Test-StartPython { return [pscustomobject]@{ config_sha256 = ('a' * 64); workers = 1; base_python = $basePython } }
function Test-Path { return $false }
function Get-ChildItem { return @() }
function Start-Process { return (New-FakeProcess $script:testOwner.launcher $false) }
function Stop-OwnedRun { param($Owner, $FailureReason) $script:startupCleanup = $FailureReason }
$startupSeconds = 0
$script:startupCleanup = $null
$rejected = $false
try { $null = Start-OwnedRun } catch { $rejected = $_.Exception.Message -match 'startup timed out' }
Assert-Test ($rejected -and $startupCleanup -match 'startup timed out') 'startup timeout must invoke owned cleanup'
Write-Output 'PASS mocked PowerShell startup-timeout cleanup'
"""


if __name__ == "__main__":
    unittest.main()
