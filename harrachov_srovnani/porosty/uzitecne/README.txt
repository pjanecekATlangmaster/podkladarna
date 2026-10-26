Užitečné vektory – vegetace, srázy, skály
========================================
Prohlížení a ruční import v OOM / QGIS (EPSG:5514).

pouzite/   … co zůstalo v auto .omap po filtrech
vyhozene/  … kandidáti zahození (atribut duvod = důvod filtru)

Vrstvy
------
vegetace.*   polygony porostů (cls, code = 401/406/408/410)
srazy_104.*  zemní srázy (linie ISOM 104)
skaly_201.*  skalní plochy (polygony 201.2 / 206)

Důvody ve vyhozene (příklady): mala_plocha, kompaktni_flek, min_plocha,
zamotany, nizky_schod, deprese, budova, prekryv_104, occupancy,
husté_vrstevnice.

Raw tick DXF a vegetation.shp jsou dál i ve složce base/ (před finální
filtrací u srázů). Tahle složka ukazuje výsledek filtrů.
