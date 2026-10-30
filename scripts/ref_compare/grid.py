import numpy as np
from PIL import Image
Image.MAX_IMAGE_PIXELS=None
def read_pgw(p):
    A,D,B,E,C,F=[float(v) for v in open(p).read().split()]; return A,D,B,E,C,F
def warp(png,pgw,x0,y1,res,W,H,dx=0,dy=0):
    """nearest-neighbour sample PNG(+pgw) into grid: col i -> x0+(i+.5)res, row j -> y1-(j+.5)res. Returns RGBA uint8."""
    A,D,B,E,C,F=read_pgw(pgw)
    img=np.asarray(Image.open(png).convert('RGBA'))
    yy,xx=np.mgrid[0:H,0:W]; X=x0+(xx+.5)*res-dx; Y=y1-(yy+.5)*res-dy
    det=A*E-B*D; ex=X-C; ey=Y-F
    c=np.rint((E*ex-B*ey)/det).astype(np.int64); r=np.rint((-D*ex+A*ey)/det).astype(np.int64)
    ok=(c>=0)&(c<img.shape[1])&(r>=0)&(r<img.shape[0])
    out=np.zeros((H,W,4),np.uint8); out[ok]=img[r[ok],c[ok]]
    if img.shape[2]==4: pass
    return out
