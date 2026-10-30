"""omap2geo.py MAP.omap OUT.geojson  -> čáry a plochy z .omap v EPSG:5514 s kódem symbolu (bez křivek, jen vrcholy)"""
import sys,json,re
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[2]))
import xml.etree.ElementTree as ET
from pathlib import Path
from app.pipeline.oom_preview import parse_omap_georef, _read_omap_bytes
from app.pipeline.oom_coords import map_to_projected
root=ET.fromstring(_read_omap_bytes(Path(sys.argv[1])))
g=parse_omap_georef(root)
ns=lambda t:t.split('}')[-1]
codes={}
for el in root.iter():
    if ns(el.tag)=='symbol' and el.get('id') is not None and el.get('code'):
        codes[el.get('id')]=(el.get('code'),int(el.get('type','0')))
feats=[]
# objekty jen z částí mapy (ne z definic symbolů)
for parts in root.iter():
    if ns(parts.tag)!='parts': continue
    for obj in parts.iter():
        if ns(obj.tag)!='object' or obj.get('type') not in ('1',): continue
        sid=obj.get('symbol');
        if sid not in codes: continue
        code,st=codes[sid]
        c=next((e for e in obj if ns(e.tag)=='coords'),None)
        if c is None or not c.text: continue
        pts=[];rings=[[]]
        for tok in c.text.strip().strip(';').split(';'):
            a=tok.split()
            if len(a)<2: continue
            x,y=map_to_projected(float(a[0]),float(a[1]),ref_x=g.ref_x,ref_y=g.ref_y,scale=g.scale,grivation_deg=g.grivation_deg,combined_scale_factor=g.combined_scale_factor,map_ref_x=g.map_ref_x,map_ref_y=g.map_ref_y)
            rings[-1].append((round(x,2),round(y,2)))
            fl=int(a[2]) if len(a)>2 else 0
            if fl & 16 or fl & 32:  # 16 = close point (konec části), 32 = hole point
                rings.append([])
        rings=[r for r in rings if len(r)>=2]
        if not rings: continue
        if st==4:  # area
            geom={'type':'Polygon','coordinates':[r+[r[0]] for r in rings if len(r)>=3]}
            if not geom['coordinates']: continue
        else:
            geom={'type':'MultiLineString','coordinates':rings}
        feats.append({'type':'Feature','properties':{'code':code},'geometry':geom})
json.dump({'type':'FeatureCollection','features':feats},open(sys.argv[2],'w'))
from collections import Counter
print(len(feats), Counter(f['properties']['code'] for f in feats).most_common(40))
