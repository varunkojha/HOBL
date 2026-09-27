# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

param(
    [Parameter(Mandatory = $true)][ValidateSet('Initialize', 'Collect', 'Release')][string]$Action,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$Destination = ''
)

. (Join-Path $PSScriptRoot 'run_common.ps1')
try {
    $runPath = Get-RunPath $RunDirectory $RunId
    $parent = Split-Path -Parent $runPath
    $lease = Join-Path $parent 'active-run.json'
    if ($Action -eq 'Initialize') {
        if (Test-Path -LiteralPath $runPath) {
            throw " ERROR - Run directory already exists; refusing reuse: $runPath"
        }
        [void][IO.Directory]::CreateDirectory($parent)
        $stream = $null
        try {
            $stream = [IO.File]::Open($lease, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $bytes = [Text.Encoding]::UTF8.GetBytes((@{schema_version = 1; run_id = $RunId} | ConvertTo-Json -Compress))
            $stream.Write($bytes, 0, $bytes.Length)
        }
        catch {
            throw " ERROR - Cannot acquire exclusive DUT lease. Recover the previous run before retrying. $($_.Exception.Message)"
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
        try {
            [void][IO.Directory]::CreateDirectory($runPath)
            $owner = @{schema_version = 1; run_id = $RunId; created_utc = [DateTime]::UtcNow.ToString('o')}
            Write-RunJson (Join-Path $runPath 'owner.json') $owner
        }
        catch {
            Remove-Item -LiteralPath $lease -Force
            throw
        }
        $os = Get-CimInstance Win32_OperatingSystem
        @{schema_version = 1; run_id = $RunId; status = 'initialized'; os_build = $os.BuildNumber;
          architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString();
          run_directory = $runPath} | ConvertTo-Json -Compress
    }
    else {
        Assert-RunOwner $runPath $RunId
        if ($Action -eq 'Collect') {
            $dest = [IO.Path]::GetFullPath($Destination)
            if ((Split-Path -Leaf $dest) -cne $RunId -or
                (Split-Path -Leaf (Split-Path -Parent $dest)) -ne 'enterprise_ai_raw') {
                throw ' ERROR - Destination must be an enterprise_ai_raw run directory.'
            }
            Assert-NoReparsePoint $dest
            [void][IO.Directory]::CreateDirectory($dest)
            foreach ($file in Get-ChildItem -LiteralPath $runPath -File -Force) {
                if ($file.Extension -in @('.json', '.jsonl', '.log', '.txt')) {
                    Assert-NoReparsePoint $file.FullName
                    Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
                }
            }
            @{schema_version = 1; run_id = $RunId; status = 'collected'} | ConvertTo-Json -Compress
        }
        else {
            if (Test-Path -LiteralPath $lease) {
                $leaseOwner = Read-RunJson $lease
                if ($leaseOwner.run_id -cne $RunId) { throw ' ERROR - Cannot release another run''s lease.' }
                Remove-Item -LiteralPath $lease -Force
            }
            @{schema_version = 1; run_id = $RunId; status = 'released'} | ConvertTo-Json -Compress
        }
    }
}
catch {
    Write-Error " ERROR - enterprise_ai workspace $Action failed: $($_.Exception.Message)"
    exit 1
}
