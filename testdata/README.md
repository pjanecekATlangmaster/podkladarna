# Testovací podklady (celé kolečko)

Malý výřez pro lokální E2E test pipeline (PDAL → pullauta → ZIP).

| Soubor | Popis |
|--------|--------|
| `DMR5G.laz` | model reliéfu |
| `DMP1G.laz` | model povrchu (lokální test; produkce stahuje DMP OK z openzu) |
| `Zabaged.zip` | ZABAGED shapefile |

## Použití

```powershell
# Docker dev musí běžet (.\scripts\dev.ps1 up)
.\scripts\dev.ps1 e2e          # upload + čekání na hotovo (~5–30 min)
.\scripts\dev.ps1 e2e-upload   # jen ověření uploadu
```

Nebo:

```powershell
python scripts/smoke_e2e.py --wait-minutes 30
```

Soubory jsou v gitu (~2,5 MB) – pytest je nepoužívá (běží v Dockeru s PDAL + pullauta).
