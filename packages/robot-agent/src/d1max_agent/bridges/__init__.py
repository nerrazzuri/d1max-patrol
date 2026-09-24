"""把 RobotHAL 包成引擎认识的 NavBackend / DeviceBackend(W00b 决定 2)。

引擎(``d1max_agent.engine``)是照着厂商导航接口长大的:它等 ``NavStatusEvent`` 的
StandBy → Initializing → Active → Succeed → StandBy,问 ``nav_status()``/``loc_status()``,
问 ``device.battery()/emergency()/has_control()``。HAL 给的是速度环与传感器。这两个桥
在中间:速度环走 W00 的控制律,状态机按仿真器的时序参数走(``init_delay_s``、
``terminal_hold_s``),真机上这些时序是 W00d 要测的参数。

W00d 出 adapter-d1max 时桥不变,只是 HAL 后面从 SimRobot 换成真狗。
"""
