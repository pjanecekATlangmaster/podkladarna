# Podkladárna

Webová služba pro generování orientačních podkladů z **ČÚZK LiDAR** (DMR 5G + DMP 1G) a **ZABAGED** pomocí [Karttapullautin](https://github.com/karttapullautin/karttapullautin).

Výstup: PNG + DXF a dva ZIP — surový a **ZIP pro OOM** (složky pro OpenOrienteering Mapper).

## Rychlý start (Synology / Docker)

**Lokální vývoj a testy:** viz **[DEV.md](./DEV.md)** – pytest + docker compose dev, deploy na NAS až potom.

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
| DMR 5G LAZ | z mapy (openzu) nebo ruční upload |
| DMP 1G LAZ | z mapy (openzu) nebo ruční upload |
| ZABAGED | z mapy (ArcGIS REST) nebo volitelný ZIP |

## Presety

- Sprint 1:4000 · 2 m / 2,5 m
- Lesní 1:7500 / 1:10 000 · 5 m
- MTBO 1:10 000 / 1:15 000 · 5 m

## Logo

- `web/static/logo.svg` – vektorové logo (lampion, barvy O-mapy)
- Volitelně PNG: zkopírujte `assets/podkladarna-logo.png` do `web/static/logo.png`

## Plán

Viz [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) – v1.5 ZIP pro OOM, v2 `.omap`.

## Lokální vývoj (bez Docker)

Vyžaduje PDAL, GDAL, Linux `pullauta` v PATH.

```bash
export PODKLADARNA_DATA=./data
pip install -r requirements.txt
uvicorn app.main:app --reload --app-dir .
```

## Licence

Podkladárna je pod **[MIT](./LICENSE)** (© 2026 Petr Janeček).

Výstupy vznikají z otevřených dat **ČÚZK** (DMR 5G, DMP 1G, ZABAGED®, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.cs))
a z [Karttapullautin](https://github.com/karttapullautin/karttapullautin) (GPL-3.0).
Přehled zdrojů a podmínek: **[LICENSES.md](./LICENSES.md)**.
