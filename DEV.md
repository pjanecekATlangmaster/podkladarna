# Lokální vývoj Podkladárny – workflow bez NAS

## Princip

1. **pytest** – API a upload (sekundy, bez Dockeru)
2. **docker compose dev** – plný stack s PDAL + pullauta lokálně
3. **smoke_e2e.py** – end-to-end proti běžící instanci
4. **GitHub Actions `test.yml`** – pytest na každý push
5. **Teprve pak** `./deploy-nas.sh` na Synology

---

## 1. Rychlé testy API (doporučeno před každým commitem)

```powershell
cd C:\Users\PetrJanecek\.cursor\projects\podkladarna
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

Ověří: health, presety, multipart upload, log jobu, chybové stavy.

---

## 2. Lokální Docker (plná pipeline)

Vyžaduje Docker Desktop (WSL2).

```powershell
docker compose -f docker-compose.dev.yml up --build
```

- http://localhost:8672
- změny v `app/`, `web/`, `configs/` → **automatický reload**
- data v `./data/`

Smoke test proti běžící instanci:

```powershell
# jen upload (fake LAZ, pipeline spadne – OK pro test API)
python scripts/smoke_e2e.py

# celá pipeline se Šance daty (dlouhé)
python scripts/smoke_e2e.py --data-dir D:\Downloads\karttapullautin-x86_64-win\sance --wait-minutes 30
```

---

## 3. Skript `scripts/dev.ps1`

```powershell
.\scripts\dev.ps1 test      # pytest
.\scripts\dev.ps1 up        # docker compose dev
.\scripts\dev.ps1 smoke     # smoke_e2e proti localhost:8672
.\scripts\dev.ps1 all         # test → build → smoke upload
```

---

## 4. NAS deploy až po zelených testech

```bash
# na NAS
./deploy-nas.sh
```

---

## Ladění selhání

| Kde | Příkaz |
|-----|--------|
| pytest | `pytest tests/ -v --tb=short` |
| Docker log | `docker compose -f docker-compose.dev.yml logs -f` |
| Job log API | `curl http://127.0.0.1:8672/api/jobs/<id>/log` |
| NAS | `docker compose -f docker-compose.nas.yml logs --tail=100` |

---

## Co pytest neřeší

- PDAL merge, pullauta běh – to až `docker compose dev` + `--data-dir`
- Reverse proxy limit – testujte HTTPS zvlášť po nasazení nginx conf
