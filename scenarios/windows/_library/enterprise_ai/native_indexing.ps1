# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

param(
    [Parameter(Mandatory = $true)][ValidateSet('Prepare', 'Start', 'Stop')][string]$Action,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [Parameter(Mandatory = $true)][string]$RunId
)

. (Join-Path $PSScriptRoot 'run_common.ps1')

function Invoke-IndexUpdater {
    param([string]$Executable, [string]$Operation, [string]$Directory)
    $process = $null
    $output = Join-Path $runPath "index_${Operation}_stdout.log"
    $errors = Join-Path $runPath "index_${Operation}_stderr.log"
    try {
        $process = Start-Process -FilePath $Executable -ArgumentList @($Operation, "`"$Directory`"") `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $output -RedirectStandardError $errors
        $created = $process.StartTime
        if (-not $process.WaitForExit(60000)) {
            $current = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
            if ($null -ne $current -and $current.StartTime -eq $created -and $current.Path -ieq $Executable) {
                Stop-Process -Id $process.Id -Force
                if (-not $process.WaitForExit(10000)) { throw ' ERROR - IndexUpdater did not exit after cancellation.' }
            }
            throw " ERROR - IndexUpdater $Operation exceeded its 60-second limit; output is retained in $runPath"
        }
        if ($process.ExitCode -ne 0) {
            throw " ERROR - IndexUpdater $Operation failed (exit $($process.ExitCode)); see $errors"
        }
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

try {
    $runPath = Get-RunPath $RunDirectory $RunId
    Assert-RunOwner $runPath $RunId
    $statePath = Join-Path $runPath 'index_state.json'
    $corpus = Join-Path $runPath 'corpus'
    $staged = Join-Path $runPath 'staged'
    $exe = Join-Path $runPath 'IndexUpdater\IndexUpdater.exe'
    if ($Action -eq 'Prepare') {
        $config = Read-RunJson (Join-Path $runPath 'config.json')
        if ($config.run_id -cne $RunId -or $config.schema_version -ne 1) {
            throw ' ERROR - Indexing configuration belongs to a different run.'
        }
        if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
            throw ' ERROR - The supplied IndexUpdater apphost requires validated x64 support; ARM64 indexing is gated.'
        }
        $service = Get-Service -Name WSearch
        if ($service.Status -ne 'Running') { throw ' ERROR - Windows Search must already be running on the dedicated DUT.' }
        $status = Get-ItemPropertyValue -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Search\SemanticIndexer' -Name SemanticIndexingStatus
        if ([uint32]$status -ne [uint32]$config.semantic_ready_value) {
            throw " ERROR - SemanticIndexingStatus=$status does not match the operator-validated ready value."
        }
        foreach ($name in @('IndexUpdater.exe', 'IndexUpdater.dll', 'IndexUpdater.deps.json', 'IndexUpdater.runtimeconfig.json')) {
            $artifact = Join-Path $runPath "IndexUpdater\$name"
            if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) { throw " ERROR - Missing required runtime artifact: $artifact" }
        }
        $dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
        $dotnetPath = if ($null -ne $dotnet) { $dotnet.Source } else { '' }
        if (-not $dotnetPath) {
            $installed = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\dotnet\Setup\InstalledVersions\x64' -ErrorAction Stop
            $dotnetPath = Join-Path $installed.InstallLocation 'dotnet.exe'
        }
        if (-not (Test-Path -LiteralPath $dotnetPath -PathType Leaf)) { throw ' ERROR - .NET x64 runtime host was not found.' }
        $runtimes = & $dotnetPath --list-runtimes
        if ($LASTEXITCODE -ne 0 -or -not ($runtimes -match '^Microsoft\.NETCore\.App 8\.0\.')) {
            throw " ERROR - IndexUpdater requires a .NET 8 x64 runtime. Checked: $dotnetPath"
        }
        $manifest = Read-RunJson (Join-Path $runPath 'corpus_manifest.json')
        if ($manifest.schema_version -ne 1 -or $manifest.file_count -lt 1) { throw ' ERROR - Corpus manifest is empty or unsupported.' }
        foreach ($file in $manifest.files) {
            $source = [IO.Path]::GetFullPath((Join-Path $staged $file.relative_path))
            if (-not $source.StartsWith("$staged\", [StringComparison]::OrdinalIgnoreCase)) {
                throw ' ERROR - Corpus entry escapes the staged directory.'
            }
            Assert-NoReparsePoint $source
            if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ine $file.sha256) {
                throw " ERROR - Corpus checksum mismatch: $($file.relative_path)"
            }
        }
        if (Test-Path -LiteralPath $corpus) { throw ' ERROR - Corpus target already exists; a fresh indexing scope is required.' }
        [void][IO.Directory]::CreateDirectory($corpus)
        $state = @{schema_version = 1; run_id = $RunId; status = 'prepared'; registration_attempted = $false;
                   scope_removed = $false; documents_submitted = 0; semantic_completion_verified = $false;
                   semantic_status = $status; dotnet_path = $dotnetPath; corpus_sha256 = $manifest.sha256}
        Write-RunJson $statePath $state
        $state | ConvertTo-Json -Compress
    }
    elseif ($Action -eq 'Start') {
        $state = Read-RunJson $statePath
        if ($state.run_id -cne $RunId -or $state.status -ne 'prepared') { throw ' ERROR - Indexing must be prepared exactly once before starting.' }
        $state.registration_attempted = $true
        $state.status = 'registering'
        Write-RunJson $statePath $state
        Invoke-IndexUpdater $exe 'add' $corpus
        $manifest = Read-RunJson (Join-Path $runPath 'corpus_manifest.json')
        $clock = [Diagnostics.Stopwatch]::StartNew()
        foreach ($file in $manifest.files) {
            $source = [IO.Path]::GetFullPath((Join-Path $staged $file.relative_path))
            $destination = [IO.Path]::GetFullPath((Join-Path $corpus $file.relative_path))
            if (-not $source.StartsWith("$staged\", [StringComparison]::OrdinalIgnoreCase) -or
                -not $destination.StartsWith("$corpus\", [StringComparison]::OrdinalIgnoreCase)) {
                throw ' ERROR - Corpus entry escapes its owned scope.'
            }
            Assert-NoReparsePoint $source
            Assert-NoReparsePoint $destination
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $destination))
            Move-Item -LiteralPath $source -Destination $destination
            $state.documents_submitted++
        }
        $clock.Stop()
        $state.status = 'submitted'
        $state | Add-Member -NotePropertyName ingestion_dispatch_seconds -NotePropertyValue $clock.Elapsed.TotalSeconds
        Write-RunJson $statePath $state
        $state | ConvertTo-Json -Compress
    }
    else {
        if (Test-Path -LiteralPath $statePath) {
            $state = Read-RunJson $statePath
            if ($state.run_id -cne $RunId) { throw ' ERROR - Index scope ownership mismatch.' }
            if ($state.registration_attempted -and -not $state.scope_removed) {
                Invoke-IndexUpdater $exe 'remove' $corpus
                $state.scope_removed = $true
                Write-RunJson $statePath $state
            }
            Remove-OwnedCorpus $runPath 'corpus'
            Remove-OwnedCorpus $runPath 'staged'
            $state.status = 'stopped'
            Write-RunJson $statePath $state
            $state | ConvertTo-Json -Compress
        }
        else {
            Remove-OwnedCorpus $runPath 'corpus'
            Remove-OwnedCorpus $runPath 'staged'
            @{schema_version = 1; run_id = $RunId; status = 'not_started'} | ConvertTo-Json -Compress
        }
    }
}
catch {
    Write-Error " ERROR - Native indexing $Action failed: $($_.Exception.Message)"
    exit 1
}
