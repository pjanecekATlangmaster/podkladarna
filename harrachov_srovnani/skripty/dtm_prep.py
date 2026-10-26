"""Příprava DTM Libereckého kraje do lokální work kopie jobu (job na testu B běžel bez DTM pro les/MTBO)."""
import os, sys, time, json
from pathlib import Path
os.environ["PODKLADARNA_DATA"] = r"C:\Users\PetrJanecek\source\repos\podkladarna\data"
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from app.pipeline.dtm_import import prepare_dtm_fences
work = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_work")
bbox = (15.395793914794924, 50.749599033556066, 15.43458938598633, 50.77522444808735)
t = time.time()
r = prepare_dtm_fences(work, bbox, log=lambda m: print("   ", m, flush=True), force=True)
print("DTM hotovo", r, round(time.time() - t, 1), "s")
