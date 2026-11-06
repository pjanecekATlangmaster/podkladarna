# Srovnání map z testu B (3.3.1) s ručními mapami z Liveloxu. Spouštět z kořene repozitáře.
# Výstup: local_test\test_331\srovnani\<oblast>\metriky.json
$ErrorActionPreference = 'Stop'
$base = 'local_test\test_331'
$out = "$base\srovnani"
New-Item -ItemType Directory -Force "$out\janov", "$out\sykorka" | Out-Null
$rc = 'scripts\ref_compare'

python "$rc\prep_ref.py" https://livelox.blob.core.windows.net/map-images/217274/50358295b8e64c64af055fba4574f423 5514 1.5412418233246161 -0.32338613628529156 581715.7749584285 -0.32338613628529156 -1.5412418233246161 -1869000.9636223896 "$out\janov\ref.png"
python "$rc\prep_ref.py" https://livelox.blob.core.windows.net/map-images/210839/2a4de66d318e4baf9ea9740b98a0c7c2 32633 1.120686825215842 -0.10909444904724763 104636.9429003365 -0.10909444904724763 -1.120686825215842 6173202.61097703 "$out\sykorka\ref.png"

foreach ($a in @(@{n='janov'; job="$base\janov\3c531f8f4c68"; obrys="$rc\obrys_okrouhlice.geojson"},
                 @{n='sykorka'; job="$base\sykorka\fa4f826c7341"; obrys="$rc\obrys_sykorka.geojson"})) {
    $omap = Get-ChildItem -Recurse $a.job -Filter *.omap | Where-Object { $_.Name -notmatch 'mtbo' } | Select-Object -First 1
    Write-Host "== $($a.n): $($omap.FullName)"
    python "$rc\omap2geo.py" $omap.FullName "$out\$($a.n)\our.geojson"
    python "$rc\compare.py" "$out\$($a.n)\ref.png" "$out\$($a.n)\ref.json" "$out\$($a.n)\our.geojson" $a.job "$out\$($a.n)" $a.obrys
}
