# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# DUT-only, explicit prep. Requires an existing pyenv-win installation.
# No shared pip changes, global Python switching, or dependency downloads beyond
# a missing pinned Python. This new arithmetic load needs only the standard library.

[CmdletBinding()]
param(
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Stop'
$scriptDrive = Split-Path -Qualifier $PSScriptRoot
$resourceDirectory = Join-Path "$scriptDrive\" 'hobl_bin\enterprise_ai_resources'
$venvDirectory = Join-Path $resourceDirectory '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
if (-not $LogFile) {
    $LogFile = Join-Path "$scriptDrive\" 'hobl_data\enterprise_ai_prep.log'
}

function Write-PrepLog {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Assert-PlainPath {
    param([string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
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

function Get-PyenvExecutable {
    $command = Get-Command -Name pyenv -CommandType Application,ExternalScript -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        return $command.Source
    }
    $roots = @($env:PYENV_ROOT, $env:PYENV, $env:PYENV_HOME)
    if ($env:USERPROFILE) { $roots += (Join-Path $env:USERPROFILE '.pyenv\pyenv-win') }
    foreach ($root in $roots) {
        if (-not $root) { continue }
        foreach ($relative in @('bin\pyenv.bat', 'pyenv-win\bin\pyenv.bat')) {
            $candidate = Join-Path $root $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    throw 'pyenv-win was not found. Provision pyenv-win on the DUT, expose it on PATH or PYENV_ROOT, then rerun enterprise_ai prep.ps1.'
}

function Get-PythonInfo {
    param([string]$Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Python executable is missing: $Executable"
    }
    $code = @'
import ctypes, ctypes.wintypes, json, multiprocessing, os, pathlib, struct, sys, sysconfig, time
print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])),
 'platform': sysconfig.get_platform(), 'bits': struct.calcsize('P') * 8,
 'prefix': os.path.abspath(sys.prefix), 'base_prefix': os.path.abspath(sys.base_prefix),
 'base_executable': os.path.abspath(sys._base_executable), 'executable': os.path.abspath(sys.executable)}))
'@
    $output = @(& $Executable -I -c $code 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Python standard-library/venv validation failed at ${Executable}: $($output -join ' ')"
    }
    return (($output -join "`n") | ConvertFrom-Json -ErrorAction Stop)
}

function Assert-PinnedPython {
    param($Info, [string]$ExpectedPlatform)
    if ($Info.version -cne '3.12.10' -or $Info.platform -cne $ExpectedPlatform -or $Info.bits -ne 64) {
        throw "Expected Python 3.12.10 $ExpectedPlatform (64-bit), found $($Info.version) $($Info.platform) $($Info.bits)-bit."
    }
}

$savedPath = $env:PATH
$savedVersion = $env:PYENV_VERSION
$savedPythonHome = $env:PYTHONHOME
$savedPythonPath = $env:PYTHONPATH
$exitCode = 1
try {
    if (-not $scriptDrive) { throw 'The prep script must reside on a local filesystem drive.' }
    if (-not [IO.Path]::IsPathRooted($LogFile) -or $LogFile -notmatch '^[A-Za-z]:\\') {
        throw 'LogFile must be an absolute local filesystem path.'
    }
    Assert-PlainPath $LogFile
    Assert-PlainPath $resourceDirectory
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($LogFile))
    Write-PrepLog "Resources: $resourceDirectory"

    $executionPolicy = Get-ExecutionPolicy -Scope Process
    if ($executionPolicy -eq 'Restricted' -or $executionPolicy -eq 'Undefined') {
        Set-ExecutionPolicy -ExecutionPolicy Unrestricted -Scope Process -Force -ErrorAction Stop
    }

    $architectures = @(Get-CimInstance -ClassName Win32_Processor -Property Architecture |
        Select-Object -ExpandProperty Architecture | Sort-Object -Unique)
    if ($architectures.Count -ne 1) { throw 'Cannot identify one supported processor architecture.' }
    switch ($architectures[0]) {
        9  { $pythonVersion = '3.12.10'; $expectedPlatform = 'win-amd64' }
        12 { $pythonVersion = '3.12.10-arm'; $expectedPlatform = 'win-arm64' }
        default { throw "Unsupported processor architecture: $($architectures[0])" }
    }

    $pyenvExecutable = Get-PyenvExecutable
    Assert-PlainPath $pyenvExecutable
    Write-PrepLog "pyenv-win: $pyenvExecutable; pinned version: $pythonVersion"
    $pyenvBin = Split-Path -Parent $pyenvExecutable
    $env:PATH = "$pyenvBin;$savedPath"
    $env:PYENV_VERSION = $pythonVersion
    $env:PYTHONHOME = $null
    $env:PYTHONPATH = $null

    $versionOutput = @(& $pyenvExecutable versions --bare 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "pyenv versions failed: $($versionOutput -join ' ')" }
    $installedVersions = @($versionOutput | ForEach-Object { ([string]$_).Trim() })
    if ($installedVersions -notcontains $pythonVersion) {
        Write-PrepLog "Installing missing Python $pythonVersion via pyenv..."
        & $pyenvExecutable install $pythonVersion 2>&1 | ForEach-Object { Write-PrepLog "pyenv: $_" }
        if ($LASTEXITCODE -ne 0) { throw "pyenv install $pythonVersion failed." }
    } else {
        Write-PrepLog "Python $pythonVersion already installed; preserving the shared installation."
    }

    $pythonOutput = @(& $pyenvExecutable which python 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "pyenv which python failed: $($pythonOutput -join ' ')" }
    $pythonPaths = @($pythonOutput | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($pythonPaths.Count -ne 1 -or $pythonPaths[0] -notmatch '(?i)\.exe$' -or
        -not (Test-Path -LiteralPath $pythonPaths[0] -PathType Leaf)) {
        throw 'pyenv which python did not return one existing python.exe. Repair the pinned pyenv install on the DUT.'
    }
    $pythonExecutable = [IO.Path]::GetFullPath($pythonPaths[0])
    Assert-PlainPath $pythonExecutable
    Write-PrepLog "Resolved pinned Python: $pythonExecutable"
    $baseInfo = Get-PythonInfo $pythonExecutable
    Assert-PinnedPython $baseInfo $expectedPlatform
    if ($baseInfo.prefix -ine $baseInfo.base_prefix -or $baseInfo.executable -ine $pythonExecutable) {
        throw 'pyenv did not resolve the pinned base interpreter.'
    }

    if (Test-Path -LiteralPath $venvDirectory) {
        Assert-PlainPath $venvDirectory
        Write-PrepLog "Checking existing scenario venv: $venvDirectory"
    } else {
        [void][IO.Directory]::CreateDirectory($resourceDirectory)
        Write-PrepLog "Creating isolated scenario venv: $venvDirectory"
        & $pythonExecutable -I -m venv $venvDirectory 2>&1 |
            ForEach-Object { Write-PrepLog "venv: $_" }
        if ($LASTEXITCODE -ne 0) { throw "Venv creation failed at $venvDirectory" }
    }

    $venvConfig = Join-Path $venvDirectory 'pyvenv.cfg'
    Assert-PlainPath $venvPython
    Assert-PlainPath $venvConfig
    if (-not (Test-Path -LiteralPath $venvConfig -PathType Leaf) -or
        (Get-Content -LiteralPath $venvConfig -Raw) -notmatch '(?im)^include-system-site-packages\s*=\s*false\s*$') {
        throw "Venv must exclude shared site-packages. Remove only $venvDirectory on the DUT and rerun prep."
    }
    $venvInfo = Get-PythonInfo $venvPython
    Assert-PinnedPython $venvInfo $expectedPlatform
    if ($venvInfo.prefix -ine $venvDirectory -or $venvInfo.prefix -ieq $venvInfo.base_prefix -or
        $venvInfo.base_executable -ine $pythonExecutable) {
        throw "Existing venv is incompatible with the pinned pyenv interpreter. Remove only $venvDirectory on the DUT and rerun prep."
    }
    Write-PrepLog "Validated isolated Python: $venvPython; base: $($venvInfo.base_executable)"
    Write-PrepLog 'enterprise_ai prep complete. No shared packages or global configuration were changed.'
    $exitCode = 0
} catch {
    $message = " ERROR - $($_.Exception.Message)"
    try { Write-PrepLog $message } catch { Write-Host $message }
} finally {
    $env:PATH = $savedPath
    $env:PYENV_VERSION = $savedVersion
    $env:PYTHONHOME = $savedPythonHome
    $env:PYTHONPATH = $savedPythonPath
}
exit $exitCode
