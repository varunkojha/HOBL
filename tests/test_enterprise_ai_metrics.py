"""Safe synthetic offline tests; no ETL capture, stress, or proprietary binaries.

All data fixtures are synthetic and held in memory. Profile checks are static
XML checks, not a claim of WPR schema/runtime or native-provider validation.
"""

import csv
import importlib.util
import io
import math
from pathlib import Path
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "enterprise_ai_metrics", ROOT / "utilities" / "open_source" / "enterprise_ai_metrics.py"
)
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)

SYNTHETIC_RUN_ID = "synthetic-run"
SYNTHETIC_BEGIN = "2026-09-27T10:00:10.0000000Z"
SYNTHETIC_END = "2026-09-27T10:00:12.0000000Z"
SYNTHETIC_OTHER_GUID = "11111111-2222-3333-4444-555555555555"
SYNTHETIC_MANIFEST = """<?xml version="1.0"?>
<diagrules xmlns="urn:synthetic:manifest"><scenarios>
  <scenario scenarioname="PT_0007_Synthetic Menu_abcdef" ptscenarioname="Synthetic Menu"/>
  <scenario scenarioname="PTSdw_8_Synthetic Paint_123456" ptscenarioname="Synthetic Paint"/>
  <scenario scenarioname="PT_9_Synthetic Truncated_abcdef" ptscenarioname="Synthetic Full Name"/>
  <scenario scenarioname="NotAPerfTrackScenario" ptscenarioname="Not whitelisted"/>
</scenarios></diagrules>
"""


def synthetic_input(text):
    """Intercept file reads so no fixture or intermediate files are created."""
    payload = text.encode("utf-8")

    def open_synthetic(path, mode="r", **kwargs):
        if not str(path).startswith("synthetic_") or mode not in ("r", "rb"):
            raise AssertionError(f"Unexpected fixture IO: {path!r}, {mode!r}")
        binary = io.BytesIO(payload)
        if mode == "rb":
            return binary
        return io.TextIOWrapper(
            binary, encoding=kwargs.get("encoding", "utf-8"), newline=kwargs.get("newline")
        )

    return mock.patch("builtins.open", side_effect=open_synthetic)


def synthetic_manifest(text=SYNTHETIC_MANIFEST):
    with synthetic_input(text):
        return metrics.build_manifest_map("synthetic_manifest.xml")


def synthetic_csv(text, mapping=None):
    if mapping is None:
        mapping = synthetic_manifest()
    with synthetic_input(text):
        return metrics.read_perf_csv("synthetic_raw.csv", mapping)


def synthetic_event(
    guid=SYNTHETIC_OTHER_GUID,
    timestamp="2026-09-27T10:00:11.0000000Z",
    event_name=None,
    data=(),
    event_id="1",
    provider_name="Synthetic.Provider",
    payload_kind="EventData",
    name_location="system",
):
    element = ET.Element("Event")
    system = ET.SubElement(element, "System")
    provider_attrs = {}
    if guid is not None:
        provider_attrs["Guid"] = guid
    if provider_name is not None:
        provider_attrs["Name"] = provider_name
    ET.SubElement(system, "Provider", provider_attrs)
    if event_id is not None:
        ET.SubElement(system, "EventID").text = event_id
    time_attrs = {"SystemTime": timestamp} if timestamp is not None else {}
    ET.SubElement(system, "TimeCreated", time_attrs)
    if event_name is not None:
        if name_location == "attribute":
            element.set("EventName", event_name)
        elif name_location == "rendering":
            ET.SubElement(ET.SubElement(element, "RenderingInfo"), "EventName").text = event_name
        else:
            ET.SubElement(system, "EventName").text = event_name
    payload = ET.SubElement(element, payload_kind)
    if payload_kind == "UserData":
        payload = ET.SubElement(payload, "{urn:synthetic:payload}SyntheticEvent")
        for name, value in data:
            ET.SubElement(payload, "{urn:synthetic:payload}" + name).text = value
    else:
        for name, value in data:
            ET.SubElement(payload, "Data", {"Name": name}).text = value
    return ET.tostring(element, encoding="unicode")


def synthetic_marker(
    phase, timestamp=None, run_id=SYNTHETIC_RUN_ID, guid=metrics.INPUT_INJECT_GUID, **kwargs
):
    timestamp = timestamp or (SYNTHETIC_BEGIN if phase == "measurement_begin" else SYNTHETIC_END)
    return synthetic_event(
        guid=guid,
        timestamp=timestamp,
        data=(("Marker", f"enterprise_ai:{run_id}:{phase}"),),
        **kwargs,
    )


def synthetic_xml(events):
    return '<Events xmlns="http://schemas.microsoft.com/win/2004/08/events/event">' + "".join(events) + "</Events>"


def synthetic_trace(events, **kwargs):
    with synthetic_input(synthetic_xml(events)):
        return metrics.read_trace("synthetic_trace.xml", SYNTHETIC_RUN_ID, **kwargs)


def synthetic_window(*events):
    return [synthetic_marker("measurement_begin"), *events, synthetic_marker("measurement_end")]


def synthetic_tip(data, **kwargs):
    return synthetic_event(guid=metrics.TIP_PROVIDER_GUID, data=data, **kwargs)


def synthetic_tip_rows(events):
    with synthetic_input(synthetic_xml(events)):
        return list(metrics.iter_tip_rows("synthetic_tip.xml", SYNTHETIC_RUN_ID))


class ManifestAndCsvTests(unittest.TestCase):
    def test_synthetic_manifest_preserves_numeric_ids_and_parser_aliases(self):
        mapping = synthetic_manifest()
        self.assertEqual(mapping["Synthetic Menu"], frozenset({"0007"}))
        self.assertEqual(mapping["PTSdw_8_Synthetic Paint_123456"], frozenset({"8"}))
        self.assertEqual(mapping["Synthetic Truncated"], frozenset({"9"}))
        self.assertEqual(mapping["Synthetic Full Name"], frozenset({"9"}))
        self.assertNotIn("Not whitelisted", mapping)

    def test_synthetic_manifest_keeps_ambiguous_aliases(self):
        mapping = synthetic_manifest("""<scenarios>
          <scenario scenarioname="PT_1_Shared_a" ptscenarioname="Shared"/>
          <scenario scenarioname="PT_2_Shared_b" ptscenarioname="Shared"/>
        </scenarios>""")
        self.assertEqual(mapping["Shared"], frozenset({"1", "2"}))
        row = synthetic_csv("Scenario,Metric,Duration\nPT_1_Shared_a,Shared,2\n", mapping)
        self.assertEqual(row[0]["PT"], "1")
        with self.assertRaisesRegex(metrics.MetricsError, "ambiguous"):
            synthetic_csv("Scenario,Metric,Duration\nsynthetic_tag,Shared,2\n", mapping)

    def test_synthetic_malformed_or_empty_manifest_fails(self):
        for value in (
            "<scenarios/>",
            '<scenarios><scenario scenarioname="PT_bad_Synthetic_a"/></scenarios>',
            '<scenarios><scenario scenarioname="PT_1_Synthetic_a">',
        ):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                synthetic_manifest(value)

    def test_synthetic_csv_preserves_legacy_schema_values_and_whitelist(self):
        raw = (
            "\ufeffScenario,Metric,Duration\r\n"
            "PT_0007_Synthetic Menu_abcdef,Synthetic Menu,001.5000\r\n"
            "enterprise_ai:synthetic-run:probe,Synthetic Paint,2e1\r\n"
            "PT_999_Unapproved_abcdef,Synthetic Menu,3\r\n"
            "synthetic_tag,Not whitelisted,4\r\n"
        )
        rows = synthetic_csv(raw)
        self.assertEqual(rows, [
            {"PT": "0007", "Metric": "Synthetic Menu", "Duration": "001.5000"},
            {"PT": "8", "Metric": "Synthetic Paint", "Duration": "2e1"},
        ])
        self.assertEqual(tuple(rows[0]), metrics.PT_COLUMNS)
        self.assertIn("Scenario,Metric,Duration", raw)

    def test_synthetic_csv_quote_handling(self):
        rows = synthetic_csv(
            'Scenario,Metric,Duration\nsynthetic_tag,"Synthetic, quoted metric",2.5\n',
            {"Synthetic, quoted metric": "11"},
        )
        self.assertEqual(rows[0]["Metric"], "Synthetic, quoted metric")

    def test_synthetic_csv_does_not_rescue_unknown_explicit_id(self):
        rows = synthetic_csv("Scenario,Metric,Duration\nPT_999_Other_a,Synthetic Menu,3\n")
        self.assertEqual(rows, [])

    def test_synthetic_csv_conflicting_identifiers_fail(self):
        with self.assertRaisesRegex(metrics.MetricsError, "conflicting"):
            synthetic_csv("Scenario,Metric,Duration\nPT_0007_Synthetic Menu_a,Synthetic Paint,2\n")
        with self.assertRaisesRegex(metrics.MetricsError, "ambiguous"):
            synthetic_csv("Scenario,Metric,Duration\nsynthetic_tag,Shared,2\n", {"Shared": {"1", "2"}})

    def test_synthetic_csv_matching_alias_intersection_disambiguates(self):
        rows = synthetic_csv(
            "Scenario,Metric,Duration\nSynthetic scenario,Shared,2\n",
            {"Synthetic scenario": {"1", "2"}, "Shared": {"2", "3"}},
        )
        self.assertEqual(rows[0]["PT"], "2")

    def test_synthetic_csv_malformed_headers_and_rows_fail(self):
        values = (
            "",
            "PT,Metric,Duration\n1,Synthetic Menu,2\n",
            "Scenario,Metric,Duration,Extra\nsynthetic_tag,Synthetic Menu,2,3\n",
            "Scenario,Metric,Metric\nsynthetic_tag,Synthetic Menu,2\n",
            " Scenario,Metric,Duration\nsynthetic_tag,Synthetic Menu,2\n",
            "Scenario,Metric,Duration\nsynthetic_tag,Synthetic Menu\n",
            "Scenario,Metric,Duration\nsynthetic_tag,Synthetic Menu,2,3\n",
            "Scenario,Metric,Duration\n,Synthetic Menu,2\n",
            "Scenario,Metric,Duration\nsynthetic_tag,,2\n",
            "Scenario,Metric,Duration\nPT_bad_Synthetic_a,Synthetic Menu,2\n",
            'Scenario,Metric,Duration\nsynthetic_tag,"Synthetic Menu,2\n',
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                synthetic_csv(value)

    def test_synthetic_csv_all_durations_must_be_finite_and_nonnegative(self):
        for value in ("", "NaN", "inf", "-Infinity", "-1", "-1e-9999", "1e9999", "1_000", "1ms", "1e-999999999999999999999999999"):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                synthetic_csv(f"Scenario,Metric,Duration\nsynthetic_tag,Synthetic Menu,{value}\n")
        with self.assertRaises(metrics.MetricsError):
            synthetic_csv("Scenario,Metric,Duration\nunmatched,unmatched,NaN\n")

    def test_synthetic_invalid_manifest_maps_fail(self):
        for value in ({}, {"": "1"}, {"Synthetic": "PT_1"}, {"Synthetic": ()}, {"Synthetic": 1}):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                synthetic_csv("Scenario,Metric,Duration\nsynthetic_tag,Synthetic,1\n", value)

    def test_synthetic_csv_read_never_writes_or_renames_raw(self):
        raw = "Scenario,Metric,Duration\nsynthetic_tag,Synthetic Menu,2\n"
        mapping = synthetic_manifest()
        with synthetic_input(raw) as opened:
            metrics.read_perf_csv("synthetic_raw.csv", mapping)
        opened.assert_called_once_with("synthetic_raw.csv", "r", encoding="utf-8-sig", newline="")

    def test_synthetic_writer_has_unchanged_three_column_legacy_schema(self):
        opener = mock.mock_open()
        with mock.patch("builtins.open", opener):
            metrics.write_perf_csv(
                "synthetic_processed.csv", [{"PT": "0007", "Metric": "Synthetic, metric", "Duration": "2.00"}]
            )
        output = "".join(call.args[0] for call in opener().write.call_args_list)
        self.assertEqual(list(csv.reader(io.StringIO(output))), [
            ["PT", "Metric", "Duration"], ["0007", "Synthetic, metric", "2.00"],
        ])

    def test_synthetic_writer_validates_before_creating_output(self):
        opener = mock.mock_open()
        with mock.patch("builtins.open", opener), self.assertRaises(metrics.MetricsError):
            metrics.write_perf_csv("synthetic_processed.csv", [{"PT": "1", "Metric": "Synthetic", "Duration": "NaN"}])
        opener.assert_not_called()


class PtSummaryTests(unittest.TestCase):
    @staticmethod
    def synthetic_rows(values, pt="0007"):
        return [{"PT": pt, "Metric": "Synthetic metric", "Duration": str(value)} for value in values]

    def test_synthetic_summary_preserves_id_count_and_median(self):
        result = metrics.summarize_pt(self.synthetic_rows((9, 1, 2, 4)), required_pts=("0007",))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pts"]["0007"]["count"], 4)
        self.assertEqual(result["pts"]["0007"]["median_ms"], 3.0)
        self.assertNotIn("p95_ms", result["pts"]["0007"])

    def test_synthetic_summary_optional_p95_requires_100_observations(self):
        small = metrics.summarize_pt(self.synthetic_rows(range(1, 100)), include_p95=True)
        self.assertNotIn("p95_ms", small["pts"]["0007"])
        self.assertEqual(small["pts"]["0007"]["p95_status"], "insufficient_observations")
        enough = metrics.summarize_pt(self.synthetic_rows(range(1, 101)), include_p95=True)
        self.assertEqual(enough["pts"]["0007"]["p95_ms"], 95.0)

    def test_synthetic_missing_required_pts_are_explicit_errors_not_zero_latencies(self):
        result = metrics.summarize_pt(self.synthetic_rows((1,)), required_pts=("0007", "99"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["missing_required_pts"], ["99"])
        self.assertEqual(result["pts"]["99"]["status"], "missing")
        self.assertEqual(result["pts"]["99"]["count"], 0)
        self.assertIsNone(result["pts"]["99"]["median_ms"])
        self.assertTrue(result["errors"])
        self.assertEqual(metrics.summarize_pt([])["status"], "error")

    def test_synthetic_summary_rejects_malformed_rows_and_preserves_finite_median(self):
        for row in (
            {"PT": "PT_1", "Metric": "Synthetic", "Duration": "1"},
            {"PT": "1", "Metric": "Synthetic", "Duration": "inf"},
            {"PT": "1", "Metric": "Synthetic", "Duration": "1", "NewColumn": "forbidden"},
        ):
            with self.subTest(row=row), self.assertRaises(metrics.MetricsError):
                metrics.summarize_pt([row])
        result = metrics.summarize_pt(self.synthetic_rows(("1e308", "1e308")))
        self.assertTrue(math.isfinite(result["pts"]["0007"]["median_ms"]))


class TraceTests(unittest.TestCase):
    def test_synthetic_namespaced_window_counts_and_observed_search_envelope(self):
        result = synthetic_trace(synthetic_window(
            synthetic_event(timestamp="2026-09-27T10:00:09Z", event_name="Outside"),
            synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, timestamp="2026-09-27T10:00:10.25Z", event_name="IndexItemInit"),
            synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, timestamp="2026-09-27T10:00:11.75Z", event_name="IndexItemDataComplete"),
            synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, event_name="SyntheticUnknownEvent"),
            synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, event_id="2"),
            synthetic_event(timestamp="2026-09-27T10:00:13Z", event_name="Outside"),
        ), required_providers=(metrics.WINDOWS_SEARCH_CORE_GUID,))
        self.assertEqual(result["scenario_runtime"], 2.0)
        self.assertEqual(result["provider_counts"], {
            metrics.INPUT_INJECT_GUID: 2, metrics.WINDOWS_SEARCH_CORE_GUID: 4,
        })
        search = result["search"]
        self.assertEqual(search["event_counts"], {"IndexItemInit": 1, "IndexItemDataComplete": 1})
        self.assertEqual(search["envelope_s"], 1.5)
        self.assertEqual(search["unknown_event_names"], {"SyntheticUnknownEvent": 1})
        self.assertEqual(search["missing_event_name_count"], 1)
        self.assertEqual(search["completion_status"], "unverified")
        self.assertIsNone(search["completed_semantic_documents"])
        self.assertIsNone(search["per_request_latency_ms"])
        self.assertEqual(result["trace_loss"]["status"], "unknown")
        self.assertIsNone(result["trace_loss"]["events_lost"])
        self.assertIsNone(result["trace_loss"]["buffers_lost"])
        self.assertEqual(result["status"], "unverified")

    def test_synthetic_overlapping_run_ids_do_not_change_selected_window(self):
        result = synthetic_trace(synthetic_window(
            synthetic_marker("measurement_begin", timestamp="2026-09-27T10:00:08Z", run_id="synthetic-run-long"),
            synthetic_marker("measurement_end", timestamp="2026-09-27T10:00:14Z", run_id="synthetic-run-long"),
            synthetic_event(event_name="SyntheticInside"),
        ))
        self.assertEqual(result["scenario_runtime"], 2.0)
        self.assertEqual(result["markers"]["measurement_begin"]["value"], "enterprise_ai:synthetic-run:measurement_begin")
        self.assertEqual(result["provider_counts"][metrics.INPUT_INJECT_GUID], 2)

    def test_synthetic_other_provider_and_provider_name_cannot_fake_markers(self):
        for guid in (SYNTHETIC_OTHER_GUID, None):
            with self.subTest(guid=guid), self.assertRaisesRegex(metrics.MetricsError, "Missing InputInject"):
                synthetic_trace([
                    synthetic_marker("measurement_begin", guid=guid, provider_name=metrics.INPUT_INJECT_GUID),
                    synthetic_marker("measurement_end", guid=guid, provider_name=metrics.INPUT_INJECT_GUID),
                ])
        result = synthetic_trace(synthetic_window(
            synthetic_marker("measurement_begin", guid=SYNTHETIC_OTHER_GUID),
            synthetic_marker("measurement_end", guid=SYNTHETIC_OTHER_GUID),
        ))
        self.assertEqual(result["scenario_runtime"], 2)

    def test_synthetic_markers_require_exact_payload_values(self):
        for value in (
            "enterprise_ai:synthetic-run:measurement_begin ",
            "prefix enterprise_ai:synthetic-run:measurement_begin",
            "enterprise_ai:synthetic-run:measurement_begin:suffix",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(metrics.MetricsError, "Missing InputInject"):
                synthetic_trace([
                    synthetic_event(guid=metrics.INPUT_INJECT_GUID, timestamp=SYNTHETIC_BEGIN, data=(("Marker", value),)),
                    synthetic_marker("measurement_end"),
                ])

    def test_synthetic_userdata_markers_and_guid_normalization(self):
        result = synthetic_trace([
            synthetic_marker("measurement_begin", guid="{" + metrics.INPUT_INJECT_GUID.upper() + "}", payload_kind="UserData"),
            synthetic_marker("measurement_end", payload_kind="UserData"),
        ])
        self.assertEqual(result["scenario_runtime"], 2)

    def test_synthetic_missing_or_duplicate_markers_fail(self):
        begin = synthetic_marker("measurement_begin")
        end = synthetic_marker("measurement_end")
        for events in ([], [begin], [end], [begin, begin, end], [begin, end, end]):
            with self.subTest(events=events), self.assertRaises(metrics.MetricsError):
                synthetic_trace(events)
        duplicate_payload = synthetic_event(
            guid=metrics.INPUT_INJECT_GUID,
            timestamp=SYNTHETIC_BEGIN,
            data=(("One", "enterprise_ai:synthetic-run:measurement_begin"), ("Two", "enterprise_ai:synthetic-run:measurement_begin")),
        )
        with self.assertRaisesRegex(metrics.MetricsError, "Duplicate"):
            synthetic_trace([duplicate_payload, end])

    def test_synthetic_reversed_or_equal_marker_timestamps_fail(self):
        for end in (SYNTHETIC_BEGIN, "2026-09-27T10:00:09Z"):
            with self.subTest(end=end), self.assertRaisesRegex(metrics.MetricsError, "strictly after"):
                synthetic_trace([synthetic_marker("measurement_begin"), synthetic_marker("measurement_end", timestamp=end)])

    def test_synthetic_missing_naive_invalid_and_nonfinite_timestamps_fail(self):
        for stamp in (None, "NaN", "2026-09-27T10:00:11", "2026-02-30T10:00:11Z", "2026-09-27T10:00:11+05:99"):
            with self.subTest(stamp=stamp), self.assertRaises(metrics.MetricsError):
                synthetic_trace(synthetic_window(synthetic_event(timestamp=stamp)))

    def test_synthetic_nanosecond_timestamps_and_timezone_offsets(self):
        result = synthetic_trace([
            synthetic_marker("measurement_begin", timestamp="2026-09-27T10:00:10.0000000Z"),
            synthetic_marker("measurement_end", timestamp="2026-09-27T15:30:10.0000001+05:30"),
        ])
        self.assertAlmostEqual(result["scenario_runtime"], 0.0000001)

    def test_synthetic_unsorted_records_use_etl_timestamps_and_closed_boundaries(self):
        result = synthetic_trace([
            synthetic_marker("measurement_end"),
            synthetic_event(timestamp=SYNTHETIC_END, event_name="AtEnd"),
            synthetic_event(timestamp=SYNTHETIC_BEGIN, event_name="AtBegin"),
            synthetic_marker("measurement_begin"),
        ])
        self.assertEqual(result["provider_counts"][SYNTHETIC_OTHER_GUID], 2)
        self.assertEqual(result["scenario_runtime"], 2)

    def test_synthetic_mandatory_provider_must_occur_inside_window(self):
        with self.assertRaisesRegex(metrics.MetricsError, "Required providers"):
            synthetic_trace(synthetic_window(
                synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, timestamp="2026-09-27T10:00:09Z")
            ), required_providers=(metrics.WINDOWS_SEARCH_CORE_GUID,))
        with self.assertRaisesRegex(metrics.MetricsError, "GUID"):
            synthetic_trace(synthetic_window(), required_providers=("not-a-guid",))

    def test_synthetic_mandatory_source_identity_and_numeric_event_ids(self):
        for event in (
            synthetic_event(guid=None, provider_name=None),
            synthetic_event(guid="not-a-guid"),
            synthetic_event(event_id="-1"),
            synthetic_event(event_id="NaN"),
        ):
            with self.subTest(event=event), self.assertRaises(metrics.MetricsError):
                synthetic_trace(synthetic_window(event))

    def test_synthetic_event_names_are_not_guessed_from_payload_or_other_provider(self):
        result = synthetic_trace(synthetic_window(
            synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, data=(("EventName", "IndexItemInit"),), event_id=None),
            synthetic_event(event_name="IndexItemDataComplete"),
        ))
        self.assertEqual(result["search"]["status"], "event_names_unavailable")
        self.assertEqual(result["search"]["observed_event_count"], 0)
        self.assertIsNone(result["search"]["envelope_s"])
        self.assertEqual(result["missing_event_id_counts"][metrics.WINDOWS_SEARCH_CORE_GUID], 1)

    def test_synthetic_event_name_metadata_locations_and_partial_coverage(self):
        for location in ("system", "attribute", "rendering"):
            with self.subTest(location=location):
                result = synthetic_trace(synthetic_window(
                    synthetic_event(guid=metrics.WINDOWS_SEARCH_CORE_GUID, event_name="IndexItemInit", name_location=location)
                ))
                self.assertEqual(result["search"]["status"], "partial")
                self.assertEqual(result["search"]["observed_event_count"], 1)
                self.assertIsNone(result["search"]["envelope_s"])

    def test_synthetic_malformed_xml_missing_files_and_invalid_run_id_fail(self):
        with synthetic_input("<Events>"), self.assertRaises(metrics.MetricsError):
            metrics.read_trace("synthetic_broken.xml", SYNTHETIC_RUN_ID)
        with mock.patch("builtins.open", side_effect=FileNotFoundError("Synthetic missing file")), self.assertRaises(metrics.MetricsError):
            metrics.read_trace("synthetic_missing.xml", SYNTHETIC_RUN_ID)
        for run_id in ("", "synthetic:run", "synthetic run"):
            with self.subTest(run_id=run_id), self.assertRaises(metrics.MetricsError):
                metrics.read_trace("synthetic_unused.xml", run_id)

    def test_synthetic_stream_removes_event_shells_in_both_passes(self):
        real_iterparse = ET.iterparse
        max_retained_children = 0
        roots = []

        def observed_iterparse(*args, **kwargs):
            nonlocal max_retained_children
            root = None
            for action, element in real_iterparse(*args, **kwargs):
                if root is None:
                    root = element
                    roots.append(root)
                yield action, element
                max_retained_children = max(max_retained_children, len(root))

        repeated = synthetic_event(event_name="SyntheticRepeatedEvent")
        events = synthetic_window(*([repeated] * 5000))
        with mock.patch.object(ET, "iterparse", side_effect=observed_iterparse), mock.patch.object(
            ET, "parse", side_effect=AssertionError("Do not materialize the trace XML")
        ):
            result = synthetic_trace(events)
        self.assertEqual(result["provider_counts"][SYNTHETIC_OTHER_GUID], 5000)
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(len(root) == 0 for root in roots))
        self.assertLess(max_retained_children, 512)


class TipTests(unittest.TestCase):
    SYNTHETIC_TIP_FIELDS = (
        ("testCaseName", "TypeToSearchTestTopResultRendered"),
        ("completionKind", "1"),
        ("durationMs", "12.50"),
    )

    def test_synthetic_tip_is_exact_provider_testcase_completion_and_window(self):
        events = synthetic_window(
            synthetic_tip(self.SYNTHETIC_TIP_FIELDS),
            synthetic_tip(self.SYNTHETIC_TIP_FIELDS, timestamp="2026-09-27T10:00:13Z"),
            synthetic_event(data=self.SYNTHETIC_TIP_FIELDS),
            synthetic_tip((("testCaseName", "PrefixTypeToSearchTestTopResultRendered"), ("completionKind", "1"), ("durationMs", "3"))),
            synthetic_tip((("testCaseName", "TypeToSearchTestTopResultRendered"), ("completionKind", "2"), ("durationMs", "3"))),
        )
        self.assertEqual(synthetic_tip_rows(events), [{"PT": "10010", "Metric": "TopResultRender", "Duration": "12.50"}])

    def test_synthetic_tip_userdata_uppercase_field_variant(self):
        fields = (("TestCaseName", "TypeToSearchTestTopResultRendered"), ("CompletionKind", "1"), ("DurationMs", "0"))
        self.assertEqual(synthetic_tip_rows(synthetic_window(synthetic_tip(fields, payload_kind="UserData")))[0]["Duration"], "0")

    def test_synthetic_tip_malformed_missing_and_ambiguous_fields_fail(self):
        for fields in (
            (("testCaseName", "TypeToSearchTestTopResultRendered"),),
            (("testCaseName", "TypeToSearchTestTopResultRendered"), ("completionKind", "1")),
            (("testCaseName", "TypeToSearchTestTopResultRendered"), ("completionKind", "NaN"), ("durationMs", "2")),
            (("testCaseName", "TypeToSearchTestTopResultRendered"), ("completionKind", "1"), ("durationMs", "-2")),
            (("testCaseName", "TypeToSearchTestTopResultRendered"), ("completionKind", "1"), ("durationMs", "Infinity")),
            self.SYNTHETIC_TIP_FIELDS + (("DurationMs", "12.50"),),
        ):
            with self.subTest(fields=fields), self.assertRaises(metrics.MetricsError):
                synthetic_tip_rows(synthetic_window(synthetic_tip(fields)))


class CaptureProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = ET.parse(ROOT / "providers" / "GTPLight_EnterpriseAI.wprp").getroot()
        cls.base = ET.parse(ROOT / "providers" / "GTPLight_CustomMemHardFaults.wprp").getroot()
        cls.utc = ET.parse(ROOT / "providers" / "perf_utc.wprp").getroot()

    def test_profile_ids_are_unique_and_all_references_resolve(self):
        nodes = list(self.profile.iter())
        ids = [node.get("Id") for node in nodes if node.get("Id")]
        self.assertEqual(len(ids), len(set(ids)))
        old_ids = {node.get("Id") for source in (self.base, self.utc) for node in source.iter() if node.get("Id")}
        self.assertFalse(set(ids) & old_ids)
        definitions = {node.get("Id"): node.tag for node in nodes if node.get("Id")}
        for node in nodes:
            if node.tag in ("SystemCollectorId", "SystemProviderId", "EventCollectorId", "EventProviderId"):
                self.assertEqual(definitions.get(node.get("Value")), node.tag[:-2])
        active = {node.get("Value") for node in self.profile.findall(".//EventProviderId")}
        declared = {node.get("Id") for node in self.profile.findall(".//EventProvider")}
        self.assertEqual(active, declared)

    def test_profile_has_all_requested_sources_levels_and_only_known_masks(self):
        providers = {node.get("Name").lower(): node for node in self.profile.findall(".//EventProvider")}
        ai_guids = {
            "49c2c27c-fe2d-40bf-8c4e-c3fb518037e7",
            "434877dc-e809-46b5-9168-38c27e6e332e",
            "87410d84-3bac-53d7-7f6a-d3257c8cc59f",
            "929dd115-1ecb-4cb5-b060-ebd4983c421d",
            "3a26b1ff-7484-7484-7484-15261f42614d",
            "d766d9ff-112c-4dac-9247-241cf99d123f",
            "afe60d91-90d2-59bd-ebbf-d321f5691437",
            "45aa7ae8-974c-5bbd-d7b5-8ea567dac172",
        }
        for guid in ai_guids | {metrics.TIP_PROVIDER_GUID, metrics.INPUT_INJECT_GUID}:
            self.assertEqual(providers[guid].get("Level"), "5")
            self.assertIsNone(providers[guid].find("Keywords"))
            self.assertNotEqual(providers[guid].get("CaptureStateOnly"), "true")
        for name in ("perftrack", "utc"):
            self.assertEqual(providers[name].get("Level"), "4")
        shell = providers["30336ed4-e327-447c-9de0-51b652c86108"]
        self.assertEqual([item.get("Value") for item in shell.findall("Keywords/Keyword")], ["0x1000000000000"])
        onnx = providers["929dd115-1ecb-4cb5-b060-ebd4983c421d"]
        self.assertEqual(onnx.find("CaptureStateOnStart/Keyword").get("Value"), "0x1")
        self.assertEqual(onnx.find("CaptureStateOnSave/Keyword").get("Value"), "0x1")

    def test_profile_file_default_light_keywords_and_no_heavy_capture(self):
        profiles = self.profile.findall(".//Profile")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].get("LoggingMode"), "File")
        self.assertEqual(profiles[0].get("Default"), "true")
        keywords = {item.get("Value") for item in self.profile.findall(".//SystemProvider/Keywords/Keyword")}
        self.assertEqual(keywords, {"MemoryInfo", "MemoryInfoWS", "HardFaults", "ProcessThread", "Loader"})
        forbidden = {"HardwareCounter", "HardwareCounterId", "Stack", "Stacks", "StackCaching", "HeapEventProvider"}
        self.assertFalse({node.tag for node in self.profile.iter()} & forbidden)
        self.assertNotIn("DxgKrnl", ET.tostring(self.profile, encoding="unicode"))

    def test_profile_preserves_existing_merge_metadata_contract(self):
        expected = {item.get("Value") for item in self.base.findall(".//TraceMergeProperty/CustomEvents/CustomEvent")}
        observed = {item.get("Value") for item in self.profile.findall(".//TraceMergeProperty/CustomEvents/CustomEvent")}
        self.assertEqual(observed, expected)
        self.assertTrue({"EventMetadata", "PerfTrackMetadata"}.issubset(observed))


if __name__ == "__main__":
    unittest.main()
