$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

Push-Location $RepoRoot
try {
    & $Python benchmarks\live_benchmark.py `
        --limit 200 `
        --workers 4 `
        --delay 0.5 `
        --site-timeout 60 `
        --request-timeout 8 `
        --playwright-timeout-ms 18000 `
        --playwright-wait-ms 2000 `
        --detail-limit 8 `
        --listing-limit 3
}
finally {
    Pop-Location
}
