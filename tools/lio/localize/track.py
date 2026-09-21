import rclpy,numpy as np,time,json,sys
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from nav_msgs.msg import Odometry
from sensor_msgs.msg import PointCloud2
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
import yaml as y; from PIL import Image
MAPYAML=sys.argv[1]; OUT=sys.argv[2]
L=json.load(open("/tmp/claude-1000/-home-liang-Projects-D1-Max/a2d92937-41c9-46bd-a2f3-9b8ebc3add6a/scratchpad/lock.json"))
dth,tx,ty=L["dth"],L["tx"],L["ty"]; c_,s_=np.cos(dth),np.sin(dth)
meta=y.safe_load(open(MAPYAML));res=meta['resolution'];ox0,oy0=meta['origin'][0],meta['origin'][1]
mp=np.array(Image.open(MAPYAML.replace('.yaml','.pgm')));H,W=mp.shape
orr,occ_c=np.where(mp<100);wx=ox0+occ_c*res;wy=oy0+(H-1-orr)*res
def yaw_of(q): return np.arctan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
class C(Node):
    def __init__(s):
        super().__init__("track");s.od=None;s.pts=None
        s.create_subscription(Odometry,"/odom/current_pose",s.cbo,10)
        s.create_subscription(PointCloud2,"/front_lidar",s.cbp,qos_profile_sensor_data)
    def cbo(s,m):
        p=m.pose.pose.position;q=m.pose.pose.orientation; s.od=(p.x,p.y,yaw_of(q))
    def cbp(s,m):
        if s.pts is not None:return
        buf=np.frombuffer(m.data,dtype=np.uint8).reshape(-1,m.point_step)
        a=np.frombuffer(buf[:,:12].tobytes(),dtype=np.float32).reshape(-1,3);a=a[np.isfinite(a).all(1)]
        xh=a[:,0];lo,hi=np.percentile(xh,30),np.percentile(xh,55);b=(xh>lo)&(xh<hi)
        yz=a[b][:,1:3];r=np.linalg.norm(yz,axis=1);yz=yz[(r>1.0)&(r<15.0)]
        if len(yz)>2500:yz=yz[np.random.choice(len(yz),2500,replace=False)]
        s.pts=yz
rclpy.init();c=C();t=time.time()
while time.time()-t<8 and (c.od is None or c.pts is None) and rclpy.ok(): rclpy.spin_once(c,timeout_sec=0.2)
if c.od is None: print("没odom");sys.exit(1)
oxx,oyy,oth=c.od
mx=tx+c_*oxx-s_*oyy; my=ty+s_*oxx+c_*oyy; mth=oth+dth
# ---- scan 微修正(在 odom 预测附近 ±1.5m ±20°,鲁棒内点)----
from scipy.ndimage import distance_transform_edt
dt=distance_transform_edt(mp>=100)*res   # dist to occupied
def score(px,py,th):
    ct,st=np.cos(th),np.sin(th)
    X=px+ct*c.pts[:,0]-st*c.pts[:,1]; Y=py+st*c.pts[:,0]+ct*c.pts[:,1]
    cc=((X-ox0)/res).astype(int); rr=(H-1-((Y-oy0)/res)).astype(int)
    ok=(cc>=0)&(cc<W)&(rr>=0)&(rr<H)
    if ok.sum()<50: return 0
    return int((dt[rr[ok],cc[ok]]<0.3).sum())
if c.pts is not None:
    best=(score(mx,my,mth),mx,my,mth)
    for px in np.arange(mx-1.5,mx+1.5,0.15):
        for py in np.arange(my-1.5,my+1.5,0.15):
            for th in np.linspace(mth-np.radians(20),mth+np.radians(20),17):
                sc=score(px,py,th)
                if sc>best[0]: best=(sc,px,py,th)
    _,mx,my,mth=best
    print(f"  scan修正后: x={mx:.2f} y={my:.2f} yaw={np.degrees(mth):.1f}° (内点{best[0]})")
print(f"里程计跟踪 → 地图位姿: x={mx:.2f} y={my:.2f} yaw={np.degrees(mth):.1f}°")
fig,ax=plt.subplots(figsize=(9,15),dpi=100)
ax.scatter(wx,wy,s=0.5,c='0.6')
if c.pts is not None:
    ct,st=np.cos(mth),np.sin(mth);SX=mx+ct*c.pts[:,0]-st*c.pts[:,1];SY=my+st*c.pts[:,0]+ct*c.pts[:,1]
    ax.scatter(SX,SY,s=1,c='red')
ax.plot(mx,my,'b*',ms=25);ax.arrow(mx,my,1.5*np.cos(mth),1.5*np.sin(mth),color='blue',width=0.15)
ax.set_aspect('equal');ax.set_title(f'里程计跟踪: ({mx:.1f},{my:.1f}) yaw{np.degrees(mth):.0f}°')
fig.savefig(OUT,bbox_inches='tight',facecolor='white');print("saved",OUT)
