# Deploy Podkladárna na Synology NAS

Cíl: **https://podkladarna.kibos.link** → NAS → Docker → port **8672**

Veškeré směrování (router, reverse proxy) míří na **Synology NAS**, ne na PC.

**Aktuální provoz je A/B (sekce 9):** ostrá A (`podkladarna.kibos.link`, port 8672) běží z image `:stable`,
test B (`pt.kibos.link`, port 8673) z `:latest`. Sekce 1–8 popisují společné části (proxy, DNS, GHCR).

---

## 1. Tok provozu

```
Internet
  → router (kibos.link DNS → veřejná IP NAS / DDNS)
  → Synology DSM (reverse proxy)
  → localhost:8672
  → kontejner podkladarna:8672
```

| Vrstva | Nastavení |
|--------|-----------|
| **DNS** | `podkladarna.kibos.link` → A/AAAA záznam na IP NAS (nebo CNAME na DDNS) |
| **Router** | Port forward **443** → NAS (HTTPS). Volitelně 80 → NAS pro redirect |
| **Synology** | Reverse Proxy: `podkladarna.kibos.link` → `http://127.0.0.1:8672` |
| **Docker** | Publish `8672:8672`, volume `./data:/data` |

Port **8672** nemusí být otevřený na routeru směrem ven — stačí reverse proxy na NAS (443 → 8672 interně).
Test B: `pt.kibos.link` → reverse proxy → `http://127.0.0.1:8673` (kontejner `podkladarna-test`, viz sekce 9).

---

## 2. GHCR – build a push (GitHub Actions)

1. V `.github/workflows/docker.yml` se image pushne na:
   `ghcr.io/<github_username>/podkladarna:latest`
2. Tag `:stable` (ostrá A) vzniká jen ručně GitHub akcí **Promote to stable**
   (`.github/workflows/promote-stable.yml`), která přetaguje stejný digest `:latest` → `:stable` (nic nebuilduje).
3. Compose soubory berou vlastníka z `GHCR_OWNER` (lowercase) v `.env`, v shellu:
   ```bash
   export GHCR_OWNER=vase_github_jmeno
   ```

Push do `master` (merge) spustí workflow `docker.yml`. Veřejný image na NAS:

```bash
# Image je vždy lowercase:
docker pull ghcr.io/pjanecekatlangmaster/podkladarna:latest   # test B
docker pull ghcr.io/pjanecekatlangmaster/podkladarna:stable   # ostrá A
```

CI drží **Docker layer cache** (`:buildcache` na GHCR + GitHub Actions cache).
Těžká vrstva PDAL/GDAL (~350 MB) se při běžné změně kódu nepřestaví a na NAS
se znovu nestahuje — jen menší vrstvy (app, configs). **Mapper CLI**
(stage `mapper-builder`, pin PR #2523) se cacheuje zvlášť; při nezměněném
pinu SHA se nepřestaví.

### Mapper CLI v image (georef PNG)

Image obsahuje headless OpenOrienteering Mapper (`/opt/mapper/bin/Mapper`)
sestavovaný z `mfbehrens/oo-mapper` `cli` @ `6dc1fd72`. Dockerfile nastaví:

| Env | Význam |
|-----|--------|
| `PODKLADARNA_MAPPER` | `/opt/mapper/bin/Mapper` |
| `PODKLADARNA_MAPPER_EXPORT` | `"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}` |
| `PODKLADARNA_MAPPER_CONVERT` | `"{mapper}" --cli convert -i "{omap}" -o "{ocd}" --output-format OCD12` |
| `QT_QPA_PLATFORM` | `offscreen` |

App doplní `{dpi}` = **300** pro georef ZIP. Po `.omap` volá convert → `.ocd`
(OCD12) do materiálového ZIPu. Web „Otevřít PNG“ zůstává Pillow.
GPL-3.0: viz `LICENSES.md` (zdroj + build recept / nabídka tarballu).

Workflow po buildu nastaví balíček GHCR jako **veřejný** (login na NAS není potřeba).

Jednorázově ručně (pokud by workflow selhal): GitHub → **Packages** → `podkladarna` → **Package settings** → **Change visibility** → Public.

---

## 3. Synology – složka na NAS

Např. `/volume1/docker/podkladarna/`. Pro A/B provoz (sekce 9) jsou potřeba:

```
podkladarna/
  update-nas-ab.sh          # aktualizace A (stable) a/nebo B (test)
  nas-lib.sh                # sdílené funkce (digest, health check, úklid)
  docker-compose.nas-ab.yml # compose bez buildu (jen GHCR image), služby podkladarna (A) a podkladarna-test (B)
  .env                      # GHCR_OWNER=pjanecekatlangmaster, SMTP… (vzor .env.example)
  data/                     # A: jobs, cache, SQLite (vytvoří se samo)
  data-test/                # B: jobs, SQLite (vytvoří se samo)
  shared/slots/             # sdílený zámek těžkého jobu
```

Soubory zkopírujte z repozitáře (`update-nas-ab.sh`, `nas-lib.sh`, `docker-compose.nas-ab.yml`).
První nasazení A/B: sekce 9.

### Aktualizace
Viz sekce 9, „Běžný tok“: B se aktualizuje samo cronem, ostrá A po akci **Promote to stable** příkazem

```bash
cd /volume1/docker/podkladarna
sudo ./update-nas-ab.sh stable
```

Ověření lokálně na NAS (očekáváno `200`) a verze v patičce webu:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/
```

Kdyby chodil mail „kontejner byl ukončen nečekaně“, v DSM: **Ovládací panel → Oznámení → Pravidla** a u Container Manageru vypnout „Neočekávaně zastaveno“. Ruční `docker compose down` ten mail pořád spustí.

Aktualizace **nepoužívá** `docker compose down` napřímo na běžící kontejner. Nejdřív ho zastaví přes `synowebapi` (stejně jako Stop v Container Manageru).

---

## 4. Synology Reverse Proxy

**Ovládací panel → Přihlašovací portál → Rozšířené → Reverse proxy**

| Pole | Hodnota |
|------|---------|
| Popis | Podkladarna |
| Zdroj – protokol | HTTPS |
| Zdroj – hostname | `podkladarna.kibos.link` |
| Zdroj – port | 443 |
| Cíl – protokol | HTTP |
| Cíl – hostname | `localhost` nebo `127.0.0.1` |
| Cíl – port | **8672** |

Certifikát: Let’s Encrypt pro `podkladarna.kibos.link` (Synology certifikát).

### Cloudflare (kibos.link)

Oranžový proxy rozbíjí mapu, i když `http://IP_NAS:8672` funguje: **Rocket Loader** přepíše Leaflet a SRI/skript spadne. WAF / Bot Fight umí zahodit i dlaždice.

V Cloudflare u této domény vypněte:

- Speed → Optimization → **Rocket Loader**
- Speed → **Auto Minify** u JavaScriptu
- Scrape Shield → **Email Address Obfuscation**
- Speed → **Polish / Mirage** (logo a PNG dlaždice)

Volitelně Cache Rule: `/static/*` a `/tiles/*` cache on, Skip Bot Fight.

Aplikace Leaflet servíruje sama (`data-cfasync="false"`). Dlaždice jdou nejdřív na OSM (jako lokální náhled), při chybě přes `/tiles/` na NAS.

### Upload velkých LAZ souborů (důležité)

LAZ data mají často **100–500 MB+**. Výchozí nginx limit na Synology je ~**1 MB** → upload přes HTTPS spadne hned (popup, **žádný log jobu**).

**Rychlý test:** nahrajte přes **`http://192.168.x.x:8672`** (bez reverse proxy). Pokud funguje, chybí limit na proxy.

**Trvalá oprava (HTTPS)** – zkopírujte z repa `deploy/synology-nginx-upload.conf` na NAS:

```bash
sudo cp deploy/synology-nginx-upload.conf /etc/nginx/conf.d/proxy_podkladarna.conf
sudo nginx -t && sudo synosystemctl restart nginx
```

Obsah souboru:

```nginx
client_max_body_size 0;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_request_buffering off;
```

Případně restart Web Station / reverse proxy dle verze DSM.

Alternativa v DSM 7: **Ovládací panel → Přihlašovací portál → Rozšířené → Reverse proxy → Upravit → Vlastní záhlaví** nepomůže na velikost těla – nutná úprava nginx výše.

---

## 5. Router / DNS (kibos.link)

- **DDNS** na Synology (pokud nemáte statickou IP)
- DNS u registrátora:
  - `podkladarna.kibos.link` → IP NAS
  - nebo wildcard `*.kibos.link` pokud už používáte pro jiné služby

Router: forward **443** (a volitelně **80**) na **vnitřní IP NAS**, ne na jiný stroj.

---

## 6. Container Manager – limity

| Parametr | Doporučení (24 GB RAM NAS) |
|----------|----------------------------|
| `mem_limit` | 12g (v compose) |
| Restart policy | unless-stopped |
| Volume | `./data` persistent (B: `./data-test`) |

Limity a časové limity (env proměnné s výchozími hodnotami z `app/settings.py`; `docker-compose.nas-ab.yml` nastavuje `JOB_RETENTION_HOURS`, `MAX_ACTIVE_JOBS_PER_IP` a `MAX_JOBS_PER_IP_HOUR`):

| Proměnná | Výchozí | Význam |
|----------|---------|--------|
| `JOB_TIMEOUT_MINUTES` | 90 | časový limit jobu |
| `SHARED_SLOT_WAIT_MINUTES` | `JOB_TIMEOUT_MINUTES` + 15 | jak dlouho job nejvýš čeká na sdílený slot (A/B) |
| `MAX_ACTIVE_JOBS_PER_IP` | 2 | souběžné joby z jedné IP |
| `MAX_JOBS_PER_IP_HOUR` | 10 | nové joby z jedné IP za hodinu |
| `JOB_RETENTION_HOURS` | 48 | jak dlouho se drží hotové joby |

Velikost výběru je omezena na 25 km² (`MAX_BBOX_AREA_KM2` v `app/pipeline/fetch_openzu.py`, od 3.2.2).

---

## 7. První test po deployi

1. Nejdřív na testu B: otevřít https://pt.kibos.link (po ostré A stejný postup na https://podkladarna.kibos.link)
2. Nový job – nahrát DMR + DMP (+ ZABAGED) z `testdata/` (`DMR5G.laz`, `DMP1G.laz`, `Zabaged.zip`) nebo vlastní výřez do 25 km²
3. Preset Sprint, spustit
4. Sledovat log, stáhnout ZIP a otevřít v OpenOrienteering Mapperu
5. Teprve potom **Promote to stable** (sekce 9)

---

## 8. Deploy checklist (A/B)

- [ ] GHCR workflow `docker.yml` proběhlo (zelená Actions), `:latest` je nový
- [ ] B (`pt.kibos.link`, port 8673) se aktualizovalo a testovací job prošel
- [ ] GitHub → Actions → **Promote to stable** → Run
- [ ] NAS: `cd /volume1/docker/podkladarna && sudo ./update-nas-ab.sh stable`
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/` → `200`, verze v patičce webu sedí
- [ ] Reverse proxy → 8672 (A) a 8673 (B), DNS + router → NAS, HTTPS funguje
- [ ] `/api/health` → `mail_configured: true`

---

## 9. A/B provoz (stabilní + test na jednom NAS)

Dvě instance ze stejného image repozitáře, každá s vlastními jobami a databází:

| | A – stabilní | B – test |
|---|---|---|
| Doména | `podkladarna.kibos.link` | `pt.kibos.link` (bez odkazů z A, `noindex`) |
| Port | 8672 | 8673 |
| Image | `:stable` | `:latest` (každý merge do `master`) |
| Data | `./data` | `./data-test` |
| Banner | žádný | „Testovací verze“ + odkaz na A |

Společné: **cache stažených dat** (`./data/cache`, v B připojená nad `./data-test`) a **zámek těžkého jobu** (`./shared/slots`) – počítá jen jeden job napříč A i B. Čekající job má štítek „čeká na uvolnění serveru“; druhá instance se o cizím jobu nedozví nic než „server je vytížený“.

### První nasazení A/B
1. GitHub → Actions → **Promote to stable** → Run (přetaguje `:latest` na `:stable`; bez toho A nenastartuje).
2. DNS (Cloudflare): `pt.kibos.link` → stejná IP jako `podkladarna.kibos.link`. DSM → Reverse Proxy: `pt.kibos.link` → `http://127.0.0.1:8673` + certifikát; stejné limity uploadu jako u A (sekce 4).
3. Na NAS do `/volume1/docker/podkladarna/`: `docker-compose.nas-ab.yml`, `update-nas-ab.sh`, doplnit `.env` (viz `.env.example`, sekce A/B).
4. Spustit `sudo ./update-nas-ab.sh both`.
5. Ověřit `curl http://127.0.0.1:8672/` a `:8673/` → 200 a banner na `https://pt.kibos.link`.

### Běžný tok
`merge do master` → workflow `docker.yml` publikuje `:latest` → B se aktualizuje (cron níže, nebo ručně `sudo ./update-nas-ab.sh test`) → ověření na `pt.kibos.link` → GitHub akce **Promote to stable** (`:latest` → `:stable`) → na NAS `cd /volume1/docker/podkladarna && sudo ./update-nas-ab.sh stable` → `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/` = `200` a verze v patičce.
Automatická aktualizace B (cron, např. každých 5 min): `flock -n /tmp/update-ab.lock /volume1/docker/podkladarna/update-nas-ab.sh test --if-idle`. Bez nového image se kontejner nerestartuje. S novým image `update-nas-ab.sh` (A i B, i ručně) běžící job nepřeruší: založí `data/.deploy-drain` / `data-test/.deploy-drain` (nové joby se jen řadí do fronty – stav `queued` restart přežije a nová verze je spustí; job čekající na sdílený slot se vrátí do fronty), čeká, až `/api/health` hlásí `"busy": false` (vidět je i `"draining": true`), a teprve pak restartuje. Limit `--wait-minutes=N` (výchozí `JOB_TIMEOUT_MINUTES` + 30); po něm se nasazení vzdá bez restartu a další běh cronu to zkusí znovu. `--force` restartuje hned (přeruší job). Zapomenutý `.deploy-drain` aplikace po 2 × `JOB_TIMEOUT_MINUTES` + 60 min ignoruje.
Kontejner s novým image `update-nas-ab.sh` nejdřív zastaví přes `synowebapi` (jako Stop v Container Manageru), aby DSM neposílal mail „stopped unexpectedly“; bez `synowebapi` (nebo bez práv) pokračuje jako dřív přes docker. Po úspěšném nasazení smaže zastavené zbytky `<id>_podkladarna*` a image `ghcr.io/<owner>/podkladarna` bez tagu, které žádný kontejner nepoužívá (nic jiného).
Rollback A: do `.env` `IMAGE_TAG_A=<sha tag nebo digest>` a `./update-nas-ab.sh stable`.

### Pozor
- Restart služby přeruší její běžící job. Aktualizujte instanci, která zrovna nepočítá.
- Sdílená cache: když B změní formát odvozené cache (reference, DTM JSON…), musí změnit i klíč / verzi formátu, aby A nečetl nekompatibilní soubor (`REF_CACHE_FORMAT` to už dělá).

## Env soubor (`.env` vedle compose)

```env
GHCR_OWNER=pjanecekatlangmaster

# SMTP – nutné pro privátní joby i zpětnou vazbu (POST /api/feedback)
SMTP_HOST=datais-cz.mail.protection.outlook.com
SMTP_PORT=25
SMTP_ENCRYPTION=starttls
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=podkladarna@datais.cz
SMTP_FROM_NAME=OB podklady
# PUBLIC_BASE_URL=https://podkladarna.example
# FEEDBACK_TO=janecek@datais.cz
```

`GHCR_OWNER` musí být **lowercase** – GHCR ukládá image jako `ghcr.io/pjanecekatlangmaster/podkladarna`.

`docker-compose.nas-ab.yml` předává `SMTP_*` / `PUBLIC_BASE_URL` / `FEEDBACK_TO`
z tohoto `.env` do kontejneru. Od **2.2.12** má compose **product default**
`SMTP_HOST` (M365 relay) a `PUBLIC_BASE_URL=https://podkladarna.kibos.link`
i když host `.env` SMTP řádky nemá. Image navíc při prázdném injectu
doplní stejné defaulty (detekce `/.dockerenv`).

Samotný `.env` u compose **není** automaticky celý injectnutý – bez řádků
v `environment:` (starý compose před #55) kontejner SMTP z host `.env`
nevidí.

Host `.env` se při aktualizaci nepřepisuje a hodnoty SMTP se nevypisují do logu; `update-nas-ab.sh` compose nesyncuje, nový `docker-compose.nas-ab.yml` zkopírujte na NAS ručně.
Po recreate `printenv` v kontejneru ukáže `SMTP_*` z host `.env` (nebo compose defaults).

Po změně `.env` (bez změny compose): `sudo ./update-nas-ab.sh <test|stable|both>` pro dotčenou instanci.

Alternativa od 2.2.10 (`data/.env` na volume): od **2.2.11** doplní i klíče,
které compose injectne jako prázdné (`SMTP_HOST=`). Restart kontejneru stačí.
Health `/api/health` má `mail_configured` (true/false, bez hesel).
