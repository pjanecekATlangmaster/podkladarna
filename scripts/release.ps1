<#
.SYNOPSIS
  Lokální náhrada workflow docker.yml: z čistého master spustí testy, sestaví image
  a pushne ho do GHCR jako :latest a :<krátký sha>. Test B si :latest vezme cronem.

.EXAMPLE
  .\scripts\release.ps1              # testy + build + push + čekání na nasazení testu B
  .\scripts\release.ps1 -NoWait      # nečekat na test B
  .\scripts\release.ps1 -NoPush      # jen testy a build (lokální kontrola)
  .\scripts\release.ps1 -SkipTests   # jen když testy na stejném SHA už prošly a je to doložené

Předpoklady (jednorázově): Docker Desktop s buildx, `docker login ghcr.io -u <login>`
s osobním tokenem (scope write:packages), závislosti pro pytest (jednou: python -m pip install -r requirements.txt -r requirements-dev.txt).
#>
param(
    [switch]$NoPush,
    [switch]$SkipTests,
    [switch]$NoWait,
    [string]$Branch = "master"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Fail([string]$msg) {
    Write-Host "Chyba: $msg" -ForegroundColor Red
    exit 1
}

function Run([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { Fail "$exe $($argv -join ' ') skončil kódem $LASTEXITCODE" }
}

# 1. Čistý strom na aktuálním master (stejně jako CI: build jen z masteru).
$current = (git rev-parse --abbrev-ref HEAD).Trim()
if ($current -ne $Branch) { Fail "jsi na větvi '$current', release jen z '$Branch'." }
if (git status --porcelain) { Fail "pracovní strom není čistý (git status)." }
Run git @("pull", "--ff-only", "origin", $Branch)
if ((git rev-parse HEAD).Trim() -ne (git rev-parse "origin/$Branch").Trim()) {
    Fail "lokální $Branch se liší od origin/$Branch (nepushnuté commity), build jen z kódu na GitHubu."
}
$sha = (git rev-parse --short=7 HEAD).Trim()

$owner = if ($env:GHCR_OWNER) { $env:GHCR_OWNER } else { "pjanecekatlangmaster" }
$image = "ghcr.io/$($owner.ToLower())/podkladarna"
Write-Host "Release $image @ $sha" -ForegroundColor Cyan

# 2. Celá sada testů – při chybě se nic nepushne.
if (-not $SkipTests) {
    Run python @("-m", "pytest", "tests/", "-q", "--tb=short")
} else {
    Write-Host "Testy přeskočeny (-SkipTests)." -ForegroundColor Yellow
}

# 3. „Co je nového“ z git logu (jako CI); soubor jde jen do image, ve stromu se vrátí.
$builtAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
try {
    Run python @("scripts/generate_whats_new.py", "--released-at", $builtAt)

    # 4. Build (linux/amd64) s cache z GHCR, aby se Mapper nekompiloval znovu.
    # Výchozí driver „docker“ neumí export cache do registry → vlastní builder docker-container.
    $builder = "podkladarna"
    # Bez přesměrování stderr: v PowerShellu 5.1 by s ErrorAction Stop skript spadl.
    if (-not ((docker buildx ls --format '{{.Name}}') -contains $builder)) {
        Run docker @("buildx", "create", "--name", $builder, "--driver", "docker-container")
    }
    $buildArgs = @(
        "buildx", "build",
        "--builder", $builder,
        "--platform", "linux/amd64",
        "--build-arg", "BUILD_DATE=$builtAt",
        "--label", "org.opencontainers.image.revision=$(git rev-parse HEAD)",
        "--label", "org.opencontainers.image.created=$builtAt",
        "--cache-from", "type=registry,ref=${image}:buildcache",
        "-t", "${image}:latest",
        "-t", "${image}:$sha",
        "-f", "Dockerfile"
    )
    if ($NoPush) {
        $buildArgs += "--load"
    } else {
        $buildArgs += @("--push", "--cache-to", "type=registry,ref=${image}:buildcache,mode=max")
    }
    $buildArgs += "."
    Run docker $buildArgs
} finally {
    git checkout -- configs/whats_new.yaml
}

if ($NoPush) {
    Write-Host "Hotovo bez pushe: ${image}:latest a ${image}:$sha jsou jen lokálně." -ForegroundColor Green
} else {
    Run docker @("buildx", "imagetools", "inspect", "${image}:latest")
    Write-Host "Pushnuto: ${image}:latest = :$sha." -ForegroundColor Green
    if ($NoWait) { exit 0 }
    # Nasazení dělá cron na NAS (každých 5 min, běžící job nechá doběhnout); jen čekáme na novou verzi.
    $url = if ($env:TEST_B_URL) { $env:TEST_B_URL } else { "https://pt.kibos.link" }
    $waitMin = if ($env:TEST_B_WAIT_MINUTES) { [int]$env:TEST_B_WAIT_MINUTES } else { 150 }
    $version = ([regex]::Match((Get-Content app/settings.py -Raw), 'APP_VERSION = "([^"]+)"')).Groups[1].Value
    Write-Host "Čekám, až $url hlásí verzi $version (max $waitMin min)..."
    $deadline = (Get-Date).AddMinutes($waitMin)
    while ($true) {
        try { $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 15 } catch { $health = $null }
        if ($health -and $health.version -eq $version) {
            $code = try { (Invoke-WebRequest -Uri "$url/" -UseBasicParsing -TimeoutSec 15).StatusCode } catch { 0 }
            Write-Host "OK: test B běží $version, $url/ -> $code." -ForegroundColor Green
            if ($code -ne 200) { exit 1 }
            exit 0
        }
        if ((Get-Date) -ge $deadline) { Fail "test B ani po $waitMin min nehlásí $version (běží dlouhý job? log cronu na NAS)." }
        Start-Sleep -Seconds 30
    }
}
