# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Owned, bounded enterprise_ai load; not a copy of percentile_stress.

This is a NEW calibrated, deterministic integer-arithmetic load, not numerically
equivalent to the old NumPy matrix workload. ``workers`` and ``duty_cycle`` are
fixed inputs, not a feedback CPU target. Foreground AI CPU never reduces them.
Samples measure total system CPU (normalized to 0..100), available physical
memory and system commit, rather than estimating them from the requested load.

Only stress_control.ps1 should launch this executable. Configuration is a
versioned JSON file directly inside an absolute run directory named by run_id.
The launcher publishes stress_owner.json before any child may be started.
Windows process creation times are decimal FILETIME strings, not rounded floats.
The 10,800-second hard ceiling covers the parent's maximum 7,200-second
measurement, 600-second settle period and 300-second shutdown budget.
Multi-processor-group systems fail explicitly: GetSystemTimes cannot promise a
whole-system measurement there. No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
import uuid


SCHEMA_VERSION = 1
MAX_SECONDS = 10800.0
STARTUP_SECONDS = 30.0
OWNER_WAIT_SECONDS = 10.0
STOP_SECONDS = 10.0
KILL_SECONDS = 5.0
POLL_SECONDS = 0.05
CYCLE_SECONDS = 0.1
CONFIG_KEYS = frozenset(
    ("schema_version", "run_id", "workers", "duty_cycle",
     "max_seconds", "sample_interval_seconds")
)
RESERVED_FILES = frozenset(
    ("stress_state.json", "stress_owner.json", "stress.stop",
     "stress_samples.jsonl", "stress_stdout.log", "stress_stderr.log",
     "stress_control.lock")
)


class StressError(RuntimeError):
    pass


class OwnershipError(StressError):
    pass


class StopRequested(StressError):
    pass


@dataclass(frozen=True)
class StressConfig:
    schema_version: int
    run_id: str
    workers: int
    duty_cycle: float
    max_seconds: float
    sample_interval_seconds: float


@dataclass(frozen=True)
class ChildConfig:
    run_id: str
    worker_id: int
    duty_cycle: float
    max_seconds: float


def _positive_number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def validate_config(document: Any, logical_cpus: int | None = None) -> StressConfig:
    if type(document) is not dict or document.keys() != CONFIG_KEYS:
        raise ValueError("Configuration must contain exactly the schema_version=1 keys")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    if not isinstance(document["run_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", document["run_id"]
    ):
        raise ValueError("run_id must be 32 lowercase hexadecimal characters")
    count = os.cpu_count() if logical_cpus is None else logical_cpus
    if type(count) is not int or count < 1:
        raise ValueError("Logical CPU count is unavailable")
    if type(document["workers"]) is not int or not 1 <= document["workers"] <= count:
        raise ValueError(f"workers must be an integer between 1 and {count}")
    duty = _positive_number(document["duty_cycle"], "duty_cycle")
    maximum = _positive_number(document["max_seconds"], "max_seconds")
    interval = _positive_number(document["sample_interval_seconds"], "sample_interval_seconds")
    if duty > 1:
        raise ValueError("duty_cycle must not exceed 1")
    if maximum > MAX_SECONDS:
        raise ValueError(f"max_seconds must not exceed the {MAX_SECONDS:g}-second safety ceiling")
    return StressConfig(1, document["run_id"], document["workers"], duty, maximum, interval)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)


def validate_scope(config_path: str, run_directory: str, run_id: str, paths=os.path) -> None:
    """Lexical checks are separate so Windows path rules can be tested without I/O."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise OwnershipError("Invalid run_id")
    for value in (config_path, run_directory):
        if not isinstance(value, str) or not paths.isabs(value):
            raise OwnershipError("Config and run directory must be absolute paths")
        if paths.sep == "\\" and not paths.splitdrive(value)[0]:
            raise OwnershipError("Drive-relative Windows paths are not absolute")
        if ".." in value.replace("\\", "/").split("/"):
            raise OwnershipError("Parent traversal is not allowed")
    directory = paths.normpath(run_directory)
    config = paths.normpath(config_path)
    if paths.basename(directory) != run_id:
        raise OwnershipError("Run directory leaf must exactly equal run_id")
    if paths.normcase(paths.dirname(config)) != paths.normcase(directory):
        raise OwnershipError("Config must be directly inside its run directory")
    name = paths.basename(config).lower()
    if name in RESERVED_FILES or name.startswith("stress_child_"):
        raise OwnershipError("Config path collides with a worker-owned artifact")


def ensure_plain_path(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise OwnershipError(f"Reparse/link paths are not allowed: {item}")


def load_config(config_path: str, run_directory: str) -> tuple[StressConfig, str]:
    path = Path(config_path)
    directory = Path(run_directory)
    if not path.is_absolute() or not directory.is_absolute():
        raise OwnershipError("Config and run directory must be absolute")
    validate_scope(config_path, run_directory, directory.name)
    ensure_plain_path(path)
    ensure_plain_path(directory)
    if not directory.is_dir() or not path.is_file():
        raise OwnershipError("Run directory or config does not exist")
    raw = path.read_bytes()
    config = validate_config(decode_json(raw))
    validate_scope(config_path, run_directory, config.run_id)
    return config, hashlib.sha256(raw).hexdigest()


def same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def validate_identity(expected: dict, actual: dict) -> None:
    if type(expected.get("pid")) is not int or expected["pid"] <= 0:
        raise OwnershipError("Invalid owned PID")
    stamp = expected.get("create_time")
    if not isinstance(stamp, str) or not re.fullmatch(r"[1-9][0-9]*", stamp):
        raise OwnershipError("Invalid process creation time")
    if (
        actual.get("pid") != expected["pid"]
        or actual.get("create_time") != stamp
        or not isinstance(expected.get("executable"), str)
        or not isinstance(actual.get("executable"), str)
        or not same_path(expected["executable"], actual["executable"])
    ):
        raise OwnershipError(f"PID {expected['pid']} is stale, foreign, or has been reused")


def validate_owner(owner: dict, config: StressConfig, config_path: str,
                   run_directory: str, config_hash: str, probe,
                   controller: dict, parent_pid: int) -> None:
    if type(owner.get("schema_version")) is not int or owner["schema_version"] != 1 or owner.get("run_id") != config.run_id:
        raise OwnershipError("Launcher manifest identity does not match configuration")
    for field, expected in (("config_path", config_path), ("run_directory", run_directory),
                            ("worker_path", str(Path(__file__).resolve()))):
        if not isinstance(owner.get(field), str) or not same_path(owner[field], expected):
            raise OwnershipError(f"Launcher manifest {field} does not match")
    if owner.get("config_sha256") != config_hash:
        raise OwnershipError("Configuration changed after Start")
    if owner.get("workers") != config.workers:
        raise OwnershipError("Launcher worker count does not match")
    launcher = owner.get("launcher", {})
    validate_identity(launcher, probe.identity(launcher.get("pid")))
    if not same_path(launcher["executable"], owner.get("venv_python", "")):
        raise OwnershipError("Launcher is not the scenario venv executable")
    permitted = (owner.get("venv_python", ""), owner.get("base_python", ""))
    if not any(same_path(controller["executable"], path) for path in permitted if path):
        raise OwnershipError("Controller executable is not the validated Python")
    if controller["pid"] == launcher["pid"]:
        validate_identity(launcher, controller)
    elif parent_pid != launcher["pid"] or int(controller["create_time"]) < int(launcher["create_time"]):
        raise OwnershipError("Controller is not the owned launcher or its venv redirector child")


class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
        ("total_pagefile", ctypes.c_uint64), ("avail_pagefile", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
        ("avail_extended_virtual", ctypes.c_uint64),
    ]


class PerformanceInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32)] + [
        (name, ctypes.c_size_t) for name in
        ("commit_total", "commit_limit", "commit_peak", "physical_total",
         "physical_available", "system_cache", "kernel_total", "kernel_paged",
         "kernel_nonpaged", "page_size")
    ] + [(name, ctypes.c_uint32) for name in ("handles", "processes", "threads")]


class JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t), ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", JobBasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class JobAccounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", ctypes.c_uint32), ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32), ("terminated_processes", ctypes.c_uint32),
    ]


def cpu_percent(previous: tuple[int, int, int], current: tuple[int, int, int]) -> float:
    idle, kernel, user = (new - old for old, new in zip(previous, current))
    total = kernel + user  # Windows kernel time includes idle time, across logical CPUs.
    if min(idle, kernel, user) < 0 or total <= 0 or idle > total:
        raise StressError("GetSystemTimes returned unavailable or inconsistent CPU counters")
    return 100.0 * (total - idle) / total


class WindowsProbe:
    """Native calls are initialized only on execution, never at import time."""

    def __init__(self):
        if os.name != "nt":
            raise StressError("Windows ctypes metrics/process APIs are required")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._bind(self.kernel, "GetSystemTimes", [ctypes.POINTER(FileTime)] * 3, ctypes.c_int)
        self._bind(self.kernel, "GlobalMemoryStatusEx", [ctypes.POINTER(MemoryStatus)], ctypes.c_int)
        self._bind(self.psapi, "GetPerformanceInfo",
                   [ctypes.POINTER(PerformanceInfo), ctypes.c_uint32], ctypes.c_int)
        self._bind(self.kernel, "GetActiveProcessorGroupCount", [], ctypes.c_ushort)
        self._bind(self.kernel, "OpenProcess",
                   [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p)
        self._bind(self.kernel, "CloseHandle", [ctypes.c_void_p], ctypes.c_int)
        self._bind(self.kernel, "GetProcessTimes",
                   [ctypes.c_void_p] + [ctypes.POINTER(FileTime)] * 4, ctypes.c_int)
        self._bind(self.kernel, "QueryFullProcessImageNameW",
                   [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                    ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int)
        if self.kernel.GetActiveProcessorGroupCount() != 1:
            raise StressError("Whole-system CPU metrics require exactly one Windows processor group")
        self.previous = None

    @staticmethod
    def _bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result

    @staticmethod
    def require(result, operation: str):
        if not result:
            raise StressError(f"{operation} failed: {ctypes.WinError(ctypes.get_last_error())}")
        return result

    def _times(self):
        idle, kernel, user = FileTime(), FileTime(), FileTime()
        self.require(self.kernel.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)), "GetSystemTimes")
        return idle.ticks(), kernel.ticks(), user.ticks()

    def prime_metrics(self):
        self.previous = self._times()

    def sample(self) -> dict:
        if self.previous is None:
            raise StressError("CPU measurement baseline is missing")
        current = self._times()
        usage = cpu_percent(self.previous, current)
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        self.require(self.kernel.GlobalMemoryStatusEx(ctypes.byref(memory)), "GlobalMemoryStatusEx")
        performance = PerformanceInfo()
        performance.size = ctypes.sizeof(performance)
        self.require(self.psapi.GetPerformanceInfo(
            ctypes.byref(performance), performance.size), "GetPerformanceInfo")
        if memory.total_phys <= 0 or memory.avail_phys > memory.total_phys or performance.page_size <= 0:
            raise StressError("Windows returned inconsistent memory counters")
        self.previous = current
        return {
            "cpu_percent": usage,
            "available_memory_bytes": int(memory.avail_phys),
            "committed_bytes": int(performance.commit_total * performance.page_size),
        }

    def identity(self, process_id: int) -> dict:
        if type(process_id) is not int or process_id <= 0:
            raise OwnershipError("Cannot inspect an invalid PID")
        handle = self.require(self.kernel.OpenProcess(0x1000, False, process_id), "OpenProcess")
        try:
            creation, exit_time, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
            self.require(self.kernel.GetProcessTimes(handle, ctypes.byref(creation),
                         ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)),
                         "GetProcessTimes")
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(buffer))
            self.require(self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)),
                         "QueryFullProcessImageNameW")
            return {"pid": process_id, "create_time": str(creation.ticks()), "executable": buffer.value}
        finally:
            self.kernel.CloseHandle(handle)


class WindowsJob:
    """Own the controller BEFORE spawning; even an abrupt kill stops all descendants."""

    def __init__(self, probe: WindowsProbe):
        self.probe = probe
        self.handle = None
        kernel = probe.kernel
        probe._bind(kernel, "CreateJobObjectW", [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p)
        probe._bind(kernel, "SetInformationJobObject",
                    [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int)
        probe._bind(kernel, "QueryInformationJobObject",
                    [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p],
                    ctypes.c_int)
        probe._bind(kernel, "AssignProcessToJobObject", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
        probe._bind(kernel, "GetCurrentProcess", [], ctypes.c_void_p)
        probe._bind(kernel, "TerminateJobObject", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int)
        handle = probe.require(kernel.CreateJobObjectW(None, None), "CreateJobObjectW")
        try:
            limits = JobExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            probe.require(kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "SetInformationJobObject")
            probe.require(kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()),
                          "AssignProcessToJobObject (required before stress can start)")
            self.handle = handle
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    def finish(self) -> bool:
        accounting = JobAccounting()
        self.probe.require(self.probe.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None),
            "QueryInformationJobObject")
        if accounting.active_processes != 1:
            # Keep the non-inheritable handle until controller exit: the OS kills
            # any remaining descendants. Never disarm a job containing children.
            return False
        limits = JobExtendedLimits()
        self.probe.require(self.probe.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "Disarm empty worker job")
        self.probe.require(self.probe.kernel.CloseHandle(self.handle), "CloseHandle(job)")
        self.handle = None
        return True

    def abort(self):
        # Called only after all controller file contexts are closed. Terminating
        # the owned job also avoids multiprocessing's unbounded atexit joins when
        # an OS-level child cleanup failure has left a process alive.
        self.probe.require(self.probe.kernel.TerminateJobObject(self.handle, 1), "TerminateJobObject")


def atomic_json(path: Path, document: dict) -> None:
    ensure_plain_path(path)
    stage = path.with_name(f"{path.name}.{uuid.uuid4().hex}.new")
    created = False
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            json.dump(document, stream, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(stage, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        if created:
            stage.unlink(missing_ok=True)


class RunFiles:
    def __init__(self, directory: str):
        self.directory = Path(directory)

    def write_state(self, state: dict):
        atomic_json(self.directory / "stress_state.json", state)

    def stop_requested(self) -> bool:
        signal = self.directory / "stress.stop"
        ensure_plain_path(signal)
        if signal.exists() and not signal.is_file():
            raise OwnershipError("The run-scoped stop signal is not a regular file")
        return signal.is_file()

    @contextlib.contextmanager
    def samples(self):
        with (self.directory / "stress_samples.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            def write_sample(sample):
                stream.write(json.dumps(sample, allow_nan=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            yield write_sample

    @contextlib.contextmanager
    def logs(self):
        with (self.directory / "stress_stdout.log").open("x", encoding="utf-8") as output:
            with (self.directory / "stress_stderr.log").open("x", encoding="utf-8") as errors:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    yield


def arithmetic_batch(value: int) -> int:
    for _ in range(256):
        value = ((value ^ (value >> 13)) * 0x5DEECE66D + 0xB) & 0xFFFFFFFFFFFFFFFF
    return value


def run_duty_cycle(config: ChildConfig, stop, *, clock=time.monotonic, compute=arithmetic_batch):
    value = 4000 + config.worker_id
    started = clock()
    deadline = started + config.max_seconds
    while not stop.is_set():
        cycle_start = clock()
        if cycle_start >= deadline:
            raise StressError("Child safety max_seconds expired")
        busy_until = min(cycle_start + CYCLE_SECONDS * config.duty_cycle, deadline)
        while clock() < busy_until and not stop.is_set():
            value = compute(value)
        remaining = min(cycle_start + CYCLE_SECONDS, deadline) - clock()
        if remaining > 0:
            stop.wait(remaining)
    return value


def child_main(config: ChildConfig, run_directory: str, stop, release, ready):
    path = Path(run_directory) / f"stress_child_{config.worker_id}.log"
    try:
        with path.open("x", encoding="utf-8") as stream:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                try:
                    probe = WindowsProbe()
                    identity = probe.identity(os.getpid())
                    ready.send({"kind": "ready", "run_id": config.run_id,
                                "worker_id": config.worker_id, "parent_pid": os.getppid(), **identity})
                    deadline = time.monotonic() + min(STARTUP_SECONDS, config.max_seconds)
                    while not release.is_set():
                        if stop.wait(POLL_SECONDS):
                            return
                        if time.monotonic() >= deadline:
                            raise StressError("Child readiness release timed out")
                    if not stop.is_set():
                        run_duty_cycle(config, stop)
                except BaseException as exc:
                    print(f" ERROR - {type(exc).__name__}: {exc}", file=stream, flush=True)
                    raise
    finally:
        ready.close()


def wait_for_readiness(children, receivers, identities, run_id, controller_pid, deadline,
                       stop_requested, *, clock=time.monotonic, sleep=time.sleep):
    pending = set(range(len(children)))
    while pending:
        if clock() >= deadline:
            raise StressError("Child readiness timed out")
        if stop_requested():
            raise StopRequested("Stop requested during startup")
        for index, process in enumerate(children):
            if not process.is_alive():
                raise StressError(f"Worker {index} exited before readiness")
            if index not in pending or not receivers[index].poll():
                continue
            message = receivers[index].recv()
            if not isinstance(message, dict) or (
                message.get("kind") != "ready" or message.get("run_id") != run_id
                or message.get("worker_id") != index or message.get("parent_pid") != controller_pid
            ):
                raise OwnershipError("Invalid child readiness identity")
            validate_identity(identities[index], message)
            pending.remove(index)
        if pending:
            sleep(min(POLL_SECONDS, max(0, deadline - clock())))
    if any(not process.is_alive() for process in children):
        raise StressError("Worker exited while completing readiness")


def stop_children(children, identities, stop, probe, *, clock=time.monotonic) -> tuple[list[int], list[str]]:
    forced, errors = [], []
    stop.set()
    deadline = clock() + STOP_SECONDS
    for process in children:
        try:
            process.join(max(0, deadline - clock()))
        except Exception as exc:
            errors.append(f"join PID {process.pid}: {exc}")
    for index, process in enumerate(children):
        if not process.is_alive():
            continue
        try:
            validate_identity(identities[index], probe.identity(process.pid))
            process.terminate()  # multiprocessing retains the exact spawned process handle.
            forced.append(process.pid)
        except Exception as exc:
            errors.append(f"stop PID {process.pid}: {exc}")
    deadline = clock() + KILL_SECONDS
    for process in children:
        try:
            process.join(max(0, deadline - clock()))
            if process.is_alive():
                errors.append(f"PID {process.pid} did not exit; owned job remains armed")
            else:
                if process.exitcode != 0:
                    errors.append(f"PID {process.pid} exited with code {process.exitcode}")
                process.close()
        except Exception as exc:
            errors.append(f"final join PID {process.pid}: {exc}")
    return forced, errors


def run_controller(config, run_directory, controller, owner, probe, context, files,
                   *, clock=time.monotonic, sleep=time.sleep) -> tuple[int, dict]:
    """Dependency-injected supervisor. Tests supply fake processes; never run load."""
    started = clock()
    deadline = started + config.max_seconds
    stop, release = context.Event(), context.Event()
    children, receivers, identities = [], [], []
    state = {
        "schema_version": 1, "run_id": config.run_id, "status": "starting",
        "controller_pid": controller["pid"], "controller_create_time": controller["create_time"],
        "controller_executable": controller["executable"],
        "controller_parent_pid": controller["parent_pid"], "children": [], "child_pids": [],
        "error": None, "elapsed_s": 0.0,
    }

    def publish(status=None):
        if status:
            state["status"] = status
        state["elapsed_s"] = max(0.0, clock() - started)
        files.write_state(state)

    try:
        publish()
        with files.samples() as write_sample:
            probe.prime_metrics()
            for index in range(config.workers):
                if clock() >= deadline:
                    raise StressError("Safety max_seconds expired during startup")
                if files.stop_requested():
                    raise StopRequested("Stop requested during startup")
                receiver, sender = context.Pipe(duplex=False)
                receivers.append(receiver)
                child_config = ChildConfig(config.run_id, index, config.duty_cycle, config.max_seconds)
                process = None
                try:
                    process = context.Process(
                        target=child_main, args=(child_config, run_directory, stop, release, sender),
                        name=f"enterprise_ai_{config.run_id}_{index}", daemon=False)
                    process.start()
                finally:
                    if process is not None and process.pid is not None:
                        children.append(process)
                    sender.close()
                identity = probe.identity(process.pid)
                identities.append(identity)
                if not any(same_path(identity["executable"], owner[key])
                           for key in ("venv_python", "base_python")):
                    raise OwnershipError("Spawned executable is not the validated Python")
                record = {"worker_id": index, "parent_pid": controller["pid"], **identity}
                state["children"].append(record)
                state["child_pids"].append(identity["pid"])
                publish()
            wait_for_readiness(children, receivers, identities, config.run_id, controller["pid"],
                               min(deadline, started + STARTUP_SECONDS), files.stop_requested,
                               clock=clock, sleep=sleep)
            write_sample({"elapsed_s": max(0.0, clock() - started), **probe.sample()})
            if clock() >= deadline:
                raise StressError("Safety max_seconds expired before readiness")
            if files.stop_requested():
                raise StopRequested("Stop requested before readiness")
            if any(not process.is_alive() for process in children):
                raise StressError("Worker exited before readiness release")
            release.set()
            publish("ready")
            next_sample = clock() + config.sample_interval_seconds
            while True:
                now = clock()
                if now >= deadline:
                    raise StressError("Safety max_seconds expired; an explicit Stop was required")
                if files.stop_requested():
                    break
                if any(not process.is_alive() for process in children):
                    raise StressError("A stress worker exited unexpectedly")
                if now >= next_sample:
                    write_sample({"elapsed_s": max(0.0, now - started), **probe.sample()})
                    publish()
                    next_sample = clock() + config.sample_interval_seconds
                sleep(min(POLL_SECONDS, max(0, deadline - clock()), max(0, next_sample - clock())))
    except StopRequested:
        pass
    except BaseException as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        forced, errors = stop_children(children, identities, stop, probe, clock=clock)
        for receiver in receivers:
            receiver.close()
        if forced:
            errors.insert(0, f"Forced termination required for owned PIDs {forced}")
        if errors:
            state["error"] = "; ".join(filter(None, [state["error"], *errors]))
        publish("failed" if state["error"] else "stopped")
    return (1 if state["error"] else 0), state


def await_owner(directory: Path, *, clock=time.monotonic, sleep=time.sleep) -> dict:
    path = directory / "stress_owner.json"
    deadline = clock() + OWNER_WAIT_SECONDS
    while clock() < deadline:
        ensure_plain_path(path)
        if path.is_file():
            return decode_json(path.read_bytes())
        sleep(POLL_SECONDS)
    raise OwnershipError("Launcher ownership manifest did not arrive; no workers were started")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-directory", required=True)
    args = parser.parse_args(argv)
    job = None
    try:
        config, digest = load_config(args.config, args.run_directory)
        files = RunFiles(args.run_directory)
        with files.logs():
            owner = await_owner(Path(args.run_directory))
            probe = WindowsProbe()
            controller = {**probe.identity(os.getpid()), "parent_pid": os.getppid()}
            validate_owner(owner, config, args.config, args.run_directory, digest, probe,
                           controller, controller["parent_pid"])
            state = {
                "schema_version": 1, "run_id": config.run_id, "status": "failed",
                "controller_pid": controller["pid"],
                "controller_create_time": controller["create_time"],
                "controller_executable": controller["executable"],
                "controller_parent_pid": controller["parent_pid"],
                "children": [], "child_pids": [], "error": None, "elapsed_s": 0.0,
            }
            result = 1
            try:
                job = WindowsJob(probe)
                result, state = run_controller(
                    config, args.run_directory, controller, owner, probe,
                    mp.get_context("spawn"), files)
            except BaseException as exc:
                state["status"] = "failed"
                state["error"] = f"{type(exc).__name__}: {exc}"
                files.write_state(state)
            finally:
                if job is not None:
                    try:
                        if not job.finish():
                            raise StressError("Owned job still contains descendants; exit will terminate them")
                    except BaseException as exc:
                        result = 1
                        state["status"] = "failed"
                        state["error"] = "; ".join(filter(None, [state["error"], str(exc)]))
                        files.write_state(state)
        return result
    except BaseException as exc:
        print(f" ERROR - {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if job is not None and job.handle is not None:
            job.abort()


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
