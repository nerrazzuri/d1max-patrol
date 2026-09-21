#!/usr/bin/env python3
"""Turn a MOLA point-cloud map (PLY, ASCII) into gravity-aligned deliverables:
  - <out>_wallplan.png : clean top-down wall-slab floor plan
  - <out>.pgm / <out>.yaml : ROS map_server occupancy grid
      black=occupied, white=free, gray=unknown

Occupancy free-space method:
  - raytrace (default, needs --traj): cast rays from each sensor pose to walls and
    mark traversed cells free -> thin walls + filled interior (commercial-style).
    Unobserved cells stay unknown (honest).
  - floorpoint (fallback if no --traj): cells with observed floor points = free.

Gravity/up is estimated from the map's dominant (floor) plane, so it is not
hard-coded to any one sensor mounting.

Usage: render_floormap.py <input.ply> <out_basename> [--traj traj.tum]
                          [--res 0.05] [--subsample 1] [--max-range 12]
"""
import sys, argparse, numpy as np
from PIL import Image, ImageDraw

def load_ply_xyz(path, sub):
    xs=[]
    with open(path) as fh:
        for line in fh:
            if line.startswith("end_header"): break
        for i,line in enumerate(fh):
            if sub>1 and (i % sub): continue
            p=line.split()
            try: xs.append((float(p[0]),float(p[1]),float(p[2])))
            except: pass
    return np.array(xs)

def estimate_up(P):
    rng=np.random.default_rng(0); best_n=None; best=0; best_d=0.0
    idx=rng.choice(len(P), min(len(P),20000), replace=False); Q=P[idx]
    for _ in range(300):
        s=Q[rng.choice(len(Q),3,replace=False)]
        n=np.cross(s[1]-s[0],s[2]-s[0]); nn=np.linalg.norm(n)
        if nn<1e-9: continue
        n/=nn; d=-n.dot(s[0]); cnt=int((np.abs(Q.dot(n)+d)<0.05).sum())
        if cnt>best: best=cnt; best_n=n; best_d=d
    n=best_n
    if np.median(P.dot(n)) < -best_d: n=-n
    return n/np.linalg.norm(n)

def raytrace_passes(occ_strong, poses, max_r, n_ang=540):
    """poses: Nx2 int (col,row). Returns per-cell ray-pass count (log-odds 'miss' count).
    Rays stop at occ_strong (walls). Cells traversed before a wall accumulate passes."""
    Hc,Wc=occ_strong.shape
    passes=np.zeros((Hc,Wc),np.int32)
    angs=np.linspace(0,2*np.pi,n_ang,endpoint=False); ca,sa=np.cos(angs),np.sin(angs)
    steps=np.arange(1,max_r)
    for px,py in poses:
        xs=(px+np.outer(ca,steps)).astype(int); ys=(py+np.outer(sa,steps)).astype(int)
        valid=(xs>=0)&(xs<Wc)&(ys>=0)&(ys<Hc)
        for r in range(n_ang):
            xr=xs[r][valid[r]]; yr=ys[r][valid[r]]
            if len(xr)==0: continue
            hit=occ_strong[yr,xr]
            stop=int(np.argmax(hit)) if hit.any() else len(xr)
            passes[yr[:stop],xr[:stop]]+=1
        if 0<=py<Hc and 0<=px<Wc: passes[py,px]+=1
    return passes

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("ply"); ap.add_argument("out")
    ap.add_argument("--traj",default=None,help="trajectory TUM for ray-traced occupancy")
    ap.add_argument("--res",type=float,default=0.05)
    ap.add_argument("--subsample",type=int,default=1)
    ap.add_argument("--max-range",type=float,default=12.0)
    ap.add_argument("--traj-margin",type=float,default=12.0,help="drop obstacle pts farther than this (m) from trajectory -> removes far glass ghosts; 0=off")
    ap.add_argument("--floor-clear",type=float,default=0.7,help="obstacle slab starts this high above floor -> skips robot's own body (removes trajectory-hugging ghost smear)")
    ap.add_argument("--wall-top",type=float,default=1.8,help="obstacle slab top above floor")
    a=ap.parse_args()
    P=load_ply_xyz(a.ply,a.subsample)
    if len(P)==0: sys.exit("no points")
    up=estimate_up(P)
    ref=np.array([0,1,0.0]) if abs(up[1])<0.9 else np.array([1,0,0.0])
    e1=ref-up*(ref@up); e1/=np.linalg.norm(e1); e2=np.cross(up,e1)
    u=P@e1; v=P@e2; h=P@up
    hist,edges=np.histogram(h,bins=300); floor=edges[np.argmax(hist)]
    # obstacle slab: floor+floor_clear .. floor+wall_top. Starting ABOVE the robot
    # body removes the dense ghost smear that hugs the trajectory (robot's own legs
    # /near-field returns), while keeping walls+pillars (they span this height).
    wall=(h>floor+a.floor_clear)&(h<floor+a.wall_top); flr=(h>floor-0.15)&(h<floor+0.25)

    # trajectory: load once; used for margin-crop (remove far glass ghosts) + raytrace
    Traj=None
    if a.traj:
        Traj=np.loadtxt(a.traj)[:,1:4]; tu=Traj@e1; tv=Traj@e2
        if a.traj_margin>0:
            try:
                from scipy.spatial import cKDTree
                d,_=cKDTree(np.stack([tu,tv],1)).query(np.stack([u,v],1),k=1)
                wall=wall&(d<a.traj_margin)
            except Exception as ex:
                print("  (traj-margin crop skipped: %s)"%ex)

    # ---- wall-slab floor plan PNG ----
    thin=wall
    uu,vv=u[thin],v[thin]
    W,H=1400,1500; img=Image.new("RGB",(W,H),"white"); px=img.load()
    umin,umax=np.percentile(uu,0.3),np.percentile(uu,99.7); vmin,vmax=np.percentile(vv,0.3),np.percentile(vv,99.7)
    sc=min((W-60)/(umax-umin),(H-60)/(vmax-vmin))
    for i in range(len(uu)):
        x=int(30+(uu[i]-umin)*sc); y=int(H-30-(vv[i]-vmin)*sc)
        if 0<=x<W and 0<=y<H: px[x,y]=(20,20,20)
    ImageDraw.Draw(img).line([(40,H-40),(40+5*sc,H-40)],fill=(0,0,0),width=3)
    img.save(a.out+"_wallplan.png")

    # ---- occupancy grid ----
    res=a.res
    umn=u.min(); vmn=v.min()
    if a.traj:
        Traj=np.loadtxt(a.traj)[:,1:4]; tu=Traj@e1; tv=Traj@e2
        umn=min(umn,tu.min()); vmn=min(vmn,tv.min())
    Wc=int((max(u.max(), (Traj@e1).max() if a.traj else u.max())-umn)/res)+2
    Hc=int((max(v.max(), (Traj@e2).max() if a.traj else v.max())-vmn)/res)+2
    occ=np.zeros((Hc,Wc),np.int32); fre=np.zeros((Hc,Wc),np.int32)
    gu=((u[wall]-umn)/res).astype(int); gv=((v[wall]-vmn)/res).astype(int)
    m=(gu>=0)&(gu<Wc)&(gv>=0)&(gv<Hc); np.add.at(occ,(gv[m],gu[m]),1)
    if a.traj:
        pc=np.stack([((tu-umn)/res).astype(int),((tv-vmn)/res).astype(int)],1)
        key=(pc[:,0]//6)*100000+(pc[:,1]//6); _,idx=np.unique(key,return_index=True)
        poses=pc[np.sort(idx)]
        # log-odds: rays stop at strong walls; cells rays pass through often are demoted to free.
        # This removes dynamic objects / transient clutter (people, glass returns) that a raw
        # point-count occupancy would wrongly mark occupied.
        passes=raytrace_passes(occ>=3, poses, int(a.max_range/res))
        ratio=occ/np.maximum(occ+passes,1)
        occ_m=(occ>=5)&(ratio>0.25)
        free_m=(passes>=1)&~occ_m
        method="raytrace-logodds"
    else:
        occ_m=occ>=3
        gu2=((u[flr]-umn)/res).astype(int); gv2=((v[flr]-vmn)/res).astype(int)
        m2=(gu2>=0)&(gu2<Wc)&(gv2>=0)&(gv2<Hc); np.add.at(fre,(gv2[m2],gu2[m2]),1)
        free_m=(fre>=1)&~occ_m; method="floorpoint"

    # clean the free space: rays that leak through undetected glass walls carve a
    # thin "fan" of free cells. Morphological OPENING removes thin lines/protrusions
    # (the fan) while preserving broad free areas (rooms, and space behind the fan)
    # -> only the thin fan line is dropped, everything broad stays.
    if a.traj:
        try:
            from scipy.ndimage import binary_opening
            free_m=binary_opening(free_m, iterations=2)
        except Exception as ex:
            print("  (free-space cleanup skipped: %s)"%ex)

    # keep only obstacles bordering the traversed (free) space -> drops floating
    # exterior ghosts (glass reflections) while keeping the walls that bound where
    # the robot actually went.
    if a.traj:
        try:
            from scipy.ndimage import binary_dilation
            occ_m = occ_m & binary_dilation(free_m, iterations=int(0.5/res))
        except Exception as ex:
            print("  (occ-near-free filter skipped: %s)"%ex)

    grid=np.full((Hc,Wc),205,np.uint8); grid[free_m]=254; grid[occ_m]=0
    obs=grid!=205; ys,xs=np.where(obs)
    y0,x0=max(ys.min()-5,0),max(xs.min()-5,0); y1,x1=ys.max()+6,xs.max()+6
    grid=grid[y0:y1,x0:x1]; imgp=np.flipud(grid)
    with open(a.out+".pgm","wb") as fp:
        fp.write(f"P5\n{grid.shape[1]} {grid.shape[0]}\n255\n".encode()); fp.write(imgp.tobytes())
    open(a.out+".yaml","w").write(
        f"image: {a.out.split('/')[-1]}.pgm\nresolution: {res}\n"
        f"origin: [{umn+x0*res:.3f}, {vmn+y0*res:.3f}, 0.0]\nnegate: 0\n"
        f"occupied_thresh: 0.65\nfree_thresh: 0.196\n")
    print(f"wrote {a.out}_wallplan.png, {a.out}.pgm ({grid.shape[1]}x{grid.shape[0]}), {a.out}.yaml")
    print(f"  up={up.round(3)} floor_h={floor:.2f} occupancy={method} "
          f"occ={int((grid==0).sum())} free={int((grid==254).sum())} unk={int((grid==205).sum())}")

if __name__=="__main__": main()
