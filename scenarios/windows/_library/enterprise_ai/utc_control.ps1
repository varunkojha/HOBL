# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

param(
    [Parameter(Mandatory = $true)][ValidateSet('Prepare', 'Restore')][string]$Action,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [Parameter(Mandatory = $true)][string]$RunId,
    [ValidateSet('0', '1')][string]$Configure = '0'
)

. (Join-Path $PSScriptRoot 'run_common.ps1')

function Get-RegistrySnapshot {
    param([string]$Path, [string]$Name, [int]$Required)
    $exists = $false
    $value = $null
    $kind = 'DWord'
    if (Test-Path -LiteralPath $Path) {
        $key = Get-Item -LiteralPath $Path
        if ($key.GetValueNames() -contains $Name) {
            $exists = $true
            $value = $key.GetValue($Name)
            $kind = $key.GetValueKind($Name).ToString()
        }
    }
    return @{path = $Path; name = $Name; exists = $exists; value = $value; kind = $kind; required = $Required}
}

try {
    $runPath = Get-RunPath $RunDirectory $RunId
    Assert-RunOwner $runPath $RunId
    $statePath = Join-Path $runPath 'utc_state.json'
    $sideload = Join-Path $env:ProgramData 'Microsoft\Diagnosis\Sideload'
    Assert-NoReparsePoint $sideload
    if ($Action -eq 'Prepare') {
        if (Test-Path -LiteralPath $statePath) { throw ' ERROR - UTC state already exists; restore the prior state before retrying.' }
        $service = Get-Service -Name DiagTrack
        if ($service.Status.ToString() -notin @('Running', 'Stopped')) { throw ' ERROR - DiagTrack is not in a stable state.' }
        $registry = @(
            (Get-RegistrySnapshot 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection' 'AllowTelemetry' 3),
            (Get-RegistrySnapshot 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting' 'DisableWerUpload' 1)
        )
        $files = @()
        foreach ($name in @('UtcPerftrack.xml', 'DisableAllUploads.json')) {
            $source = Join-Path $runPath $name
            $target = Join-Path $sideload $name
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw " ERROR - Missing UTC input: $source" }
            $exists = Test-Path -LiteralPath $target -PathType Leaf
            $backup = Join-Path $runPath "$name.original"
            if ($Configure -eq '1' -and $exists) { Copy-Item -LiteralPath $target -Destination $backup }
            if ($Configure -eq '0' -and (-not $exists -or
                (Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash)) {
                throw ' ERROR - UTC sideload does not match the selected manifest. Use configure_utc=1 to snapshot, apply and restore it.'
            }
            $files += @{target = $target; source = $source; backup = $backup; existed = $exists}
        }
        if ($Configure -eq '0') {
            foreach ($entry in $registry) {
                if (-not $entry.exists -or $entry.value -ne $entry.required) {
                    throw " ERROR - UTC prerequisite $($entry.name) is not configured; use configure_utc=1 on the dedicated DUT."
                }
            }
            if ($service.Status -ne 'Running') { throw ' ERROR - DiagTrack must be running for preconfigured UTC collection.' }
        }
        $state = @{schema_version = 1; run_id = $RunId; changed = ($Configure -eq '1'); restored = $false;
                   service_status = $service.Status.ToString(); registry = $registry; files = $files}
        Write-RunJson $statePath $state
        if ($Configure -eq '1') {
            [void][IO.Directory]::CreateDirectory($sideload)
            foreach ($entry in $registry) {
                if (-not (Test-Path -LiteralPath $entry.path)) { [void](New-Item -Path $entry.path -Force) }
                [void](New-ItemProperty -LiteralPath $entry.path -Name $entry.name -Value $entry.required -PropertyType DWord -Force)
            }
            foreach ($file in $files) { Copy-Item -LiteralPath $file.source -Destination $file.target -Force }
            if ($service.Status -eq 'Running') { Restart-Service -Name DiagTrack } else { Start-Service -Name DiagTrack }
            (Get-Service -Name DiagTrack).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
        }
        @{schema_version = 1; run_id = $RunId; status = 'prepared'; changed = $state.changed} | ConvertTo-Json -Compress
    }
    else {
        if (Test-Path -LiteralPath $statePath) {
            $state = Read-RunJson $statePath
            if ($state.run_id -cne $RunId -or $state.schema_version -ne 1) { throw ' ERROR - UTC state owner mismatch.' }
            if ($state.changed -and -not $state.restored) {
                if ($state.files.Count -ne 2 -or $state.registry.Count -ne 2) { throw ' ERROR - UTC snapshot is incomplete.' }
                foreach ($file in $state.files) {
                    $name = Split-Path -Leaf $file.target
                    if ($name -notin @('UtcPerftrack.xml', 'DisableAllUploads.json') -or
                        $file.target -ine (Join-Path $sideload $name) -or
                        $file.backup -ine (Join-Path $runPath "$name.original")) {
                        throw ' ERROR - UTC snapshot contains an unexpected file path.'
                    }
                    if ($file.existed) {
                        if (-not (Test-Path -LiteralPath $file.backup -PathType Leaf)) { throw " ERROR - Missing UTC backup: $($file.backup)" }
                        Copy-Item -LiteralPath $file.backup -Destination $file.target -Force
                    }
                    elseif (Test-Path -LiteralPath $file.target) { Remove-Item -LiteralPath $file.target -Force }
                }
                foreach ($entry in $state.registry) {
                    $validEntry = ($entry.name -eq 'AllowTelemetry' -and
                        $entry.path -eq 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection') -or
                        ($entry.name -eq 'DisableWerUpload' -and
                        $entry.path -eq 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting')
                    if (-not $validEntry) { throw ' ERROR - UTC snapshot contains an unexpected registry value.' }
                    if ($entry.exists) {
                        [void](New-ItemProperty -LiteralPath $entry.path -Name $entry.name -Value $entry.value -PropertyType $entry.kind -Force)
                    }
                    elseif (Test-Path -LiteralPath $entry.path) {
                        $key = Get-Item -LiteralPath $entry.path
                        if ($key.GetValueNames() -contains $entry.name) { Remove-ItemProperty -LiteralPath $entry.path -Name $entry.name }
                    }
                }
                if ($state.service_status -eq 'Running') {
                    Restart-Service -Name DiagTrack
                    (Get-Service -Name DiagTrack).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
                }
                else {
                    Stop-Service -Name DiagTrack
                    (Get-Service -Name DiagTrack).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
                }
            }
            $state.restored = $true
            Write-RunJson $statePath $state
        }
        @{schema_version = 1; run_id = $RunId; status = 'restored'} | ConvertTo-Json -Compress
    }
}
catch {
    Write-Error " ERROR - UTC $Action failed: $($_.Exception.Message)"
    exit 1
}
