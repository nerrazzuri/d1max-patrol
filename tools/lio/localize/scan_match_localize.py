import rclpy, numpy as np, time, sys
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from scipy.ndimage import distance_transform_edt
import yaml as y
from PIL import Image

MAPYAML=sys.argv[1]; OUT=sys.argv[2]; PLY=sys.argv[3]; TRAJ=sys.argv[4]
meta=y.safe_load(open(MAPYAML)); res=meta['resolution']; ox,oy=meta['origin'][0],meta['origin'][1]
mp=np.array(Image.open(MAPYAML.replace('.yaml','.pgm'))); H,W=mp.shape
occ=(mp<100); dt=distance_transform_edt(~occ)*res
orr,occ_c=np.where(occ); wx=ox+occ_c*res; wy=oy+(H-1-orr)*res

# ---- e1,e2 from up (same as render_floormap, seed 0) ----
def load_ply(path,sub):
    xs=[]
    with open(path) as fh:
        for l in fh:
            if l.startswith("end_header"): break
        for i,l in enumerate(fh):
            if i%sub: continue
            p=l.split()
            try: xs.append((float(p[0]),float(p[1]),float(p[2])))
            except: pass
    return np.array(xs)
def est_up(P):
    rng=np.random.default_rng(0); bn=None;b=0;bd=0
    idx=rng.choice(len(P),min(len(P),20000),replace=False);Q=P[idx]
    for _ in range(300):
        s=Q[rng.choice(len(Q),3,replace=False)]
        n=np.cross(s[1]-s[0],s[2]-s[0]);nn=np.linalg.norm(n)
        if nn<1e-9:continue
        n/=nn;d=-n.dot(s[0]);c=int((np.abs(Q.dot(n)+d)<0.05).sum())
        if c>b:b=c;bn=n;bd=d
    n=bn
    if np.median(P.dot(n))<-bd:n=-n
    return n/np.linalg.norm(n)
P=load_ply(PLY,20); up=est_up(P)
ref=np.array([0,1,0.0]) if abs(up[1])<0.9 else np.array([1,0,0.0])
e1=ref-up*(ref@up);e1/=np.linalg.norm(e1);e2=np.cross(up,e1)
T=np.loadtxt(TRAJ)[:,1:4]; tu=T@e1; tv=T@e2
gx,gy=tu[-1],tv[-1]                      # 轨迹末=初值位置
dvec=np.array([tu[-1]-tu[-5],tv[-1]-tv[-5]]); gth=np.arctan2(dvec[1],dvec[0]) if np.linalg.norm(dvec)>0.1 else 0.0
print(f"轨迹末(初值): x={gx:.2f} y={gy:.2f} heading≈{np.degrees(gth):.0f}°")

class C(Node):
    def __init__(s):
        super().__init__("loc"); s.pts=None
        s.create_subscription(PointCloud2,"/front_lidar",s.cb,qos_profile_sensor_data)
    def cb(s,m):
        if s.pts is not None:return
        buf=np.frombuffer(m.data,dtype=np.uint8).reshape(-1,m.point_step)
        a=np.frombuffer(buf[:,:12].tobytes(),dtype=np.float32).reshape(-1,3)
        a=a[np.isfinite(a).all(1)]
        xh=a[:,0]; lo,hi=np.percentile(xh,30),np.percentile(xh,55); b=(xh>lo)&(xh<hi)
        yz=a[b][:,1:3]; r=np.linalg.norm(yz,axis=1); yz=yz[(r>1.0)&(r<15.0)]
        if len(yz)>3000: yz=yz[np.random.choice(len(yz),3000,replace=False)]
        s.pts=yz
rclpy.init();c=C();t=time.time()
while time.time()-t<12 and c.pts is None and rclpy.ok(): rclpy.spin_once(c,timeout_sec=0.2)
if c.pts is None: print("没雷达");sys.exit(1)
scan=c.pts
def score(px,py,th):
    ct,st=np.cos(th),np.sin(th)
    X=px+ct*scan[:,0]-st*scan[:,1]; Y=py+st*scan[:,0]+ct*scan[:,1]
    c_=((X-ox)/res).astype(int); r_=(H-1-((Y-oy)/res)).astype(int)
    ok=(c_>=0)&(c_<W)&(r_>=0)&(r_<H)
    if ok.sum()<50:return 1e9
    return float(np.mean(dt[r_[ok],c_[ok]]))
# 局部搜索:轨迹末±4m,朝向以 gth 为中心±180°(但从真值附近种子)
best=(1e9,gx,gy,gth)
for px in np.arange(gx-4,gx+4,0.4):
    for py in np.arange(gy-4,gy+4,0.4):
        for th in np.linspace(gth-np.pi,gth+np.pi,36,endpoint=False):
            sc=score(px,py,th)
            if sc<best[0]:best=(sc,px,py,th)
_,bx,by,bth=best
for scale in (0.15,0.05):
    for px in np.arange(bx-0.5,bx+0.5,scale):
        for py in np.arange(by-0.5,by+0.5,scale):
            for th in np.linspace(bth-0.2,bth+0.2,11):
                sc=score(px,py,th)
                if sc<best[0]:best=(sc,px,py,th)
    _,bx,by,bth=best
print(f"定位: x={bx:.2f} y={by:.2f} yaw={np.degrees(bth):.1f}° 匹配={best[0]:.2f}m")
ct,st=np.cos(bth),np.sin(bth)
SX=bx+ct*scan[:,0]-st*scan[:,1]; SY=by+st*scan[:,0]+ct*scan[:,1]
fig,ax=plt.subplots(figsize=(10,16),dpi=100)
ax.scatter(wx,wy,s=0.5,c='0.6'); ax.scatter(SX,SY,s=1,c='red')
ax.plot(tu,tv,'-',c='orange',lw=0.8,alpha=0.6)   # 轨迹
ax.plot(bx,by,'b*',ms=25); ax.arrow(bx,by,1.5*ct,1.5*st,color='blue',width=0.15)
ax.set_aspect('equal'); ax.set_title(f'定位(轨迹初值): ({bx:.1f},{by:.1f}) yaw{np.degrees(bth):.0f}° 匹配{best[0]:.2f}m')
fig.savefig(OUT,bbox_inches='tight',facecolor='white'); print("saved",OUT)
