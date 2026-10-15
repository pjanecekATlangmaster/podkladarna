# CLAUDE.md — Podkladárna

## Co je tento projekt

Podkladárna je webová služba pro generování orientačních podkladů z ČÚZK LiDAR (DMR 5G + DMP OK) a ZABAGED. Výstupem je ZIP pro OpenOrienteering Mapper (`podkladarna_output.zip`).

- Backend: Python / FastAPI, vstupní bod `app.main:app`
- Frontend: statické soubory ve `web/`
- Geoprocessing: PDAL, GDAL, vlastní pipeline pro vegetaci, srázy a DEM
- Kontejnerizace: Docker / Docker Compose
- Testy: pytest v `tests/`, end-to-end smoke test `scripts/smoke_e2e.py`
- CI: GitHub Actions, build a push image do GHCR

## Základní pravidla pro agenta

- Komunikuj česky.
- Před úpravou si přečti relevantní soubory a vysvětli krátký plán změny.
- Zachovávej stávající strukturu projektu, styl kódu a pojmenování.
- Nepřepisuj `.env`, hesla, tokeny, SMTP údaje ani produkční konfiguraci.
- Nikdy necommituj tajemství, přihlašovací údaje ani lokální data.
- Před commitem spusť alespoň rychlé testy:

```powershell
python -m pytest tests/ -v
```

- Nedělej rozsáhlý refaktoring bez výslovného souhlasu.
- Neměň Dockerfile, `docker-compose*.yml`, deploy skripty ani GitHub Actions bez vysvětlení dopadu.
- Do produkce na NAS nikdy nesahej bez výslovného souhlasu.

## Struktura repozitáře

- `app/` — FastAPI backend, business logika, pipeline
- `web/` — frontend, statické soubory a UI
- `configs/` — konfigurace aplikace
- `scripts/` — vývojové, testovací a pomocné skripty
- `tests/` — pytest testy
- `testdata/` — malá testovací data pro lokální e2e test
- `deploy/` — konfigurace pro Synology / nginx
- `Dockerfile` — produkční image včetně PDAL, GDAL a Mapper CLI
- `docker-compose.yml` — obecný compose
- `docker-compose.dev.yml` — lokální vývojový stack
- `docker-compose.nas.yml` — produkční compose pro Synology NAS
- `deploy-nas.sh` — první deploy / šetrný restart na NAS
- `update-nas.sh` — aktualizace image z GHCR na NAS
- `nas-lib.sh` — sdílené funkce pro NAS deploy
- `.github/workflows/` — GitHub Actions, zejména build a push do GHCR
- `DEV.md` — podrobný vývojový workflow
- `DEPLOY.md` — podrobný postup nasazení a provozu na NAS

## Lokální vývoj na Windows

Standardní checkout Petra je:

```text
C:\Users\PetrJanecek\.cursor\projects\podkladarna
```

Agenti mají editovat worktree:

```text
C:\Users\PetrJanecek\.cursor\projects\podkladarna-agent
```

Neukládej změny přímo do Petrova live checkoutu, pokud běží dlouhé joby; `--reload` nebo file-watch může běžící job přerušit.

### Rychlé testy

```powershell
cd C:\Users\PetrJanecek\.cursor\projects\podkladarna-agent
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

### Pomocné příkazy

```powershell
.\scripts\dev.ps1 test         # pytest
.\scripts\dev.ps1 run          # uvicorn bez reload, vhodné pro smoke / dlouhé joby
.\scripts\dev.ps1 run-reload   # uvicorn s reload, jen krátký UI vývoj bez běžícího jobu
.\scripts\dev.ps1 up           # docker compose dev
.\scripts\dev.ps1 smoke        # fake upload přes API
.\scripts\dev.ps1 e2e-upload   # upload testdat
.\scripts\dev.ps1 e2e          # celá pipeline s testdaty
.\scripts\dev.ps1 all          # pytest + docker + e2e
```

### Lokální Docker

```powershell
docker compose -f docker-compose.dev.yml up --build
```

Lokální adresa:

```text
http://localhost:8672
```

Při e2e testu v Dockeru neukládej změny kódu uprostřed běžícího jobu, protože dev compose používá automatický reload.

### Důležité prostředí PROJ / GDAL

Pro QGIS nástroje a EPSG:5514 je nutné použít QGIS `proj.db`, nikoli starší `proj.db` z pip pyproj. Skript `scripts/dev.ps1` i `app.tool_env.apply_local_gis_env()` to nastavují automaticky.

Pokud se nástroje spouštějí mimo skript:

```powershell
$env:PROJ_DATA = 'C:\QGIS\share\proj'
$env:PROJ_LIB = $env:PROJ_DATA
$env:PATH = $env:PATH + ';C:\QGIS\bin'
```

## Ostré prostředí

- Veřejná adresa: `https://podkladarna.kibos.link`
- Hosting: Synology NAS
- Provoz: Internet → router → Synology DSM reverse proxy → `127.0.0.1:8672` → Docker kontejner
- Interní port kontejneru: `8672`
- Docker image: `ghcr.io/pjanecekatlangmaster/podkladarna:latest`
- NAS složka: `/volume1/docker/podkladarna/`
- Perzistentní data: `./data` na NAS, uvnitř kontejneru `/data`
- Port 8672 nemusí být otevřený na routeru; veřejný přístup jde přes HTTPS reverse proxy na NAS.

## Release proces

Standardní postup:

1. Vytvoř feature větev z aktuálního `master`.
2. Proveď změny a lokálně spusť:

```powershell
python -m pytest tests/ -v
```

3. Pro změny pipeline, Dockerfile nebo závislostí spusť podle potřeby:

```powershell
.\scripts\dev.ps1 up
.\scripts\dev.ps1 e2e
```

4. Commitni změny a pushni větev do GitHubu.
5. Vytvoř Pull Request do `master`.
6. Po merge do `master` GitHub Actions workflow `docker.yml` sestaví a publikuje image do:

```text
ghcr.io/pjanecekatlangmaster/podkladarna:latest
```

7. Zkontroluj, že Actions workflow je zelené.
8. Na NAS spusť aktualizaci:

```bash
cd /volume1/docker/podkladarna
sudo ./update-nas.sh
```

9. Ověř funkčnost:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/
```

Očekávaný výsledek je `200`.

10. Otevři `https://podkladarna.kibos.link` a ověř základní funkce webu.

## Nasazení na NAS

`update-nas.sh` ve výchozím režimu:

- synchronizuje `docker-compose.nas.yml` z GitHubu,
- čeká na dokončení GitHub Actions workflow,
- porovnává digest lokálního image a GHCR,
- při novém digestu zastaví kontejner, stáhne nový image a spustí jej,
- nepřepisuje host `.env`.

Užitečné varianty:

```bash
./update-nas.sh --no-wait       # nečekat na GitHub Actions
./update-nas.sh --force         # vynutit pull / recreate
./update-nas.sh v1.0.0          # nasadit konkrétní tag
./update-nas.sh --keep-image    # ponechat starý image pro rollback
```

Ruční postup:

```bash
cd /volume1/docker/podkladarna
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml down
docker compose -f docker-compose.nas.yml up -d
```

## Konfigurace a tajemství

- Lokálně vycházej z `.env.example`; skutečný `.env` necommituj.
- Na NAS je `.env` vedle `docker-compose.nas.yml`.
- `GHCR_OWNER=pjanecekatlangmaster` musí být lowercase.
- SMTP je nutné pro privátní joby a zpětnou vazbu.
- Důležité proměnné: `SMTP_HOST`, `SMTP_PORT`, `SMTP_ENCRYPTION`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_FROM_NAME`, `PUBLIC_BASE_URL`, `FEEDBACK_TO`, `PRIVATE_JOB_RETENTION_HOURS`.
- Zdravotní stav mailu lze ověřit přes `/api/health`, pole `mail_configured`.

## Testování po deployi

- Otevřít `https://podkladarna.kibos.link`.
- Vytvořit nový job s testovacími nebo reálnými DMR/DMP/ZABAGED daty.
- Zvolit preset, například Sprint.
- Sledovat log jobu.
- Stáhnout výsledný ZIP.
- Ověřit, že se ZIP otevře v OpenOrienteering Mapperu.
- Pokud jde o změnu SMTP nebo privátních jobů, ověřit e-mail s odkazem.

## Diagnostika

```powershell
# pytest
python -m pytest tests/ -v --tb=short

# lokální Docker log
docker compose -f docker-compose.dev.yml logs -f

# log jobu přes API
curl http://127.0.0.1:8672/api/jobs/<id>/log
```

```bash
# log na NAS
docker compose -f docker-compose.nas.yml logs --tail=100
```

## Co je zakázáno bez souhlasu

- Spouštět `./deploy-nas.sh` nebo `./update-nas.sh` na ostré NAS.
- Mazat data, joby, cache nebo Docker image.
- Měnit DNS, router, Synology reverse proxy, certifikáty nebo firewall.
- Měnit produkční `.env`, SMTP údaje nebo secrets.
- Publikovat novou Docker image bez úspěšných testů.
- Měnit schéma SQLite databáze nebo formát uložených jobů bez migračního plánu.
