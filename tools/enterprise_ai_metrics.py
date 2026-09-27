# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Collect once through HOBL; decode offline without altering legacy UTC tools."""

import json
import logging
from pathlib import Path
import shutil
import subprocess
import re

from core.app_scenario import Scenario
from core.parameters import Params
from scenarios.windows._library.enterprise_ai.configuration import file_hash
from utilities.open_source.enterprise_ai_metrics import (
    MetricsError, build_manifest_map, iter_tip_rows, read_perf_csv, read_trace,
    summarize_pt, write_perf_csv,
)
from utilities.open_source.enterprise_ai_report import (
    build_report, read_stress_samples, write_report,
)


def run_decoder(arguments, log_path, timeout=1800):
    """No shell: paths, corpus names and user-provided output locations stay data."""
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
    Path(log_path).write_text(
        "STDOUT\n" + result.stdout + "\nSTDERR\n" + result.stderr, encoding="utf-8",
    )
    if result.returncode != 0:
        raise MetricsError(f"Decoder failed with exit {result.returncode}; see {log_path}")


class Tool(Scenario):
    module = "enterprise_ai_metrics"
    Params.setDefault(
        module, "trace_validator", "",
        desc="Optional approved absolute .exe path: <validator> <etl> <evidence.json>. No proprietary decoder is bundled.",
    )
    provider = "GTPLight_EnterpriseAI.wprp"

    def preflight(self, scenario):
        self.scenario = scenario
        for name in (
            "PerfParser.exe", "PerfParser.dll", "PerfParser.deps.json", "PerfParser.runtimeconfig.json",
            "StressUtcPerftrack.xml", "DisableAllUploads.json",
        ):
            artifact = Path(scenario.resolve("utilities\\proprietary\\ParseUtc\\" + name))
            if not artifact.is_file():
                raise MetricsError("Required host parser artifact is missing: " + str(artifact))
        self.tracerpt = shutil.which("tracerpt.exe")
        if self.tracerpt is None:
            raise MetricsError("tracerpt.exe is required on the Windows analysis host before running a workload.")
        configured = Params.get(self.module, "trace_validator")
        self.validator = Path(configured) if configured else None
        if self.validator is not None:
            if not self.validator.is_absolute() or not self.validator.is_file() or self.validator.suffix.lower() != ".exe":
                raise MetricsError("trace_validator must be an existing, approved absolute executable path.")
        self._host_checked = True

    def initCallback(self, scenario):
        self.scenario = scenario
        if not getattr(self, "_host_checked", False):
            self.preflight(scenario)
        self._previous_providers = Params.getCalculated("trace_providers")
        providers = self._previous_providers.split()
        if providers and set(providers) != {self.provider}:
            raise MetricsError("enterprise_ai requires its single validated profile set; extra providers need separate validation.")
        Params.setCalculated("trace_providers", self.provider)

    def cleanup(self):
        if hasattr(self, "_previous_providers"):
            Params.setCalculated("trace_providers", self._previous_providers)

    def dataReadyCallback(self):
        root = Path(self.scenario.result_dir)
        quality = None
        try:
            # Core tearDown has already stopped WPR; restoration must precede reporting.
            self.scenario.finish_environment()
            run = json.loads((root / "enterprise_ai_run.json").read_text(encoding="utf-8"))
            if not isinstance(run, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(run.get("run_id", ""))):
                raise MetricsError("The run manifest has an invalid run_id.")
            if run["run_id"] != self.scenario.ai_controller.run_id:
                raise MetricsError("The run manifest does not belong to this scenario instance.")
            raw = root / "enterprise_ai_raw" / run["run_id"]
            raw.mkdir(parents=True, exist_ok=True)
            etl = root / (self.scenario.testname + ".etl")
            if not etl.is_file() or not etl.stat().st_size:
                raise MetricsError("The enterprise_ai ETL is missing or empty.")
            etl_digest = file_hash(etl)
            manifest = Path(self.scenario.resolve(r"utilities\proprietary\ParseUtc\StressUtcPerftrack.xml"))
            if file_hash(manifest) != run.get("utc_manifest_sha256"):
                raise MetricsError("The UTC manifest changed between capture and extraction.")
            parser = self.scenario.resolve(r"utilities\proprietary\ParseUtc\PerfParser.exe")
            raw_perf = raw / "PerfMetrics_raw.csv"
            run_decoder([parser, str(etl), str(manifest), str(raw_perf)], raw / "perf_parser.log")
            mapping = build_manifest_map(manifest)
            rows = read_perf_csv(raw_perf, mapping)
            event_xml = raw / "events.xml"
            run_decoder(
                [self.tracerpt, str(etl), "-of", "XML", "-o", str(event_xml),
                 "-summary", str(raw / "trace_summary.xml"), "-y"],
                raw / "tracerpt.log",
            )
            trace = read_trace(event_xml, run["run_id"])
            # The side channel is a replacement for a missing PT, never an addition
            # to a parser result for the same ID.
            if "10010" not in {row["PT"] for row in rows} and any("10010" in ids for ids in mapping.values()):
                rows.extend(iter_tip_rows(event_xml, run["run_id"]))
            write_perf_csv(raw / "PerfMetrics.csv", rows)
            required = run["config"]["required_pts"] if run["foreground"] else ()
            pts = summarize_pt(rows, required_pts=required, include_p95=True)
            samples = read_stress_samples(raw / "stress_samples.jsonl") if run["stress"] else []
            if run["stress"]:
                state = json.loads((raw / "stress_state.json").read_text(encoding="utf-8-sig"))
                if state.get("run_id") != run["run_id"] or state.get("status") != "stopped":
                    raise MetricsError("Stress did not stop successfully for this run; its samples cannot be accepted.")
            evidence = None
            if self.validator is not None:
                output = raw / "validated_trace_evidence.json"
                run_decoder([str(self.validator), str(etl), str(output)], raw / "trace_validator.log")
                evidence = json.loads(output.read_text(encoding="utf-8-sig"))
            if file_hash(etl) != etl_digest:
                raise MetricsError("The captured ETL changed during extraction.")
            summary, quality = build_report(
                run, trace, pts, stress_samples=samples, evidence=evidence, etl_sha256=etl_digest,
            )
            quality["etl_sha256"] = etl_digest
            if self.validator is not None:
                quality["trace_validator_sha256"] = file_hash(self.validator)
            quality["raw_artifacts"] = str(raw.relative_to(root))
            write_report(root, summary, quality)
            logging.info("========== ENTERPRISE AI METRICS ==========")
            logging.info("scenario_runtime = %s seconds (ETL marker wall-clock interval)", summary["scenario_runtime"])
            logging.info("condition = %s; validation = %s", run["condition"], quality["status"])
            if quality["status"] != "valid":
                logging.warning("enterprise_ai results are NOT benchmark-valid: %s", quality["reasons"])
            if quality["status"] == "invalid" or (
                run["config"]["validation_mode"] == "strict" and quality["status"] != "valid"
            ):
                raise MetricsError("enterprise_ai quality gates failed: " + "; ".join(quality["reasons"]))
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            logging.error("enterprise_ai extraction/validation failed: %s", error)
            if quality is None:
                write_report(
                    root, {"validation_status": "invalid"},
                    {"schema_version": 1, "run_id": self.scenario.ai_controller.run_id,
                     "status": "invalid", "reasons": [str(error)]},
                )
            raise
