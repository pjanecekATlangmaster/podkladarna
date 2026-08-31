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

První push na `main`/`master` spustí workflow. Image na NAS:

```bash
# Image je vždy lowercase:
# ghcr.io/pjanecekatlangmaster/podkladarna:latest

docker login ghcr.io -u pjanecekatlangmaster -p <PAT s read:packages>
docker pull ghcr.io/pjanecekatlangmaster/podkladarna:latest
```

**Poznámka:** Repozitář je privátní → bez `docker login` dostanete `manifest unknown`.

---

## 3. Synology – složka na NAS

Např. `/volume1/docker/podkladarna/`:

```
podkladarna/
  deploy-nas.sh           # skript pro aktualizaci
  docker-compose.nas.yml  # compose bez buildu (jen GHCR image)
  .env                    # GHCR_OWNER, volitelně GHCR_TOKEN
  data/                   # jobs, cache, SQLite (vytvoří se samo)
```

Soubory zkopírujte z repozitáře (stačí tyto čtyři + složka `data`).

### První nasazení

```bash
cd /volume1/docker/podkladarna
cp .env.example .env          # upravte GHCR_OWNER
chmod +x deploy-nas.sh
./deploy-nas.sh
```

### Aktualizace po novém buildu v GHCR

```bash
cd /volume1/docker/podkladarna
./deploy-nas.sh
```

Skript: přihlášení do GHCR (pokud je `GHCR_TOKEN`) → `docker pull` → zastavení starého kontejneru → spuštění nového → health check na portu 8672.

Konkrétní tag místo `latest`:

```bash
./deploy-nas.sh v1.0.0
```

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
GHCR_USER=pjanecekatlangmaster
GHCR_TOKEN=ghp_xxxxxxxx
```

`GHCR_OWNER` musí být **lowercase** – GHCR ukládá image jako `ghcr.io/pjanecekatlangmaster/podkladarna`.

```yaml
# docker-compose už používá ${GHCR_OWNER:-OWNER}
```
