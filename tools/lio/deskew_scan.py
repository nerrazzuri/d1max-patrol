"""前雷达 deskew + 2D 激光。
用每点绝对时戳 + odom 位姿(SE2 线性插值)把一帧内 100ms 各时刻的点校正回帧末同一位姿,
再拍平成 base_link 系 LaserScan。治运动畸变(弯墙/扇形)。
外参 base<-lidar: (tx,ty,tz,yaw)。用法给参数。
"""
import sys, math, bisect
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2, LaserScan
from nav_msgs.msg import Odometry
from sensor_msgs_py import point_cloud2

TX,TY,TZ,YAW, RMIN,MINH,MAXH = [float(x) for x in sys.argv[1:8]]
NB=1800; AMIN,AMAX=-math.pi,math.pi; AINC=(AMAX-AMIN)/NB

def yaw_of(q):
    return math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))

class Deskew(Node):
    def __init__(self):
        super().__init__("deskew_scan")
        self.ts=[]; self.xs=[]; self.ys=[]; self.yaws=[]   # odom 缓冲
        self.create_subscription(Odometry,"/odom/mc_odom",self.on_odom,50)
        self.create_subscription(PointCloud2,"/front_lidar",self.on_cloud,5)
        self.pub=self.create_publisher(LaserScan,"/scan_desk",5)
    def on_odom(self,m):
        t=m.header.stamp.sec+m.header.stamp.nanosec*1e-9
        p=m.pose.pose
        self.ts.append(t); self.xs.append(p.position.x); self.ys.append(p.position.y); self.yaws.append(yaw_of(p.orientation))
        if len(self.ts)>2000:
            for a in (self.ts,self.xs,self.ys,self.yaws): del a[:1000]
    def pose_at(self,t):
        if not self.ts: return None
        i=bisect.bisect_left(self.ts,t)
        if i<=0: return self.xs[0],self.ys[0],self.yaws[0]
        if i>=len(self.ts): return self.xs[-1],self.ys[-1],self.yaws[-1]
        t0,t1=self.ts[i-1],self.ts[i]; f=(t-t0)/(t1-t0) if t1>t0 else 0
        y0,y1=self.yaws[i-1],self.yaws[i]
        dy=(y1-y0+math.pi)%(2*math.pi)-math.pi     # 角度插值防跳变
        return (self.xs[i-1]+f*(self.xs[i]-self.xs[i-1]),
                self.ys[i-1]+f*(self.ys[i]-self.ys[i-1]),
                y0+f*dy)
    def on_cloud(self,m):
        pts=np.array([[x,y,z,ts] for x,y,z,ts in point_cloud2.read_points(m,("x","y","z","timestamp"),skip_nans=True)],dtype=np.float64)
        if len(pts)==0: return
        px,py,pz,pt=pts[:,0],pts[:,1],pts[:,2],pts[:,3]
        # 1) 雷达系 -> base_link@采集时刻 (静态外参)
        c,s=math.cos(YAW),math.sin(YAW)
        bx=c*px-s*py+TX; by=s*px+c*py+TY; bz=pz+TZ
        # 2) deskew: 校正到帧头位姿(=header.stamp,和 slam 查 odom TF 一致)
        tmin=pt.min(); tmax=pt.max()
        pe=self.pose_at(tmin)                # 参考=帧头
        if pe is None: xe,ye,the=0,0,0
        else: xe,ye,the=pe
        p0=pe or (xe,ye,the); p1=self.pose_at(tmax) or (xe,ye,the)
        span=(tmax-tmin) or 1e-9
        f=(pt-tmin)/span                     # 每点在帧内的时间比例
        xi=p0[0]+f*(p1[0]-p0[0]); yi=p0[1]+f*(p1[1]-p0[1])
        dyaw=(p1[2]-p0[2]+math.pi)%(2*math.pi)-math.pi
        thi=p0[2]+f*dyaw
        # p_e = R(thi-the)*p_i + R(-the)*(xi-xe, yi-ye)
        dth=thi-the
        cd,sd=np.cos(dth),np.sin(dth)
        ex=cd*bx-sd*by; ey=sd*bx+cd*by
        cxe,sxe=math.cos(-the),math.sin(-the)
        dx=xi-xe; dy=yi-ye
        ex=ex + (cxe*dx - sxe*dy); ey=ey + (sxe*dx + cxe*dy)
        # 3) 拍平成 scan
        rh=np.hypot(ex,ey)
        keep=(rh>=RMIN)&(bz>=MINH)&(bz<=MAXH)&(rh<=15)
        ex,ey,rh=ex[keep],ey[keep],rh[keep]
        rng=np.full(NB,np.inf)
        idx=np.clip(((np.arctan2(ey,ex)-AMIN)/AINC).astype(int),0,NB-1)
        np.minimum.at(rng,idx,rh)
        out=LaserScan(); out.header=m.header; out.header.frame_id="base_link"
        out.angle_min=AMIN; out.angle_max=AMAX; out.angle_increment=AINC
        out.range_min=float(RMIN); out.range_max=15.0
        out.ranges=[float(v) for v in rng]
        self.pub.publish(out)

def main(): rclpy.init(); rclpy.spin(Deskew())
if __name__=="__main__": main()
