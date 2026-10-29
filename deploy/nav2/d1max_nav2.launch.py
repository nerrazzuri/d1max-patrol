"""W47:我们的 Nav2(deploy/nav2/params.yaml)和转接节点一起起。

参数:``params_file``(缺省同目录 params.yaml)、``socket``(代理的 nav2.sock)、
``pythonpath``(这一版带的 packages/localizer/src:packages/contract/src)。
行为树用同目录的 navigate_to_pose_d1max.xml(脱困不原地转,决策 76):参数文件里填的是相对名,
这里换成绝对路径。转接节点挂了自己重起;Nav2 的节点由它的生命周期管理器管。

**不用 nav2_bringup 的 navigation_launch.py**(复查第三轮 P4):那里 behavior_server 的速度发到
``/cmd_vel``,跟速度平滑器的输出是同一个话题,转接节点要么收不到 BackUp,要么收到平滑器拖着的旧目标
速度。这里自己起需要的四个节点:控制器 → ``/cmd_vel_nav``,脱困动作 → ``/cmd_vel_behavior``,
转接节点两路都订、标上来源;不起速度平滑器(加减速由代理和机器人管),也不起没用到的
smoother_server、waypoint_follower。
"""

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.descriptions import ParameterFile
from nav2_common.launch import RewrittenYaml

#: 生命周期管理器按这个顺序配置、激活(转接节点问 /lifecycle_manager_navigation/is_active)。
LIFECYCLE_NODES = ["controller_server", "planner_server", "behavior_server", "bt_navigator"]


def generate_launch_description():
    here = os.path.dirname(os.path.abspath(__file__))
    params = ParameterFile(RewrittenYaml(
        source_file=LaunchConfiguration("params_file"),
        param_rewrites={"use_sim_time": "true", "autostart": "true",
                        "default_nav_to_pose_bt_xml":
                        os.path.join(here, "navigate_to_pose_d1max.xml")},
        convert_types=True), allow_substs=True)
    sock = LaunchConfiguration("socket")
    pypath = LaunchConfiguration("pythonpath")
    tf = [("/tf", "tf"), ("/tf_static", "tf_static")]

    def nav2(package, executable, remap=()):
        return Node(package=package, executable=executable, name=executable, output="screen",
                    respawn=False, parameters=[params], remappings=tf + list(remap))

    return LaunchDescription([
        DeclareLaunchArgument("params_file", default_value=os.path.join(here, "params.yaml")),
        DeclareLaunchArgument("socket", default_value="/var/lib/d1max/agent/nav2.sock"),
        DeclareLaunchArgument("pythonpath", default_value=""),
        nav2("nav2_controller", "controller_server", [("cmd_vel", "cmd_vel_nav")]),
        nav2("nav2_planner", "planner_server"),
        nav2("nav2_behaviors", "behavior_server", [("cmd_vel", "cmd_vel_behavior")]),
        nav2("nav2_bt_navigator", "bt_navigator"),
        Node(package="nav2_lifecycle_manager", executable="lifecycle_manager",
             name="lifecycle_manager_navigation", output="screen",
             parameters=[{"use_sim_time": True}, {"autostart": True},
                         {"node_names": LIFECYCLE_NODES}]),
        ExecuteProcess(cmd=["/usr/bin/python3", "-m", "d1max_localizer.nav2bridge",
                            "--socket", sock],
                       # 接在 ROS 原有的 PYTHONPATH 前面(覆盖掉就找不到 rclpy)
                       additional_env={"PYTHONPATH": [pypath, ":", EnvironmentVariable(
                           "PYTHONPATH", default_value="")]},
                       output="screen",
                       respawn=True, respawn_delay=2.0),
    ])
