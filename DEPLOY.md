# Deploy Podkladárna na Synology NAS

Cíl: **https://podkladarna.kibos.link** → NAS → Docker → port **8672**

Veškeré směrování (router, reverse proxy) míří na **Synology NAS**, ne na PC.

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

---

## 2. GHCR – build a push (GitHub Actions)

1. Repozitář na GitHubu s obsahem složky `podkladarna/`
2. V `.github/workflows/docker.yml` se image pushne na:
   `ghcr.io/<github_username>/podkladarna:latest`
3. V `docker-compose.yml` nahraďte `OWNER` nebo nastavte env:
   ```bash
   export GHCR_OWNER=vase_github_jmeno
   ```

První push na `main`/`master` spustí workflow. Veřejný image na NAS:

```bash
# Image je vždy lowercase:
docker pull ghcr.io/pjanecekatlangmaster/podkladarna:latest
```

Workflow po buildu nastaví balíček GHCR jako **veřejný** (login na NAS není potřeba).

Jednorázově ručně (pokud by workflow selhal): GitHub → **Packages** → `podkladarna` → **Package settings** → **Change visibility** → Public.

---

## 3. Synology – složka na NAS

Např. `/volume1/docker/podkladarna/`:

```
podkladarna/
  deploy-nas.sh           # první deploy / chytrý restart (přeskočí pokud beze změny)
  update-nas.sh           # aktualizace (digest check, volitelně smaže starý image)
  nas-lib.sh              # sdílené funkce (digest, health check)
  docker-compose.nas.yml  # compose bez buildu (jen GHCR image)
  .env                    # GHCR_OWNER=pjanecekatlangmaster
  data/                   # jobs, cache, SQLite (vytvoří se samo)
```

Soubory zkopírujte z repozitáře (stačí tyto čtyři + složka `data`).

### První nasazení

```bash
cd /volume1/docker/podkladarna
```bash
cp .env.example .env
chmod +x deploy-nas.sh update-nas.sh
./deploy-nas.sh
```
```

### Aktualizace po novém buildu v GHCR

```bash
cd /volume1/docker/podkladarna
chmod +x update-nas.sh
sudo ./update-nas.sh
```

`update-nas.sh` **chytrý režim** (výchozí):
- porovná **digest** lokálního image s GHCR (`docker manifest inspect`)
- stejný digest + běžící kontejner → **nic nestahuje** (~350 MB ušetřeno)
- stejný digest, kontejner neběží → restart bez pull
- nový digest → stop, smazat starý image, pull, start

Vynucení plného stažení: `./update-nas.sh --force`

`deploy-nas.sh` – stejná logika digestu, ale **nesmaže** starý image před pull (šetrnější).

Konkrétní tag:

```bash
./update-nas.sh v1.0.0
```

Starý image ponechat (např. rollback): `./update-nas.sh --keep-image`

Ručně (bez skriptu):

```bash
cd /volume1/docker/podkladarna
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml down
docker compose -f docker-compose.nas.yml up -d
```

Ověření lokálně na NAS:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8672/
# očekáváno: 200
```

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

Pak: `sudo nginx -t && sudo synosystemctl restart nginx` (nebo restart Web Station / reverse proxy dle verze DSM).

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
| Volume | `./data` persistent |

---

## 7. První test po deployi

1. Otevřít https://podkladarna.kibos.link
2. Nový job – nahrát DMR + DMP (+ ZABAGED) ze složky `sance/`
3. Preset Sprint, spustit
4. Sledovat log, stáhnout ZIP

---

## 8. Commit a deploy checklist

- [ ] GitHub repo s `podkladarna/`
- [ ] GHCR workflow proběhlo (zelená Actions)
- [ ] `docker-compose.yml` – správný `ghcr.io/.../podkladarna:latest`
- [ ] NAS: `docker compose up -d`
- [ ] Reverse proxy → 8672
- [ ] DNS + router → NAS
- [ ] HTTPS funguje

---

## Env soubor (volitelně `.env` vedle compose)

```env
GHCR_OWNER=pjanecekatlangmaster
```

`GHCR_OWNER` musí být **lowercase** – GHCR ukládá image jako `ghcr.io/pjanecekatlangmaster/podkladarna`.

```yaml
# docker-compose už používá ${GHCR_OWNER:-OWNER}
```
