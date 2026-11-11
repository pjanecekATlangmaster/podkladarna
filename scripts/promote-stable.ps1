<#
.SYNOPSIS
  Lokální náhrada workflow promote-stable.yml: přetaguje ověřený image na :stable
  (stejný digest, nic se nebuilduje). Ostrá A si :stable vezme cronem do 5 minut.

.EXAMPLE
  .\scripts\promote-stable.ps1            # :latest -> :stable
  .\scripts\promote-stable.ps1 1a2b3c4    # konkrétní sha tag -> :stable

Spouští Petr po ověření změny na testu B (pt.kibos.link).
Předpoklad: `docker login ghcr.io` s tokenem write:packages.
#>
param(
    [Parameter(Position = 0)]
    [string]$Source = "latest"
)

$ErrorActionPreference = "Stop"
$owner = if ($env:GHCR_OWNER) { $env:GHCR_OWNER } else { "pjanecekatlangmaster" }
$image = "ghcr.io/$($owner.ToLower())/podkladarna"

Write-Host "Promote ${image}:$Source -> :stable" -ForegroundColor Cyan
& docker buildx imagetools create -t "${image}:stable" "${image}:$Source"
if ($LASTEXITCODE -ne 0) { Write-Host "Chyba: přetagování selhalo." -ForegroundColor Red; exit 1 }
& docker buildx imagetools inspect "${image}:stable"
Write-Host "Hotovo. Ostrá A na pve1 si :stable vezme cronem do 5 minut (nebo ručně ./update-nas-ab.sh stable)." -ForegroundColor Green
