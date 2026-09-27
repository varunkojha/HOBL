# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root.

"""Synthetic offline reporting tests: all filesystem effects are mocked.

Validator evidence here is fabricated test data, not real DUT validation.
No subprocess, capture, stress worker, prep, or host/DUT operation is invoked.
"""

from contextlib import ExitStack
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from utilities.open_source import enterprise_ai_metrics as metrics
from utilities.open_source import enterprise_ai_report as report


SYNTHETIC_RUN_ID = "a1" * 16
SYNTHETIC_ETL_SHA256 = "b2" * 32
SYNTHETIC_CONDITIONS = {
    "A": (True, False, False), "B": (True, True, False),
    "C": (False, False, True), "D": (True, False, True),
    "E": (True, True, True), "F": (False, True, True),
}
SYNTHETIC_SAMPLES = [
    {"elapsed_s": 0, "cpu_percent": 20, "available_memory_bytes": 4096, "committed_bytes": 8192},
    {"elapsed_s": 2.5, "cpu_percent": 40.5, "available_memory_bytes": 2048, "committed_bytes": 16384},
]


def synthetic_manifest(condition="D", mode="capture"):
    foreground, stress, ai = SYNTHETIC_CONDITIONS[condition]
    result = {
        "schema_version": 1, "run_id": SYNTHETIC_RUN_ID, "status": "collected",
        "condition": condition, "protocol": "indexing",
        "foreground": foreground, "stress": stress, "ai": ai,
        "config": {"measurement_seconds": 30, "validation_mode": mode, "required_pts": ["0007"]},
        "semantic_completion_verified": False,
        "host_observation_seconds": 999.5,
        "cleanup_errors": [], "dut": {"architecture": "AMD64"},
    }
    if ai:
        result["corpus"] = {"file_count": 2}
        result["indexing"] = {"status": "submitted", "documents_submitted": 2, "semantic_completion_verified": False}
    if stress:
        result["stress_start"] = {"schema_version": 1, "run_id": SYNTHETIC_RUN_ID, "status": "ready"}
        result["stress_stop"] = {"schema_version": 1, "run_id": SYNTHETIC_RUN_ID, "status": "stopped"}
    return result


def synthetic_input(text):
    payload = text.encode("utf-8")

    def open_synthetic(path, mode="r", **kwargs):
        if not str(path).startswith("synthetic_") or mode not in ("r", "rb"):
            raise AssertionError("Unexpected synthetic fixture IO")
        stream = io.BytesIO(payload)
        if mode == "rb":
            return stream
        return io.TextIOWrapper(stream, encoding=kwargs.get("encoding", "utf-8"), newline=kwargs.get("newline"))

    return mock.patch("builtins.open", side_effect=open_synthetic)


def synthetic_trace():
    def event(guid, second, name="", marker=""):
        return (
            f'<Event><System><Provider Guid="{guid}"/><EventID>1</EventID>'
            f'<TimeCreated SystemTime="2026-09-27T10:00:{second:02d}.0000000Z"/>'
            + (f"<EventName>{name}</EventName>" if name else "")
            + f"</System><EventData><Data Name=\"Marker\">{marker}</Data></EventData></Event>"
        )

    xml = (
        '<Events xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
        + event(metrics.INPUT_INJECT_GUID, 0, marker=f"enterprise_ai:{SYNTHETIC_RUN_ID}:measurement_begin")
        + event(metrics.WINDOWS_SEARCH_CORE_GUID, 5, name="IndexItemInit")
        + event(metrics.WINDOWS_SEARCH_CORE_GUID, 25, name="IndexItemDataComplete")
        + event(metrics.INPUT_INJECT_GUID, 30, marker=f"enterprise_ai:{SYNTHETIC_RUN_ID}:measurement_end")
        + "</Events>"
    )
    with synthetic_input(xml):
        return metrics.read_trace("synthetic_trace.xml", SYNTHETIC_RUN_ID)


def synthetic_pt_summary(foreground=True, count=2):
    rows = [
        {"PT": "0007", "Metric": "Synthetic Menu", "Duration": str(10 + index)}
        for index in range(count)
    ] if foreground else []
    return metrics.summarize_pt(rows, required_pts=("0007",) if foreground else (), include_p95=True)


def synthetic_evidence():
    return {
        "schema_version": 1,
        "run_id": SYNTHETIC_RUN_ID,
        "etl_sha256": SYNTHETIC_ETL_SHA256,
        "validation_basis": "dut-validated",
        "validator": {"name": "Synthetic Approved Validator", "version": "0.0-synthetic"},
        "trace_loss": {"events_lost": 0, "buffers_lost": 0},
        "foreground_pt_window_verified": True,
        "semantic_completion": {
            "verified": True, "expected_documents": 2, "completed_documents": 2,
            "source": "Synthetic independently validated completion contract",
        },
        "ai_model": "Synthetic model", "ai_device": "Synthetic device",
    }


def synthetic_report(manifest=None, trace=None, pts=None, **kwargs):
    manifest = synthetic_manifest() if manifest is None else manifest
    trace = synthetic_trace() if trace is None else trace
    pts = synthetic_pt_summary(manifest["foreground"]) if pts is None else pts
    return report.build_report(manifest, trace, pts, **kwargs)


def synthetic_set(mapping, path, value):
    target = mapping
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


class ReportTests(unittest.TestCase):
    def test_synthetic_capture_uses_etl_time_never_host_diagnostic_or_completion_counts(self):
        summary, quality = synthetic_report()
        self.assertEqual(summary["scenario_runtime"], 30.0)
        self.assertEqual(quality["diagnostics"]["host_observation_seconds"], 999.5)
        self.assertEqual(summary["validation_status"], "inconclusive")
        self.assertEqual(quality["status"], "inconclusive")
        self.assertEqual(quality["raw_pt_scope"], "whole_trace_unverified")
        self.assertEqual(quality["trace_loss_status"], "unknown")
        self.assertEqual(quality["semantic_completion_status"], "unverified")
        self.assertEqual(summary["ai_model"], "unverified")
        self.assertEqual(summary["ai_device"], "unverified")
        self.assertEqual(summary["documents_submitted"], 2)
        self.assertEqual(summary["observed_index_item_init_count"], 1)
        self.assertEqual(summary["observed_index_item_data_complete_count"], 1)
        self.assertNotIn("documents_indexed", summary)
        self.assertNotIn("documents_completed", summary)
        self.assertFalse(any("token" in key for key in summary))
        self.assertEqual(quality["measurement_timebase"], report.MEASUREMENT_TIMEBASE)
        self.assertTrue(any("Trace loss" in reason for reason in quality["reasons"]))
        self.assertTrue(any("Semantic completion" in reason for reason in quality["reasons"]))
        self.assertTrue(any("PT window" in reason for reason in quality["reasons"]))

    def test_synthetic_strict_is_valid_only_with_complete_bound_evidence(self):
        summary, quality = synthetic_report(
            synthetic_manifest(mode="strict"), evidence=synthetic_evidence(), etl_sha256=SYNTHETIC_ETL_SHA256,
        )
        self.assertEqual(quality["status"], "valid")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(quality["raw_pt_scope"], "measurement_window_verified")
        self.assertEqual(summary["trace_loss_status"], "verified_zero")
        self.assertEqual(summary["semantic_completion_status"], "verified")
        self.assertEqual(summary["ai_model"], "Synthetic model")
        self.assertEqual(summary["ai_device"], "Synthetic device")

    def test_synthetic_strict_without_evidence_returns_invalid_not_success_or_exception(self):
        summary, quality = synthetic_report(synthetic_manifest(mode="strict"))
        self.assertEqual(quality["status"], "invalid")
        self.assertEqual(summary["trace_loss_status"], "unknown")
        self.assertEqual(summary["semantic_completion_status"], "unverified")
        self.assertTrue(quality["reasons"])

    def test_synthetic_capture_never_valid_even_with_complete_evidence(self):
        _, quality = synthetic_report(evidence=synthetic_evidence(), etl_sha256=SYNTHETIC_ETL_SHA256)
        self.assertEqual(quality["status"], "inconclusive")
        self.assertTrue(any("Capture mode" in reason for reason in quality["reasons"]))

    def test_synthetic_all_six_conditions_use_real_parser_and_pt_summary_shapes(self):
        for condition, (foreground, stress, ai) in SYNTHETIC_CONDITIONS.items():
            with self.subTest(condition=condition):
                summary, quality = synthetic_report(
                    synthetic_manifest(condition, "strict"),
                    stress_samples=SYNTHETIC_SAMPLES if stress else (),
                    evidence=synthetic_evidence(), etl_sha256=SYNTHETIC_ETL_SHA256,
                )
                self.assertEqual(quality["status"], "valid", quality["reasons"])
                self.assertEqual(summary["condition"], condition)
                self.assertEqual(summary["semantic_completion_status"], "verified" if ai else "not_applicable")
                self.assertEqual("PT_0007_count" in summary, foreground)

    def test_synthetic_strict_ai_only_does_not_require_foreground_pt_attribution(self):
        evidence = synthetic_evidence()
        evidence["foreground_pt_window_verified"] = False
        for condition in ("C", "F"):
            with self.subTest(condition=condition):
                _, quality = synthetic_report(
                    synthetic_manifest(condition, "strict"),
                    stress_samples=SYNTHETIC_SAMPLES if condition == "F" else (),
                    evidence=evidence, etl_sha256=SYNTHETIC_ETL_SHA256,
                )
                self.assertEqual(quality["status"], "valid")
                self.assertEqual(quality["raw_pt_scope"], "not_applicable")
                self.assertFalse(any("PT window" in reason for reason in quality["reasons"]))

    def test_synthetic_bad_evidence_bindings_and_schema_raise(self):
        for path, value in (
            (("etl_sha256",), "f" * 64),
            (("etl_sha256",), "not a digest"),
            (("run_id",), "f" * 32),
            (("run_id",), "not a run ID"),
            (("schema_version",), True),
            (("schema_version",), 1.0),
            (("schema_version",), 2),
            (("validation_basis",), "unvalidated"),
            (("validator", "name"), ""),
            (("validator", "version"), ""),
            (("trace_loss", "events_lost"), True),
            (("trace_loss", "buffers_lost"), 0.0),
            (("trace_loss", "events_lost"), -1),
            (("trace_loss", "buffers_lost"), float("nan")),
            (("foreground_pt_window_verified",), 1),
            (("semantic_completion", "verified"), "true"),
            (("semantic_completion", "expected_documents"), True),
            (("semantic_completion", "completed_documents"), float("inf")),
            (("semantic_completion", "source"), ""),
            (("ai_model",), ""),
            (("ai_device",), None),
        ):
            evidence = synthetic_evidence()
            synthetic_set(evidence, path, value)
            with self.subTest(path=path, value=value), self.assertRaises(metrics.MetricsError):
                synthetic_report(evidence=evidence, etl_sha256=SYNTHETIC_ETL_SHA256)
        with self.assertRaisesRegex(metrics.MetricsError, "etl_sha256"):
            synthetic_report(evidence=synthetic_evidence())

    def test_synthetic_incomplete_or_negative_validator_results_are_invalid(self):
        for path, value, expected in (
            (("trace_loss", "events_lost"), 1, "nonzero"),
            (("trace_loss", "buffers_lost"), 2, "nonzero"),
            (("foreground_pt_window_verified",), False, "PT window"),
            (("semantic_completion", "verified"), False, "Semantic completion"),
            (("semantic_completion", "expected_documents"), 3, "expected_documents"),
            (("semantic_completion", "completed_documents"), 1, "completed_documents"),
        ):
            evidence = synthetic_evidence()
            synthetic_set(evidence, path, value)
            with self.subTest(path=path):
                summary, quality = synthetic_report(
                    synthetic_manifest(mode="strict"), evidence=evidence, etl_sha256=SYNTHETIC_ETL_SHA256,
                )
                self.assertEqual(quality["status"], "invalid")
                self.assertTrue(any(expected in reason for reason in quality["reasons"]))
                if path[0] == "semantic_completion":
                    self.assertEqual(summary["ai_model"], "unverified")

    def test_synthetic_nonzero_trace_loss_cannot_be_overridden_by_zero_evidence(self):
        trace = synthetic_trace()
        trace["trace_loss"]["events_lost"] = 3
        summary, quality = synthetic_report(
            synthetic_manifest(mode="strict"), trace,
            evidence=synthetic_evidence(), etl_sha256=SYNTHETIC_ETL_SHA256,
        )
        self.assertEqual(quality["status"], "invalid")
        self.assertEqual(summary["trace_loss_status"], "loss_detected")

    def test_synthetic_unapproved_zero_loss_or_manifest_completion_never_verifies_run(self):
        trace = synthetic_trace()
        trace["trace_loss"] = {"status": "ok", "events_lost": 0, "buffers_lost": 0}
        summary, _ = synthetic_report(trace=trace)
        self.assertEqual(summary["trace_loss_status"], "unknown")
        manifest = synthetic_manifest()
        manifest["semantic_completion_verified"] = True
        with self.assertRaisesRegex(metrics.MetricsError, "approved evidence"):
            synthetic_report(manifest)

    def test_synthetic_window_budget_is_fixed_absolute_one_second(self):
        for runtime, expected in ((29, "valid"), (31, "valid"), (28.999, "invalid"), (31.001, "invalid")):
            manifest = synthetic_manifest(mode="strict")
            manifest["config"]["max_window_drift_seconds"] = 999
            trace = synthetic_trace()
            trace["scenario_runtime"] = runtime
            with self.subTest(runtime=runtime):
                summary, quality = synthetic_report(
                    manifest, trace, evidence=synthetic_evidence(), etl_sha256=SYNTHETIC_ETL_SHA256,
                )
                self.assertEqual(quality["status"], expected)
                self.assertEqual(quality["max_window_drift_seconds"], 1)
                self.assertEqual(summary["scenario_runtime"], runtime)
                self.assertIs(type(summary["scenario_runtime"]), type(runtime))

    def test_synthetic_missing_runtime_and_marker_coverage_returns_invalid_without_zero_fill(self):
        trace = synthetic_trace()
        del trace["scenario_runtime"]
        del trace["markers"]["measurement_end"]
        summary, quality = synthetic_report(trace=trace)
        self.assertEqual(quality["status"], "invalid")
        self.assertNotIn("scenario_runtime", summary)
        self.assertTrue(any("measurement_end" in reason for reason in quality["reasons"]))
        self.assertTrue(any("scenario_runtime" in reason for reason in quality["reasons"]))

    def test_synthetic_lifecycle_and_cleanup_failure_are_invalid_in_capture(self):
        for field, value in (("status", "failed"), ("failure", "Synthetic failure"), ("cleanup_errors", ["Synthetic cleanup failure"])):
            manifest = synthetic_manifest()
            manifest[field] = value
            with self.subTest(field=field):
                _, quality = synthetic_report(manifest)
                self.assertEqual(quality["status"], "invalid")

    def test_synthetic_document_submission_mismatch_and_missing_search_are_invalid(self):
        manifest = synthetic_manifest()
        manifest["indexing"]["documents_submitted"] = 1
        summary, quality = synthetic_report(manifest)
        self.assertEqual(summary["documents_submitted"], 1)
        self.assertEqual(quality["status"], "invalid")
        self.assertTrue(any("Submitted document count" in reason for reason in quality["reasons"]))
        trace = synthetic_trace()
        trace["search"]["status"] = "event_names_unavailable"
        trace["search"]["event_counts"] = {}
        summary, quality = synthetic_report(trace=trace)
        self.assertEqual(quality["status"], "invalid")
        self.assertNotIn("observed_index_item_init_count", summary)

    def test_synthetic_failed_indexing_state_is_invalid_even_with_matching_submitted_count(self):
        manifest = synthetic_manifest()
        manifest["indexing"]["status"] = "failed"
        _, quality = synthetic_report(manifest)
        self.assertEqual(quality["status"], "invalid")
        self.assertTrue(any("Indexing did not confirm" in reason for reason in quality["reasons"]))

    def test_synthetic_missing_corpus_and_submission_are_coverage_failures_not_exceptions(self):
        manifest = synthetic_manifest()
        del manifest["corpus"]
        del manifest["indexing"]
        summary, quality = synthetic_report(manifest)
        self.assertEqual(quality["status"], "invalid")
        self.assertNotIn("documents_submitted", summary)

    def test_synthetic_missing_required_pts_preserve_ids_and_omit_missing_medians(self):
        pts = metrics.summarize_pt([], required_pts=("0007",))
        summary, quality = synthetic_report(pts=pts)
        self.assertEqual(quality["status"], "invalid")
        self.assertEqual(quality["missing_required_pts"], ["0007"])
        self.assertEqual(summary["PT_0007_count"], 0)
        self.assertNotIn("PT_0007_median_ms", summary)
        self.assertNotIn("PT_0007_p95_ms", summary)
        pts["pts"]["0007"]["median_ms"] = 0
        summary, _ = synthetic_report(pts=pts)
        self.assertNotIn("PT_0007_median_ms", summary)

    def test_synthetic_pt_medians_and_p95_require_real_observations(self):
        summary, _ = synthetic_report()
        self.assertEqual(summary["PT_0007_count"], 2)
        self.assertEqual(summary["PT_0007_median_ms"], 10.5)
        self.assertNotIn("PT_0007_p95_ms", summary)
        summary, _ = synthetic_report(pts=synthetic_pt_summary(count=100))
        self.assertEqual(summary["PT_0007_p95_ms"], 104.0)
        pts = synthetic_pt_summary()
        pts["pts"]["0007"]["p95_ms"] = 12
        summary, quality = synthetic_report(pts=pts)
        self.assertEqual(quality["status"], "invalid")
        self.assertNotIn("PT_0007_p95_ms", summary)
        pts["pts"]["0007"]["median_ms"] = None
        summary, quality = synthetic_report(pts=pts)
        self.assertEqual(quality["status"], "invalid")
        self.assertNotIn("PT_0007_median_ms", summary)

    def test_synthetic_malformed_numbers_types_and_identifiers_raise(self):
        for path, value in (
            (("schema_version",), True), (("run_id",), "synthetic-run"),
            (("foreground",), 1), (("condition",), "Z"), (("protocol",), "model"),
            (("config", "measurement_seconds"), True), (("config", "measurement_seconds"), 0),
            (("config", "measurement_seconds"), float("inf")),
            (("host_observation_seconds",), float("nan")),
            (("corpus", "file_count"), True),
            (("indexing", "documents_submitted"), 2.0),
            (("config", "required_pts"), ["PT_7"]),
        ):
            manifest = synthetic_manifest()
            synthetic_set(manifest, path, value)
            with self.subTest(path=path, value=value), self.assertRaises(metrics.MetricsError):
                synthetic_report(manifest)
        for path, value in (
            (("scenario_runtime",), True),
            (("scenario_runtime",), float("nan")),
            (("run_id",), "c3" * 16),
            (("search", "event_counts", "IndexItemInit"), True),
        ):
            trace = synthetic_trace()
            synthetic_set(trace, path, value)
            with self.subTest(path=path), self.assertRaises(metrics.MetricsError):
                synthetic_report(trace=trace)
        pts = synthetic_pt_summary()
        pts["pts"]["0007"]["median_ms"] = float("nan")
        with self.assertRaises(metrics.MetricsError):
            synthetic_report(pts=pts)
        pts = synthetic_pt_summary()
        pts["pts"][8] = dict(pts["pts"]["0007"])
        with self.assertRaises(metrics.MetricsError):
            synthetic_report(pts=pts)

    def test_synthetic_input_objects_are_not_mutated_and_optional_architecture_is_unverified(self):
        manifest, trace, pts, evidence = synthetic_manifest(), synthetic_trace(), synthetic_pt_summary(), synthetic_evidence()
        del manifest["dut"]["architecture"]
        originals = copy.deepcopy((manifest, trace, pts, evidence))
        summary, _ = synthetic_report(manifest, trace, pts, evidence=evidence, etl_sha256=SYNTHETIC_ETL_SHA256)
        self.assertEqual(summary["architecture"], "unverified")
        self.assertEqual((manifest, trace, pts, evidence), originals)


class StressReportTests(unittest.TestCase):
    def test_synthetic_stress_metrics_are_explicitly_controller_lifetime_not_etl(self):
        summary, quality = synthetic_report(synthetic_manifest("E"), stress_samples=SYNTHETIC_SAMPLES)
        self.assertEqual(summary["scenario_runtime"], 30)
        self.assertEqual(summary["stress_lifetime_sample_span_s"], 2.5)
        self.assertEqual(summary["stress_lifetime_sample_count"], 2)
        self.assertEqual(summary["stress_lifetime_cpu_percent_mean"], 30.25)
        self.assertEqual(summary["stress_lifetime_available_memory_bytes_min"], 2048)
        self.assertEqual(summary["stress_lifetime_committed_bytes_max"], 16384)
        self.assertTrue(all(key.startswith("stress_lifetime_") for key in summary if key.startswith("stress_")))
        self.assertIn("not correlated to ETL", quality["stress_timebase"])

    def test_synthetic_missing_or_nonmonotonic_stress_coverage_returns_invalid(self):
        for samples in ((), SYNTHETIC_SAMPLES[:1], SYNTHETIC_SAMPLES[::-1], [SYNTHETIC_SAMPLES[0]] * 2):
            with self.subTest(samples=samples):
                summary, quality = synthetic_report(synthetic_manifest("E"), stress_samples=samples)
                self.assertEqual(quality["status"], "invalid")
                self.assertNotIn("stress_lifetime_cpu_percent_mean", summary)

    def test_synthetic_stress_state_failures_and_wrong_run_ids_are_detected(self):
        manifest = synthetic_manifest("E")
        manifest["stress_state"] = {"status": "failed", "run_id": SYNTHETIC_RUN_ID, "error": "Synthetic worker failure"}
        _, quality = synthetic_report(manifest, stress_samples=SYNTHETIC_SAMPLES)
        self.assertEqual(quality["status"], "invalid")
        self.assertTrue(any("Synthetic worker failure" in reason for reason in quality["reasons"]))
        manifest["stress_state"]["run_id"] = "f" * 32
        with self.assertRaises(metrics.MetricsError):
            synthetic_report(manifest, stress_samples=SYNTHETIC_SAMPLES)

    def test_synthetic_stress_samples_for_disabled_condition_are_invalid(self):
        _, quality = synthetic_report(stress_samples=SYNTHETIC_SAMPLES)
        self.assertEqual(quality["status"], "invalid")

    def test_synthetic_jsonl_samples_preserve_numeric_types_and_read_only_contract(self):
        text = "\ufeff" + "\n".join(json.dumps(row) for row in SYNTHETIC_SAMPLES) + "\n"
        with synthetic_input(text) as opened:
            rows = report.read_stress_samples("synthetic_stress_samples.jsonl")
        self.assertEqual(rows, SYNTHETIC_SAMPLES)
        self.assertIs(type(rows[0]["elapsed_s"]), int)
        self.assertIs(type(rows[1]["elapsed_s"]), float)
        self.assertIs(type(rows[0]["available_memory_bytes"]), int)
        opened.assert_called_once_with("synthetic_stress_samples.jsonl", "r", encoding="utf-8-sig")
        with synthetic_input(""):
            self.assertEqual(report.read_stress_samples("synthetic_empty.jsonl"), [])

    def test_synthetic_jsonl_and_direct_samples_reject_bad_numeric_values(self):
        for key, value in (
            ("elapsed_s", True), ("elapsed_s", -1), ("elapsed_s", float("inf")),
            ("cpu_percent", True), ("cpu_percent", 101), ("cpu_percent", "25"),
            ("cpu_percent", float("nan")), ("available_memory_bytes", False),
            ("available_memory_bytes", 1.5), ("committed_bytes", -1),
        ):
            sample = dict(SYNTHETIC_SAMPLES[0])
            sample[key] = value
            with self.subTest(key=key, value=value):
                with synthetic_input(json.dumps(sample)), self.assertRaises(metrics.MetricsError):
                    report.read_stress_samples("synthetic_bad.jsonl")
                with self.assertRaises(metrics.MetricsError):
                    synthetic_report(synthetic_manifest("E"), stress_samples=[sample])

    def test_synthetic_jsonl_malformed_duplicate_and_missing_fields_raise(self):
        for text in ("\n", "{", "[]", "{}", '{"elapsed_s":0,"elapsed_s":1}'):
            with self.subTest(text=text), synthetic_input(text), self.assertRaises(metrics.MetricsError):
                report.read_stress_samples("synthetic_bad.jsonl")
        with mock.patch("builtins.open", side_effect=FileNotFoundError("Synthetic missing file")), self.assertRaises(metrics.MetricsError):
            report.read_stress_samples("synthetic_missing.jsonl")


class SyntheticFilesystem:
    """In-memory model of exclusive writes and atomic replacements."""

    def __init__(self):
        self.directory = Path("synthetic_report_directory")
        self.files = {self.directory / "synthetic_existing_raw.csv": b"synthetic untouched raw data"}
        self.events = []
        self.replace_failure_at = None
        self.fsync_failure = False
        self.unlink_failure = False
        self.replace_count = 0

    def open(self, path, mode):
        path = Path(path)
        if mode != "xb" or path.parent != self.directory:
            raise AssertionError("Unexpected synthetic report IO")
        if path in self.files:
            raise FileExistsError("Synthetic collision")
        self.files[path] = b""
        self.events.append(("open", path))
        owner = self

        class SyntheticStream(io.BytesIO):
            def fileno(self):
                return 123

            def close(self):
                if not self.closed:
                    owner.files[path] = self.getvalue()
                    owner.events.append(("closed", path))
                super().close()

        return SyntheticStream()

    def fsync(self, descriptor):
        self.events.append(("fsync", descriptor))
        if self.fsync_failure:
            raise OSError("Synthetic fsync failure")

    def replace(self, source, destination):
        self.replace_count += 1
        if self.replace_count == self.replace_failure_at:
            raise OSError("Synthetic atomic replacement failure")
        self.events.append(("replace", source, destination))
        self.files[Path(destination)] = self.files.pop(Path(source))

    def unlink(self, path, missing_ok=False):
        self.events.append(("unlink", path))
        if self.unlink_failure:
            raise OSError("Synthetic staging cleanup failure")
        self.files.pop(Path(path), None)

    def patches(self):
        context = ExitStack()
        context.enter_context(mock.patch("builtins.open", side_effect=self.open))
        context.enter_context(mock.patch.object(Path, "is_dir", autospec=True, side_effect=lambda path: path == self.directory))
        context.enter_context(mock.patch.object(Path, "unlink", autospec=True, side_effect=self.unlink))
        context.enter_context(mock.patch.object(report.os, "replace", side_effect=self.replace))
        context.enter_context(mock.patch.object(report.os, "fsync", side_effect=self.fsync))
        return context


class ReportWriterTests(unittest.TestCase):
    def test_synthetic_writer_is_headerless_scalar_only_and_quality_commits_summary_hash(self):
        summary, quality = synthetic_report()
        summary["ai_model"] = "Synthetic, quoted model"
        originals = copy.deepcopy((summary, quality))
        filesystem = SyntheticFilesystem()
        with filesystem.patches():
            report.write_report(filesystem.directory, summary, quality)
        written_summary = filesystem.files[filesystem.directory / report.SUMMARY_FILENAME]
        written_quality = json.loads(filesystem.files[filesystem.directory / report.QUALITY_FILENAME])
        rows = list(csv.reader(io.StringIO(written_summary.decode("utf-8"))))
        self.assertTrue(all(len(row) == 2 for row in rows))
        self.assertEqual(dict(rows)["scenario_runtime"], "30.0")
        self.assertEqual(dict(rows)["ai_model"], "Synthetic, quoted model")
        self.assertNotIn(["Metric", "Value"], rows)
        self.assertNotIn(["PT", "Metric", "Duration"], rows)
        self.assertEqual(written_quality["summary_sha256"], hashlib.sha256(written_summary).hexdigest())
        self.assertEqual(filesystem.files[filesystem.directory / "synthetic_existing_raw.csv"], b"synthetic untouched raw data")
        self.assertEqual({path.name for path in filesystem.files}, {
            report.SUMMARY_FILENAME, report.QUALITY_FILENAME, "synthetic_existing_raw.csv",
        })
        self.assertEqual((summary, quality), originals)
        first_replace = next(index for index, event in enumerate(filesystem.events) if event[0] == "replace")
        self.assertEqual(sum(event[0] == "closed" for event in filesystem.events[:first_replace]), 2)
        self.assertEqual(sum(event[0] == "fsync" for event in filesystem.events[:first_replace]), 2)
        destinations = [event[2].name for event in filesystem.events if event[0] == "replace"]
        self.assertEqual(destinations, [report.SUMMARY_FILENAME, report.QUALITY_FILENAME])
        self.assertTrue(all(event[1].suffix == ".pending" for event in filesystem.events if event[0] == "open"))

    def test_synthetic_staging_failure_keeps_previous_outputs_and_cleans_owned_pending_files(self):
        filesystem = SyntheticFilesystem()
        filesystem.files[filesystem.directory / report.SUMMARY_FILENAME] = b"synthetic old summary"
        filesystem.files[filesystem.directory / report.QUALITY_FILENAME] = b"synthetic old quality"
        original_files = dict(filesystem.files)
        filesystem.fsync_failure = True
        summary, quality = synthetic_report()
        with filesystem.patches(), self.assertRaises(metrics.MetricsError):
            report.write_report(filesystem.directory, summary, quality)
        self.assertEqual(filesystem.files, original_files)

    def test_synthetic_interrupted_pair_publication_is_detectable_by_quality_hash(self):
        filesystem = SyntheticFilesystem()
        filesystem.files[filesystem.directory / report.QUALITY_FILENAME] = json.dumps({"summary_sha256": "0" * 64}).encode()
        filesystem.replace_failure_at = 2
        summary, quality = synthetic_report()
        with filesystem.patches(), self.assertRaises(metrics.MetricsError):
            report.write_report(filesystem.directory, summary, quality)
        published = filesystem.files[filesystem.directory / report.SUMMARY_FILENAME]
        commit_record = json.loads(filesystem.files[filesystem.directory / report.QUALITY_FILENAME])
        self.assertNotEqual(commit_record["summary_sha256"], hashlib.sha256(published).hexdigest())
        self.assertFalse(any(path.suffix == ".pending" for path in filesystem.files))

    def test_synthetic_staging_cleanup_failure_logs_path_and_preserves_primary_error(self):
        filesystem = SyntheticFilesystem()
        filesystem.fsync_failure = True
        filesystem.unlink_failure = True
        summary, quality = synthetic_report()
        with filesystem.patches(), self.assertLogs(report.__name__, level="WARNING") as logged:
            with self.assertRaisesRegex(metrics.MetricsError, "Synthetic fsync failure"):
                report.write_report(filesystem.directory, summary, quality)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("Synthetic staging cleanup failure", logged.output[0])
        self.assertIn("synthetic_report_directory", logged.output[0])
        self.assertIn(".pending", logged.output[0])

    def test_synthetic_invalid_scalar_values_are_rejected_before_file_operations(self):
        for value in (True, None, [], float("nan"), float("inf")):
            summary, quality = synthetic_report()
            summary["synthetic_bad"] = value
            with self.subTest(value=value), mock.patch("builtins.open") as opened, self.assertRaises(metrics.MetricsError):
                report.write_report("synthetic_report_directory", summary, quality)
            opened.assert_not_called()
        summary, quality = synthetic_report()
        summary[1] = "synthetic bad key"
        with self.assertRaises(metrics.MetricsError):
            report.write_report("synthetic_report_directory", summary, quality)

    def test_synthetic_missing_directory_and_inconsistent_quality_raise(self):
        summary, quality = synthetic_report()
        with mock.patch.object(Path, "is_dir", return_value=False), self.assertRaises(metrics.MetricsError):
            report.write_report("synthetic_missing_directory", summary, quality)
        quality["status"] = "valid"
        with self.assertRaises(metrics.MetricsError):
            report.write_report("synthetic_report_directory", summary, quality)


if __name__ == "__main__":
    unittest.main()
