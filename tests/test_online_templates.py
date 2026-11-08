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
    assert len(built) == 10 and all(p.parent == tmp_path for p in built.values())
    # OSM: TMS vedle VRT, okno výřezu v S-JTSK do MAX_PX; zdroj relativně (gdalwarp v references/)
    cmd = calls[0]
    assert cmd[cmd.index("-t_srs") + 1] == ot.CRS_PROJ4 and cmd[-2:] == ["osm_dlazdice.xml", "osm.vrt"]
    assert max(int(v) for v in cmd[cmd.index("-ts") + 1:cmd.index("-ts") + 3]) <= MAX_PX
    assert ET.parse(tmp_path / "osm_dlazdice.xml").getroot().findtext("DataWindow/TileLevel") == "18"
    for key, path in built.items():
        if key == "online_osm":
            continue
        root = ET.parse(path).getroot()
        size = (int(root.findtext("DataWindow/SizeX")), int(root.findtext("DataWindow/SizeY")))
        assert max(size) <= MAX_PX and root.findtext("Projection") == "EPSG:5514"
        styles = root.findtext("Service/Styles") or ""
        assert styles == ("" if "dmvs.cuzk.gov.cz" in root.findtext("Service/ServerUrl") else "default")
        assert root.findtext("BandsCount") == ("4" if key in {"online_katastr", "online_dtm_plochy", "online_dtm_linie"} else "3")

    templates = [t for t in collect_oom_templates(tmp_path, built_refs=built) if t.kind == "gdal"]
    assert len(templates) == 10 and all(t.loaded and not t.visible for t in templates)
    xml = _template_xml(next(t for t in templates if t.relpath == "references/ortofoto_cuzk.xml"))
    assert 'type="GdalTemplate" open="true"' in xml and 'path="references/ortofoto_cuzk.xml"' in xml


def test_online_templates_without_gdalwarp_skip_osm(tmp_path, monkeypatch):
    """Bez gdalwarp se OSM online nezapíše (okno v EPSG:3857 Mapper nenačte), ostatní ano."""
    monkeypatch.setattr(ot, "find_tool", lambda name: (_ for _ in ()).throw(RuntimeError(name)))
    built = write_online_templates((-770000.0, -1183000.0, -769000.0, -1182000.0), tmp_path)
    assert len(built) == 9 and "online_osm" not in built
    assert not (tmp_path / "osm_dlazdice.xml").exists()
