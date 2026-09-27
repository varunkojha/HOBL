# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw " ERROR - Python executable was not found: $PythonExecutable"
}
Write-Host "Using Python: $PythonExecutable"
$python = @'
import ast
import pathlib
import sys
import unittest

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
files = list((root / "scenarios" / "windows" / "enterprise_ai").glob("*.py"))
files += list((root / "scenarios" / "windows" / "_library" / "enterprise_ai").glob("*.py"))
files += list((root / "utilities" / "open_source").glob("enterprise_ai_*.py"))
files += [root / "tools" / "enterprise_ai_metrics.py"]
for filename in files:
    ast.parse(filename.read_text(encoding="utf-8-sig"), filename=str(filename))
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_enterprise_ai_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
'@
& $PythonExecutable -c $python $repo
if ($LASTEXITCODE -ne 0) { throw ' ERROR - enterprise_ai Python checks failed.' }
$scripts = @(Get-ChildItem -LiteralPath (Join-Path $repo 'scenarios\windows\_library\enterprise_ai') -Filter '*.ps1' -File)
$scripts += Get-Item -LiteralPath (Join-Path $repo 'testplans\enterprise_ai.ps1')
$scripts += Get-Item -LiteralPath $PSCommandPath
foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
    if ($errors) {
        $errors | ForEach-Object { Write-Host (" ERROR - " + $_.Message) -ForegroundColor Red }
        throw " ERROR - PowerShell parsing failed: $($script.FullName)"
    }
}
Write-Host 'Safe checks passed. No DUT, prep, stress, WPR or native AI workload was executed.' -ForegroundColor Green
