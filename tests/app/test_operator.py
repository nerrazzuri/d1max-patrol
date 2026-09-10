"""自报姓名这一侧:狗**记下但从不核实**(§6.3)。

**这个模块只测狗这一侧。** 姓名本身存在手机上(``mobile/lib/store/
registry_store.dart``,按狗分别记 —— 换一只狗常常就是换一个班);狗这边
只做两件事:收下当前是谁,以及把"换人"这件事记进审计,好让事后翻得出
那一趟是谁签的名。

**``operator_verified`` 恒为 ``False``,这一条不许改。** 改成 ``True`` 就是
假装这台机器做过身份认证 —— 它没有,它连一份能对的名册都没有。手机和网页
都照着这个字段说话:写成"已登录:张三"的那天,一次冒名操作在事后的记录里
就跟本人操作长得一模一样,而这正是 §6.3 拿一整节来防的那件事。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.app.auth import MAX_OPERATOR_LEN
from d1max_patrol.app.server import AppServer
from tests.app.conftest import get_err, get_json, make_ctx, request

PIN = "428913"


def 记名(server, name: str, token: str = ""):
    return get_json(server, "/api/operator", method="PUT",
                    payload={"name": name}, headers=带签(token))


def 留痕(server, token: str = "") -> list[dict]:
    return get_json(server, "/api/control/audit", headers=带签(token))["audit"]


def 带签(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture
def 有pin的服务(bridge, tmp_path):
    """设了 PIN 的那一档 —— **这一档才有"会话"这回事。**

    上面那些测试用的 ``server`` 夹具不设 PIN,``req.session`` 恒为 ``None``,
    于是 ``_operator_put`` 里"会话跟着换名字"那一支永远走不到。换班这件事在
    真机上恰恰全发生在这一档:手机连上来先解锁,拿到 token 才谈得上谁是谁。
    """
    s = AppServer(make_ctx(bridge, tmp_path), port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def 解锁(server, operator: str) -> str:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": operator})
    assert code == 200, body
    return json.loads(body)["token"]


def test_姓名只是自报_从不核实(server):
    """§6.3:狗那侧 operator 是自报姓名。写成 True 就是假装有身份认证。"""
    记名(server, "老王")
    body = get_json(server, "/api/operator")
    assert body["name"] == "老王"
    assert body["operator_verified"] is False


def test_换人这件事进审计(server):
    记名(server, "老王")
    记名(server, "小李")
    kinds = [a["kind"] for a in 留痕(server)]
    assert kinds.count("operator_changed") == 2


def test_留痕上记的是换上来的那个名字(server):
    """**不是"有一条记录"就够了。**

    只断种类的话,一个把 ``operator`` 写成空串的实现照样绿 —— 而审计上一条
    没有名字的"换人"记录,事后跟没记是一回事:翻账的人看得见换过,看不见
    换成了谁。
    """
    记名(server, "老王")
    记名(server, "小李")
    换人 = [a for a in 留痕(server) if a["kind"] == "operator_changed"]
    assert [a["operator"] for a in 换人] == ["老王", "小李"]


def test_空名字不许存(server):
    r = get_err(server, "/api/operator", 400, method="PUT",
                payload={"name": "  "})
    assert r["error"]


def test_空名字既不落账也不进审计(server):
    """**拒绝要拒得干净。** 先记住老王,再拿一个空名字去试。

    拒了却已经把名字冲成空的,比收下更坏:屏上那个署名会变成空白,而请求
    明明回的是 400,人以为什么都没发生。
    """
    记名(server, "老王")
    get_err(server, "/api/operator", 400, method="PUT", payload={"name": ""})
    assert get_json(server, "/api/operator")["name"] == "老王"
    kinds = [a["kind"] for a in 留痕(server)]
    assert kinds.count("operator_changed") == 1


def test_名字前后的空白收拾掉(server):
    """手机上的输入框很容易带一个尾随空格。

    不收拾的话,"老王"和"老王 "在账上是两个人,而屏上长得一模一样。
    """
    assert 记名(server, "  老王  ")["name"] == "老王"


def test_没人报过名字的时候是空串_不是编一个(server):
    body = get_json(server, "/api/operator")
    assert body["name"] == ""
    assert body["operator_verified"] is False


def test_请求体不是对象就拒(server):
    get_err(server, "/api/operator", 400, method="PUT", payload=["老王"])


# ------------------------------------------ 换了人,这次会话后面的账要跟着换


def test_换了人之后_这次会话后面的留痕签的是新名字(有pin的服务):
    """**修复轮 1 必修 1a。** 这是本文件里最承重的一条。

    老王解锁进屏,小李接班点 chip 改成"小李"。此刻审计里已经有一条
    ``operator_changed: 小李``、屏上也写着小李 —— 可小李接着抢控制权产生的
    那条 ``acquired``,如果还签着"老王",事后翻账看到的就是:同一分钟里换成
    了小李,而老王取走了控制权。**两份记录互相矛盾,且没有任何一处提示发生
    过什么。**

    挂账 65 接受的代价是"甲忘了切、乙的操作签在甲名下" —— 人的疏忽,系统
    无辜。这里是人记得切、系统也记下了这次切换,账还是错的:重一档。
    """
    tok = 解锁(有pin的服务, "老王")
    记名(有pin的服务, "小李", tok)
    code, body, _ = request(有pin的服务, "/api/control/acquire",
                            method="POST", payload={}, headers=带签(tok))
    assert code == 200, body
    取控制权 = [a for a in 留痕(有pin的服务, tok) if a["kind"] == "acquired"]
    assert [a["operator"] for a in 取控制权] == ["小李"], (
        "换过人之后,这次会话后面的留痕还签在上一个人名下")


def test_没换过人的时候_留痕签的还是解锁时报的那个名字(有pin的服务):
    """上一条的对照组。

    只有上一条的话,一个"所有留痕一律签 ``self._operator``"的实现也绿 ——
    而那个实现在两台手机连一只狗时,会让甲的操作签上乙刚报的名字。这一条钉住
    署名的出处仍然是**这个会话**,不是服务器上那份"最后报到的人"。
    """
    甲 = 解锁(有pin的服务, "老王")
    乙 = 解锁(有pin的服务, "小李")
    记名(有pin的服务, "小李", 乙)          # 乙那台手机换了名字
    code, body, _ = request(有pin的服务, "/api/control/acquire",
                            method="POST", payload={}, headers=带签(甲))
    assert code == 200, body
    取控制权 = [a for a in 留痕(有pin的服务, 甲) if a["kind"] == "acquired"]
    assert [a["operator"] for a in 取控制权] == ["老王"], (
        "甲那台手机没换过人,它的操作不许签上乙刚报的名字")


def test_换名字不是重新认证(有pin的服务):
    """§6.3:狗只记,不核。**换署名这件事一分身份认证都不许多出来。**

    token 还是原来那个(没换发)、``operator_verified`` 还是 ``False``、会话
    也没多出一个 —— 换名字要是悄悄变成一次隐式解锁,那这台机器上"名字不可
    信"这句话就只剩一半是真的了。
    """
    tok = 解锁(有pin的服务, "老王")
    之前 = get_json(有pin的服务, "/api/sessions", headers=带签(tok))
    body = 记名(有pin的服务, "小李", tok)
    assert body["operator_verified"] is False
    之后 = get_json(有pin的服务, "/api/sessions", headers=带签(tok))
    assert len(之后["sessions"]) == len(之前["sessions"]) == 1
    我 = 之后["sessions"][0]
    assert 我["ref"] == 之前["sessions"][0]["ref"], "token 不许被换发"
    assert 我["operator"] == "小李", "会话上的署名要跟着换"
    assert 我["operator_verified"] is False


# ---------------------------------------------- 收拾名字的规矩只许有一套


def test_超长名字截断到跟解锁那道门一样的长度(有pin的服务):
    """两道门给出不同长度的话,同一个人在会话上是一截、在审计里是另一截。"""
    长 = "王" * 100
    tok = 解锁(有pin的服务, 长)
    报上去 = 记名(有pin的服务, 长, tok)["name"]
    assert len(报上去) == MAX_OPERATOR_LEN
    我 = get_json(有pin的服务, "/api/sessions", headers=带签(tok))["sessions"][0]
    assert 我["operator"] == 报上去, "解锁那道门和换人那道门要收拾出同一个名字"


def test_中间的连续空白也压成一个(server):
    """屏上"老 王"和"老  王"长得一模一样,账上却是两个人。"""
    assert 记名(server, "老  王")["name"] == "老 王"


def test_不可打印的字符换成空格(server):
    """一个 NUL 混进审计环里,谁也说不清那一趟是谁开的。"""
    assert 记名(server, "老\x00王")["name"] == "老 王"


def test_报上来的时刻跟审计里那一条对得上(server):
    """``at_ms`` 是本次新增的公开字段。

    它要是永远 0,任何拿它算"这个署名报了多久了"的地方一律得到 1970 年 ——
    而屏上会理直气壮地写出一个 56 年。
    """
    body = 记名(server, "老王")
    assert body["at_ms"] > 0
    换人 = [a for a in 留痕(server) if a["kind"] == "operator_changed"]
    assert [a["at_ms"] for a in 换人] == [body["at_ms"]]
