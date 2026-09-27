# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [ValidateSet('A', 'B', 'C', 'D', 'E', 'F')][string[]]$Conditions = @('A', 'B', 'C', 'D', 'E'),
    [ValidateRange(1, 100)][int]$Repetitions = 5,
    [ValidateRange(30, 7200)][int]$MeasurementSeconds = 1800,
    [int]$Seed = 20260927,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$scriptDrive = Split-Path -Qualifier $PSScriptRoot
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$launcher = Join-Path $repo 'hobl.cmd'
if (-not (Test-Path -LiteralPath $Profile -PathType Leaf)) { throw " ERROR - DUT profile is missing: $Profile" }
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw " ERROR - HOBL launcher is missing: $launcher" }
if (@($Conditions | Select-Object -Unique).Count -ne $Conditions.Count) {
    throw ' ERROR - Conditions must not contain duplicates.'
}
$profilePath = (Resolve-Path -LiteralPath $Profile).Path
$random = [Random]::new($Seed)
$originalLocation = Get-Location
try {
    Set-Location -LiteralPath $repo
    Write-Host "enterprise_ai matrix: seed=$Seed, repetitions=$Repetitions, drive=$scriptDrive"
    if (-not $Execute) { Write-Host 'Preview only. Use -Execute on a configured, dedicated DUT to run the plan.' }
    for ($repeat = 1; $repeat -le $Repetitions; $repeat++) {
        $order = @($Conditions)
        for ($i = $order.Count - 1; $i -gt 0; $i--) {
            $j = $random.Next($i + 1)
            $order[$i], $order[$j] = $order[$j], $order[$i]
        }
        foreach ($condition in $order) {
            $arguments = @(
                '-p', $profilePath, '-s', 'enterprise_ai',
                "enterprise_ai:condition=$condition",
                "enterprise_ai:measurement_seconds=$MeasurementSeconds",
                'global:iterations=1', 'global:attempts=1',
                "global:module_name=enterprise_ai_${condition}_r$($repeat.ToString('00'))"
            )
            Write-Host ("Condition $condition, repetition $repeat : " + ($arguments -join ' '))
            if ($Execute -and $PSCmdlet.ShouldProcess("dedicated DUT from $profilePath", "Run condition $condition, repetition $repeat")) {
                & $launcher @arguments
                if ($LASTEXITCODE -ne 0) {
                    throw " ERROR - Condition $condition failed. Results are retained; fix the cause before resuming."
                }
            }
        }
    }
}
finally {
    Set-Location -LiteralPath $originalLocation
}
