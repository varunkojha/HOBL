# Enterprise AI: continue on another HOST

## Read this first

Repository: https://github.com/varunkojha/HOBL

Branch: `ai_enterprise`

The implementation is an **indexing-first, capture-first foundation**, not a
completed or hardware-validated semantic-search benchmark. The last implementation
validation passed 152 safe automated tests plus Python/PowerShell parsing and
JSON/provider/reference checks. No DUT workload or performance collection was run
on the development workstation.

Start with [the operator guide](HOBL_EnterpriseAI.md). The original research plan
and illustrated Word/PDF documents are supplied separately in the local handoff
bundle. That plan is a historical design snapshot; this guide and the operator
guide describe the implemented boundary.

Keep the HOST and DUT distinct. Run HOBL from the HOST; the scenario sends the
workload to an exclusive remote Windows DUT with a dedicated test account.

## 1. Bring the code to the HOST

### Recommended: a separate clone

Run from your chosen parent folder on the new HOST:

```powershell
git clone --branch ai_enterprise https://github.com/varunkojha/HOBL.git HOBL
Set-Location .\HOBL
git remote add upstream https://github.com/microsoft/HOBL.git
git config --local remote.pushDefault origin
git config --local fetch.all true
git config --local pull.ff only
git --no-pager status --short --branch
git --no-pager rev-parse HEAD
```

Compare the commit with `handoff-version.json` in the transfer bundle. A later
branch revision is not automatically the same tested snapshot.

This keeps pulls/pushes on your fork and exposes Microsoft as `upstream` for
intentional updates. Do not merge/rebase the old donor branches as part of setup.

### Existing clone whose origin is already your fork

First inspect local changes and remotes:

```powershell
git --no-pager status --short
git remote -v
git fetch origin
git switch ai_enterprise
git pull --ff-only origin ai_enterprise
```

If the local branch does not exist, use `git switch --track origin/ai_enterprise`.
If the working tree has changes, the branch diverges, or `origin` is not
`varunkojha/HOBL`, stop and resolve that explicitly. Do not reset/clean/force-push
to make setup succeed.

### Existing clone whose origin is microsoft/HOBL

Do not silently replace its origin. Prefer the separate clone above. Alternatively,
if neither the `varun` remote nor a local `ai_enterprise` branch exists:

```powershell
git remote add varun https://github.com/varunkojha/HOBL.git
git fetch varun
git switch --track -c ai_enterprise varun/ai_enterprise
git config --local branch.ai_enterprise.pushRemote varun
```

Inspect and reuse existing remotes/branches rather than overwriting them. Subsequent
pull/push commands in this clone should explicitly use `varun`, not Microsoft.

Authenticate on the new HOST using your own GitHub account and normal browser/Git
Credential Manager flow. Do not copy tokens, credential stores, or passwords from
the old machine. VS Code sign-in and command-line Git authentication are separate.

## 2. Files that arrive through Git

Paths below are relative to the new checkout. Clone the **whole repository**, not
just the new scenario: it depends on existing HOBL core, app libraries, assets,
RPC plugins, and proprietary utilities already supplied with the project.

| Path | Purpose |
|---|---|
| `scenarios\windows\enterprise_ai\` | EC-derived action JSON, defaults, orchestrator and image template |
| `scenarios\windows\_library\enterprise_ai\` | Native indexing, owned stress and lifecycle helpers |
| `providers\GTPLight_EnterpriseAI.wprp` | Active lightweight capture profile |
| `tools\enterprise_ai_metrics.py` | HOBL capture/extraction/report integration |
| `utilities\open_source\enterprise_ai_metrics.py` | Offline PT and trace-observation parsing |
| `utilities\open_source\enterprise_ai_report.py` | Strict quality checks and scalar report writer |
| `profile_templates\enterprise_ai.ini` | Safe, incomplete configuration template; not a runnable DUT profile |
| `testplans\enterprise_ai.ps1` | Seeded control matrix; preview by default |
| `tests\test_enterprise_ai_*.py` | Synthetic/mock tests |
| `tests\run_enterprise_ai_checks.ps1` | Safe local validation entry point |
| `docs\support\docs\HOBL_EnterpriseAI.md` | Full operator, metrics, safety and recovery guide |
| `docs\support\docs\HOBL_EnterpriseAI_Handoff.md` | This portable continuation guide |

All new PowerShell files:

```text
testplans\enterprise_ai.ps1
tests\run_enterprise_ai_checks.ps1
scenarios\windows\_library\enterprise_ai\prep.ps1
scenarios\windows\_library\enterprise_ai\stress_control.ps1
scenarios\windows\_library\enterprise_ai\native_indexing.ps1
scenarios\windows\_library\enterprise_ai\utc_control.ps1
scenarios\windows\_library\enterprise_ai\workspace.ps1
scenarios\windows\_library\enterprise_ai\run_common.ps1
```

Only the test runner and matrix preview are intended for direct HOST use.
The library scripts are staged/invoked on the DUT by the scenario. Do not execute
`prep.ps1`, `native_indexing.ps1`, `utc_control.ps1`, or stress control on the HOST
just because their files are present there.

Existing binary dependencies are already tracked in Git:

- `utilities\proprietary\IndexUpdater\`: all four executable/DLL/configuration
  files, not just `IndexUpdater.exe`.
- `utilities\proprietary\ParseUtc\`: the entire package, including its DLLs,
  manifests, `DisableAllUploads.json`, and `wpt` subtree.

Use these only under their existing project licenses. Do not modify, decompile,
or publish an internal replacement parser as part of the public scenario.

## 3. What to transfer separately

The local handoff bundle contains:

- The complete original Markdown plan.
- The illustrated Word document and PDF.
- The seven diagrams in both SVG and PNG formats for a later presentation.
- Copies of the operator/handoff guides for offline reading.
- The supplied ASGIHV WPRP/region XML as **optional research references**, not the
  active capture profile.
- A version/status manifest and SHA-256 inventory.

The source code and PowerShell scripts come from the Git branch; they are not
duplicated in the reference bundle.

This reference bundle may contain internal research material. Transfer it only
through approved internal methods; do not commit/upload it to the public fork.
Local paths inside the historical plan refer to the original workstation and
must be mapped to the new checkout/reference locations.

Do **not** copy:

- GitHub tokens, passwords, private account settings, credential stores or real
  device profiles into the repository.
- The development workstation's `.venv`, pyenv version directories or prep-status
  files. Recreate/validate machine-specific environments through normal setup.
- Generated `HOBLStatusWindow\obj` output.
- The old workstation's entire Copilot/session-state directory.
- Private AI-model/parser source or packages into public HOBL.

An approved corpus, a configured DUT profile and a validated trace decoder are
**not supplied** by this implementation. They must be selected/provisioned for the
new HOST/DUT. The optional model/API milestone also needs its genuine approved
runner/model package; it is not implemented by copying the reference WPRP.

## 4. HOST and DUT prerequisite checklist

### HOST

- Git and normal HOBL HOST setup; verify the new checkout works with the installed
  HOBL UI/launcher rather than continuing to run a different clone.
- A suitable Python runtime and the standard HOBL HOST dependencies. The development
  validation used Python 3.12.10; the test suite itself uses the standard library.
  The current `hobl.cmd` prefers `downloads\python_embed\python.exe` when present,
  otherwise it invokes `python` from the HOST environment.
- PowerShell, `tracerpt.exe`, and the .NET/runtime support required by the existing
  ParseUtc package (including .NET 8 x64 for its apphost).
- Network/RPC connectivity to the dedicated DUT and a fresh results location.
- An approved local corpus directory when an AI-enabled cell is selected.

### DUT

- Exclusive remote Windows test device/account with matching HOBL DUT setup and
  SimpleRemote/InputInject plugins. Do not point at the HOST itself.
- Office/Edge/Teams and the relevant EC account/replay prerequisites.
- WPR and the required UTC/Office diagnostics configuration.
- For indexing: supported x64 hardware/OS feature/model/language/policy, running
  Windows Search, .NET 8 x64, and the operator-validated semantic status value.
- For stress: pyenv-win already provisioned. Scenario prep conditionally obtains
  the pinned Python and creates its own venv; it does not force-reinstall shared
  Python or upgrade shared packages.

See the operator guide for the single-processor-group stress limitation and
the explicitly gated ARM64 indexing/model/query paths.

## 5. Configuration to supply

Merge the template settings into a **copy of the existing dedicated DUT profile
outside the repository**, preserving its legitimate app/account/network settings.
Do not replace the real profile wholesale with the incomplete template.

| Value | Supply on the new HOST |
|---|---|
| `global:dut_ip` | The dedicated remote DUT address/name, never localhost |
| `global:result_dir` | A fresh HOST study output directory |
| `global:platform`, `local_execution` | `Windows`, `0` |
| `global:tools` | `run_report` only; the AI metrics tool is registered automatically |
| `collection_enabled`, `training_mode` | `1`, `0` |
| `enterprise_ai:dedicated_dut` | `1` only after confirming exclusive DUT ownership |
| `condition` | A/B/C/D/E, optionally F; begin with one controlled smoke run |
| `protocol` | `indexing` |
| `measurement_seconds` | Same fixed budget for every matched cell; default 1800 |
| `stress_workers`, `stress_duty_cycle` | Calibrated fixed settings, not assumed achieved system CPU |
| `corpus_dir` | Approved HOST folder, stable contents, bounded size, no reparse points |
| `semantic_ready_value` | Confirmed enabled DWORD value for the target build; no guessed default |
| `configure_utc` | `0` to verify existing state; `1` to explicitly snapshot/apply/restore it |
| `validation_mode` | Initially `capture`; it does not produce benchmark-valid results |
| `required_pts` | PTs matching the actual foreground actions and DUT build |
| `enterprise_ai_metrics:trace_validator` | Leave empty until an approved compatible validator exists |

Indexing cells are C, D, E and F. They require the corpus and semantic status
configuration. A/B do not enable AI; they are not proof that semantic indexing works.

## 6. Safe continuation sequence

1. Clone/select the exact handoff revision and inspect `git status`.
2. Read this guide, the operator guide and the historical plan.
3. Run safe tests with a **real executable path**, not a pyenv shim:

   ```powershell
   .\tests\run_enterprise_ai_checks.ps1 -PythonExecutable '<absolute-python.exe-path>'
   ```

   If using pyenv, `pyenv which python` identifies the real executable. Do not
   use `Get-Command python` to resolve a pyenv-managed executable.

4. Preview the matrix with the copied, edited device profile:

   ```powershell
   .\testplans\enterprise_ai.ps1 -Profile '<full-path-to-profile.ini>' -Repetitions 1
   ```

   The preview does not run HOBL. Keep `-Execute` absent until the DUT is ready.

5. With explicit lab approval, validate one native capability/telemetry case and
   inspect its trace before scaling. A single HOBL invocation is:

   ```powershell
   .\hobl.cmd -p '<full-path-to-profile.ini>' -s enterprise_ai `
       enterprise_ai:condition=D global:iterations=1 global:attempts=1
   ```

   This is a **real DUT workload**, not another safe local test. It requires all
   native/foreground prerequisites. Start in capture mode and expect inconclusive
   results until validated completion/loss/window-attribution evidence exists.

6. Inspect the ETL, `enterprise_ai_run.json`, raw files, summary and quality JSON.
   Confirm actual providers, markers, corpus submission, worker health and cleanup.
7. Validate three consecutive lifecycle runs and failure recovery, then run the
   five-cell paired pilot. Keep failures; do not replace them with best-case retries.
8. Establish trace overhead/noise and actual indexing completion before reporting
   latency/throughput or proceeding to cross-device comparisons.

The matrix runner requires `-Execute` for real runs and stops on failure. Do not
remove an outstanding run lease or snapshot merely to get the next run started;
follow the recovery sequence in the operator guide.

## 7. Remaining work to continue

- Real-DUT capability/telemetry validation and matching foreground baseline.
- A validated decoder/completion contract: trace loss, per-window PT attribution,
  semantic document completion and observed model/backend identity.
- Measured semantic queries with deterministic expected results and warm/cold
  protocols; ordinary filename search is not equivalent.
- Calibrated stress and repeated-run/failure cleanup on hardware.
- Paired pilot data, capture overhead/noise analysis and Intel/AMD comparison.
- Click to Do as a separate native extension.
- Genuine AI Fundamentals/model API integration through approved external tooling.
- Optional well-resolved power/GPU/NPU attribution and the eventual presentation.

## 8. Context to paste into the next assistant session

```text
Continue the HOBL enterprise_ai project on this HOST. Repository:
https://github.com/varunkojha/HOBL, branch ai_enterprise. Check the exact handoff
commit and git status first; preserve existing changes.

Read docs\support\docs\HOBL_EnterpriseAI_Handoff.md and HOBL_EnterpriseAI.md, plus
the original Markdown/Word plan in the separately copied local handoff bundle.

The implemented scope is indexing-first: isolated EC-derived foreground actions,
owned indexing/stress lifecycle, lightweight UTC/AI capture, raw evidence and
strict scalar reporting. 152 safe automated tests passed on the old HOST.
No real DUT validation or benchmark results were obtained there.

Use visible VS Code terminal commands. Do not run workloads on the HOST.
Use only the explicit dedicated DUT profile I supply. Do not change global
Python/account settings, wipe shared environments, kill processes by name, or
merge/rebase old donor branches. Preserve the raw ETL and failures.

Capture mode is inconclusive. Native semantic completion, trace loss and PT-window
attribution still need validated evidence. Queries/model APIs, Click to Do and
power/GPU/NPU expansion remain gated; do not fabricate their results.

Next: verify HOST/DUT prerequisites and safe tests, then a single approved
capability/telemetry run on the dedicated DUT. I will supply the device profile,
approved corpus and target-build semantic status information.
```
