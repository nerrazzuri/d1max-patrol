"""任务引擎 —— 巡检任务的定义、归档、起飞检查、降级规则与状态机。

**这一层不认识 HTTP。** 它是个纯库对象,能脱离 app 单独用、单独测。
app 只是它的一个调用者;W00b 起它住在 robot-agent 包里(总设计 §5),W00c4 删了根包里
那层别名壳,引擎只有 ``d1max_agent.engine`` 一个名字。有一条测试(tests/app/test_server.py)
会扫本包的源码来守「不认识 HTTP」这条约束。

``mission``、``schedule`` 的真身在契约包(W00c2a,本包里是转手);``alerts``、``baselines`` 按
总设计归站点,在这里只是**借住**,随老 HTTP 面退役(W00c5)再定去留。
"""
