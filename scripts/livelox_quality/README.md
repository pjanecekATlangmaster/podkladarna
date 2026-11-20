# Kontrola kvality zemí proti mapám z Liveloxu

Petr 2026-11-20: ruční kontrola celé EU není reálná. Pro každou zemi (a oblast s jiným pokrytím dat) je v
`references.yaml` jedna referenční mapa z Liveloxu. Jednou týdně se v cloudu z `master` vygenerují naše mapy
téhož výřezu, porovnají se s referencí a výrazné odchylky (zeleň místo bílého lesa, mnohem hustší vrstevnice …)
se nahlásí v projektu.

- **Autorská práva:** mapy z Liveloxu jsou díla mapařů. `fetch_ref.js` je stahuje jen do soukromé cache mimo
  repozitář, slouží jen k měření, do repa, sdílené složky ani reportu nejdou (report obsahuje jen čísla).
- **Zátěž:** joby běží na vlastním serveru v cloudovém kontejneru, ne na ostré A ani testu B. Česko se nekontroluje
  a nic se v něm nemění.

## Soubory

| Soubor | Co dělá |
|---|---|
| `references.yaml` | oblasti: Livelox `class_id`, druh (les / sprint), měřítko, velikost výřezu, terén, data |
| `fetch_ref.js` | Playwright: veřejný prohlížeč Liveloxu → `blob.json` (rohy mapy) a obrázek do cache |
| `jobs.py` | výřez kolem středu reference, založení a čekání na job přes API |
| `measure.py` | obě mapy do UTM mřížky (les 1 m, sprint 0,5 m), dorovnání posunu podle černé kresby, třídy podle barvy, metriky |
| `report.py` | prahy, semafory, změna proti minulému běhu (`history.jsonl`) → `report.md` |
| `run_check.py` | celý běh |

## Metriky a prahy

Podíl plochy mapy: bílý les, zeleň 406–410 (a stupně), otevřený prostor 401/403, voda, oliva, budovy (šedá),
zpevněné 501; délka vrstevnic v km/km² (plocha hnědé / šířka čáry 0,14 mm v měřítku mapy); F1 shody zeleně,
bílého lesa a otevřeného prostoru. Výrazná odchylka (les): rozdíl podílu nad 15 p. b. (otevřené 10, voda 5),
poměr mimo 0,5–2 (bílá 0,6–1,6), vrstevnice mimo 0,6–1,6× reference, F1 zeleně pod 0,25. Sprint: budovy, 501,
otevřené, zeleň a oliva. Změna o více než 5 p. b. proti minulému běhu se hlásí žlutě. Prahy jsou hrubé:
reference je jiný rok a jiný mapař, kontrola má chytit hrubé chyby, ne ladit kresbu na konkrétní mapu.

## Spuštění v cloudu

Prostředí jako u cloudové otočky: `apt-get install gdal-bin python3-gdal`, venv z `/usr/bin/python3.12
--system-site-packages`, `pip install "numpy<2" "scipy<1.15" -r requirements.txt`, obaly `gdal_calc`,
`gdal_fillnodata`, `gdal_polygonize` volající venv python. Server:

```bash
MAX_ACTIVE_JOBS_PER_IP=50 MAX_JOBS_PER_IP_HOUR=100 PODKLADARNA_DATA=$DATA SITES="localhost=cs:free:*" COUNTRIES_BETA="*" \
  python -m uvicorn app.main:app --port 8672 &
python scripts/livelox_quality/run_check.py --base http://localhost:8672 --data $DATA --cache $CACHE --out $OUT \
  --history /mnt/project-files/kontrola-livelox/history.jsonl
```

API se volá přes `localhost` (ne `127.0.0.1`), jinak doména neodpovídá `SITES` a výřez je „mimo Česko“.

## Přidání oblasti

Na Liveloxu najít akci v oblasti (`/Home/SearchEvents` v kontextu stránky), vybrat třídu s georeferencovanou mapou
bez přetisku tratí (nebo s ním – fialová se z měření vynechá) a přidat řádek do `references.yaml`. Zvlášť patří
oblasti s jiným pokrytím dat (Vídeň, Paříž, Brusel, Helsinky, kraje s jinými výškami).
