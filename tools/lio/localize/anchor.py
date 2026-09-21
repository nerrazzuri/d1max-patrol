import rclpy,numpy as np,time,json,sys
from rclpy.node import Node
from nav_msgs.msg import Odometry
def yaw_of(q): return np.arctan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
class C(Node):
    def __init__(s):
        super().__init__("anchor"); s.od=None
        # /odom/current_pose 类型? 试 PoseStamped
        s.create_subscription(Odometry,"/odom/current_pose",s.cb,10)
    def cb(s,m):
        p=m.pose.pose.position; q=m.pose.pose.orientation
        s.od=(p.x,p.y,yaw_of(q))
rclpy.init();c=C();t=time.time()
while time.time()-t<8 and c.od is None and rclpy.ok(): rclpy.spin_once(c,timeout_sec=0.2)
if c.od is None: print("没读到 odom"); sys.exit(1)
ox,oy,oth=c.od
# 用户真值锚点(地图系)
mx,my,mth=1.2,-6.7,np.radians(-90)
# T_map_odom: map = T ∘ odom  =>  R(dth), t
dth=mth-oth
c_,s_=np.cos(dth),np.sin(dth)
tx=mx-(c_*ox - s_*oy); ty=my-(s_*ox + c_*oy)
json.dump({"dth":dth,"tx":tx,"ty":ty,"anchor_odom":[ox,oy,oth],"anchor_map":[mx,my,mth]},
          open("/tmp/claude-1000/-home-liang-Projects-D1-Max/a2d92937-41c9-46bd-a2f3-9b8ebc3add6a/scratchpad/lock.json","w"))
print(f"当前 odom=({ox:.2f},{oy:.2f},{np.degrees(oth):.0f}°)")
print(f"锚定到 map=(1.2,-6.7,-90°). 锁定: dth={np.degrees(dth):.1f}° t=({tx:.2f},{ty:.2f})")
