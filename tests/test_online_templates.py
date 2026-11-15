import xml.etree.ElementTree as ET

from app.pipeline.build_oom_map import _template_xml
from app.pipeline.fetch_openzu import crop_bounds_5514
from app.pipeline.oom_layers import collect_oom_templates
from app.pipeline import online_templates as ot
from app.pipeline.online_templates import MAX_PX, write_online_templates


def test_online_templates_files_and_omap_entries(tmp_path, monkeypatch):
    """Online šablony: GDAL WMS .xml ve výřezu do MAX_PX, OSM přes gdalwarp do S-JTSK; v .omap GdalTemplate v seznamu, ale skrytá."""
    calls = []

    def fake_run(cmd, *, cwd=None, **_k):
        calls.append(cmd)
        (cwd / cmd[-1]).write_text("<VRTDataset/>", encoding="utf-8")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(ot, "find_tool", lambda name: name)
    monkeypatch.setattr(ot, "run_cmd", fake_run)
    bbox = (14.305, 48.80, 14.33, 48.82)
    built = write_online_templates(crop_bounds_5514(*bbox), tmp_path)
    # 12 vrstev × maximální a střední zoom
    assert len(built) == 24 and all(p.parent == tmp_path for p in built.values())
    assert built["online_orthophoto_stredni"].name == "ortofoto_cuzk_stredni.xml"
    # OSM: TMS vedle VRT, okno výřezu v S-JTSK do MAX_PX; zdroj relativně (gdalwarp v references/)
    sizes = []
    for cmd, vrt in zip(calls, ("osm.vrt", "osm_stredni.vrt")):
        assert cmd[cmd.index("-t_srs") + 1] == ot.CRS_PROJ4 and cmd[-2:] == ["osm_dlazdice.xml", vrt]
        sizes.append(max(int(v) for v in cmd[cmd.index("-ts") + 1:cmd.index("-ts") + 3]))
    assert MAX_PX >= sizes[0] > sizes[1]
    assert ET.parse(tmp_path / "osm_dlazdice.xml").getroot().findtext("DataWindow/TileLevel") == "18"
    for key, path in built.items():
        if key.startswith("online_osm"):
            continue
        root = ET.parse(path).getroot()
        size = (int(root.findtext("DataWindow/SizeX")), int(root.findtext("DataWindow/SizeY")))
        assert max(size) <= MAX_PX and root.findtext("Projection") == "EPSG:5514"
        styles = root.findtext("Service/Styles") or ""
        server = root.findtext("Service/ServerUrl")
        assert styles == ("" if any(h in server for h in ot.NO_DEFAULT_STYLE_HOSTS) else "default")
        base = key.removesuffix("_stredni")
        assert root.findtext("BandsCount") == ("4" if base in {"online_katastr", "online_dtm_plochy", "online_dtm_linie", "online_orthophoto_archiv"} else "3")
    archiv = ET.parse(built["online_orthophoto_archiv"]).getroot()
    assert archiv.findtext("Service/Layers") == "2005,2003,2004" and archiv.findtext("Service/ImageFormat") == "image/png"
    full = ET.parse(built["online_orthophoto"]).getroot()
    mid = ET.parse(built["online_orthophoto_stredni"]).getroot()
    assert int(full.findtext("DataWindow/SizeX")) > int(mid.findtext("DataWindow/SizeX"))

    templates = [t for t in collect_oom_templates(tmp_path, built_refs=built) if t.kind == "gdal"]
    assert len(templates) == 24 and all(t.loaded and not t.visible for t in templates)
    xml = _template_xml(next(t for t in templates if t.relpath == "references/ortofoto_cuzk.xml"))
    assert 'type="GdalTemplate" open="true"' in xml and 'path="references/ortofoto_cuzk.xml"' in xml


def test_online_templates_without_gdalwarp_skip_osm(tmp_path, monkeypatch):
    """Bez gdalwarp se OSM online nezapíše (okno v EPSG:3857 Mapper nenačte), ostatní ano."""
    monkeypatch.setattr(ot, "find_tool", lambda name: (_ for _ in ()).throw(RuntimeError(name)))
    built = write_online_templates((-770000.0, -1183000.0, -769000.0, -1182000.0), tmp_path)
    assert len(built) == 22 and not any(k.startswith("online_osm") for k in built)
    assert not (tmp_path / "osm_dlazdice.xml").exists()


def test_online_templates_only_online_layers(tmp_path, monkeypatch):
    """Se staženými rastry jen vrstvy bez staženého protějšku (archivní ortofoto), bez OSM; sklon je jako stínování."""
    monkeypatch.setattr(ot, "find_tool", lambda name: (_ for _ in ()).throw(RuntimeError(name)))
    built = write_online_templates(
        (-770000.0, -1183000.0, -769000.0, -1182000.0), tmp_path, only=ot.ONLINE_ONLY_KEYS
    )
    assert sorted(built) == ["online_orthophoto_archiv", "online_orthophoto_archiv_stredni"]
    full = write_online_templates((-770000.0, -1183000.0, -769000.0, -1182000.0), tmp_path)
    assert ET.parse(full["online_sklon"]).getroot().findtext("Service/Layers") == "dmr5g:SlopeRGBMap"


def test_foreign_templates_downloaded_when_requested(tmp_path, monkeypatch):
    """Zahraničí se „Stáhnout předem“: každá vrstva jako rastr + world file (ortofoto JPEG, katastr PNG), varianty pryč;
    vrstva, jejíž stažení selže, zůstane online. V .omap stažený rastr jako obyčejná šablona obrázku."""
    import subprocess

    calls = []

    def fake_translate(cmd, *, cwd=None, **_k):
        calls.append(cmd)
        src, dest = cmd[-2], cmd[-1]
        if src.startswith("plan_ign"):
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP error 502")
        (cwd / dest).write_bytes(b"raster")
        (cwd / dest).with_suffix(".wld").write_text("0.2\n0\n0\n-0.2\n0\n0\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ot, "find_tool", lambda name: (_ for _ in ()).throw(RuntimeError(name)) if name == "gdalwarp" else name)
    monkeypatch.setattr(subprocess, "run", fake_translate)
    written = write_online_templates((-625000.0, -1200000.0, -624000.0, -1199000.0), tmp_path, country="FR")
    assert sorted(written) == sorted(f"online_fr_{k}{s}" for k in ("ortho", "plan", "cadastre") for s in ("", "_stredni"))
    out = ot.download_online_templates(written, "FR")
    assert len(calls) == 3 and all("_stredni" not in c[-2] for c in calls)  # jen nejjemnější varianta
    assert out["online_fr_ortho"].name == "ortho_ign.jpg" and (tmp_path / "ortho_ign.jgw").is_file()
    assert out["online_fr_cadastre"].name == "cadastre_ign.png" and (tmp_path / "cadastre_ign.pgw").is_file()
    assert "online_fr_ortho_stredni" not in out and not (tmp_path / "ortho_ign.xml").exists()
    assert out["online_fr_plan"].suffix == ".xml" and "online_fr_plan_stredni" in out  # selhalo → online zůstává
    ortho_cmd = next(c for c in calls if c[-2] == "ortho_ign.xml")
    assert ortho_cmd[ortho_cmd.index("-of") + 1] == "JPEG" and "-b" in ortho_cmd

    kinds = {t.relpath: t.kind for t in collect_oom_templates(tmp_path, built_refs=out)}
    assert kinds["references/ortho_ign.jpg"] == "image" and kinds["references/plan_ign.xml"] == "gdal"
