import rclpy, math, sys, time
from rclpy.node import Node
from nav_msgs.msg import Odometry
class M(Node):
    def __init__(s):
        super().__init__("odom_mon")
        s.create_subscription(Odometry,"/Odometry",s.cb,10)
        s.p0=None; s.last=None; s.path=0.0; s.n=0; s.maxd=0.0; s.t0=time.time()
    def cb(s,m):
        p=m.pose.pose.position; c=(p.x,p.y,p.z); s.n+=1
        if s.p0 is None: s.p0=c
        if s.last is not None:
            d=math.dist(c,s.last); s.path+=d
        s.last=c
        disp=math.dist(c,s.p0); s.maxd=max(s.maxd,disp)
        if s.n%20==0:
            print(f"[{time.time()-s.t0:5.1f}s] n={s.n} pos=({c[0]:+.2f},{c[1]:+.2f},{c[2]:+.2f}) disp={disp:.2f}m path={s.path:.2f}m",flush=True)
    def summary(s):
        print(f"\n=== SUMMARY: {s.n} odom msgs, cumulative path={s.path:.2f}m, max displacement from start={s.maxd:.2f}m ===",flush=True)
def main():
    rclpy.init(); m=M()
    try: rclpy.spin(m)
    except KeyboardInterrupt: pass
    finally: m.summary()
main()
