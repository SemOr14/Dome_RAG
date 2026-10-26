# Запуск агента одной командой: .\run.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (Test-Path .\.venv\Scripts\python.exe) { $py = ".\.venv\Scripts\python.exe" } else { $py = "python" }
& $py -X utf8 -u main.py
