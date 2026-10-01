# Stáhne zdroje OpenOrienteering Mapper (GPL-3.0) do third_party/mapper.
# Adresář je v .gitignore – do gitu se necommituje.
# Tag v0.9.6 odpovídá Windows buildu „OpenOrienteering Mapper 0.9.6“.

$ErrorActionPreference = "Stop"
$dest = Join-Path $PSScriptRoot "..\third_party\mapper"
$dest = [System.IO.Path]::GetFullPath($dest)

if (Test-Path (Join-Path $dest "src\main.cpp")) {
    Write-Output "Mapper zdroje už jsou v $dest"
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
git clone --depth 1 --branch v0.9.6 https://github.com/OpenOrienteering/mapper.git $dest
Write-Output "Hotovo: $dest"
Write-Output "Headless PNG v 0.9.6 není: src/main.cpp jen otevírá GUI, export je PrintWidget::exportToImage()."
