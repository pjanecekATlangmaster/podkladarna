param(
    [Parameter(Position = 0)]
    [ValidateSet("test", "up", "down", "smoke", "all", "logs")]
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
    "up" {
        docker compose -f docker-compose.dev.yml up --build
    }
    "down" {
        docker compose -f docker-compose.dev.yml down
    }
    "smoke" {
        Ensure-DevDeps
        python scripts/smoke_e2e.py --base http://127.0.0.1:8672
    }
    "all" {
        Ensure-DevDeps
        python -m pytest tests/ -v
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        docker compose -f docker-compose.dev.yml up --build -d
        Start-Sleep -Seconds 15
        python scripts/smoke_e2e.py --base http://127.0.0.1:8672
        $code = $LASTEXITCODE
        docker compose -f docker-compose.dev.yml logs --tail=30
        exit $code
    }
    "logs" {
        docker compose -f docker-compose.dev.yml logs -f
    }
}
