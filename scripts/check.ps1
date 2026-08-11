$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment Python not found: $python"
}

Push-Location (Join-Path $repositoryRoot 'backend_service')
try {
    & $python manage.py check
    & $python manage.py makemigrations --check --dry-run
    & $python manage.py test --verbosity 1
} finally {
    Pop-Location
}

Push-Location $repositoryRoot
try {
    & $python -m unittest discover -s mcp_server\tests -v
    & $python -m compileall -q backend_service mcp_server
    git diff --check
} finally {
    Pop-Location
}
