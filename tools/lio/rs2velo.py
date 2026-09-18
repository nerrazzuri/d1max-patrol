"""RS-Airy /front_lidar -> velodyne 格式 /velodyne_points，供 FAST_LIO 用。
RS-Airy: x,y,z,intensity,ring(u16),timestamp(f64 绝对秒)
velodyne_ros::Point: x,y,z,intensity,time(f32 相对秒),ring(u16)
转换: time = timestamp - header.stamp。FAST_LIO 配 timestamp_unit=0(秒)。
"""
import sys, numpy as np, rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2, PointField
from sensor_msgs_py import point_cloud2

VELO_DTYPE = np.dtype({
    'names':   ['x','y','z','intensity','time','ring'],
    'formats': ['<f4','<f4','<f4','<f4','<f4','<u2'],
    'offsets': [0,4,8,12,16,20],
    'itemsize': 24,
})
FIELDS = [
    PointField(name='x',offset=0,datatype=PointField.FLOAT32,count=1),
    PointField(name='y',offset=4,datatype=PointField.FLOAT32,count=1),
    PointField(name='z',offset=8,datatype=PointField.FLOAT32,count=1),
    PointField(name='intensity',offset=12,datatype=PointField.FLOAT32,count=1),
    PointField(name='time',offset=16,datatype=PointField.FLOAT32,count=1),
    PointField(name='ring',offset=20,datatype=PointField.UINT16,count=1),
]

class C(Node):
    def __init__(s):
        super().__init__("rs2velo")
        s.pub=s.create_publisher(PointCloud2,"/velodyne_points",qos_profile_sensor_data)
        s.create_subscription(PointCloud2,"/front_lidar",s.cb,qos_profile_sensor_data)
    def cb(s,m):
        raw=point_cloud2.read_points(m,("x","y","z","intensity","ring","timestamp"),skip_nans=True)
        raw=np.array(list(raw))
        if len(raw)==0: return
        # read_points 返回结构化或普通数组，统一成列
        if raw.dtype.names:
            x=raw['x'].astype('<f4'); y=raw['y'].astype('<f4'); z=raw['z'].astype('<f4')
            inten=raw['intensity'].astype('<f4'); ring=raw['ring'].astype('<u2'); tsv=raw['timestamp'].astype('<f8')
        else:
            raw=raw.astype(np.float64)
            x=raw[:,0].astype('<f4'); y=raw[:,1].astype('<f4'); z=raw[:,2].astype('<f4')
            inten=raw[:,3].astype('<f4'); ring=raw[:,4].astype('<u2'); tsv=raw[:,5]
        t0=tsv.min()
        rel=(tsv-t0).astype('<f4')      # 相对帧头的秒
        out=np.zeros(len(x),dtype=VELO_DTYPE)
        out['x']=x; out['y']=y; out['z']=z; out['intensity']=inten; out['time']=rel; out['ring']=ring
        msg=PointCloud2()
        msg.header=m.header
        msg.height=1; msg.width=len(out)
        msg.fields=FIELDS; msg.is_bigendian=False
        msg.point_step=24; msg.row_step=24*len(out)
        msg.is_dense=True; msg.data=out.tobytes()
        s.pub.publish(msg)

def main(): rclpy.init(); rclpy.spin(C())
if __name__=="__main__": main()
