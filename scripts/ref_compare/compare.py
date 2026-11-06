"""compare.py REF.png GEOREF.json OUR.geojson JOBDIR OUTDIR  – cesty a porosty: ruční reference vs. naše .omap (0,5 m mřížka)"""
import sys,json,numpy as np,shapefile
sys.path.insert(0,sys.path[0]); from grid import warp
from PIL import Image,ImageDraw
from scipy import ndimage as ndi
from shapely.geometry import shape,MultiPoint,Polygon,LineString
from shapely.strtree import STRtree
from collections import defaultdict
refp,geop,ourp,job,outd=sys.argv[1:6]
q=json.load(open(geop))['quad_5514']['vertices']
xs=[v['x'] for v in q]; ys=[v['y'] for v in q]
res=0.5; x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys)
W=int((x1-x0)/res); H=int((y1-y0)/res)
R=warp(refp,refp[:-4]+'.pgw',x0,y1,res,W,H)
rgb=R[...,:3].astype(np.int16); inside=R[...,3]>0
col=lambda c,t=20: (np.abs(rgb-np.array(c)).max(-1)<t)&inside
topx=lambda x,y:((x-x0)/res,(y1-y)/res)
tow=lambda c,r:(x0+(c+.5)*res,y1-(r+.5)*res)
# --- mapové pole: konvexní obal barevných pixelů (zelená, žlutá, hnědá) bez okrajů s logy
colored=col((120,232,96))|col((184,240,176))|col((248,216,152))|col((184,72,24),30)
# logy jsou mimo – omez na sloupce/řádky s hustou barevnou kresbou
rr,cc=np.nonzero(ndi.binary_opening(colored,iterations=3)[::8,::8])
pts=MultiPoint([tow(c*8,r*8) for r,c in zip(rr,cc)])
field=pts.convex_hull
if len(sys.argv)>6: field=field.intersection(shape(json.load(open(sys.argv[6]))))
core=field.buffer(-20)
def mask_poly(polys):
    m=Image.new('1',(W,H),0); d=ImageDraw.Draw(m)
    for p in polys:
        for g in (p.geoms if hasattr(p,"geoms") else [p]):
            if g.is_empty or g.geom_type!="Polygon": continue
            d.polygon([topx(*c) for c in g.exterior.coords],fill=1)
            for i in g.interiors: d.polygon([topx(*c) for c in i.coords],fill=0)
    return np.asarray(m)
fieldm=mask_poly([core])&inside
json.dump({'type':'Feature','properties':{},'geometry':json.loads(json.dumps(field.__geo_interface__))},open(f'{outd}/obrys.geojson','w'))
# --- reference: třídy
black=(rgb.max(-1)<90)&inside
open_ref=ndi.binary_opening(col((248,192,72))|col((248,216,152)),iterations=2)
y401=ndi.binary_opening(col((248,192,72)),iterations=2)
g1=col((184,240,176)); g2=col((120,232,96)); g3=col((40,216,0),40)
green_ref=ndi.binary_opening(g1|g2|g3,iterations=1)&~open_ref
blue=col((0,96,208),40)|col((0,208,208),40)
# --- naše plochy
feats=json.load(open(ourp))['features']
ours=np.zeros((H,W),np.uint8); val={'401.0':1,'403.0':2,'406.0':3,'408.0':4,'410.0':5}
for code in ['401.0','403.0','406.0','408.0','410.0']:
    ps=[]
    for f in feats:
        if f['properties']['code']!=code: continue
        try: ps.append(shape(f['geometry']).buffer(0))
        except Exception: pass
    ours[mask_poly(ps)]=val[code]
valid=fieldm&~ndi.binary_dilation(black|blue,iterations=3)
def f1(ref,our,k=8):
    rd=ndi.binary_dilation(ref,iterations=k); od=ndi.binary_dilation(our,iterations=k)
    p=(our&rd&valid).sum()/max(1,(our&valid).sum()); rc=(ref&od&valid).sum()/max(1,(ref&valid).sum())
    return dict(ref_podil=round(float((ref&valid).mean()/valid.mean()),3),nas_podil=round(float((our&valid).mean()/valid.mean()),3),precision=round(float(p),3),recall=round(float(rc),3),f1=round(float(2*p*rc/max(1e-9,p+rc)),3))
out={'plocha_ha':round(float(valid.sum()*res*res/1e4),1)}
out['otevreno_401_403']=f1(open_ref,(ours==1)|(ours==2))
out['jen_401']=f1(y401,ours==1)
out['zelen_406_410']=f1(green_ref,ours>=3)
y403=ndi.binary_opening(col((248,216,152)),iterations=2)
out['jen_403']=f1(y403,ours==2)
out['ref403_jako']={n:round(float((y403&valid&sel).sum()*res*res/1e4),1) for n,sel in [('401',ours==1),('403',ours==2),('zelen',ours>=3),('bily',ours==0)]}
# matice úrovní zeleně (podíl plochy)
lv_ref=np.zeros((H,W),np.uint8); lv_ref[g1]=1; lv_ref[g2]=2; lv_ref[g3]=3
mat={}
for a,an in [(0,'bily'),(1,'406'),(2,'408'),(3,'410')]:
    for b,bn in [(0,'bily/otevreno'),(3,'406'),(4,'408'),(5,'410')]:
        sel=valid&(lv_ref==a)&~open_ref & ((ours==b) if b else (ours<3))
        mat[f'ref {an} -> nas {bn}']=round(float(sel.sum()*res*res/1e4),1)
out['zelen_matice_ha']=mat
# --- cesty: podíl délky naší čáry s černou v referenci do 3 m
near=ndi.binary_dilation(black,iterations=6)
osm=[shape(f['geometry']) for f in json.load(open(f'{job}/work/osm_paths/paths_osm.geojson'))['features'] if f['geometry']]
osm_t=STRtree(osm)
def shp_lines(p):
    try: r=shapefile.Reader(p)
    except Exception: return []
    out=[]
    for s in r.shapes():
        pts=s.points; parts=list(s.parts)+[len(pts)]
        for i in range(len(parts)-1):
            if parts[i+1]-parts[i]>=2: out.append(LineString(pts[parts[i]:parts[i+1]]))
    return out
zc=shp_lines(f'{job}/_oom_objects/Cesta/Cesta.shp'); zp=shp_lines(f'{job}/_oom_objects/Pesina/Pesina.shp')
zc_t=STRtree(zc) if zc else None; zp_t=STRtree(zp) if zp else None
def src(p):
    if osm_t.query(p.buffer(4)).size and min(osm[i].distance(p) for i in osm_t.query(p.buffer(4)))<4: return 'OSM'
    if zp_t is not None and zp_t.query(p.buffer(4)).size and min(zp[i].distance(p) for i in zp_t.query(p.buffer(4)))<4: return 'ZABAGED Pěšina'
    if zc_t is not None and zc_t.query(p.buffer(4)).size and min(zc[i].distance(p) for i in zc_t.query(p.buffer(4)))<4: return 'ZABAGED Cesta'
    return 'jiný'
stat=defaultdict(lambda:[0,0]); ourline=Image.new('1',(W,H),0); dl=ImageDraw.Draw(ourline)
misses=[]
for f in feats:
    c=f['properties']['code']
    if not (c.startswith('50') and c not in ('501.0','501.1','501.2')) and not c.startswith('509'): continue
    if f['geometry']['type']!='MultiLineString': continue
    for line in f['geometry']['coordinates']:
        L=LineString(line); dl.line([topx(*p) for p in line],fill=1,width=int(4/res))
        n=max(2,int(L.length/4))
        for i in range(n+1):
            p=L.interpolate(i/n,normalized=True)
            if not core.contains(p): continue
            cx,ry=topx(p.x,p.y); cx=int(cx); ry=int(ry)
            if not (0<=ry<H and 0<=cx<W): continue
            key=(c.split('.')[0] if c!='502.2' else c, src(p))
            hit=bool(near[ry,cx]); stat[key][0]+=L.length/n; stat[key][1]+=(L.length/n)*hit
            if not hit: misses.append((p.x,p.y,key))
out['cesty']={f'{k[0]} {k[1]}':{'km':round(v[0]/1000,2),'shoda':round(v[1]/max(1,v[0]),2)} for k,v in sorted(stat.items())}
# reference: tenké černé čáry mimo naše cesty a mimo budovy/skály (hrubý odhad délky)
ol=ndi.binary_dilation(np.asarray(ourline),iterations=int(6/res))
thin=black&~ndi.binary_opening(black,iterations=3)   # bez plných ploch (budovy)
extra=thin&~ol&fieldm
lab,n=ndi.label(ndi.binary_dilation(extra,iterations=2))
sizes=ndi.sum(extra,lab,range(1,n+1))
keep=np.isin(lab,np.nonzero(sizes*res>=60)[0]+1)&extra   # komponenty s >= ~60 m čáry
out['reference_cerne_cary_mimo_nase_km']=round(float(keep.sum()*res/1000/ (2/res*res)),1)
json.dump(out,open(f'{outd}/metriky.json','w'),ensure_ascii=False,indent=1)
np.save(f'{outd}/extra.npy',np.packbits(keep)); json.dump({'x0':x0,'y1':y1,'res':res,'W':W,'H':H},open(f'{outd}/grid.json','w'))
json.dump(misses,open(f'{outd}/misses.json','w'))
print(json.dumps(out,ensure_ascii=False,indent=1))
