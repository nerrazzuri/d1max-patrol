"""版本兼容级别(商业化 A7)。站点、狗上的代理、手机 App 各自独立升级,哪些版本能配在一起,看这几个数。

**级别只在「两头都要会」的东西改了的时候加一**(新加的可有可无的字段不算):

代理级别(:data:`AGENT_LEVEL`,狗在能力 ``tasks.compat.level`` 里报;老代理不报算 1):

- 1:W 系列做完以前的代理。
- 2:认站点的「这份证据删过了,当传完」回执(``discarded``,PR #88)。站点自从 PR #88 起会发这种回执,
  **1 级的代理收到它会一直重传**,所以站点要求代理至少 2 级。

站点接口级别(:data:`SITE_API_LEVEL`,登录回复里带;手机 App 看它):

- 1:现在的接口。

规矩(站点按它报警、拒登记升级包;App 按它拦登录):

- 狗的代理级别 < :data:`SITE_MIN_AGENT_LEVEL`:**狗太老**,要给狗升级(站点报 P2)。
- 狗的代理级别 > :data:`SITE_MAX_AGENT_LEVEL`:**站点太老**,先升站点(站点报 P2)。
- 升级包的代理级别不在这两个数之间:站点不登记(下发了也配不上)。
- App 的接口级别 < 站点要的 :data:`MIN_APP_API_LEVEL`:App 要升级;站点的接口级别 < App 要的
  :data:`MIN_SITE_API_LEVEL`(在 App 里):站点要升级。

升级顺序见 ``docs/版本兼容与升级顺序.md``:**先站点、再狗、旁路进程跟着狗、App 随时**。
"""

from __future__ import annotations

AGENT_LEVEL = 2
SITE_MIN_AGENT_LEVEL = 2
SITE_MAX_AGENT_LEVEL = 2
SITE_API_LEVEL = 1
MIN_APP_API_LEVEL = 1


def agent_verdict(level: int | None) -> str:
    """站点看一只狗的代理级别:``ok`` / ``agent_too_old`` / ``site_too_old``。没报的算 1。"""
    lv = 1 if level is None else int(level)
    if lv < SITE_MIN_AGENT_LEVEL:
        return "agent_too_old"
    if lv > SITE_MAX_AGENT_LEVEL:
        return "site_too_old"
    return "ok"
