"""故障注入命令的解析与语义。"""

import pytest

from d1max_sim.inject import FaultState, InjectError, apply_command


def test_默认状态什么都不注入():
    s = FaultState()
    assert s.speed_scale == 1.0
    assert s.stuck is False
    assert s.frame_count_zero is False
    assert s.response_delay_s == 0.0
    assert s.disconnect_seconds == 0.0
    assert s.half_open is False
    assert s.fail_next_nav is False
    assert s.queued_alg_errors == []


def test_定位丢失与恢复():
    s = FaultState()
    assert "定位丢失" in apply_command(s, "loc_lost")
    assert s.loc_lost_requested is True
    apply_command(s, "loc_ok")
    assert s.loc_recover_requested is True


def test_注入算法故障码():
    s = FaultState()
    apply_command(s, "alg_error 13330")
    assert len(s.queued_alg_errors) == 1
    item = s.queued_alg_errors[0]
    assert item.code == 13330
    assert item.description == "navigation blocked"   # 已知码带默认描述
    assert item.severity == 0


def test_注入未知故障码也接受():
    s = FaultState()
    apply_command(s, "alg_error 19999 2")
    item = s.queued_alg_errors[0]
    assert (item.code, item.severity) == (19999, 2)
    assert item.description == "injected"


def test_取走故障码后队列清空():
    s = FaultState()
    apply_command(s, "alg_error 13331")
    assert [i.code for i in s.take_alg_errors()] == [13331]
    assert s.queued_alg_errors == []
    assert s.take_alg_errors() == []


def test_故障码必须是整数():
    with pytest.raises(InjectError, match="故障码"):
        apply_command(FaultState(), "alg_error abc")


def test_alg_error_不带故障码被拒绝():
    with pytest.raises(InjectError, match="缺少故障码"):
        apply_command(FaultState(), "alg_error")


def test_alg_error_严重度非整数被拒绝():
    with pytest.raises(InjectError, match="严重度"):
        apply_command(FaultState(), "alg_error 13330 xyz")


def test_预约下次导航失败():
    s = FaultState()
    apply_command(s, "nav_fail")
    assert s.fail_next_nav is True


def test_减速命令换算成速度缩放():
    """slow 2 表示慢一倍。"""
    s = FaultState()
    apply_command(s, "slow 2")
    assert s.speed_scale == 0.5
    apply_command(s, "slow 1")
    assert s.speed_scale == 1.0


@pytest.mark.parametrize("bad", ["slow 0", "slow -1", "slow abc", "slow"])
def test_减速参数非法(bad):
    with pytest.raises(InjectError):
        apply_command(FaultState(), bad)


def test_卡住开关():
    s = FaultState()
    apply_command(s, "stuck on")
    assert s.stuck is True
    apply_command(s, "stuck off")
    assert s.stuck is False


def test_开关参数非法():
    with pytest.raises(InjectError, match="on 或 off"):
        apply_command(FaultState(), "stuck maybe")


def test_帧号归零开关():
    s = FaultState()
    apply_command(s, "frame_count_zero on")
    assert s.frame_count_zero is True


def test_乱序延迟():
    s = FaultState()
    apply_command(s, "reorder 0.3")
    assert s.response_delay_s == pytest.approx(0.3)


def test_断链秒数():
    s = FaultState()
    apply_command(s, "disconnect 2")
    assert s.disconnect_seconds == pytest.approx(2.0)


def test_半开链路开关():
    s = FaultState()
    apply_command(s, "half_open on")
    assert s.half_open is True
    apply_command(s, "half_open off")
    assert s.half_open is False
    with pytest.raises(InjectError, match="on 或 off"):
        apply_command(s, "half_open 3")


def test_sdk_链路命令给出明确的未实现提示():
    """battery / fault fatal / control_lost 属 SDK 链路,第 2 卷才有。"""
    for line in ("battery 20", "fault fatal", "control_lost"):
        with pytest.raises(InjectError, match="第 2 卷"):
            apply_command(FaultState(), line)


def test_reset_清空所有注入():
    s = FaultState()
    apply_command(s, "slow 4")
    apply_command(s, "stuck on")
    apply_command(s, "alg_error 13330")
    apply_command(s, "half_open on")
    apply_command(s, "reset")
    assert s.speed_scale == 1.0
    assert s.stuck is False
    assert s.half_open is False
    assert s.queued_alg_errors == []


def test_status_命令回显当前注入():
    s = FaultState()
    apply_command(s, "slow 2")
    out = apply_command(s, "status")
    assert "speed_scale=0.5" in out
    apply_command(s, "loc_lost")
    assert "loc_lost_requested=True" in apply_command(s, "status")


def test_help_命令列出全部命令():
    out = apply_command(FaultState(), "help")
    for name in ("loc_lost", "alg_error", "nav_fail", "slow", "stuck",
                 "frame_count_zero", "reorder", "disconnect", "half_open", "reset"):
        assert name in out


@pytest.mark.parametrize("bad", ["", "   ", "fly_to_the_moon"])
def test_未知命令报错(bad):
    with pytest.raises(InjectError, match="未知命令|命令为空"):
        apply_command(FaultState(), bad)


def test_命令大小写与多余空格容忍():
    s = FaultState()
    apply_command(s, "  STUCK   ON  ")
    assert s.stuck is True
