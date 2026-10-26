"""Sestavení .omap z lokální work kopie jobu. Argumenty: varianty ve tvaru les:mixed mtbo:osm …"""
import os, sys, time, json
from pathlib import Path
os.environ["PODKLADARNA_DATA"] = r"C:\Users\PetrJanecek\source\repos\podkladarna\data"
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from app.pipeline.job_options import load_presets
from app.pipeline.package_oom import oom_metadata, prepare_oom_map
from app.pipeline.fetch_zabaged import ostatni_plocha_max_m2, resolve_ostatni_plocha
from app.pipeline.residual_paved import residual_max_m2, resolve_residual_size
base = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
work = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_work")
out = base / "mapy"; out.mkdir(exist_ok=True)
presets = load_presets()
options = json.loads((base / "mapy" / "options.json").read_text(encoding="utf-8"))
bbox = tuple(options["bbox_wgs84"])
PRESET = {"les": "forest_10000", "mtbo": "mtbo_10000"}
times = {}
tf = out / "build_times.json"
if tf.exists():
    times = json.loads(tf.read_text())
for arg in sys.argv[1:]:
    disc, src = arg.split(":")
    pid = PRESET[disc]; preset = presets[pid]
    meta = oom_metadata(pid, preset, options, "Harrachov")
    vc = Path(str(preset.get("vectorconf", "zabaged.txt"))).name
    name = f"Harrachov-{disc}-{src}"
    t = time.time()
    with open(out / f"build-{name}.log", "w", encoding="utf-8") as logf:
        log = lambda m: (logf.write(str(m) + "\n"), logf.flush())
        prepare_oom_map(work, out / f"{name}.omap", use_dtm=True, map_name="Harrachov", scale=10000, preset_id=pid,
                        bbox_wgs84=bbox, built_refs=None, zabaged_clean=work / "zabaged_clean.zip", vectorconf_name=vc,
                        include_dxf=True, contour_interval_m=meta.get("contour_interval_m"), formline=0,
                        indexcontours_m=options.get("indexcontours", 25.0), cliff_symbol=options.get("kp_cliff_symbol", "auto"),
                        courtyard_fill=options.get("sprint_courtyard_fill", "building"), contour_formlines="off",
                        path_source=src, aopk_trees=None,
                        max_ostatni_m2=ostatni_plocha_max_m2(resolve_ostatni_plocha(options)), ostatni_as_403=False,
                        residual_paved=False, max_residual_m2=residual_max_m2(resolve_residual_size(options)),
                        veg_smooth=int(options.get("veg_smooth", 0) or 0), log=log)
    times[name] = round(time.time() - t, 1); print(name, times[name], "s", flush=True)
    tf.write_text(json.dumps(times, indent=1))
