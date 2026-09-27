# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""DUT orchestration; importing this module never performs RPC, prep or indexing."""

from dataclasses import asdict
from datetime import datetime, timezone
import base64
import json
import logging
import ntpath
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from .configuration import corpus_manifest, file_hash


LIBRARY = Path(__file__).parent
INDEX_ARTIFACTS = (
    "IndexUpdater.exe", "IndexUpdater.dll",
    "IndexUpdater.deps.json", "IndexUpdater.runtimeconfig.json",
)


def powershell_literal(value):
    if "\0" in str(value):
        raise ValueError("NUL is not permitted in a PowerShell argument.")
    return "'" + str(value).replace("'", "''") + "'"


def invoke_script(scenario, script, timeout=120, **arguments):
    command = "& " + powershell_literal(script)
    for name, value in arguments.items():
        if not name.isascii() or not name.isalpha():
            raise ValueError("Invalid PowerShell parameter name.")
        command += " -" + name + " " + powershell_literal(value)
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return scenario._call(
        ["powershell.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded],
        timeout=timeout, expected_exit_code="0",
    )


class Controller:
    def __init__(self, scenario, config, marker, monotonic=time.monotonic, sleep=time.sleep):
        self.scenario = scenario
        self.config = config
        self.marker = marker
        self.clock = monotonic
        self.sleep = sleep
        self.run_id = uuid.uuid4().hex
        drive = ntpath.splitdrive(scenario.dut_exec_path)[0]
        if len(drive) != 2 or drive[1] != ":":
            raise ValueError("The DUT executable path must have a Windows drive qualifier.")
        self.remote_scripts = ntpath.join(scenario.dut_exec_path, "enterprise_ai")
        self.run_dir = ntpath.join(drive + "\\", "hobl_bin", "enterprise_ai_resources", "runs", self.run_id)
        self.raw_dir = ntpath.join(scenario.dut_data_path, "enterprise_ai_raw", self.run_id)
        self.manifest_path = Path(scenario.result_dir, "enterprise_ai_run.json")
        self.initialized = False
        self.native_prepared = False
        self.utc_attempted = False
        self.stress_attempted = False
        self.measurement_start = None
        self.measurement_ended = False
        self.cleaned = False
        self.manifest = {
            "schema_version": 1, "run_id": self.run_id, "status": "preparing",
            "condition": config.condition, "protocol": config.protocol,
            "foreground": config.foreground, "stress": config.stress, "ai": config.ai,
            "config": asdict(config), "created_utc": datetime.now(timezone.utc).isoformat(),
            "measurement_timebase": "ETL InputInject markers; host clock is diagnostic only",
            "semantic_completion_verified": False, "cleanup_errors": [],
        }

    def save(self):
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        temporary.replace(self.manifest_path)

    def script(self, filename, action, timeout=120, **arguments):
        output = invoke_script(
            self.scenario, ntpath.join(self.remote_scripts, filename), timeout=timeout,
            Action=action, RunDirectory=self.run_dir, RunId=self.run_id, **arguments,
        )
        try:
            result = json.loads(output)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{filename} {action} did not return a valid JSON result: {output!r}") from error
        if not isinstance(result, dict) or result.get("run_id") != self.run_id or result.get("schema_version") != 1:
            raise RuntimeError(f"{filename} {action} returned invalid or foreign run state.")
        return result

    def upload_json(self, name, content):
        with tempfile.TemporaryDirectory(prefix="hobl_enterprise_ai_") as temporary:
            source = Path(temporary, name)
            source.write_text(json.dumps(content, indent=2), encoding="utf-8")
            self.scenario._upload(str(source), self.run_dir, check_modified=False)

    def prepare(self):
        self.save()
        required = ["run_common.ps1", "workspace.ps1", "utc_control.ps1"]
        if self.config.ai:
            required.append("native_indexing.ps1")
        if self.config.stress:
            required.extend(["prep.ps1", "stress_control.ps1", "stress_worker.py"])
        self.scenario._remote_make_dir(self.remote_scripts)
        self.manifest["helper_sha256"] = {}
        for name in required:
            source = LIBRARY / name
            self.manifest["helper_sha256"][name] = file_hash(source)
            self.scenario._upload(str(source), self.remote_scripts, check_modified=False)
        result = self.script("workspace.ps1", "Initialize")
        self.initialized = True
        self.manifest["dut"] = result
        try:
            revision = subprocess.run(
                ["git", "--no-pager", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
                cwd=Path(__file__).resolve().parents[4],
            )
            self.manifest["source_commit"] = revision.stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            logging.warning("enterprise_ai source revision is unavailable: %s", error)
            self.manifest["source_commit"] = "unavailable"
        configuration = asdict(self.config) | {"run_id": self.run_id, "schema_version": 1}
        self.upload_json("config.json", configuration)
        if self.config.ai:
            inventory = corpus_manifest(self.config.corpus_dir)
            self.manifest["corpus"] = inventory
            self.upload_json("corpus_manifest.json", inventory)
            index_dir = self.scenario.resolve(r"utilities\proprietary\IndexUpdater")
            remote_index = ntpath.join(self.run_dir, "IndexUpdater")
            self.scenario._remote_make_dir(remote_index)
            self.manifest["index_updater_sha256"] = {}
            for name in INDEX_ARTIFACTS:
                source = Path(index_dir, name)
                self.manifest["index_updater_sha256"][name] = file_hash(source)
                self.scenario._upload(str(source), remote_index, check_modified=False)
            for entry in inventory["files"]:
                relative = entry["relative_path"]
                source = Path(self.config.corpus_dir, *relative.split("\\"))
                remote = ntpath.join(self.run_dir, "staged", ntpath.dirname(relative))
                self.scenario._remote_make_dir(remote)
                self.scenario._upload(str(source), remote, check_modified=False)
            # Cleanup also covers a partially failed Prepare call.
            self.native_prepared = True
            self.manifest["indexing"] = self.script("native_indexing.ps1", "Prepare")
        if self.config.stress:
            invoke_script(
                self.scenario, ntpath.join(self.remote_scripts, "prep.ps1"), timeout=1800,
                LogFile=ntpath.join(self.run_dir, "prep.log"),
            )
            stress_config = {
                "schema_version": 1, "run_id": self.run_id, "workers": self.config.stress_workers,
                "duty_cycle": self.config.stress_duty_cycle,
                "max_seconds": self.config.measurement_seconds + self.config.settle_seconds + 300,
                "sample_interval_seconds": 1,
            }
            self.upload_json("stress_config.json", stress_config)
        self.utc_attempted = True
        with tempfile.TemporaryDirectory(prefix="hobl_utc_") as temporary:
            manifest = Path(self.scenario.resolve(r"utilities\proprietary\ParseUtc\StressUtcPerftrack.xml"))
            uploads = Path(self.scenario.resolve(r"utilities\proprietary\ParseUtc\DisableAllUploads.json"))
            self.manifest["utc_manifest_sha256"] = file_hash(manifest)
            target = Path(temporary, "UtcPerftrack.xml")
            target.write_bytes(manifest.read_bytes())
            self.scenario._upload(str(target), self.run_dir, check_modified=False)
            self.scenario._upload(str(uploads), self.run_dir, check_modified=False)
        self.manifest["utc"] = self.script(
            "utc_control.ps1", "Prepare", Configure="1" if self.config.configure_utc else "0",
        )
        self.manifest["status"] = "prepared"
        self.save()

    def begin(self):
        if not self.initialized or self.manifest["status"] != "prepared":
            raise RuntimeError("enterprise_ai must finish preparation before measurement.")
        self.sleep(self.config.settle_seconds)
        if self.config.stress:
            self.stress_attempted = True
            result = self.script(
                "stress_control.ps1", "Start", ConfigPath=ntpath.join(self.run_dir, "stress_config.json"),
            )
            if result.get("status") != "ready":
                raise RuntimeError("Stress workers did not report confirmed readiness.")
            self.manifest["stress_start"] = result
        self.marker(f"enterprise_ai:{self.run_id}:measurement_begin")
        self.measurement_start = self.clock()
        self.manifest["status"] = "measuring"
        self.save()
        if self.config.ai:
            result = self.script("native_indexing.ps1", "Start")
            if result.get("status") != "submitted" or result.get("documents_submitted") != self.manifest["corpus"]["file_count"]:
                raise RuntimeError("The complete approved corpus was not submitted to the owned index scope.")
            self.manifest["indexing"] = result
            self.save()

    def finish_window(self):
        if self.measurement_start is None:
            raise RuntimeError("Measurement has not started.")
        deadline = self.measurement_start + self.config.measurement_seconds
        if self.clock() > deadline:
            raise RuntimeError("Foreground actions exceeded measurement_seconds. Increase the matched budget and rerun all cells.")
        while self.clock() < deadline:
            if self.config.stress:
                result = self.script("stress_control.ps1", "Status")
                if result.get("status") != "ready":
                    raise RuntimeError("A stress worker ended before the observation window completed.")
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(5, remaining))
        self.end_measurement()
        self.manifest["status"] = "collected"
        self.save()

    def end_measurement(self):
        if self.measurement_start is not None and not self.measurement_ended:
            self.marker(f"enterprise_ai:{self.run_id}:measurement_end")
            self.manifest["host_observation_seconds"] = self.clock() - self.measurement_start
            self.measurement_ended = True
            self.save()

    def fail(self, error):
        self.manifest["status"] = "failed"
        self.manifest["failure"] = str(error)
        self.save()

    def stop_work(self):
        errors = []
        if self.stress_attempted:
            try:
                result = self.script("stress_control.ps1", "Stop", timeout=120)
                self.manifest["stress_stop"] = result
                if result.get("status") not in ("stopped", "not_started"):
                    raise RuntimeError("Stress cleanup did not finish cleanly.")
                self.stress_attempted = False
            except Exception as error:
                logging.error("enterprise_ai stress cleanup failed: %s", error)
                errors.append(str(error))
        if self.native_prepared:
            try:
                self.manifest["index_stop"] = self.script("native_indexing.ps1", "Stop")
                self.native_prepared = False
            except Exception as error:
                logging.error("enterprise_ai index cleanup failed: %s", error)
                errors.append(str(error))
        self.manifest["cleanup_errors"].extend(errors)
        self.save()
        if errors:
            raise RuntimeError("enterprise_ai workload cleanup failed: " + "; ".join(errors))

    def collect_before_trace_stop(self):
        if self.initialized:
            self.script("workspace.ps1", "Collect", Destination=self.raw_dir)
            self.save()

    def restore_and_collect(self):
        if self.cleaned or not self.initialized:
            return
        errors = []
        if self.utc_attempted:
            try:
                self.manifest["utc_restore"] = self.script("utc_control.ps1", "Restore")
                self.utc_attempted = False
            except Exception as error:
                logging.error("enterprise_ai UTC restoration failed: %s", error)
                errors.append(str(error))
        try:
            self.script("workspace.ps1", "Collect", Destination=self.raw_dir)
            destination = Path(self.scenario.result_dir, "enterprise_ai_raw", self.run_id)
            destination.mkdir(parents=True, exist_ok=True)
            self.scenario._copy_data_from_remote(str(destination), source=self.raw_dir)
        except Exception as error:
            logging.error("enterprise_ai artifact collection failed: %s", error)
            errors.append(str(error))
        if not errors and not self.stress_attempted and not self.native_prepared and not self.manifest["cleanup_errors"]:
            self.script("workspace.ps1", "Release")
            self.cleaned = True
        self.manifest["cleanup_errors"].extend(errors)
        self.save()
        if errors:
            raise RuntimeError("Run recovery required at " + self.run_dir + ": " + "; ".join(errors))
