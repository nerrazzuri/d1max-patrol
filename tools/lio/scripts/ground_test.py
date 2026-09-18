import rclpy, numpy as np, sys
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2, Imu
from sensor_msgs_py import point_cloud2

def ransac_plane(P, iters=300, thr=0.03):
    best_n=None; best_in=0; N=len(P)
    rng=np.random.default_rng(0)
    for _ in range(iters):
        idx=rng.choice(N,3,replace=False); a,b,c=P[idx]
        n=np.cross(b-a,c-a); nn=np.linalg.norm(n)
        if nn<1e-9: continue
        n=n/nn; d=-n.dot(a)
        dist=np.abs(P.dot(n)+d); cnt=int((dist<thr).sum())
        if cnt>best_in: best_in=cnt; best_n=n; best_d=d
    # refit on inliers
    dist=np.abs(P.dot(best_n)+best_d); inl=P[dist<thr]
    c=inl.mean(0); u,s,vt=np.linalg.svd(inl-c); n=vt[-1]
    return n/np.linalg.norm(n), best_in, len(inl)

class T(Node):
    def __init__(s):
        super().__init__("ground_test")
        s.create_subscription(PointCloud2,"/front_lidar",s.pc,qos_profile_sensor_data)
        s.create_subscription(Imu,"/front_lidar/imu",s.im,qos_profile_sensor_data)
        s.cloud=None; s.acc=[]
    def pc(s,m):
        pts=point_cloud2.read_points(m,("x","y","z"),skip_nans=True)
        a=np.array([[p[0],p[1],p[2]] for p in pts],dtype=np.float64)
        r=np.linalg.norm(a,axis=1); s.cloud=a[(r>0.5)&(r<15)]
    def im(s,m):
        s.acc.append([m.linear_acceleration.x,m.linear_acceleration.y,m.linear_acceleration.z])

def main():
    rclpy.init(); t=T()
    import time; t0=time.time()
    while time.time()-t0<7 and rclpy.ok():
        rclpy.spin_once(t,timeout_sec=0.2)
    if t.cloud is None or len(t.acc)<5:
        print(f"数据不足: cloud={None if t.cloud is None else len(t.cloud)} imu={len(t.acc)}"); return
    P=t.cloud; g_I=np.array(t.acc).mean(0); g_I=g_I/np.linalg.norm(g_I)
    n_L,inl0,inl=ransac_plane(P)
    # make n_L point "up-ish" consistently: choose sign so it roughly opposes lidar origin->floor (floor below)
    print(f"# floor plane inliers={inl}/{len(P)}  |acc_mean|={np.linalg.norm(np.array(t.acc).mean(0)):.4f} g")
    print(f"# n_L (floor normal in LiDAR/cloud frame) = {n_L.round(4)}")
    print(f"# g_I (gravity dir in IMU frame)          = {g_I.round(4)}")
    def R_from_q(qx,qy,qz,qw):
        x,y,z,w=qx,qy,qz,qw
        return np.array([
          [1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
          [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
          [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    cands={
      "off1092 q(-.7029,.7112,-.0028,-.0080) as R_IL":R_from_q(-0.702914,0.711225,-0.002825,-0.007971),
      "off1084 q(0,0,-.7029,.7112) as R_IL":R_from_q(0,0,-0.702914,0.711225),
    }
    print("\n# score = |dot(R·n_L, g_I)|  (理想→1.0 表示该 R 把地面法向映到重力,即正确的 LiDAR→IMU 旋转)")
    for name,R in cands.items():
        s1=abs(float((R@n_L).dot(g_I)))
        s2=abs(float((R.T@n_L).dot(g_I)))
        print(f"  {name:48s}  direct={s1:.4f}  transpose={s2:.4f}")
main()
