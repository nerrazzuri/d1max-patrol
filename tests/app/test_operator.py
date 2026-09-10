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

from tests.app.conftest import get_err, get_json


def 记名(server, name: str):
    return get_json(server, "/api/operator", method="PUT",
                    payload={"name": name})


def 留痕(server) -> list[dict]:
    return get_json(server, "/api/control/audit")["audit"]


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
