"""资源表与断线策略表是**数据**,不是代码分支(总设计 §4;设计 §3)。"""

from __future__ import annotations

from d1max_contract.policy import OFFLINE_POLICY, OfflinePolicy, policy_for
from d1max_contract.resources import RESOURCES, TASK_RESOURCES, conflicts, resources_for


def test_六种资源():
    assert RESOURCES == ("motion", "light", "sound", "spotlight", "head", "camera")


def test_W00两个任务的资源():
    assert TASK_RESOURCES["goto"] == frozenset({"motion"})
    assert TASK_RESOURCES["abort"] == frozenset()
    for kind, rs in TASK_RESOURCES.items():
        assert rs <= set(RESOURCES), kind


def test_占motion的互斥_不占的不冲突():
    assert conflicts("goto", "goto") is True
    assert conflicts("goto", "abort") is False
    assert conflicts("abort", "abort") is False


def test_未知任务不许所有命令共用一个槽():
    """总设计 §4.1:不许所有命令共用一个任务槽。不认识的任务**不能**默认占 motion,
    也不能默认什么都不占 —— 必须显式登记。"""
    import pytest
    with pytest.raises(KeyError):
        resources_for("dance")


def test_断线策略表与总设计一致():
    goto = OFFLINE_POLICY["goto"]
    assert goto == OfflinePolicy(on_disconnect="continue_if_safe", queue_cmds=frozenset({"abort"}),
                                 new_cmd_expires=True)
    abort = OFFLINE_POLICY["abort"]
    assert abort == OfflinePolicy(on_disconnect="execute_locally", queue_cmds=frozenset(),
                                  new_cmd_expires=False)


def test_on_disconnect只认三个值():
    import pytest
    with pytest.raises(ValueError):
        OfflinePolicy(on_disconnect="party", queue_cmds=frozenset(), new_cmd_expires=True)


def test_policy_for未知任务报错而不是猜():
    import pytest
    with pytest.raises(KeyError):
        policy_for("dance")
    assert policy_for("goto") is OFFLINE_POLICY["goto"]


def test_patrol占移动_相机_灯_云台_断线继续并排队abort():
    """W00c2a:整趟巡检要走、要拍、要开灯、要转头;与 goto 互斥,与 abort 不冲突。"""
    assert TASK_RESOURCES["patrol"] == frozenset({"motion", "camera", "light", "head"})
    assert conflicts("patrol", "goto") and conflicts("patrol", "patrol")
    assert not conflicts("patrol", "abort")
    assert policy_for("patrol") == OfflinePolicy(on_disconnect="continue_if_safe",
                                                 queue_cmds=frozenset({"abort"}),
                                                 new_cmd_expires=True)
