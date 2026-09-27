# Enterprise AI workload-impact scenario

Moving to another HOST? Start with the [cross-host handoff guide](HOBL_EnterpriseAI_Handoff.md).

## Current implementation boundary

`enterprise_ai` is a separate, EC-derived scenario. The existing `enterprise_collab`,
`perf_stress`, `mincp_base`, their defaults, and donor branch histories are unchanged.

Implemented:

- The current EC foreground action JSON, with explicit control cells and fixed
  observation boundaries.
- An isolated native Windows **indexing-load** library: staged/checksummed corpus,
  preflight, owned IndexUpdater scope, ingestion after trace start, and cleanup.
- Optional deterministic CPU workers with an isolated Python environment,
  readiness, telemetry, and bounded process-identity-based cleanup.
- A new `GTPLight_EnterpriseAI.wprp` profile and offline PT/AI observation parsing.
- Quality/summary output that distinguishes observation from validated performance.
- A reproducibly shuffled, preview-by-default experiment plan and safe local tests.

**Not validated on a DUT yet:** native provider schemas/keywords, actual Search
readiness values, semantic completion, capture overhead, Windows/runtime behavior,
or Intel/AMD results. Unit tests use synthetic data and mocks; they are not hardware
or benchmark validation.

**Gated:** measured semantic queries, Click to Do integration, genuine model/API
runners, and optional power/GPU/NPU attribution. An ordinary Explorer filename
search is never used as a semantic-search substitute. No proprietary FunGates
source, model package, or decoder is copied into HOBL.

## Safety and prerequisites

Use an exclusive **remote Windows DUT with a dedicated test account**, never the
developer workstation. Local execution, loopback/host addresses, and missing
`enterprise_ai:dedicated_dut=1` consent are rejected before scenario RPC setup.
The underlying HOBL framework assumes it owns the DUT and its WPR session.
Do not run alongside another HOBL study, an unrelated WPR session, or another user.

The first implementation uses only `run_report` plus its automatically registered
`enterprise_ai_metrics` tool. Extra tools (including the usual `power_light` default)
are rejected rather than silently adding capture overhead or changing power policy.
Use a separate protocol after validating additional tool/profile combinations.
The scenario explicitly selects file-mode capture for its file-mode-only WPR profile.

Prerequisites for all cells:

1. Normal HOBL host/DUT setup, RPC plugins including InputInject, matching versions,
   an approved device profile and the relevant EC app/account prerequisites.
2. WPR on the DUT, `tracerpt.exe` on the analysis host, and the supplied ParseUtc
   package (executable, DLLs, runtime configuration, and applicable WPT files).
3. UTC configured for `StressUtcPerftrack.xml`, or explicit `configure_utc=1`.
   Office telemetry settings must independently support the selected Office PTs;
   this scenario does not click through Office account/privacy dialogs.

Additional indexing prerequisites (cells C, D, E, F):

- A supported x64 DUT and .NET 8 x64 runtime for the supplied IndexUpdater apphost.
  ARM64 indexing is gated rather than silently emulating an unvalidated backend.
- Windows Search already running and the semantic feature/model/language/policy
  configured for that OS build.
- An **operator-validated** enabled `SemanticIndexingStatus` DWORD value supplied as
  `semantic_ready_value`. No numeric meaning is assumed from the old donor helper.
  Equality is a preflight condition, not proof of corpus indexing completion.
- An approved local host corpus directory, at most 1,000 regular files / 256 MiB,
  with no symlinks or reparse points. Do not point at a home directory or drive root.
  Keep content stable and approved for copying to the DUT; no corpus is bundled.
- The four existing `utilities\proprietary\IndexUpdater` files. Their hashes and the
  corpus inventory are recorded for provenance.

Additional stress prerequisites (cells B, E, F):

- pyenv-win already installed and discoverable on the DUT. Prep reports an
  actionable failure if it is missing; it does not replace pyenv with winget Python.
- Python 3.12.10 (x64) or 3.12.10-arm is installed conditionally, without forcing
  reinstall or changing the global selection. The scenario venv is under the
  script drive's `hobl_bin\enterprise_ai_resources\.venv`.
- The worker uses the standard library, so no shared `pip install`, NumPy, or pip
  upgrade is needed. It is a new calibrated arithmetic load, **not numerically
  equivalent** to the legacy matrix-multiply stress workload.
- The initial stress sampler supports a single Windows processor group. Unsupported
  group configurations fail rather than being reported as whole-system CPU.

## Configure and preview

Copy the settings from
[`profile_templates\enterprise_ai.ini`](../../../profile_templates/enterprise_ai.ini)
into your dedicated DUT profile outside the repository. Retain the existing
account/app/network configuration without committing credentials.

Important settings:

| Setting | Meaning |
|---|---|
| `dedicated_dut=0` | Safe default; change to `1` only for the dedicated DUT |
| `condition=A` | Control cell; see the matrix below |
| `protocol=indexing` | Only executable AI protocol in this milestone |
| `measurement_seconds=1800` | Fixed observation budget; foreground overrun invalidates the run |
| `settle_seconds=10` | Trace settling outside the measured window |
| `stress_workers=1`, `stress_duty_cycle=0.25` | Fixed workload configuration, not a promise of 25% system CPU |
| `corpus_dir`, `semantic_ready_value` | Required when the selected cell enables indexing |
| `configure_utc=0` | Verify pre-existing manifest/policy without changing it |
| `configure_utc=1` | Snapshot, apply and restore only the selected UTC policy/files/service state |
| `validation_mode=capture` | Evidence collection, explicitly inconclusive and excluded from perf gates |
| `validation_mode=strict` | Requires validated trace evidence; never assumes unknown loss is zero |
| `required_pts=8805 8806 8807` | Required foreground PT IDs; tune only against proven actions on the target build |

Preview the matrix (no workloads):

```powershell
.\testplans\enterprise_ai.ps1 -Profile '<full-path-to-dedicated-profile.ini>'
```

After capability/telemetry checks and explicit lab approval, add `-Execute`. The
plan records its shuffle seed, uses one attempt per cell, and stops on failures
rather than quietly replacing failed observations with successful retries.

```powershell
.\testplans\enterprise_ai.ps1 -Profile '<full-path-to-dedicated-profile.ini>' `
    -Conditions A,B,C,D,E -Repetitions 5 -MeasurementSeconds 1800 -Execute
```

For a single explicitly configured DUT run:

```powershell
.\hobl.cmd -p '<full-path-to-dedicated-profile.ini>' -s enterprise_ai `
    enterprise_ai:condition=D global:iterations=1 global:attempts=1
```

`query` and `model` fail with a clear gated-feature message before any workload,
including on a control cell. They do not invoke a placeholder runner, mislabel an
indexing control as another protocol, or fabricate success.

## Controls and timing

| Cell | Foreground EC | Stress | Indexing |
|---|---|---|---|
| A | On | Off | Off |
| B | On | On | Off |
| C | Off | Off | On |
| D | On | Off | On |
| E | On | On | On |
| F (diagnostic) | Off | On | On |

D vs A and E vs B estimate AI effects on foreground responsiveness. D vs C and
E vs D characterize enterprise/stress effects on AI observations. Compare only
equivalent device/build/backend/corpus/capture/cache conditions.

Preparation and corpus transfer occur before WPR. The owned index directory stays
empty and unregistered until after `measurement_begin`. The measured ingestion
moves staged files into that scope; it does **not** time network upload as indexing.
`ingestion_dispatch_seconds` is dispatch time, not model inference or indexing latency.

One copied EC foreground sequence runs within the fixed observation budget. The
remaining time is an observation interval, not extra AI work. Foreground overrun
fails instead of silently producing a different-duration cell.
`scenario_runtime` is derived from exact run-ID InputInject begin/end events in
the ETL, not from the sum of overlapping tasks. Host monotonic timing is diagnostic.
The report rejects marker-window drift exceeding one second from the budget.

CPU stress uses fixed worker count/duty across matched conditions. Calibrate it
alone, then freeze configuration; do not back off in response to AI CPU usage.
Samples are controller-relative and labeled **stress lifetime**; they are not
claimed to be ETL-synchronized per-action power/resource measurements.

The initial copied foreground baseline enables Office/Explorer/Snipping/Settings.
Legacy EC OneDrive download/upload branches and its telemetry/privacy UI mutation
branches are excluded from the new JSON. This prevents unowned thread/process
cleanup and uncontrolled network traffic. Compare against EC with those same
branches disabled; never compare mismatched defaults as if they were identical.

## Results and quality

In the HOBL result directory:

- Original `<testname>.etl`.
- `enterprise_ai_run.json`: control, budget, source/helper/corpus hashes, preflight,
  dispatch counts, lifecycle status and cleanup errors.
- `enterprise_ai_raw\<run_id>\`: raw PerfParser CSV, preserved `PT,Metric,Duration`
  CSV, tracerpt XML/summary, native/stress states, JSONL samples, and decoder logs.
- `enterprise_ai_summary.csv`: headerless key/value rows for the existing rollup.
- `enterprise_ai_quality.json`: status and explicit coverage/validation reasons.

The scenario narrows `run_report:files` to scalar summary and standard run/config
metadata. Raw multi-column CSVs are not included in the wildcard rollup.
Scenario-specific tool/report overrides are restored when the instance completes.

PT counts and medians are retained; p95 is emitted only with at least 100
observations. That threshold alone is not statistical independence: use multiple
paired runs and a run-grouped uncertainty method. Do not publish a tiny pilot's
tail statistic as a production gate.

Native Search event counts and a first/last **observed event envelope** do not prove
per-document semantic completion or query correctness. Missing event names are
reported as unavailable. Model/TTFT/token metrics are not invented from indexing.

PerfParser's raw output has no per-row timestamp in the supported contract. Its PT
summary is therefore labeled whole-trace/unverified until a validated decoder
proves measurement-window attribution. Merely renaming a CSV cannot fix this.

### Optional approved validation boundary

`enterprise_ai_metrics:trace_validator` may point to an approved absolute `.exe`
on the analysis host. It is invoked without a shell:

```text
<validator.exe> <captured.etl> <validated_trace_evidence.json>
```

The validator must return exit 0 and a JSON object bound to **that exact ETL SHA-256
and run ID**:

```json
{
  "schema_version": 1,
  "run_id": "<the captured 32-character run ID>",
  "etl_sha256": "<SHA-256 of the captured ETL>",
  "validator": { "name": "<approved decoder>", "version": "<validated version>" },
  "validation_basis": "dut-validated",
  "trace_loss": { "events_lost": 0, "buffers_lost": 0 },
  "foreground_pt_window_verified": true,
  "ai_model": "<observed model>",
  "ai_device": "<observed backend>",
  "semantic_completion": {
    "verified": true,
    "expected_documents": 10,
    "completed_documents": 10,
    "source": "<validated event/correlation contract>"
  }
}
```

This is a schema illustration, **not usable evidence**. Do not manufacture it or
reuse it across traces. Semantic fields apply to AI cells and must match the actual
corpus count; foreground attribution applies to foreground cells. Exact field
validation and all missing/invalid cases are covered by safe synthetic tests.
Inspect the current helper contract before writing a new decoder.

No validator is bundled because neither the donor code nor the supplied FunGates
parser establishes these native per-operation/trace-loss contracts. Without
validated evidence, capture results remain inconclusive and strict mode cannot
pass. Proprietary harness extraction remains an external, authorized artifact.

## Failure handling and recovery

Every run owns a unique directory below the script drive's
`hobl_bin\enterprise_ai_resources\runs\<run_id>`. An exclusive `active-run.json`
lease prevents another run from hiding an incomplete cleanup.

Normal order:

1. End the measurement markers.
2. Stop/join the owned stress workers and unregister only the owned index scope.
3. Clean up the selected EC foreground apps outside the measured window, then
   finalize the core ETL.
4. Restore UTC files, the original registry values and original DiagTrack state.
5. Collect finalized artifacts, release the lease, and parse/report.

Cleanup also runs on setup/run failures. It never force-reinstalls shared Python,
deletes model caches, clears the global Search index, or kills all Python processes.
Failures to unregister/restore/stop remain explicit and retain the lease.

After an abrupt host/DUT failure, inspect `enterprise_ai_run.json`, the leased run
directory, and its snapshots. Recover **that same run ID** using the staged
`stress_control.ps1 -Action Stop`, `native_indexing.ps1 -Action Stop`,
then save/stop this run's WPR session if trace finalization was interrupted.
Only after that, use `utc_control.ps1 -Action Restore` and
`workspace.ps1 -Action Collect/Release`.
Each requires the exact owned `-RunDirectory` and `-RunId`. Do not delete a lease
or snapshots to bypass a failed restore. Do not run recovery on an unrelated machine.
The operator must confirm indexer quiescence before the next comparison; removing
a scope is not evidence that every native background operation instantly stopped.

## Safe local verification

Use an existing real Python executable (or `pyenv which python`), not a pyenv shim:

```powershell
.\tests\run_enterprise_ai_checks.ps1 -PythonExecutable '<absolute-python.exe-path>'
```

These checks parse Python/PowerShell, validate transitive JSON/assets/provider
references, and exercise configuration, mocked lifecycle, worker ownership,
synthetic trace parsing, and report contracts. No DUT, stress loop, WPR session,
native indexing, service change, or proprietary binary is run.

Before declaring the lab implementation complete, validate a known semantic
action on a dedicated x64 DUT, actual providers/event names and loss accounting,
approved corpus semantics, three consecutive lifecycle runs, failure recovery,
light-profile overhead and paired-control noise. Only then extend to a comparable
AMD DUT and the separately gated native-query/model/API milestones.
