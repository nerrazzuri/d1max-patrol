"""W47:我们的 Nav2(navigation_launch.py + deploy/nav2/params.yaml)和转接节点一起起。

参数:``params_file``(缺省同目录 params.yaml)、``socket``(代理的 nav2.sock)、
``pythonpath``(这一版带的 packages/localizer/src:packages/contract/src)。
行为树用同目录的 navigate_to_pose_d1max.xml(脱困不原地转,决策 76):参数文件里填的是相对名,
这里换成绝对路径。转接节点挂了自己重起;Nav2 的节点由它的生命周期管理器管。
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from nav2_common.launch import RewrittenYaml


def generate_launch_description():
    here = os.path.dirname(os.path.abspath(__file__))
    bringup = os.path.join(get_package_share_directory("nav2_bringup"), "launch",
                           "navigation_launch.py")
    params = RewrittenYaml(
        source_file=LaunchConfiguration("params_file"),
        param_rewrites={"default_nav_to_pose_bt_xml":
                        os.path.join(here, "navigate_to_pose_d1max.xml")},
        convert_types=True)
    sock = LaunchConfiguration("socket")
    pypath = LaunchConfiguration("pythonpath")
    return LaunchDescription([
        DeclareLaunchArgument("params_file", default_value=os.path.join(here, "params.yaml")),
        DeclareLaunchArgument("socket", default_value="/var/lib/d1max/agent/nav2.sock"),
        DeclareLaunchArgument("pythonpath", default_value=""),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(bringup), launch_arguments={
            "params_file": params, "use_sim_time": "true", "autostart": "true"}.items()),
        ExecuteProcess(cmd=["/usr/bin/python3", "-m", "d1max_localizer.nav2bridge",
                            "--socket", sock],
                       # 接在 ROS 原有的 PYTHONPATH 前面(覆盖掉就找不到 rclpy)
                       additional_env={"PYTHONPATH": [pypath, ":", EnvironmentVariable(
                           "PYTHONPATH", default_value="")]},
                       output="screen",
                       respawn=True, respawn_delay=2.0),
    ])
