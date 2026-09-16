import re
from pathlib import Path

for name in ["ISOM_2017-2_10000.omap", "ISOM_2017-2_15000.omap", "ISSprOM_2019_4000.omap"]:
    p = Path("configs/oom") / name
    if not p.exists():
        print(name, "missing")
        continue
    t = p.read_text(encoding="utf-8")
    print("===", name)
    for code in ["521", "523", "526"]:
        for m in re.finditer(rf'<symbol\b[^>]*\bcode="{code}"[^>]*>', t):
            n = re.search(r'name="([^"]+)"', m.group(0))
            print(f"  {code}: {n.group(1) if n else '?'}")
