"""任务引擎 —— 巡检任务的定义、归档、起飞检查、降级规则与状态机。

**这一层不认识 HTTP。** 它是个纯库对象,能脱离 app 单独用、单独测。
app 只是它的一个调用者;W00b 起它住在 robot-agent 包里(总设计 §5),根包的
``d1max_patrol.engine`` 只是一层别名壳。有一条测试(tests/app/test_server.py)会扫本包
的源码来守「不认识 HTTP」这条约束。

``schedule``、``alerts``、``baselines`` 三个模块按总设计归站点,在这里只是**借住**,
W00c 搬去 site-node。
"""
