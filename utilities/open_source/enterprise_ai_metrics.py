# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root.

"""Offline, standard-library-only helpers for enterprise_ai observations.

No function starts capture, runs an extractor, or imports HOBL. Callers retain
the original ETL, raw PerfParser CSV, and tracerpt XML as separate artifacts.
Durations in the legacy ``PT,Metric,Duration`` schema are milliseconds; PT IDs
remain numeric strings, including any leading zeroes.

``read_trace`` makes two streaming passes over immutable tracerpt event XML.
Its memory use is proportional to XML depth, one event, and distinct
provider/event identities, not the number of events. Explicit EventName
metadata is supported; task numbers, message text, and payload strings are
never guessed to be event names. These decoders have synthetic-test coverage,
not real-ETL/DUT validation.

Successful parsing is not proof of semantic completion or a loss-free trace.
Event XML alone cannot establish trace loss: read_trace always reports that
quality gate as unknown. The caller must validate loss from an independently
validated capture/summary contract before declaring the run valid.
"""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import math
import re
import statistics
import uuid
import xml.etree.ElementTree as ET


INPUT_INJECT_GUID = "150a3791-7792-452d-858c-15d647ecb48f"
TIP_PROVIDER_GUID = "50109fbd-6d85-5815-731e-c907eca1607b"
WINDOWS_SEARCH_CORE_GUID = "49c2c27c-fe2d-40bf-8c4e-c3fb518037e7"
RAW_COLUMNS = ("Scenario", "Metric", "Duration")
PT_COLUMNS = ("PT", "Metric", "Duration")

_PT_NAME = re.compile(r"^PT(?:Sdw)?_([0-9]+)_(.+)$")
_PT_ID = re.compile(r"^[0-9]+$")
_NUMBER = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TIMESTAMP = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
_SEARCH_EVENTS = ("IndexItemInit", "IndexItemDataComplete")
_TIP_TEST_CASE = "TypeToSearchTestTopResultRendered"


class MetricsError(ValueError):
    """Input is malformed, ambiguous, or missing mandatory observations."""


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _iter_elements(path, element_name):
    """Detach completed elements, including their empty shells, from parents."""
    try:
        with open(path, "rb") as source:
            stack = []
            selected = None
            for action, element in ET.iterparse(source, events=("start", "end")):
                if action == "start":
                    stack.append(element)
                    if selected is None and _local_name(element.tag) == element_name:
                        selected = element
                    continue
                if element is selected:
                    try:
                        yield element
                    finally:
                        selected = None
                        element.clear()
                        if len(stack) > 1:
                            stack[-2].remove(element)
                elif selected is None:
                    element.clear()
                    if len(stack) > 1:
                        stack[-2].remove(element)
                stack.pop()
    except (OSError, ET.ParseError) as exc:
        raise MetricsError(f"Cannot read XML {path}: {exc}") from exc


def _pt_id(value):
    if not isinstance(value, str) or not _PT_ID.fullmatch(value):
        raise MetricsError(f"PT ID must be a numeric string: {value!r}")
    return value


def _duration(value, context):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value.strip()):
        raise MetricsError(f"{context}: duration must be a finite nonnegative number")
    value = value.strip()
    try:
        number = float(value)
        decimal = Decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise MetricsError(f"{context}: duration is outside the supported numeric range") from exc
    if not math.isfinite(number) or number < 0 or decimal < 0:
        raise MetricsError(f"{context}: duration must be a finite nonnegative number")
    return value


def build_manifest_map(path):
    """Return alias -> frozenset of numeric PT IDs from manifest scenario attrs.

    Full scenarioname, ptscenarioname, and the parser's suffix-stripped name are
    aliases. Collisions are retained, not resolved by XML order. Non-PT
    scenarios are ignored. No triggers/state machines are interpreted.
    """
    aliases = defaultdict(set)
    for element in _iter_elements(path, "scenario"):
        name = element.get("scenarioname", "").strip()
        match = _PT_NAME.fullmatch(name)
        if not match:
            if name.startswith(("PT_", "PTSdw_")):
                raise MetricsError(f"Malformed manifest PT scenario: {name!r}")
            continue
        pt, suffix = match.groups()
        names = {name, element.get("ptscenarioname", "").strip()}
        if "_" in suffix:
            names.add(suffix.rsplit("_", 1)[0].strip())
        for alias in names - {""}:
            aliases[alias].add(pt)
    if not aliases:
        raise MetricsError("Manifest contains no PT scenarios")
    return {alias: frozenset(ids) for alias, ids in sorted(aliases.items())}


def _manifest_map(mapping):
    if not hasattr(mapping, "items") or not mapping:
        raise MetricsError("A nonempty manifest alias map is required")
    result = {}
    for alias, ids in mapping.items():
        if not isinstance(alias, str) or not alias.strip():
            raise MetricsError("Manifest aliases must be nonempty strings")
        if isinstance(ids, str):
            ids = (ids,)
        try:
            values = frozenset(_pt_id(value) for value in ids)
        except TypeError as exc:
            raise MetricsError("Manifest values must contain numeric PT strings") from exc
        if not values:
            raise MetricsError(f"Manifest alias has no PT IDs: {alias!r}")
        result[alias.strip()] = result.get(alias.strip(), frozenset()) | values
    return result


def read_perf_csv(path, manifest_map):
    """Read strict raw CSV and return legacy-shaped string rows without writing.

    The map may contain one ID string or an iterable of ID strings per alias.
    Only IDs in this map are whitelisted. An explicit unknown PT is filtered,
    not rescued through its Metric. A known explicit PT can disambiguate an
    alias; conflicting identifiers or unresolved collisions raise MetricsError.
    Every input row, including filtered rows, must have a valid duration.
    """
    aliases = _manifest_map(manifest_map)
    whitelist = frozenset().union(*aliases.values())
    result = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            if tuple(next(reader, ())) != RAW_COLUMNS:
                raise MetricsError("Raw CSV header must be exactly Scenario,Metric,Duration")
            for fields in reader:
                context = f"Raw CSV line {reader.line_num}"
                if len(fields) != len(RAW_COLUMNS):
                    raise MetricsError(f"{context}: expected exactly three fields")
                scenario, metric, duration = (field.strip() for field in fields)
                if not scenario or not metric:
                    raise MetricsError(f"{context}: Scenario and Metric must be nonempty")
                duration = _duration(duration, context)
                explicit = _PT_NAME.fullmatch(scenario)
                if scenario.startswith(("PT_", "PTSdw_")) and not explicit:
                    raise MetricsError(f"{context}: malformed explicit PT scenario")
                candidates = aliases.get(scenario, frozenset())
                if explicit:
                    pt = explicit.group(1)
                    if pt not in whitelist:
                        continue
                    if candidates and pt not in candidates:
                        raise MetricsError(f"{context}: conflicting scenario PT identifiers")
                    candidates = frozenset((pt,))
                metric_ids = aliases.get(metric, frozenset())
                if candidates and metric_ids:
                    candidates = candidates & metric_ids
                    if not candidates:
                        raise MetricsError(f"{context}: conflicting Scenario and Metric PT IDs")
                else:
                    candidates = candidates or metric_ids
                if not candidates:
                    continue
                if len(candidates) != 1:
                    raise MetricsError(f"{context}: ambiguous PT IDs {sorted(candidates)}")
                result.append({"PT": next(iter(candidates)), "Metric": metric, "Duration": duration})
    except (OSError, UnicodeError, csv.Error) as exc:
        raise MetricsError(f"Cannot read raw CSV {path}: {exc}") from exc
    return result


def _validated_row(row, context):
    if not hasattr(row, "keys") or set(row) != set(PT_COLUMNS):
        raise MetricsError(f"{context}: expected only PT,Metric,Duration")
    pt = _pt_id(row["PT"])
    metric = row["Metric"]
    if not isinstance(metric, str) or not metric.strip():
        raise MetricsError(f"{context}: Metric must be nonempty")
    return {"PT": pt, "Metric": metric.strip(), "Duration": _duration(row["Duration"], context)}


def write_perf_csv(path, rows):
    """Write only PT,Metric,Duration; caller must use a path distinct from raw CSV.

    All rows are validated before opening the destination. This helper never
    renames a raw source into a processed output on failure.
    """
    rows = [_validated_row(row, f"PT row {index}") for index, row in enumerate(rows, 1)]
    try:
        with open(path, "w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=PT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise MetricsError(f"Cannot write PT CSV {path}: {exc}") from exc


def summarize_pt(rows, required_pts=(), include_p95=False):
    """Return status/errors, missing_required_pts, and a per-ID ``pts`` mapping.

    Each PT has count, metrics (names), status, and median_ms. Missing required
    PTs have count=0 and median_ms=None, with top-level status=error. No rows is
    also an error. Optional p95_ms uses nearest rank and is emitted only with
    at least 100 observations; p95_status explains smaller samples.
    """
    required = {_pt_id(pt) for pt in required_pts}
    groups = defaultdict(list)
    names = defaultdict(set)
    for index, source_row in enumerate(rows, 1):
        row = _validated_row(source_row, f"PT row {index}")
        groups[row["PT"]].append(Decimal(row["Duration"]))
        names[row["PT"]].add(row["Metric"])
    missing = sorted(required - groups.keys())
    errors = [f"Missing required PT {pt}" for pt in missing]
    if not groups:
        errors.append("No whitelisted PT observations")
    summaries = {}
    for pt in sorted(groups.keys() | required):
        values = sorted(groups.get(pt, ()))
        entry = {
            "status": "observed" if values else "missing",
            "count": len(values),
            "metrics": sorted(names.get(pt, ())),
            "median_ms": float(statistics.median(values)) if values else None,
        }
        if include_p95:
            entry["p95_status"] = "observed" if len(values) >= 100 else "insufficient_observations"
            if len(values) >= 100:
                entry["p95_ms"] = float(values[(95 * len(values) + 99) // 100 - 1])
        summaries[pt] = entry
    return {
        "status": "error" if errors else "ok",
        "errors": errors,
        "missing_required_pts": missing,
        "pts": summaries,
    }


def _guid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MetricsError(f"Invalid provider GUID: {value!r}") from exc


def _timestamp(value):
    match = _TIMESTAMP.fullmatch(value or "")
    if not match:
        raise MetricsError(f"Timestamp must be timezone-qualified ISO 8601: {value!r}")
    base, fraction, offset = match.groups()
    if offset != "Z" and (int(offset[1:3]) > 23 or int(offset[4:]) > 59):
        raise MetricsError(f"Invalid timestamp offset: {value!r}")
    try:
        instant = datetime.fromisoformat(base + ("+00:00" if offset == "Z" else offset))
        utc = instant.astimezone(timezone.utc)
        elapsed = utc - datetime(1, 1, 1, tzinfo=timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise MetricsError(f"Invalid timestamp: {value!r}") from exc
    # Integer nanoseconds preserve tracerpt's seven-digit ETW fractional times.
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def _children(element, name):
    return [child for child in element if _local_name(child.tag) == name]


def _only_child(element, name):
    children = _children(element, name)
    if len(children) != 1:
        raise MetricsError(f"Event requires exactly one {name}")
    return children[0]


def _decode_event(element):
    system = _only_child(element, "System")
    provider = _only_child(system, "Provider")
    guid = _guid(provider.get("Guid")) if provider.get("Guid") is not None else None
    provider_name = provider.get("Name", "").strip()
    if not guid and not provider_name:
        raise MetricsError("Event provider identity is missing")
    timestamp = _only_child(system, "TimeCreated").get("SystemTime")
    event_ids = _children(system, "EventID")
    if len(event_ids) > 1:
        raise MetricsError("Event has duplicate EventID fields")
    event_id = (event_ids[0].text or "").strip() if event_ids else None
    if event_id is not None and not _PT_ID.fullmatch(event_id):
        raise MetricsError(f"EventID must be a nonnegative integer: {event_id!r}")
    event_names = [element.get("EventName")] if element.get("EventName") is not None else []
    for container in [system] + _children(element, "RenderingInfo"):
        event_names.extend(child.text or "" for child in _children(container, "EventName"))
    names = {name.strip() for name in event_names if name.strip()}
    if len(names) > 1:
        raise MetricsError(f"Conflicting EventName metadata: {sorted(names)}")
    data = defaultdict(list)
    values = []
    for container in _children(element, "EventData") + _children(element, "UserData"):
        for child in container.iter():
            if child is container or len(child) or child.text is None:
                continue
            values.append(child.text)
            data[child.get("Name") or _local_name(child.tag)].append(child.text)
    return {
        "provider": guid or "name:" + provider_name.casefold(),
        "guid": guid,
        "timestamp": timestamp,
        "time_ns": _timestamp(timestamp),
        "event_name": next(iter(names), None),
        "event_id": event_id,
        "data": data,
        "values": values,
    }


def _events(path):
    for element in _iter_elements(path, "Event"):
        yield _decode_event(element)


def _marker_values(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise MetricsError("run_id must be 1-128 ASCII letters/digits/dots/underscores/hyphens")
    return {phase: f"enterprise_ai:{run_id}:{phase}" for phase in ("measurement_begin", "measurement_end")}


def _record_markers(event, expected, markers):
    if event["guid"] != INPUT_INJECT_GUID:
        return
    for phase, value in expected.items():
        count = event["values"].count(value)
        if count > 1 or (count and phase in markers):
            raise MetricsError(f"Duplicate {phase} marker")
        if count:
            markers[phase] = {"value": value, "timestamp": event["timestamp"], "time_ns": event["time_ns"]}


def _read_marker_window(path, run_id):
    expected = _marker_values(run_id)
    markers = {}
    for event in _events(path):
        _record_markers(event, expected, markers)
    missing = expected.keys() - markers.keys()
    if missing:
        raise MetricsError(f"Missing InputInject marker(s): {', '.join(sorted(missing))}")
    begin = markers["measurement_begin"]["time_ns"]
    end = markers["measurement_end"]["time_ns"]
    if end <= begin:
        raise MetricsError("measurement_end timestamp must be strictly after measurement_begin")
    return markers, begin, end


def read_trace(path, run_id, required_providers=()):
    """Return ETL-marker runtime and observed events in the closed [begin,end] window.

    Exactly one begin/end payload value for run_id is required from the
    InputInject GUID (a provider Name cannot substitute). Other run IDs and
    other-provider lookalikes do not delimit the window. Unsorted event records
    are supported: ordering and inclusion use timestamps, not XML order.

    Optional required_providers is an iterable of GUIDs which must occur inside
    the window (e.g. WINDOWS_SEARCH_CORE_GUID for an indexing-enabled cell).
    All events require source identity and a valid timezone-qualified timestamp.
    Provider counts use canonical GUID keys, or name:<casefolded name> when a
    GUID is absent. Event-name and event-ID counts are kept separate.

    The result has markers, scenario_runtime (seconds), provider_counts,
    event_counts, event_id_counts, missing_event_name_counts, search, and
    trace_loss. Search's envelope_s is only the first-to-last *observed* named
    indexing-event envelope; no request pairing, throughput, or semantic
    document completion is inferred. status remains unverified because native
    completion and trace loss cannot be established by this decoder.
    """
    required = {_guid(provider) for provider in required_providers}
    markers, begin, end = _read_marker_window(path, run_id)
    provider_counts = Counter()
    event_counts = defaultdict(Counter)
    event_id_counts = defaultdict(Counter)
    missing_names = Counter()
    missing_ids = Counter()
    observed_markers = {}
    expected = _marker_values(run_id)
    search_first = search_last = None
    for event in _events(path):
        _record_markers(event, expected, observed_markers)
        if not begin <= event["time_ns"] <= end:
            continue
        provider = event["provider"]
        provider_counts[provider] += 1
        if event["event_name"] is None:
            missing_names[provider] += 1
        else:
            event_counts[provider][event["event_name"]] += 1
        if event["event_id"] is None:
            missing_ids[provider] += 1
        else:
            event_id_counts[provider][event["event_id"]] += 1
        if provider == WINDOWS_SEARCH_CORE_GUID and event["event_name"] in _SEARCH_EVENTS:
            point = (event["time_ns"], event["timestamp"])
            search_first = min(search_first, point) if search_first is not None else point
            search_last = max(search_last, point) if search_last is not None else point
    if observed_markers != markers:
        raise MetricsError("Trace markers changed between XML passes")
    missing_sources = sorted(required - provider_counts.keys())
    if missing_sources:
        raise MetricsError(f"Required providers not observed in measurement window: {missing_sources}")
    search_names = event_counts.get(WINDOWS_SEARCH_CORE_GUID, {})
    search_counts = {name: search_names.get(name, 0) for name in _SEARCH_EVENTS}
    known_count = sum(search_counts.values())
    missing_search_names = missing_names[WINDOWS_SEARCH_CORE_GUID]
    if not provider_counts[WINDOWS_SEARCH_CORE_GUID]:
        search_status = "provider_not_observed"
    elif all(search_counts.values()):
        search_status = "observed"
    elif known_count:
        search_status = "partial"
    elif missing_search_names:
        search_status = "event_names_unavailable"
    else:
        search_status = "expected_events_not_observed"
    return {
        "status": "unverified",
        "source_validation": "passed",
        "run_id": run_id,
        "scenario_runtime": (end - begin) / 1_000_000_000,
        "timebase": "ETL System.TimeCreated.SystemTime",
        "window_inclusion": "closed",
        "markers": {
            phase: {"value": marker["value"], "timestamp": marker["timestamp"]}
            for phase, marker in markers.items()
        },
        "provider_counts": dict(sorted(provider_counts.items())),
        "event_counts": {key: dict(sorted(value.items())) for key, value in sorted(event_counts.items())},
        "event_id_counts": {key: dict(sorted(value.items())) for key, value in sorted(event_id_counts.items())},
        "missing_event_name_counts": dict(sorted(missing_names.items())),
        "missing_event_id_counts": dict(sorted(missing_ids.items())),
        "search": {
            "status": search_status,
            "event_counts": search_counts,
            "observed_event_count": known_count,
            "unknown_event_names": {
                name: count for name, count in sorted(search_names.items()) if name not in _SEARCH_EVENTS
            },
            "missing_event_name_count": missing_search_names,
            "first_event_time": search_first[1] if search_first else None,
            "last_event_time": search_last[1] if search_last else None,
            "envelope_s": (search_last[0] - search_first[0]) / 1_000_000_000 if known_count >= 2 else None,
            "completion_status": "unverified",
            "completed_semantic_documents": None,
            "per_request_latency_ms": None,
        },
        "trace_loss": {
            "status": "unknown",
            "events_lost": None,
            "buffers_lost": None,
            "reason": "Event XML is not a validated trace-loss summary",
        },
    }


def _tip_field(event, *names):
    values = [value for name in names for value in event["data"].get(name, ())]
    if len(values) > 1:
        raise MetricsError(f"Ambiguous TIP field: {names[0]}")
    return values[0] if values else None


def iter_tip_rows(path, run_id):
    """Yield optional PT_10010/TopResultRender legacy rows inside the marker window.

    Only the exact TIP GUID, TypeToSearchTestTopResultRendered testCaseName,
    completionKind=1, and finite nonnegative durationMs are supported. Missing
    required fields on this test case fail instead of guessing. Other TIP test
    cases/completion kinds yield no rows. This is foreground Type-to-Search,
    NOT semantic-search completion. Callers must prevent double counting with
    PerfParser results and enforce their manifest whitelist before appending.
    """
    _, begin, end = _read_marker_window(path, run_id)
    for event in _events(path):
        if event["guid"] != TIP_PROVIDER_GUID or not begin <= event["time_ns"] <= end:
            continue
        test_case = _tip_field(event, "testCaseName", "TestCaseName")
        if test_case != _TIP_TEST_CASE:
            continue
        completion = _tip_field(event, "completionKind", "CompletionKind")
        if completion is None or not _PT_ID.fullmatch(completion):
            raise MetricsError("Matching TIP test case requires numeric completionKind")
        if completion != "1":
            continue
        duration = _duration(_tip_field(event, "durationMs", "DurationMs"), "Matching TIP test case")
        yield {"PT": "10010", "Metric": "TopResultRender", "Duration": duration}
