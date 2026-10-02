param(
    [Parameter(Position = 0)]
    [ValidateSet("test", "up", "down", "run", "run-reload", "smoke", "e2e", "e2e-upload", "all", "logs")]
    [string]$Action = "test"
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Ensure-DevDeps {
    python -m pip install -q -r requirements.txt -r requirements-dev.txt
}

function Ensure-QgisGisEnv {
    # QGIS ogr2ogr must not pick pip pyproj's older proj.db (EPSG:5514 fails).
    $candidates = @(
        $env:OSGEO4W_ROOT,
        "C:\QGIS",
        "C:\OSGeo4W",
        "C:\OSGeo4W64"
    ) | Where-Object { $_ }
    foreach ($cand in $candidates) {
        $projDir = Join-Path $cand "share\proj"
        $binDir = Join-Path $cand "bin"
        if (-not (Test-Path (Join-Path $projDir "proj.db"))) { continue }
        $env:OSGEO4W_ROOT = $cand
        $env:PROJ_DATA = $projDir
        $env:PROJ_LIB = $projDir
        if ((Test-Path $binDir) -and ($env:PATH -notmatch [regex]::Escape($binDir))) {
            $env:PATH = $env:PATH + [IO.Path]::PathSeparator + $binDir
        }
        return
    }
}

Ensure-QgisGisEnv

# Načti .env (SMTP / PUBLIC_BASE_URL) do procesu – stejná logika jako app.settings._load_dotenv.
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or ($line -notmatch "=")) { return }
        $key, $val = $line.Split("=", 2)
        $key = $key.Trim()
        $val = $val.Trim().Trim('"').Trim("'")
        if ($key -and -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($key, "Process"))) {
            return
        }
        if ($key) { Set-Item -Path "Env:$key" -Value $val }
    }
}

switch ($Action) {
    "test" {
        Ensure-DevDeps
        python -m pytest tests/ -v
    }
    "run" {
        # Bez --reload: úpravy souborů / agent / pytest jinak zabijí běžící job.
        Ensure-QgisGisEnv
        $env:PODKLADARNA_DATA = Join-Path $Root "data"
        python -m uvicorn app.main:app --host 127.0.0.1 --port 8672
    }
    "run-reload" {
        Ensure-QgisGisEnv
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
