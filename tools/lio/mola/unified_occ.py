#!/usr/bin/env python3
"""Density-independent, unified occupancy renderer for FAIR SLAM comparison.
Same voxel, Z-range, resolution, threshold for any input cloud.
Steps: estimate up (RANSAC floor) -> Z-crop [floor+ZLO, floor+ZHI] ->
       3D voxel-dedup @ VOX (equalize density) -> 2D grid @ RES, occupied if >=1 dedup point.
Usage: unified_occ.py <in.ply> <out.png> [--sub N] [--vox 0.05] [--res 0.05] [--zlo 0.2] [--zhi 1.8] [--label TEXT]
"""
import sys, argparse, numpy as np
from PIL import Image, ImageDraw

def load(path, sub):
    xs=[]
    with open(path) as f:
        for line in f:
            if line.startswith("end_header"): break
        for i,line in enumerate(f):
            if sub>1 and i%sub: continue
            p=line.split()
            try: xs.append((float(p[0]),float(p[1]),float(p[2])))
            except: pass
    return np.array(xs)

def est_up(P):
    rng=np.random.default_rng(0); best=0; bn=None; bd=0
    Q=P[rng.choice(len(P),min(len(P),20000),replace=False)]
    for _ in range(300):
        s=Q[rng.choice(len(Q),3,replace=False)]; n=np.cross(s[1]-s[0],s[2]-s[0]); nn=np.linalg.norm(n)
        if nn<1e-9: continue
        n/=nn; d=-n.dot(s[0]); c=int((np.abs(Q.dot(n)+d)<0.05).sum())
        if c>best: best=c; bn=n; bd=d
    n=bn/np.linalg.norm(bn)
    if np.median(P@n) < -bd: n=-n
    return n

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("ply"); ap.add_argument("out")
    ap.add_argument("--sub",type=int,default=1); ap.add_argument("--vox",type=float,default=0.05)
    ap.add_argument("--res",type=float,default=0.05); ap.add_argument("--zlo",type=float,default=0.2)
    ap.add_argument("--zhi",type=float,default=1.8); ap.add_argument("--label",default="")
    a=ap.parse_args()
    P=load(a.ply,a.sub)
    up=est_up(P)
    ref=np.array([1,0,0.]) if abs(up[0])<0.9 else np.array([0,1,0.])
    e1=ref-up*(ref@up); e1/=np.linalg.norm(e1); e2=np.cross(up,e1)
    u=P@e1; v=P@e2; h=P@up
    hist,edges=np.histogram(h,bins=400); floor=edges[np.argmax(hist)]
    slab=(h-floor>a.zlo)&(h-floor<a.zhi)
    u,v,h=u[slab],v[slab],h[slab]
    # 3D voxel-dedup at VOX -> density equalized
    keys=np.stack([np.floor(u/a.vox),np.floor(v/a.vox),np.floor(h/a.vox)],1).astype(np.int64)
    _,idx=np.unique(keys[:,0]*73856093 ^ keys[:,1]*19349663 ^ keys[:,2]*83492791, return_index=True)
    u,v=u[idx],v[idx]
    # 2D occupancy @ res, occupied if >=1 dedup point
    umn,vmn=u.min(),v.min()
    gu=((u-umn)/a.res).astype(int); gv=((v-vmn)/a.res).astype(int)
    Wc,Hc=gu.max()+1,gv.max()+1
    occ=np.zeros((Hc,Wc),bool); occ[gv,gu]=True
    img=np.where(np.flipud(occ),0,255).astype(np.uint8)
    im=Image.fromarray(img,"L").convert("RGB")
    d=ImageDraw.Draw(im)
    d.line([(20,Hc-20),(20+int(5/a.res),Hc-20)],fill=(255,0,0),width=3)
    d.text((10,8),f"{a.label}  occ_cells={int(occ.sum())}  vox={a.vox} Z[{a.zlo},{a.zhi}]",fill=(0,0,0))
    im.save(a.out)
    print(f"{a.label}: dedup_pts={len(u)} occ_cells={int(occ.sum())} grid={Wc}x{Hc}")

if __name__=="__main__": main()
