# CLAUDE.md — Podkladárna

## Co je tento projekt

Podkladárna je webová služba pro generování orientačních podkladů z ČÚZK LiDAR (DMR 5G + DMP OK) a ZABAGED. Výstupem je ZIP pro OpenOrienteering Mapper (`podkladarna_output.zip`).

- Backend: Python / FastAPI, vstupní bod `app.main:app`
- Frontend: statické soubory ve `web/`
- Geoprocessing: PDAL, GDAL, vlastní pipeline pro vegetaci, srázy a DEM
- Kontejnerizace: Docker / Docker Compose
- Testy: pytest v `tests/`, end-to-end smoke test `scripts/smoke_e2e.py`
- CI: GitHub Actions — `test.yml` (testy), `docker.yml` (build a push image do GHCR)

## Pravidla pro agenta

- Komunikuj česky; commit zprávy také česky (prefix `docs:`, `fix:`, `feat:` apod.).
- Před úpravou si přečti relevantní soubory a vysvětli krátký plán změny.
- Zachovávej stávající strukturu projektu, styl kódu a pojmenování.
- Nedělej rozsáhlý refaktoring bez výslovného souhlasu.
- Nikdy necommituj tajemství, přihlašovací údaje ani lokální data.
- Před commitem spusť rychlé testy (viz „Vývoj a testy").
- Při změnách kódu a opravách chyb zvyš `APP_VERSION` v `app/settings.py` (viz „Verze a Novinky"); u dokumentace ne.

### Jen se souhlasem Petra

- Spouštět `./deploy-nas.sh` nebo `./update-nas.sh` na ostré NAS; do produkce na NAS nesahat vůbec.
- Mazat data, joby, cache nebo Docker image.
- Měnit DNS, router, Synology reverse proxy, certifikáty nebo firewall.
- Měnit produkční `.env`, SMTP údaje nebo secrets (ani `.env` lokálně nepřepisovat).
- Měnit Dockerfile, `docker-compose*.yml`, deploy skripty ani GitHub Actions bez vysvětlení dopadu.
- Publikovat novou Docker image bez úspěšných testů.
- Měnit schéma SQLite databáze nebo formát uložených jobů bez migračního plánu.

## Dělba práce (agenti)

Agenti jsou v `.claude/agents/`. Šetříme drahé modely: implementuje junior, senioři radí a revidují.

- `junior-dev` (Sonnet) — výchozí volba pro rutinní implementaci: opravy chyb, menší funkce, testy, úpravy UI. Eskaluje, když se zasekne.
- `opus-advisor` (read-only) — konzultace plánu před netriviální změnou (pipeline, API, formát jobů, geometrie).
- `opus-reviewer` (read-only) — review diffu před commitem u netriviálních změn. U drobností a dokumentace se přeskakuje.
- `test-runner` (Haiku, read-only) — spouští pytest (e2e jen na požádání) a vrací stručné shrnutí selhání místo dlouhého logu.
- `opus-expert` — jen eskalace těžkých algoritmických problémů, kde junior uvázl. Ne pro rutinní práci.

Typický tok: plán → (advisor u složitých věcí) → junior-dev implementuje → test-runner (pytest) → reviewer → commit.

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
- `.github/workflows/` — GitHub Actions (`test.yml`, `docker.yml`)
- `DEV.md` — podrobný vývojový workflow
- `DEPLOY.md` — podrobný postup nasazení a provozu na NAS

## Vývoj a testy

Předpoklady: Windows + PowerShell, nainstalovaný QGIS v `C:\QGIS` (kvůli `proj.db`, viz níže).

### Rychlé testy

Z kořene repozitáře:

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

Pro změny v `app/` stačí pytest. E2E test a Docker (`dev.ps1 up`, `dev.ps1 e2e`) spouštěj jen u změn pipeline, Dockerfile nebo závislostí.

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

Lokální adresa: `http://localhost:8672`

Při e2e testu v Dockeru neukládej změny kódu uprostřed běžícího jobu, protože dev compose používá automatický reload.

### Důležité prostředí PROJ / GDAL

Pro QGIS nástroje a EPSG:5514 je nutné použít QGIS `proj.db`, nikoli starší `proj.db` z pip pyproj. Skript `scripts/dev.ps1` i `app.tool_env.apply_local_gis_env()` to nastavují automaticky.

Pokud se nástroje spouštějí mimo skript:

```powershell
$env:PROJ_DATA = 'C:\QGIS\share\proj'
$env:PROJ_LIB = $env:PROJ_DATA
$env:PATH = $env:PATH + ';C:\QGIS\bin'
```

### Diagnostika

```powershell
# pytest podrobněji
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

## Verze a Novinky

- Verze aplikace je `APP_VERSION` v `app/settings.py` (formát `MAJOR.MINOR.PATCH`).
- Při každé změně kódu včetně oprav chyb zvyš verzi (patch, např. `2.8.1` → `2.8.2`). Čistě dokumentační úpravy (CLAUDE.md, DEV.md, DEPLOY.md apod.) verzi nezvyšují.
- U velké změny zvyš minor o jedna a patch vynuluj (`2.8.2` → `2.9.0`). Zda jde o velkou změnu, posuzuje agent odhadem (nová funkce, změna výstupu pipeline, výrazné zrychlení apod.).
- Box „Co je nového" na webu: `configs/whats_new.yaml` je generovaný CI z git logu — neupravuj ho ručně. Ruční milníky jsou v `configs/whats_new_milestones.yaml`; zapisuj sem jen významné změny z posledního období, ne drobnosti.

## Ostré prostředí

- Veřejná adresa: `https://podkladarna.kibos.link`
- Hosting: Synology NAS
- Provoz: Internet/Cloudflare → router → Synology DSM reverse proxy → `127.0.0.1:8672` → Docker kontejner
- Interní port kontejneru: `8672`
- Docker image: `ghcr.io/pjanecekatlangmaster/podkladarna:latest`
- NAS složka: `/volume1/docker/podkladarna/`
- Perzistentní data: `./data` na NAS, uvnitř kontejneru `/data`

## Release proces

Agent dělá kroky 1–7, nasazení na NAS (od kroku 8) dělá Petr.

1. Vytvoř feature větev z aktuálního `master`.
2. Proveď změny, zvyš `APP_VERSION` (kromě čistě dokumentačních změn) a lokálně spusť `python -m pytest tests/ -v`.
3. Pro změny pipeline, Dockerfile nebo závislostí navíc `.\scripts\dev.ps1 up` a `.\scripts\dev.ps1 e2e`.
4. Commitni změny a pushni větev do GitHubu.
5. Vytvoř Pull Request do `master`.
6. Po merge do `master` workflow `docker.yml` sestaví a publikuje `ghcr.io/pjanecekatlangmaster/podkladarna:latest`.
7. Zkontroluj, že Actions workflow je zelené.
8. (Petr) Na NAS spusť aktualizaci:

```bash
cd /volume1/docker/podkladarna
sudo ./update-nas.sh
```

9. (Petr) Ověř funkčnost, očekávaný výsledek je `200`:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/
```

## Kde se co spouští (Docker a NAS)

- **Vývojový stroj (Windows):** tady je repozitář. `dev.ps1 up` / `dev.ps1 e2e` staví image z lokálního pracovního stromu (`docker-compose.dev.yml`) a zkouší tedy kód z aktuální větve. Je to volitelná kontrola před mergem; nutná je jen u změn pipeline, Dockerfile nebo závislostí (viz „Release proces").
- **NAS:** repozitář se tam nestahuje. Běží jen hotový image `ghcr.io/pjanecekatlangmaster/podkladarna:latest` sestavený workflow `docker.yml` po mergu do `master` (kopíruje se `configs/`, `app/`, `web/`). Na NAS se proto nikdy nezkouší kód z větve ani se tam nespouští `dev.ps1`; po `update-nas.sh` se jen ověřuje nasazený image (health `200`, testovací job, log jobu, stažený ZIP).
- Agent proto při předávání pokynů „co pustit" vždy rozlišuje: před mergem = vývojový stroj, po mergu = NAS (jen `update-nas.sh` + ruční ověření podle `DEPLOY.md`).

## Nasazení na NAS (dělá Petr)

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

Ruční test po deployi (job s testovacími daty, preset např. Sprint, stažení ZIP a otevření v OpenOrienteering Mapperu, u SMTP e-mail s odkazem) je popsán v `DEPLOY.md`.

## Konfigurace a tajemství

- Lokálně vycházej z `.env.example`; skutečný `.env` necommituj.
- Na NAS je `.env` vedle `docker-compose.nas.yml`.
- `GHCR_OWNER=pjanecekatlangmaster` musí být lowercase.
- SMTP je nutné pro privátní joby a zpětnou vazbu.
- Důležité proměnné: `SMTP_HOST`, `SMTP_PORT`, `SMTP_ENCRYPTION`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_FROM_NAME`, `PUBLIC_BASE_URL`, `FEEDBACK_TO`, `PRIVATE_JOB_RETENTION_HOURS`.
- Zdravotní stav mailu lze ověřit přes `/api/health`, pole `mail_configured`.
