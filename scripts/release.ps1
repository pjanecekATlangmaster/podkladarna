<#
.SYNOPSIS
  Lokální náhrada workflow docker.yml: z čistého master spustí testy, sestaví image
  a pushne ho do GHCR jako :latest a :<krátký sha>. Test B si :latest vezme cronem.

.EXAMPLE
  .\scripts\release.ps1              # testy + build + push
  .\scripts\release.ps1 -NoPush      # jen testy a build (lokální kontrola)
  .\scripts\release.ps1 -SkipTests   # jen když testy na stejném SHA už prošly a je to doložené

Předpoklady (jednorázově): Docker Desktop s buildx, `docker login ghcr.io -u <login>`
s osobním tokenem (scope write:packages), závislosti pro pytest (dev.ps1 test).
#>
param(
    [switch]$NoPush,
    [switch]$SkipTests,
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
$sha = (git rev-parse --short=7 HEAD).Trim()

$owner = if ($env:GHCR_OWNER) { $env:GHCR_OWNER } else { "pjanecekatlangmaster" }
$image = "ghcr.io/$($owner.ToLower())/podkladarna"
Write-Host "Release $image @ $sha" -ForegroundColor Cyan

# 2. Celá sada testů – při chybě se nic nepushne.
if (-not $SkipTests) {
    python -m pip install -q -r requirements.txt -r requirements-dev.txt
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
    & docker buildx inspect $builder *> $null
    if ($LASTEXITCODE -ne 0) {
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
    Write-Host "Hotovo: ${image}:latest = :$sha. Test B (pt.kibos.link) si ho vezme cronem, až neběží job." -ForegroundColor Green
}
