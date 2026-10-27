param([string]$Python = 'python', [switch]$Quick)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (Test-Path -LiteralPath $Python) { $Python = (Resolve-Path -LiteralPath $Python).Path }
$env:PYTHONPATH = @($repoRoot, "$repoRoot/rag", "$repoRoot/agent/src", "$repoRoot/services/data_api") -join [IO.Path]::PathSeparator
$env:PYTHONIOENCODING = 'utf-8'
$env:DOME_ALLOW_CONTROL = '0'
$env:AGENT_DRY_RUN = '1'
$reportDir = Join-Path $PSScriptRoot 'reports'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$failed = $false
Push-Location $repoRoot
try {
    & $Python -m pytest tests services/data_api/tests rag/tests integration/test_smoke.py -q --tb=short "--junitxml=$reportDir/integration.xml"
    if ($LASTEXITCODE -ne 0) { $failed = $true }
    Push-Location 'agent'
    try {
        $testArgs = @('-m', 'pytest', '-q', '-o', 'addopts=', '--tb=short', "--junitxml=$reportDir/agent.xml")
        if ($Quick) { $testArgs += @('-m', 'not slow') }
        & $Python @testArgs
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } finally { Pop-Location }
    & $Python (Join-Path $PSScriptRoot 'replay_day.py')
    if ($LASTEXITCODE -ne 0) { $failed = $true }
} finally { Pop-Location }
if ($failed) { exit 1 }
