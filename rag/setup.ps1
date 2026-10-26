param([switch]$LexicalOnly)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        & python -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
    }
    if (-not $LexicalOnly) {
        & $pythonPath -m pip install -r requirements-semantic.txt
        if ($LASTEXITCODE -ne 0) { throw 'Semantic dependency installation failed' }
    }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    $buildArgs = @('-m', 'rag', 'build')
    if (-not $LexicalOnly) { $buildArgs += '--semantic' }
    & $pythonPath @buildArgs
    if ($LASTEXITCODE -ne 0) { throw 'Index build failed' }
} finally { Pop-Location }
