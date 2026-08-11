$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'venv\Scripts\python.exe'

function Invoke-Checked {
    param([scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment Python not found: $python"
}

Push-Location (Join-Path $repositoryRoot 'backend_service')
try {
    Invoke-Checked { & $python manage.py check }
    Invoke-Checked { & $python manage.py makemigrations --check --dry-run }
} finally {
    Pop-Location
}

Push-Location $repositoryRoot
try {
    Invoke-Checked { & $python -m ruff check backend_service mcp_server }
    Invoke-Checked { & $python -m coverage erase }
    Invoke-Checked {
        & $python -m coverage run --append backend_service\manage.py test `
            cis_elements core jobs --verbosity 1
    }
    Invoke-Checked {
        & $python -m coverage run --append -m unittest discover -s mcp_server\tests -v
    }
    Invoke-Checked { & $python -m coverage report }
    Invoke-Checked { & $python -m compileall -q backend_service mcp_server }
    Invoke-Checked { git diff --check }
} finally {
    Pop-Location
}
