"""W47:我们的 Nav2(navigation_launch.py + deploy/nav2/params.yaml)和转接节点一起起。

参数:``params_file``(缺省同目录 params.yaml)、``socket``(代理的 nav2.sock)、
``pythonpath``(这一版带的 packages/localizer/src:packages/contract/src)。
转接节点挂了自己重起;Nav2 的节点由它的生命周期管理器管。
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    here = os.path.dirname(os.path.abspath(__file__))
    bringup = os.path.join(get_package_share_directory("nav2_bringup"), "launch",
                           "navigation_launch.py")
    params = LaunchConfiguration("params_file")
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
                       additional_env={"PYTHONPATH": pypath}, output="screen",
                       respawn=True, respawn_delay=2.0),
    ])
