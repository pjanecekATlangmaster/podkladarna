# Podkladárna

Webová služba pro generování orientačních podkladů z **ČÚZK LiDAR** (DMR 5G + DMP 1G) a **ZABAGED** pomocí [Karttapullautin](https://github.com/karttapullautin/karttapullautin).

Výstup: PNG + DXF + vyčištěný ZABAGED ZIP pro **OpenOrienteering Mapper**.

## Rychlý start (Synology / Docker)

```bash
# Upravte ghcr.io/OWNER/podkladarna v docker-compose.yml
docker compose up -d --build
```

Otevřete: http://localhost:8672 nebo https://podkladarna.kibos.link

## Deploy na NAS (podkladarna.kibos.link)

Kompletní návod: **[DEPLOY.md](./DEPLOY.md)**

- Port kontejneru: **8672** (reverse proxy na NAS, ne nutně otevřený na routeru)
- DNS/router: veškerý veřejný provoz → **Synology NAS**
- Image: `ghcr.io/pjanecekatlangmaster/podkladarna:latest` (lowercase)

```bash
cp .env.example .env
chmod +x deploy-nas.sh
./deploy-nas.sh
```

## Vstup

| Soubor | Popis |
|--------|--------|
| DMR 5G LAZ | model reliéfu |
| DMP 1G LAZ | model povrchu (vegetace, budovy) |
| ZABAGED ZIP | volitelně, Shapefile z Geoprohlížeče |

## Presety

- Sprint 1:4000 · 2 m / 2,5 m
- Lesní 1:7500 / 1:10 000 · 5 m

## Logo

- `web/static/logo.svg` – vektorové logo (lampion, barvy O-mapy)
- Volitelně PNG: zkopírujte `assets/podkladarna-logo.png` do `web/static/logo.png`

## Plán

Viz [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) – v1.1 auto-stahování openzu, v1.5 iterace zeleně.

## Lokální vývoj (bez Docker)

Vyžaduje PDAL, GDAL, Linux `pullauta` v PATH.

```bash
export PODKLADARNA_DATA=./data
pip install -r requirements.txt
uvicorn app.main:app --reload --app-dir .
```

## Licence

Karttapullautin – viz upstream. Podkladárna – váš projekt.
