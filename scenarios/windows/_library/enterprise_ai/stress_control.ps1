# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# DUT-only controller. Start requires a fresh run_id directory and a config file
# directly inside it. Status/Stop use the immutable stress_owner.json manifest.
# Successful calls emit ONE compact JSON state. A failed Start/Stop exits nonzero.
# Status can report a durable "failed" state without concealing it as "stopped".
# Never discovers/kills by process name. Retained Process handles prevent PID-reuse
# races between identity validation and forced termination. The Python controller
# arms a kill-on-close Windows job before spawning any children.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Status', 'Stop')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$RunDirectory,
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [string]$ConfigPath = ''
)

$ErrorActionPreference = 'Stop'
$scriptDrive = Split-Path -Qualifier $PSScriptRoot
$venvDirectory = Join-Path "$scriptDrive\" 'hobl_bin\enterprise_ai_resources\.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
$workerPath = Join-Path $PSScriptRoot 'stress_worker.py'
$startupSeconds = 45
$graceSeconds = 20
$killSeconds = 5
$utf8 = New-Object System.Text.UTF8Encoding($false)
$handles = @{}
$lock = $null
$result = $null
$exitCode = 1

function Assert-AbsolutePath {
    param([string]$Path)
    if (-not $Path -or $Path -notmatch '^[A-Za-z]:\\' -or $Path -match '["\r\n]' -or
        $Path -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Expected an absolute local filesystem path without parent traversal: $Path"
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Assert-PlainPath {
    param([string]$Path)
    $current = $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse/link paths are not allowed: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Read-RunJson {
    param([string]$Name, [switch]$Optional)
    $path = Join-Path $RunDirectory $Name
    Assert-PlainPath $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        if ($Optional) { return $null }
        throw "Required run artifact is missing: $path"
    }
    return ([IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop)
}

function Write-RunJson {
    param([string]$Name, $Value, [switch]$CreateNew)
    $path = Join-Path $RunDirectory $Name
    Assert-PlainPath $path
    if ($CreateNew -and (Test-Path -LiteralPath $path)) { throw "Refusing to overwrite $path" }
    $stage = Join-Path $RunDirectory ($Name + '.' + [Guid]::NewGuid().ToString('N') + '.new')
    $stream = $null
    $created = $false
    try {
        $bytes = $utf8.GetBytes(($Value | ConvertTo-Json -Depth 12 -Compress) + "`n")
        $stream = New-Object IO.FileStream($stage, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $created = $true
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            try {
                if (Test-Path -LiteralPath $path) {
                    if ($CreateNew) { throw "Refusing to overwrite $path" }
                    [IO.File]::Replace($stage, $path, $null)
                } else {
                    [IO.File]::Move($stage, $path)
                }
                break
            } catch [IO.IOException] {
                if ($timer.Elapsed.TotalSeconds -ge 2) { throw }
                Start-Sleep -Milliseconds 20
            }
        }
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($created -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Force }
    }
}

function Assert-IdentityRecord {
    param($Identity)
    if (-not $Identity -or $Identity.pid -is [bool] -or
        [string]$Identity.pid -notmatch '^[1-9][0-9]*$' -or
        [long]$Identity.pid -gt [int]::MaxValue -or
        $Identity.create_time -isnot [string] -or $Identity.create_time -notmatch '^[1-9][0-9]*$') {
        throw 'Invalid PID/creation-time ownership record.'
    }
    $null = Assert-AbsolutePath $Identity.executable
}

function Get-ProcessIdentity {
    param([Diagnostics.Process]$Process)
    $null = $Process.Handle
    return [pscustomobject]@{
        pid = $Process.Id
        create_time = $Process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture)
        executable = [IO.Path]::GetFullPath($Process.MainModule.FileName)
    }
}

function Add-OwnedHandle {
    param($Identity, [int]$ParentPid = 0)
    Assert-IdentityRecord $Identity
    $key = [string]$Identity.pid
    if ($handles.ContainsKey($key)) {
        $known = $handles[$key].Identity
        if ($known.create_time -cne $Identity.create_time -or $known.executable -ine $Identity.executable) {
            throw "Conflicting ownership for PID $key"
        }
        return
    }
    $process = $null
    try {
        try { $process = [Diagnostics.Process]::GetProcessById([int]$Identity.pid) }
        catch [ArgumentException] { return }
        $actual = Get-ProcessIdentity $process
        if ($process.HasExited) { $process.Dispose(); return }
        if ($actual.create_time -cne $Identity.create_time -or $actual.executable -ine $Identity.executable) {
            throw "PID $key is stale, foreign, or reused; refusing to control it."
        }
        if ($ParentPid -gt 0) {
            $detail = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $key" -OperationTimeoutSec 2
            if ($detail -and [int]$detail.ParentProcessId -ne $ParentPid) {
                throw "PID $key is not a child of its recorded owner."
            }
        }
        $handles[$key] = [pscustomobject]@{ Process = $process; Identity = $Identity }
        $process = $null
    } finally {
        if ($process) { $process.Dispose() }
    }
}

function Assert-Owner {
    param($Owner)
    if (-not $Owner -or $Owner.schema_version -is [bool] -or $Owner.schema_version -ne 1 -or $Owner.run_id -cne $RunId) {
        throw 'Foreign or invalid launcher manifest.'
    }
    if ($Owner.run_directory -ine $RunDirectory -or $Owner.worker_path -ine $workerPath -or
        $Owner.venv_python -ine $venvPython) { throw 'Launcher paths do not match this scenario/run.' }
    $ownedConfig = Assert-AbsolutePath $Owner.config_path
    if ([IO.Path]::GetDirectoryName($ownedConfig) -ine $RunDirectory -or
        $Owner.config_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$Owner.workers -notmatch '^[1-9][0-9]*$' -or [long]$Owner.workers -gt 64) {
        throw 'Invalid launcher configuration identity.'
    }
    $null = Assert-AbsolutePath $Owner.base_python
    Assert-IdentityRecord $Owner.launcher
    if ($Owner.launcher.executable -ine $venvPython) { throw 'Launcher is not the scenario venv Python.' }
}

function Assert-ConfigUnchanged {
    param($Owner)
    Assert-PlainPath $Owner.config_path
    $stream = [IO.File]::OpenRead($Owner.config_path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        if ($digest -cne $Owner.config_sha256) { throw 'Configuration changed after Start.' }
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Assert-State {
    param($Owner, $State)
    if (-not $State -or $State.schema_version -is [bool] -or $State.schema_version -ne 1 -or $State.run_id -cne $RunId -or
        $State.status -cnotin @('starting', 'ready', 'stopped', 'failed')) {
        throw 'Foreign or invalid stress state.'
    }
    $controller = [pscustomobject]@{
        pid = $State.controller_pid; create_time = $State.controller_create_time
        executable = $State.controller_executable
    }
    Assert-IdentityRecord $controller
    if ($controller.executable -ine $Owner.venv_python -and $controller.executable -ine $Owner.base_python) {
        throw 'Controller executable does not match the validated Python.'
    }
    if ($controller.pid -eq $Owner.launcher.pid) {
        if ($controller.create_time -cne $Owner.launcher.create_time -or
            $controller.executable -ine $Owner.launcher.executable) { throw 'Controller/launcher identity mismatch.' }
    } elseif ($State.controller_parent_pid -ne $Owner.launcher.pid -or
        [long]$controller.create_time -lt [long]$Owner.launcher.create_time) {
        throw 'Controller is not the owned launcher or its venv redirector child.'
    }
    $children = @($State.children)
    if ($children.Count -gt $Owner.workers -or @($State.child_pids).Count -ne $children.Count) {
        throw 'Invalid child ownership count.'
    }
    if ($State.status -eq 'ready' -and $children.Count -ne $Owner.workers) {
        throw 'Readiness was reported before every child was registered.'
    }
    $seen = @{}
    for ($index = 0; $index -lt $children.Count; $index++) {
        $child = $children[$index]
        Assert-IdentityRecord $child
        if ($child.parent_pid -ne $controller.pid -or $child.worker_id -ne $index -or
            $child.pid -eq $controller.pid -or $child.pid -eq $Owner.launcher.pid -or
            $seen.ContainsKey([string]$child.pid) -or $State.child_pids[$index] -ne $child.pid -or
            [long]$child.create_time -lt [long]$controller.create_time -or
            ($child.executable -ine $Owner.venv_python -and $child.executable -ine $Owner.base_python)) {
            throw 'Foreign or inconsistent child identity.'
        }
        $seen[[string]$child.pid] = $true
    }
}

function Register-StateHandles {
    param($Owner, $State, [System.Collections.Generic.List[string]]$Errors = $null)
    Assert-State $Owner $State
    $controller = [pscustomobject]@{
        pid = $State.controller_pid; create_time = $State.controller_create_time
        executable = $State.controller_executable
    }
    $parentId = 0
    if ($controller.pid -ne $Owner.launcher.pid) { $parentId = [int]$Owner.launcher.pid }
    $records = @(
        @{ Identity = $Owner.launcher; ParentPid = 0 },
        @{ Identity = $controller; ParentPid = $parentId }
    )
    foreach ($child in @($State.children)) { $records += @{ Identity = $child; ParentPid = 0 } }
    foreach ($record in $records) {
        try { Add-OwnedHandle $record.Identity $record.ParentPid }
        catch {
            if ($null -eq $Errors) { throw }
            $Errors.Add($_.Exception.Message)
        }
    }
}

function Get-LiveOwnedHandles {
    return @($handles.Values | Where-Object { -not $_.Process.HasExited })
}

function Find-OwnedDescendants {
    param($Owner, [Diagnostics.Stopwatch]$Timer, [double]$TimeoutSeconds)
    # Narrow, exact-parent discovery is only a recovery path for a controller
    # killed between spawn and atomic state publication. Never enumerate globally.
    $visited = @{}
    for ($depth = 0; $depth -lt 3; $depth++) {
        foreach ($entry in @(Get-LiveOwnedHandles)) {
            $parentId = [int]$entry.Identity.pid
            if ($visited.ContainsKey([string]$parentId)) { continue }
            if ($Timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'Owned-descendant discovery timed out.' }
            $visited[[string]$parentId] = $true
            $descendants = @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $parentId" -OperationTimeoutSec 2)
            if ($entry.Process.HasExited) { continue }
            foreach ($child in $descendants) {
                if ($Timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'Owned-descendant discovery timed out.' }
                if ($entry.Process.HasExited) { break }
                if ($handles.Count -ge (2 * [int]$Owner.workers + 2)) {
                    if ($handles.ContainsKey([string]$child.ProcessId)) { continue }
                    throw 'Unexpected number of descendants; refusing unbounded discovery.'
                }
                $isController = $child.CommandLine -and
                    $child.CommandLine.Contains('"' + $Owner.worker_path + '"') -and
                    $child.CommandLine.Contains('--config "' + $Owner.config_path + '"') -and
                    $child.CommandLine.Contains('--run-directory "' + $RunDirectory + '"')
                $isSpawn = $child.CommandLine -and $child.CommandLine.Contains('multiprocessing.spawn') -and
                    $child.CommandLine.Contains('--multiprocessing-fork') -and
                    $child.CommandLine.Contains("parent_pid=$parentId")
                if (($child.ExecutablePath -ine $Owner.base_python -and $child.ExecutablePath -ine $Owner.venv_python) -or
                    (-not $isController -and -not $isSpawn)) {
                    throw "Unrecognized descendant of PID $parentId; refusing to claim it."
                }
                $process = $null
                try {
                    try { $process = [Diagnostics.Process]::GetProcessById([int]$child.ProcessId) }
                    catch [ArgumentException] { continue }
                    $identity = Get-ProcessIdentity $process
                    if ([long]$identity.create_time -lt [long]$entry.Identity.create_time -or
                        $identity.executable -ine $child.ExecutablePath) { throw 'Descendant PID was reused.' }
                    Add-OwnedHandle $identity $parentId
                } finally {
                    if ($process) { $process.Dispose() }
                }
            }
        }
    }
}

function New-StopSignal {
    $path = Join-Path $RunDirectory 'stress.stop'
    Assert-PlainPath $path
    if (Test-Path -LiteralPath $path) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Stop signal is not a file.' }
        return
    }
    $stream = New-Object IO.FileStream($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $bytes = $utf8.GetBytes($RunId + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function New-FailedState {
    param($Owner, $State, [string]$Reason)
    if (-not $State) {
        $State = [pscustomobject]@{
            schema_version = 1; run_id = $RunId; status = 'failed'
            controller_pid = $Owner.launcher.pid; controller_create_time = $Owner.launcher.create_time
            controller_executable = $Owner.launcher.executable; controller_parent_pid = 0
            children = @(); child_pids = @(); error = $null; elapsed_s = 0.0
        }
    }
    $State.status = 'failed'
    $State.error = (@($State.error, $Reason) | Where-Object { $_ }) -join '; '
    return $State
}

function Stop-OwnedRun {
    param($Owner, [string]$FailureReason = '')
    Assert-Owner $Owner
    $state = $null
    $errors = New-Object 'System.Collections.Generic.List[string]'
    if ($FailureReason) { $errors.Add($FailureReason) }
    try { Assert-ConfigUnchanged $Owner } catch { $errors.Add($_.Exception.Message) }
    try { $state = Read-RunJson 'stress_state.json' -Optional; if ($state) { Assert-State $Owner $state } }
    catch { $errors.Add($_.Exception.Message); $state = $null }
    try { Add-OwnedHandle $Owner.launcher } catch { $errors.Add($_.Exception.Message) }
    if ($state) {
        try { Register-StateHandles $Owner $state $errors } catch { $errors.Add($_.Exception.Message) }
    }
    $stopWaitSeconds = $graceSeconds
    try { New-StopSignal }
    catch { $errors.Add("Could not signal graceful stop: $($_.Exception.Message)"); $stopWaitSeconds = 0 }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    if ($stopWaitSeconds -gt 0) {
        try { Find-OwnedDescendants $Owner $timer $stopWaitSeconds } catch { $errors.Add($_.Exception.Message) }
    }
    while ($timer.Elapsed.TotalSeconds -lt $stopWaitSeconds) {
        if (@(Get-LiveOwnedHandles).Count -eq 0 -and $state -and $state.status -in @('stopped', 'failed')) { break }
        Start-Sleep -Milliseconds 100
        try {
            $updated = Read-RunJson 'stress_state.json' -Optional
            if ($updated) { Register-StateHandles $Owner $updated $errors; $state = $updated }
        } catch { $errors.Add($_.Exception.Message); break }
    }
    $live = @(Get-LiveOwnedHandles)
    if ($live.Count -gt 0) {
        $errors.Add('Forced termination was required; the run did not stop cleanly.')
        try { Find-OwnedDescendants $Owner ([Diagnostics.Stopwatch]::StartNew()) $killSeconds }
        catch { $errors.Add($_.Exception.Message) }
        # Kill the controller first: closing its job kills every owned descendant,
        # including a child not yet published. Kill a venv redirector last.
        $ordered = @(Get-LiveOwnedHandles | Sort-Object @{
            Expression = {
                if ($state -and $_.Identity.pid -eq $state.controller_pid) { 0 }
                elseif ($_.Identity.pid -eq $Owner.launcher.pid) { 2 }
                else { 1 }
            }
        })
        foreach ($entry in $ordered) {
            try {
                if (-not $entry.Process.HasExited) {
                    $actual = Get-ProcessIdentity $entry.Process
                    if ($actual.create_time -cne $entry.Identity.create_time -or
                        $actual.executable -ine $entry.Identity.executable) {
                        throw "PID $($entry.Identity.pid) ownership changed; refusing termination."
                    }
                    $entry.Process.Kill()
                }
            } catch { $errors.Add($_.Exception.Message) }
        }
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    foreach ($entry in @($handles.Values)) {
        $remaining = [Math]::Max(0, [int](1000 * ($killSeconds - $timer.Elapsed.TotalSeconds)))
        if (-not $entry.Process.WaitForExit($remaining)) {
            $errors.Add("Owned PID $($entry.Identity.pid) has not exited; log handles may still be open.")
        }
    }
    try {
        $updated = Read-RunJson 'stress_state.json' -Optional
        if ($updated) { Assert-State $Owner $updated; $state = $updated }
    } catch { $errors.Add($_.Exception.Message) }
    if (-not $state -or $state.status -notin @('stopped', 'failed')) {
        $errors.Add('Controller exited without a durable terminal state.')
    }
    if ($errors.Count -gt 0) {
        $state = New-FailedState $Owner $state (($errors | Select-Object -Unique) -join '; ')
        Write-RunJson 'stress_state.json' $state
    }
    return $state
}

function Test-StartPython {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Scenario venv is missing at $venvPython. Run enterprise_ai prep.ps1 on the DUT; never use a shared Python."
    }
    Assert-PlainPath $venvPython
    Assert-PlainPath $workerPath
    $venvConfig = Join-Path $venvDirectory 'pyvenv.cfg'
    Assert-PlainPath $venvConfig
    if (-not (Test-Path -LiteralPath $venvConfig -PathType Leaf) -or
        (Get-Content -LiteralPath $venvConfig -Raw) -notmatch '(?im)^include-system-site-packages\s*=\s*false\s*$') {
        throw 'Scenario venv is missing or enables shared site-packages. Re-prep is required on the DUT.'
    }
    $code = @'
import ctypes, ctypes.wintypes, json, multiprocessing, os, pathlib, struct, sys, sysconfig, time
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
import stress_worker
config, digest = stress_worker.load_config(sys.argv[2], sys.argv[3])
probe = stress_worker.WindowsProbe()
probe.prime_metrics()
print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])),
 'platform': sysconfig.get_platform(), 'bits': struct.calcsize('P') * 8,
 'prefix': os.path.abspath(sys.prefix), 'base_prefix': os.path.abspath(sys.base_prefix),
 'base_python': os.path.abspath(sys._base_executable), 'workers': config.workers, 'config_sha256': digest}))
'@
    $output = @(& $venvPython -I -c $code $workerPath $ConfigPath $RunDirectory 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Venv/config/Windows API validation failed. Repair or re-prep on the DUT. $($output -join ' ')"
    }
    $info = ($output -join "`n") | ConvertFrom-Json -ErrorAction Stop
    $architectures = @(Get-CimInstance -ClassName Win32_Processor -Property Architecture |
        Select-Object -ExpandProperty Architecture | Sort-Object -Unique)
    if ($architectures.Count -ne 1) { throw 'Unable to determine one supported CPU architecture.' }
    switch ($architectures[0]) {
        9 { $expectedPlatform = 'win-amd64' }
        12 { $expectedPlatform = 'win-arm64' }
        default { throw "Unsupported processor architecture: $($architectures[0])" }
    }
    if ($info.version -cne '3.12.10' -or $info.platform -cne $expectedPlatform -or $info.bits -ne 64 -or
        $info.prefix -ine $venvDirectory -or $info.prefix -ieq $info.base_prefix -or
        -not (Test-Path -LiteralPath $info.base_python -PathType Leaf)) {
        throw "Incompatible scenario venv at $venvDirectory. Re-prep is required on the DUT."
    }
    return $info
}

function Start-OwnedRun {
    $info = Test-StartPython
    foreach ($name in @('stress_owner.json', 'stress_state.json', 'stress.stop', 'stress_samples.jsonl',
                        'stress_stdout.log', 'stress_stderr.log')) {
        if (Test-Path -LiteralPath (Join-Path $RunDirectory $name)) {
            throw "Run already has stress artifacts ($name). Use a new run_id; existing ownership is never replaced."
        }
    }
    if (@(Get-ChildItem -LiteralPath $RunDirectory -Filter 'stress_child_*.log').Count -gt 0) {
        throw 'Run already has child logs. Use a new run_id.'
    }
    $arguments = '-I "{0}" --config "{1}" --run-directory "{2}"' -f $workerPath, $ConfigPath, $RunDirectory
    $process = $null
    $owner = $null
    try {
        $process = Start-Process -FilePath $venvPython -ArgumentList $arguments -WorkingDirectory $RunDirectory -WindowStyle Hidden -PassThru
        $identity = Get-ProcessIdentity $process
        if ($identity.executable -ine $venvPython) { throw 'Start-Process did not launch the scenario venv executable.' }
        $owner = [pscustomobject]@{
            schema_version = 1; run_id = $RunId; run_directory = $RunDirectory
            config_path = $ConfigPath; config_sha256 = $info.config_sha256; workers = $info.workers
            worker_path = $workerPath; venv_python = $venvPython; base_python = $info.base_python
            launcher = $identity
        }
        $handles[[string]$identity.pid] = [pscustomobject]@{ Process = $process; Identity = $identity }
        $process = $null
        Write-RunJson 'stress_owner.json' $owner -CreateNew
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt $startupSeconds) {
            $state = Read-RunJson 'stress_state.json' -Optional
            if ($state) {
                Register-StateHandles $owner $state
                if ($state.status -eq 'ready') {
                    foreach ($processId in @($owner.launcher.pid, $state.controller_pid) + @($state.child_pids)) {
                        if (-not $handles.ContainsKey([string]$processId) -or $handles[[string]$processId].Process.HasExited) {
                            throw "Owned PID $processId exited before readiness could be returned."
                        }
                    }
                    return $state
                }
                if ($state.status -in @('failed', 'stopped')) { throw "Controller ended before readiness: $($state.error)" }
            }
            if ($handles[[string]$identity.pid].Process.HasExited) { throw 'Owned launcher exited before readiness.' }
            Start-Sleep -Milliseconds 100
        }
        throw 'Bounded stress startup timed out.'
    } catch {
        $failure = $_.Exception.Message
        if ($owner) {
            $null = Stop-OwnedRun $owner $failure
        } elseif ($process) {
            # Before manifest publication no worker can start. Allow the controller's
            # bounded owner-wait to exit and close logs before killing an exact handle.
            if (-not $process.WaitForExit(15000)) { $process.Kill(); $null = $process.WaitForExit(5000) }
        }
        throw $failure
    } finally {
        if ($process) { $process.Dispose() }
    }
}

try {
    if (-not $scriptDrive) { throw 'Controller script must reside on a local filesystem drive.' }
    if ($RunId -cnotmatch '^[0-9a-f]{32}$') { throw 'RunId must contain exactly 32 lowercase hexadecimal characters.' }
    $RunDirectory = Assert-AbsolutePath $RunDirectory
    if ([IO.Path]::GetFileName($RunDirectory) -cne $RunId) { throw 'Run directory leaf must exactly equal RunId.' }
    Assert-PlainPath $RunDirectory
    if (-not (Test-Path -LiteralPath $RunDirectory -PathType Container)) { throw 'Run directory does not exist.' }
    if ($Action -eq 'Start') {
        $ConfigPath = Assert-AbsolutePath $ConfigPath
        if ([IO.Path]::GetDirectoryName($ConfigPath) -ine $RunDirectory) { throw 'Config must be directly inside the run directory.' }
        Assert-PlainPath $ConfigPath
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'Config file does not exist.' }
    } elseif ($ConfigPath) {
        throw 'ConfigPath is valid only with -Action Start.'
    }
    $lockPath = Join-Path $RunDirectory 'stress_control.lock'
    Assert-PlainPath $lockPath
    $lock = New-Object IO.FileStream($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    switch ($Action) {
        'Start' { $result = Start-OwnedRun; $exitCode = 0 }
        'Status' {
            $owner = Read-RunJson 'stress_owner.json'
            Assert-Owner $owner
            Assert-ConfigUnchanged $owner
            $result = Read-RunJson 'stress_state.json'
            Register-StateHandles $owner $result
            if ($result.status -in @('starting', 'ready')) {
                foreach ($processId in @($owner.launcher.pid, $result.controller_pid) + @($result.child_pids)) {
                    if (-not $handles.ContainsKey([string]$processId) -or $handles[[string]$processId].Process.HasExited) {
                        throw "Owned PID $processId is absent; state is stale."
                    }
                }
            } elseif (@(Get-LiveOwnedHandles).Count -ne 0) {
                throw 'Terminal state is not yet final: an owned process/log handle remains live. Retry Status.'
            }
            $exitCode = 0
        }
        'Stop' {
            $owner = Read-RunJson 'stress_owner.json'
            $result = Stop-OwnedRun $owner
            if ($result.status -eq 'stopped') { $exitCode = 0 }
            else { [Console]::Error.WriteLine(" ERROR - $($result.error)") }
        }
    }
} catch {
    $result = $null
    [Console]::Error.WriteLine(" ERROR - $($_.Exception.Message)")
} finally {
    foreach ($entry in @($handles.Values)) { $entry.Process.Dispose() }
    if ($lock) { $lock.Dispose() }
}
if ($result) { [Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 12 -Compress)) }
exit $exitCode
