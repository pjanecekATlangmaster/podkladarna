param(
    [Parameter(Position = 0)]
    [ValidateSet("test", "up", "down", "run", "smoke", "e2e", "e2e-upload", "all", "logs")]
    [string]$Action = "test"
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Ensure-DevDeps {
    python -m pip install -q -r requirements.txt -r requirements-dev.txt
}

switch ($Action) {
    "test" {
        Ensure-DevDeps
        python -m pytest tests/ -v
    }
    "run" {
        $env:PODKLADARNA_DATA = Join-Path $Root "data"
        python -m uvicorn app.main:app --host 127.0.0.1 --port 8672 --reload
    }
    "down" {
        docker compose -f docker-compose.dev.yml down
    }
    "smoke" {
        Ensure-DevDeps
        python scripts/smoke_e2e.py --fake
    }
    "e2e-upload" {
        Ensure-DevDeps
        python scripts/smoke_e2e.py --base http://127.0.0.1:8672 --data-dir testdata
    }
    "e2e" {
        Ensure-DevDeps
        python scripts/smoke_e2e.py --base http://127.0.0.1:8672 --data-dir testdata --wait-minutes 45
    }
    "all" {
        Ensure-DevDeps
        python -m pytest tests/ -v
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        docker compose -f docker-compose.dev.yml up --build -d
        Start-Sleep -Seconds 20
        python scripts/smoke_e2e.py --base http://127.0.0.1:8672 --data-dir testdata --wait-minutes 45
        $code = $LASTEXITCODE
        docker compose -f docker-compose.dev.yml logs --tail=50
        exit $code
    }
    "logs" {
        docker compose -f docker-compose.dev.yml logs -f
    }
}
