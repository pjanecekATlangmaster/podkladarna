import xml.etree.ElementTree as ET

from app.pipeline.build_oom_map import _template_xml
from app.pipeline.fetch_openzu import crop_bounds_5514
from app.pipeline.oom_layers import collect_oom_templates
from app.pipeline.online_templates import MAX_PX, write_online_templates


def test_online_templates_files_and_omap_entries(tmp_path):
    """Online šablony: GDAL WMS .xml ve výřezu do MAX_PX, OSM okno .vrt nad TMS; v .omap GdalTemplate nenačtená a skrytá."""
    bbox = (14.305, 48.80, 14.33, 48.82)
    built = write_online_templates(crop_bounds_5514(*bbox), bbox, tmp_path)
    assert len(built) == 10 and all(p.parent == tmp_path for p in built.values())
    for key, path in built.items():
        root = ET.parse(path).getroot()
        if key == "online_osm":
            assert root.tag == "VRTDataset" and int(root.get("rasterXSize")) <= MAX_PX
            assert root.find(".//SourceFilename").text == "osm_dlazdice.xml"
            continue
        size = (int(root.findtext("DataWindow/SizeX")), int(root.findtext("DataWindow/SizeY")))
        assert max(size) <= MAX_PX and root.findtext("Projection") == "EPSG:5514"
        styles = root.findtext("Service/Styles") or ""
        assert styles == ("" if "dmvs.cuzk.gov.cz" in root.findtext("Service/ServerUrl") else "default")
        assert root.findtext("BandsCount") == ("4" if key in {"online_katastr", "online_dtm_plochy", "online_dtm_linie"} else "3")

    templates = [t for t in collect_oom_templates(tmp_path, built_refs=built) if t.kind == "gdal"]
    assert len(templates) == 10 and not any(t.visible or t.loaded for t in templates)
    xml = _template_xml(next(t for t in templates if t.relpath == "references/ortofoto_cuzk.xml"))
    assert 'type="GdalTemplate" open="false"' in xml and 'path="references/ortofoto_cuzk.xml"' in xml
