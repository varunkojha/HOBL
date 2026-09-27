# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root.

"""Offline, standard-library reporting for the enterprise_ai indexing protocol.

Only ETL marker time determines scenario_runtime. The fixed, absolute permitted
window drift is MAX_WINDOW_DRIFT_SECONDS (one second), never a percentage.
Capture mode is always inconclusive unless a known failure makes it invalid.
Strict mode requires externally approved evidence; missing evidence produces an
invalid report with unverified gates, not an exception or an implied success.

Evidence schema version 1:
  schema_version, run_id, etl_sha256, validation_basis="dut-validated",
  validator={"name": <nonempty string>, "version": <nonempty string>},
  trace_loss={"events_lost": <nonnegative int>, "buffers_lost": <nonnegative int>},
  foreground_pt_window_verified=<bool>.
For AI cells it additionally requires:
  semantic_completion={"verified": <bool>, "expected_documents": <int>,
      "completed_documents": <int>, "source": <nonempty string>},
  ai_model=<nonempty string>, ai_device=<nonempty string>.

The caller is responsible for approving/authenticating the external validator.
An arbitrary JSON file is not itself evidence of that approval. This module
checks the supplied contract and binds it to the exact run ID and ETL digest.
It does not execute a validator, inspect private decoders, or infer completed
semantic documents from Search events.
"""

from collections.abc import Mapping
import csv
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import uuid

try:
    from .enterprise_ai_metrics import MetricsError
except ImportError:
    from enterprise_ai_metrics import MetricsError


MAX_WINDOW_DRIFT_SECONDS = 1
SUMMARY_FILENAME = "enterprise_ai_summary.csv"
QUALITY_FILENAME = "enterprise_ai_quality.json"
MEASUREMENT_TIMEBASE = "ETL InputInject measurement_begin/measurement_end marker delta"

_RUN_ID = re.compile(r"[0-9a-fA-F]{32}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_PT_ID = re.compile(r"[0-9]+")
_SCALAR_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CONDITIONS = {
    "A": (True, False, False),
    "B": (True, True, False),
    "C": (False, False, True),
    "D": (True, False, True),
    "E": (True, True, True),
    "F": (False, True, True),
}
_SAMPLE_FIELDS = ("elapsed_s", "cpu_percent", "available_memory_bytes", "committed_bytes")
_LOGGER = logging.getLogger(__name__)


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise MetricsError(f"{name} must be an object")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise MetricsError(f"{name} must be a nonempty string")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise MetricsError(f"{name} must be a boolean")
    return value


def _number(value, name, *, integer=False, positive=False, maximum=None):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        raise MetricsError(f"{name} must be a {'nonnegative integer' if integer else 'finite number'}, not a boolean")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (positive and value == 0) or (maximum is not None and value > maximum):
        raise MetricsError(f"{name} is outside its finite {'positive' if positive else 'nonnegative'} range")
    return value


def _strings(value, name):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise MetricsError(f"{name} must be a list of strings")
    return list(value)


def _run_id(value, name):
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise MetricsError(f"{name} must be a 32-character hexadecimal run ID")
    return value


def _hash(value, name, *, optional=False):
    if optional and value == "":
        return value
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise MetricsError(f"{name} must be a SHA-256 hexadecimal digest")
    return value.lower()


def _pt_id(value, name):
    if not isinstance(value, str) or not _PT_ID.fullmatch(value):
        raise MetricsError(f"{name} must be a numeric PT string")
    return value


def _json_bytes(value, name):
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise MetricsError(f"{name} must contain finite, JSON-serializable values: {exc}") from exc


def _sample(value, name):
    source = _mapping(value, name)
    _json_bytes(source, name)
    result = {}
    for field in _SAMPLE_FIELDS:
        result[field] = _number(
            source.get(field), f"{name}.{field}",
            integer=field.endswith("_bytes"),
            maximum=100 if field == "cpu_percent" else None,
        )
    return result


def read_stress_samples(path):
    """Read the run-owned JSONL sample file without modifying it.

    The caller establishes file/run ownership: these records have no ETL time
    correlation or required per-record run ID. Each row must have elapsed_s,
    cpu_percent (0..100), and nonnegative integer memory-byte fields. Numerical
    types are preserved. Empty files return []; sample-count and monotonically
    increasing clock coverage are report-quality checks in build_report.
    """
    samples = []
    try:
        with open(path, "r", encoding="utf-8-sig") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    raise MetricsError(f"Stress sample line {line_number} is empty")
                record = json.loads(line, object_pairs_hook=_unique_keys)
                _json_bytes(record, f"Stress sample line {line_number}")
                samples.append(_sample(record, f"Stress sample line {line_number}"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetricsError(f"Cannot read stress samples {path}: {exc}") from exc
    return samples


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetricsError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _evidence(value, run_id, etl_sha256, ai):
    if value is None:
        return None
    evidence = _mapping(value, "evidence")
    _json_bytes(evidence, "evidence")
    if _number(evidence.get("schema_version"), "evidence.schema_version", integer=True) != 1:
        raise MetricsError("Unsupported evidence schema_version")
    if _run_id(evidence.get("run_id"), "evidence.run_id") != run_id:
        raise MetricsError("Evidence run_id does not match the run manifest")
    digest = _hash(evidence.get("etl_sha256"), "evidence.etl_sha256")
    if not etl_sha256 or digest != etl_sha256:
        raise MetricsError("Evidence etl_sha256 does not match the supplied ETL digest")
    if evidence.get("validation_basis") != "dut-validated":
        raise MetricsError("Evidence validation_basis must be dut-validated")
    validator = _mapping(evidence.get("validator"), "evidence.validator")
    for key in ("name", "version"):
        _text(validator.get(key), f"evidence.validator.{key}")
    loss = _mapping(evidence.get("trace_loss"), "evidence.trace_loss")
    for key in ("events_lost", "buffers_lost"):
        _number(loss.get(key), f"evidence.trace_loss.{key}", integer=True)
    _boolean(evidence.get("foreground_pt_window_verified"), "evidence.foreground_pt_window_verified")
    completion = evidence.get("semantic_completion")
    if ai or completion is not None:
        completion = _mapping(completion, "evidence.semantic_completion")
        _boolean(completion.get("verified"), "evidence.semantic_completion.verified")
        for key in ("expected_documents", "completed_documents"):
            _number(completion.get(key), f"evidence.semantic_completion.{key}", integer=True)
        _text(completion.get("source"), "evidence.semantic_completion.source")
    if ai:
        for key in ("ai_model", "ai_device"):
            _text(evidence.get(key), f"evidence.{key}")
    return evidence


def _pt_metrics(pt_summary, required_pts, foreground, summary, invalid):
    source = _mapping(pt_summary, "pt_summary")
    _json_bytes(source, "pt_summary")
    if source.get("status") not in ("ok", "error"):
        raise MetricsError("pt_summary.status must be ok or error")
    errors = _strings(source.get("errors", []), "pt_summary.errors")
    declared_missing = {
        _pt_id(value, "pt_summary.missing_required_pts")
        for value in _strings(source.get("missing_required_pts", []), "pt_summary.missing_required_pts")
    }
    entries = _mapping(source.get("pts"), "pt_summary.pts")
    for pt in entries:
        _pt_id(pt, "pt_summary.pts key")
    observed = set()
    for pt, entry in sorted(entries.items()):
        entry = _mapping(entry, f"PT {pt}")
        count = _number(entry.get("count"), f"PT {pt}.count", integer=True)
        status = entry.get("status")
        if status not in ("observed", "missing"):
            raise MetricsError(f"PT {pt}.status must be observed or missing")
        median = entry.get("median_ms")
        if median is not None:
            _number(median, f"PT {pt}.median_ms")
        p95 = entry.get("p95_ms")
        if p95 is not None:
            _number(p95, f"PT {pt}.p95_ms")
        summary[f"PT_{pt}_count"] = count
        if status == "observed" and count > 0 and median is not None:
            observed.add(pt)
            summary[f"PT_{pt}_median_ms"] = median
            if p95 is not None:
                if count < 100:
                    invalid.append(f"PT {pt} p95 requires at least 100 observations")
                else:
                    summary[f"PT_{pt}_p95_ms"] = p95
        elif count > 0 or status == "observed":
            invalid.append(f"PT {pt} lacks a valid observed median/count")
    missing = sorted((set(required_pts) - observed) | declared_missing) if foreground else []
    if missing:
        invalid.append("Missing required foreground PTs: " + ", ".join(missing))
    if foreground and not observed:
        invalid.append("No foreground PT observations are available")
    if foreground and source["status"] == "error":
        invalid.extend("PT summary: " + message for message in errors or ["unspecified PT coverage failure"])
    return missing


def _stress_metrics(manifest, samples, enabled, summary, quality, invalid):
    if isinstance(samples, (str, bytes, Mapping)):
        raise MetricsError("stress_samples must be an iterable of sample objects")
    try:
        samples = [_sample(value, f"Stress sample {index}") for index, value in enumerate(samples, 1)]
    except TypeError as exc:
        raise MetricsError("stress_samples must be an iterable of sample objects") from exc
    if not enabled:
        if samples:
            invalid.append("Stress samples were supplied for a condition with stress disabled")
        return
    quality["stress_timebase"] = "controller-relative monotonic elapsed_s; not correlated to ETL"
    quality["stress_lifetime_sample_count"] = len(samples)
    summary["stress_lifetime_sample_count"] = len(samples)
    for name in ("stress_start", "stress_stop", "stress_state"):
        if name not in manifest:
            continue
        state = _mapping(manifest[name], name)
        status = _text(state.get("status"), f"{name}.status")
        if "schema_version" in state and _number(state["schema_version"], f"{name}.schema_version", integer=True) != 1:
            raise MetricsError(f"Unsupported {name}.schema_version")
        if "run_id" in state and _run_id(state["run_id"], f"{name}.run_id") != manifest["run_id"]:
            raise MetricsError(f"{name}.run_id does not match the manifest")
        error = state.get("error")
        if error is not None and not isinstance(error, str):
            raise MetricsError(f"{name}.error must be a string or null")
        permitted = {"ready"} if name == "stress_start" else {"stopped"} if name == "stress_stop" else {"ready", "stopped"}
        if status not in permitted or error:
            invalid.append(f"{name} failed or was not confirmed healthy: {status}" + (f" ({error})" if error else ""))
    if len(samples) < 2:
        invalid.append("Stress requires at least two controller-lifetime samples")
        return
    if any(right["elapsed_s"] <= left["elapsed_s"] for left, right in zip(samples, samples[1:])):
        invalid.append("Stress controller elapsed_s samples must be strictly increasing")
        return
    cpus = [sample["cpu_percent"] for sample in samples]
    summary.update({
        "stress_lifetime_sample_span_s": samples[-1]["elapsed_s"] - samples[0]["elapsed_s"],
        "stress_lifetime_cpu_percent_mean": math.fsum(cpus) / len(cpus),
        "stress_lifetime_cpu_percent_min": min(cpus),
        "stress_lifetime_cpu_percent_max": max(cpus),
        "stress_lifetime_available_memory_bytes_min": min(sample["available_memory_bytes"] for sample in samples),
        "stress_lifetime_committed_bytes_max": max(sample["committed_bytes"] for sample in samples),
    })


def build_report(run_manifest, trace_dict_from_read_trace, pt_summary_from_summarize_pt,
                 stress_samples=(), evidence=None, etl_sha256=""):
    """Return (scalar_summary_dict, quality_dict), without writing or mutating inputs.

    Malformed types, nonfinite numbers, and malformed/misbound evidence raise
    MetricsError. Missing observations and observed failures instead produce
    explicit invalid quality. Missing validator proof is inconclusive in capture
    mode and invalid in strict mode. Raw PT scope remains whole_trace_unverified
    unless the approved, bound evidence explicitly verifies its window. For
    foreground-disabled cells, raw PT scope is not_applicable and its evidence
    flag need not be true.

    Optional stress_start/stress_stop/stress_state manifest records are checked
    when present. Samples summarize the controller lifetime, not the ETL window
    or individual foreground operations. Source files must belong to this run.
    """
    manifest = _mapping(run_manifest, "run_manifest")
    trace = _mapping(trace_dict_from_read_trace, "trace")
    _json_bytes(manifest, "run_manifest")
    _json_bytes(trace, "trace")
    if _number(manifest.get("schema_version"), "run_manifest.schema_version", integer=True) != 1:
        raise MetricsError("Unsupported run_manifest schema_version")
    run_id = _run_id(manifest.get("run_id"), "run_manifest.run_id")
    condition = _text(manifest.get("condition"), "run_manifest.condition")
    if condition not in _CONDITIONS:
        raise MetricsError("condition must be one of A-F")
    flags = tuple(_boolean(manifest.get(key), f"run_manifest.{key}") for key in ("foreground", "stress", "ai"))
    if flags != _CONDITIONS[condition]:
        raise MetricsError("condition does not match foreground/stress/ai flags")
    foreground, stress, ai = flags
    if manifest.get("protocol") != "indexing":
        raise MetricsError("Only protocol=indexing is supported")
    config = _mapping(manifest.get("config"), "run_manifest.config")
    budget = _number(config.get("measurement_seconds"), "config.measurement_seconds", positive=True)
    mode = config.get("validation_mode")
    if mode not in ("capture", "strict"):
        raise MetricsError("validation_mode must be capture or strict")
    required_pts = [_pt_id(pt, "config.required_pts") for pt in _strings(config.get("required_pts"), "config.required_pts")]
    cleanup_errors = _strings(manifest.get("cleanup_errors"), "run_manifest.cleanup_errors")
    dut = _mapping(manifest.get("dut", {}), "run_manifest.dut")
    architecture = _text(dut["architecture"], "dut.architecture") if "architecture" in dut else "unverified"
    if "semantic_completion_verified" in manifest:
        if _boolean(manifest["semantic_completion_verified"], "semantic_completion_verified"):
            raise MetricsError("Run manifest semantic_completion_verified must remain false; use approved evidence")
    etl_sha256 = _hash(etl_sha256, "etl_sha256", optional=True)
    approved = _evidence(evidence, run_id, etl_sha256, ai)
    invalid, unverified = [], []
    status = _text(manifest.get("status"), "run_manifest.status")
    if status != "collected":
        invalid.append(f"Run lifecycle did not finish collected: {status}")
    if manifest.get("failure"):
        invalid.append("Run failure: " + _text(manifest["failure"], "run_manifest.failure"))
    invalid.extend("Cleanup failure: " + message for message in cleanup_errors)
    summary = {
        "architecture": architecture,
        "condition": condition,
        "ai_protocol": "indexing",
        "ai_model": "unverified",
        "ai_device": "unverified",
    }
    quality = {
        "schema_version": 1,
        "run_id": run_id,
        "condition": condition,
        "validation_mode": mode,
        "measurement_timebase": MEASUREMENT_TIMEBASE,
        "raw_pt_scope": "whole_trace_unverified" if foreground else "not_applicable",
        "max_window_drift_seconds": MAX_WINDOW_DRIFT_SECONDS,
        "configured_measurement_seconds": budget,
        "etl_sha256": etl_sha256,
        "validator": dict(approved["validator"]) if approved else None,
        "validation_basis": "dut-validated" if approved else "unverified",
    }
    if "host_observation_seconds" in manifest:
        quality["diagnostics"] = {
            "host_observation_seconds": _number(manifest["host_observation_seconds"], "host_observation_seconds")
        }
    if "run_id" not in trace:
        invalid.append("Trace run ID is unavailable")
    elif _run_id(trace["run_id"], "trace.run_id") != run_id:
        raise MetricsError("Trace run_id does not match the run manifest")
    if trace.get("source_validation") != "passed":
        invalid.append("Trace source/marker validation is unavailable or failed")
    markers = _mapping(trace.get("markers", {}), "trace.markers")
    for phase in ("measurement_begin", "measurement_end"):
        marker = _mapping(markers.get(phase, {}), f"trace.markers.{phase}")
        if marker.get("timestamp") is not None:
            _text(marker["timestamp"], f"trace.markers.{phase}.timestamp")
        if marker.get("value") != f"enterprise_ai:{run_id}:{phase}" or not marker.get("timestamp"):
            invalid.append(f"Trace lacks the exact {phase} marker for this run")
    runtime = trace.get("scenario_runtime")
    if runtime is None:
        invalid.append("ETL marker scenario_runtime is unavailable")
    else:
        _number(runtime, "trace.scenario_runtime")
        summary["scenario_runtime"] = runtime
        quality["observed_measurement_seconds"] = runtime
        drift = abs(runtime - budget)
        quality["window_drift_seconds"] = drift
        if runtime == 0 or drift > MAX_WINDOW_DRIFT_SECONDS:
            invalid.append(f"ETL measurement window differs from its budget by {drift} seconds (maximum 1 second)")
    loss_status = "unknown"
    loss = _mapping(trace.get("trace_loss", {}), "trace.trace_loss")
    for key in ("events_lost", "buffers_lost"):
        if loss.get(key) is not None and _number(loss[key], f"trace.trace_loss.{key}", integer=True) > 0:
            loss_status = "loss_detected"
            invalid.append(f"Trace reports nonzero {key}: {loss[key]}")
    if approved:
        for key in ("events_lost", "buffers_lost"):
            if approved["trace_loss"][key] > 0:
                loss_status = "loss_detected"
                invalid.append(f"Validator reports nonzero {key}: {approved['trace_loss'][key]}")
        if loss_status != "loss_detected":
            loss_status = "verified_zero"
        if foreground and approved["foreground_pt_window_verified"]:
            quality["raw_pt_scope"] = "measurement_window_verified"
    if loss_status == "unknown":
        unverified.append("Trace loss is unknown: event XML is not a supported trace-loss validation contract")
    if quality["raw_pt_scope"] == "whole_trace_unverified":
        unverified.append("PT window attribution is unverified: PerfParser raw durations have no timestamps")
    quality["missing_required_pts"] = _pt_metrics(
        pt_summary_from_summarize_pt, required_pts, foreground, summary, invalid,
    )
    if foreground and mode == "strict" and not required_pts:
        invalid.append("Strict foreground validation requires an explicit required_pts list")
    search = _mapping(trace.get("search", {}), "trace.search")
    search_counts = _mapping(search.get("event_counts", {}), "trace.search.event_counts")
    for event_name, key in (
        ("IndexItemInit", "observed_index_item_init_count"),
        ("IndexItemDataComplete", "observed_index_item_data_complete_count"),
    ):
        if event_name in search_counts:
            summary[key] = _number(search_counts[event_name], f"trace.search.{event_name}", integer=True)
    if search.get("envelope_s") is not None:
        summary["observed_index_envelope_s"] = _number(search["envelope_s"], "trace.search.envelope_s")
    completion_status = "not_applicable"
    if ai:
        corpus = _mapping(manifest.get("corpus", {}), "run_manifest.corpus")
        indexing = _mapping(manifest.get("indexing", {}), "run_manifest.indexing")
        if "semantic_completion_verified" in indexing:
            if _boolean(indexing["semantic_completion_verified"], "indexing.semantic_completion_verified"):
                raise MetricsError("Indexing observations cannot assert semantic completion; use approved evidence")
        if "status" in indexing and _text(indexing["status"], "indexing.status") != "submitted":
            invalid.append("Indexing did not confirm corpus submission: " + indexing["status"])
        count = corpus.get("file_count")
        submitted = indexing.get("documents_submitted")
        if count is not None:
            _number(count, "corpus.file_count", integer=True)
        if submitted is not None:
            summary["documents_submitted"] = _number(submitted, "indexing.documents_submitted", integer=True)
        if count is None or count == 0:
            invalid.append("A nonempty approved corpus file_count is unavailable")
        if submitted is None or submitted != count:
            invalid.append("Submitted document count is missing or differs from the actual corpus file_count")
        if search.get("status") != "observed" or any(search_counts.get(name, 0) <= 0 for name in ("IndexItemInit", "IndexItemDataComplete")):
            invalid.append("WindowsSearchCore indexing events were not fully observed inside the ETL marker window")
        completion_status = "unverified"
        if approved:
            completion = approved["semantic_completion"]
            if completion["expected_documents"] != count:
                invalid.append("Validator expected_documents does not match the actual corpus file_count")
            elif completion["completed_documents"] != count:
                invalid.append("Validator completed_documents does not match the expected corpus count")
            elif completion["verified"]:
                completion_status = "verified"
                summary["ai_model"] = approved["ai_model"]
                summary["ai_device"] = approved["ai_device"]
        if completion_status != "verified":
            unverified.append("Semantic completion is unverified: raw Search event counts are not completed semantic documents")
    _stress_metrics(manifest, stress_samples, stress, summary, quality, invalid)
    reasons = invalid + unverified
    if mode == "capture":
        reasons.append("Capture mode collects evidence only and cannot declare a valid run")
    quality_status = "invalid" if invalid or (mode == "strict" and unverified) else "inconclusive" if mode == "capture" else "valid"
    quality.update({
        "status": quality_status,
        "reasons": list(dict.fromkeys(reasons)),
        "trace_loss_status": loss_status,
        "semantic_completion_status": completion_status,
    })
    summary.update({
        "validation_status": quality_status,
        "trace_loss_status": loss_status,
        "semantic_completion_status": completion_status,
    })
    _json_bytes(summary, "summary")
    _json_bytes(quality, "quality")
    return summary, quality


def write_report(directory, summary, quality):
    """Atomically replace the two report files in an existing, run-owned directory.

    Both complete payloads are staged in that directory before publication.
    Quality is replaced last and includes summary_sha256 as a commit record:
    readers should verify that hash to detect interruption between the two
    individually atomic replacements. Filesystems do not offer a portable
    atomic transaction over two filenames. Staging files use .pending, never
    .csv, so scalar CSV wildcards cannot consume partial or raw operation data.
    """
    summary = _mapping(summary, "summary")
    quality = _mapping(quality, "quality")
    if _number(quality.get("schema_version"), "quality.schema_version", integer=True) != 1:
        raise MetricsError("Unsupported quality schema_version")
    _run_id(quality.get("run_id"), "quality.run_id")
    if quality.get("status") not in ("valid", "invalid", "inconclusive"):
        raise MetricsError("Invalid quality.status")
    if summary.get("validation_status") != quality["status"]:
        raise MetricsError("Summary validation_status and quality status disagree")
    _strings(quality.get("reasons"), "quality.reasons")
    for key in summary:
        if not isinstance(key, str) or not _SCALAR_KEY.fullmatch(key):
            raise MetricsError("Summary keys must be nonempty scalar metric identifiers")
    csv_text = io.StringIO(newline="")
    writer = csv.writer(csv_text)
    for key, value in sorted(summary.items()):
        if type(value) in (int, float):
            _number(value, f"summary.{key}")
        elif not isinstance(value, str):
            raise MetricsError(f"summary.{key} must be a string or finite number, not a boolean/container/null")
        writer.writerow((key, value))
    try:
        summary_bytes = csv_text.getvalue().encode("utf-8")
    except UnicodeError as exc:
        raise MetricsError("Summary contains invalid Unicode") from exc
    published_quality = dict(quality)
    published_quality["summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    quality_bytes = _json_bytes(published_quality, "quality")
    try:
        directory = Path(directory)
    except TypeError as exc:
        raise MetricsError("Report directory must be a filesystem path") from exc
    if not directory.is_dir():
        raise MetricsError(f"Report directory does not exist: {directory}")
    token = uuid.uuid4().hex
    destinations = (directory / SUMMARY_FILENAME, directory / QUALITY_FILENAME)
    stages = tuple(directory / f".{path.name}.{token}.pending" for path in destinations)
    owned_stages = []
    try:
        for stage, payload in zip(stages, (summary_bytes, quality_bytes)):
            with open(stage, "xb") as stream:
                owned_stages.append(stage)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        for stage, destination in zip(stages, destinations):
            os.replace(stage, destination)
            owned_stages.remove(stage)
    except OSError as exc:
        raise MetricsError(f"Cannot publish enterprise_ai report: {exc}") from exc
    finally:
        for stage in owned_stages:
            try:
                stage.unlink(missing_ok=True)
            except OSError as exc:
                _LOGGER.warning("Could not remove report staging file %s: %s", stage, exc)
