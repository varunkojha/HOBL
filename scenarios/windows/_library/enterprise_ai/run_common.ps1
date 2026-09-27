# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $candidate = [IO.Path]::GetFullPath($Path)
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw " ERROR - Reparse points are not allowed in run paths: $candidate"
            }
        }
        $parent = Split-Path -Parent $candidate
        if ($parent -eq $candidate) { break }
        $candidate = $parent
    }
}

function Get-RunPath {
    param(
        [Parameter(Mandatory = $true)][string]$RunDirectory,
        [Parameter(Mandatory = $true)][string]$RunId
    )
    if ($RunId -cnotmatch '^[a-f0-9]{32}$') {
        throw ' ERROR - RunId must be a 32-character lowercase hexadecimal identifier.'
    }
    $scriptDrive = Split-Path -Qualifier $PSScriptRoot
    $expected = [IO.Path]::GetFullPath("$scriptDrive\hobl_bin\enterprise_ai_resources\runs\$RunId")
    $actual = [IO.Path]::GetFullPath($RunDirectory)
    if ($actual -ine $expected) { throw " ERROR - Run directory must be the owned path: $expected" }
    Assert-NoReparsePoint $actual
    return $actual
}

function Read-RunJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw " ERROR - Required run state is missing: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-RunJson {
    param([string]$Path, [object]$Value)
    $temporary = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Assert-RunOwner {
    param([string]$RunDirectory, [string]$RunId)
    $owner = Read-RunJson (Join-Path $RunDirectory 'owner.json')
    if ($owner.schema_version -ne 1 -or $owner.run_id -cne $RunId) {
        throw ' ERROR - Run ownership mismatch; refusing to change state.'
    }
}

function Remove-OwnedCorpus {
    param([string]$RunDirectory, [ValidateSet('corpus', 'staged')][string]$Name)
    $target = [IO.Path]::GetFullPath((Join-Path $RunDirectory $Name))
    Assert-NoReparsePoint $target
    if (Test-Path -LiteralPath $target) {
        foreach ($item in Get-ChildItem -LiteralPath $target -Recurse -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw " ERROR - Cannot clean a corpus with reparse points: $($item.FullName)"
            }
        }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
