"""IMU 当 MOLA 的转速先验(工单 27):安装角的换算、从录包核安装角、给 MOLA 的参数。合成数据。

背景:不给 IMU 时 MOLA 的运动模型靠前后帧外推,某一帧 ICP 错跳一下就被当成「在转」(玻璃那段录包里
估出 1.66 rad/s 的假转速),下一帧按它外推越推越远、回不来;给了 IMU,角速度按陀螺仪,站着就是 0,
错一两帧自己回来(回放:错的帧 180 → 2)。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from d1max_localizer import imu, main, replay, tools
from d1max_localizer.frames import Frames, mat_to_quat
from d1max_localizer.supervisor import mola_command

# C40221 前雷达:IMU 的「上」是 +y,点云的「上」是 −x(倒装);录包核出来 ω_点云 = (−ω_y, −ω_x, −ω_z)
C40221 = np.array([[0.0, -1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])


def test_安装角换算_C40221_是_yaw_负90_roll_180():
    assert np.allclose(imu.matrix_from_ypr((-90.0, 0.0, 180.0)), C40221, atol=1e-9)
    y, p, r = imu.ypr_from_matrix(C40221)
    assert np.allclose(imu.matrix_from_ypr((y, p, r)), C40221, atol=1e-9)


@pytest.mark.parametrize("ypr", [(30.0, 10.0, -20.0), (-90.0, 0.0, 180.0), (90.0, 45.0, 0.0),
                                 (170.0, -60.0, 120.0)])
def test_安装角来回换算不变(ypr):
    m = imu.matrix_from_ypr(ypr)
    assert np.allclose(imu.matrix_from_ypr(imu.ypr_from_matrix(m)), m, atol=1e-9)


def _rates(n=600, seed=1, yaw_amp=0.8):
    """机身(点云系)角速度:绕「上」(−x)来回转,另加一点俯仰 / 横滚的晃。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n) * 0.1
    yaw = yaw_amp * np.sin(t * 0.7)
    up = np.array([-1.0, 0.0, 0.0])
    w = yaw[:, None] * up + rng.normal(0, 0.05, (n, 3)) * np.array([0.0, 1.0, 1.0])
    return t, w


def test_从机身角速度与陀螺仪核出安装角_吸到直角():
    t, w_body = _rates()
    rng = np.random.default_rng(2)
    gyro = w_body @ C40221 + rng.normal(0, 0.01, w_body.shape)      # ω_imu = Mᵀ ω_body
    gyro += np.array([0.005, 0.02, -0.01])                           # 零偏
    r = imu.fit_axes(w_body, gyro)
    assert r.ok, r.why
    assert np.allclose(imu.matrix_from_ypr(r.ypr), C40221, atol=1e-6)
    assert r.snapped and r.snap_deg < 5.0
    assert r.yaw_corr > 0.95


def test_几乎只转朝向_横向晃得少_照样挑对直角装法():
    """原地转身的录包:只有绕「上」的转动是强的,自由拟合在另两根轴上会漂(真包里漂了 20°),
    按 24 种直角装法各算残差挑最小的。"""
    rng = np.random.default_rng(5)
    t = np.arange(600) * 0.1
    up = np.array([-1.0, 0.0, 0.0])
    w_body = (0.8 * np.sin(t * 0.7))[:, None] * up
    w_body += rng.normal(0, 0.02, w_body.shape) * np.array([0.0, 1.0, 1.0])   # 一点点晃
    gyro = w_body @ C40221 + rng.normal(0, 0.02, w_body.shape)               # 陀螺仪噪声跟晃差不多
    r = imu.fit_axes(w_body, gyro)
    assert r.ok, r.why
    assert np.allclose(imu.matrix_from_ypr(r.ypr), C40221, atol=1e-6)


def _弱晃(true, seed=427):
    """外审 R3:主要绕 z 左右转,另两根轴只晃 0.02 rad/s;陀螺仪按 ``true`` 装、不加噪声。"""
    rng = np.random.default_rng(seed)
    t = np.arange(600) * 0.1
    wb = np.column_stack([rng.normal(0, 0.02, 600), rng.normal(0, 0.02, 600),
                          0.8 * np.sin(0.7 * t)])
    return wb, wb @ true


def test_外审R3_另两根轴晃得少_但数据明摆着不是直角装法_不硬吸到直角():
    true = imu.matrix_from_ypr((40.0, 0.0, 0.0))
    wb, gyro = _弱晃(true)
    r = imu.fit_axes(wb, gyro)
    if r.ok:                                       # 要么按拟合值给对,要么拒答、让补录多轴的
        assert imu._angle_deg(imu.matrix_from_ypr(r.ypr), true) < 5.0
        assert not r.snapped
    else:
        assert "晃" in r.why or "走动" in r.why


def test_外审R3_吸到直角时_相关系数按最后给出的装法算():
    rng = np.random.default_rng(5)
    t = np.arange(600) * 0.1
    w_body = (0.8 * np.sin(t * 0.7))[:, None] * np.array([-1.0, 0.0, 0.0])
    w_body += rng.normal(0, 0.02, w_body.shape) * np.array([0.0, 1.0, 1.0])
    gyro = w_body @ C40221 + rng.normal(0, 0.02, w_body.shape)
    r = imu.fit_axes(w_body, gyro)
    assert r.ok and r.snapped
    m = imu.matrix_from_ypr(r.ypr)
    wb_c, g_c = w_body - w_body.mean(axis=0), gyro - gyro.mean(axis=0)
    k = int(np.argmax(wb_c.std(axis=0)))
    want = float(np.corrcoef(wb_c[:, k], (g_c @ m.T)[:, k])[0, 1])
    assert r.yaw_corr == pytest.approx(want, abs=1e-9)


def test_只转朝向_一点不晃_另两根轴定不下来():
    t = np.arange(600) * 0.1
    w_body = (0.8 * np.sin(t * 0.7))[:, None] * np.array([-1.0, 0.0, 0.0])
    r = imu.fit_axes(w_body, w_body @ C40221)
    assert not r.ok
    assert "定不下来" in r.why


def test_没怎么转_核不出来():
    t, w_body = _rates(yaw_amp=0.01)
    gyro = w_body @ C40221
    r = imu.fit_axes(w_body, gyro)
    assert not r.ok
    assert "转" in r.why


def test_陀螺仪跟机身对不上_不给结论():
    t, w_body = _rates()
    rng = np.random.default_rng(3)
    gyro = rng.normal(0, 0.5, w_body.shape)                          # 话题选错了:跟转动无关
    r = imu.fit_axes(w_body, gyro)
    assert not r.ok


def test_从轨迹算机身角速度():
    """绕「上」(z)匀速 0.5 rad/s 转:机身系角速度 (0, 0, 0.5)。"""
    rows = []
    for i in range(20):
        a = 0.05 * i
        c, s = math.cos(a), math.sin(a)
        rows.append((100.0 + 0.1 * i, (0.0, 0.0, 0.0),
                     mat_to_quat(((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0)))))
    mid, w = imu.body_rates(rows)
    assert len(mid) == 19
    assert np.allclose(w, [[0.0, 0.0, 0.5]] * 19, atol=1e-6)


def test_陀螺仪按轨迹的每一段取平均():
    stamps = np.array([0.0, 0.1, 0.2])
    g = np.array([[t, 1.0 if t < 0.1 else 2.0, 0.0, 0.0] for t in np.arange(0.0, 0.2, 0.005)])
    out = imu.average_between(g, stamps)
    assert out.shape == (2, 3)
    assert np.allclose(out[:, 0], [1.0, 2.0])


# ------------------------------------------------------------ 给 MOLA 的参数


def test_狗上_MOLA_接_IMU_的启动参数与环境变量():
    p = imu.ImuPrior("/front_lidar/imu", (-90.0, 0.0, 180.0))
    cmd = mola_command(main_prior(), lidar_topic="/d1max/merged_lidar", imu=p)
    args = dict(a.split(":=", 1) for a in cmd[4:])
    assert args["imu_topic_name"] == "/front_lidar/imu"
    assert args["ignore_imu_pose_from_tf"] == "True"          # 安装角按下面的环境变量,不查 tf
    assert p.env() == {"IMU_POSE_YAW": "-90", "IMU_POSE_PITCH": "0", "IMU_POSE_ROLL": "180"}


def test_不给_IMU_启动参数不变():
    args = dict(a.split(":=", 1) for a in mola_command(main_prior(), lidar_topic="/x")[4:])
    assert "imu_topic_name" not in args and "ignore_imu_pose_from_tf" not in args


def main_prior():
    from pathlib import Path
    return Path("/var/lib/d1max/agent/maps/m/3/prior.mm")


def test_定位器参数_给了话题就得给安装角(capsys):
    a = main.parse_args(["--imu-topic", "/front_lidar/imu", "--imu-ypr=-90,0,180"])
    assert a.imu == imu.ImuPrior("/front_lidar/imu", (-90.0, 0.0, 180.0))
    assert main.parse_args([]).imu is None
    with pytest.raises(SystemExit):
        main.parse_args(["--imu-topic", "/front_lidar/imu"])
    with pytest.raises(SystemExit):
        main.parse_args(["--imu-ypr=-90,0,180"])


def test_定位器_接了_IMU_看管器带上安装角的环境变量():
    a = main.parse_args(["--imu-topic", "/front_lidar/imu", "--imu-ypr=-90,0,180"])
    env = main.mola_env(a, base={"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "IMU_POSE_YAW": "-90", "IMU_POSE_PITCH": "0",
                   "IMU_POSE_ROLL": "180"}
    assert main.mola_env(main.parse_args([]), base={"PATH": "/usr/bin"}) is None


def test_回放_MOLA_命令行接_IMU(tmp_path):
    Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
           sensor_height=0.0).save(tmp_path / "frames.json")
    env, _ = replay.mola_env_and_cmd(tmp_path / "bag", tmp_path, (0.0, 0.0, 0.0), tmp_path / "o",
                                     imu=imu.ImuPrior("/front_lidar/imu", (-90.0, 0.0, 180.0)))
    assert env["MOLA_IMU_TOPIC"] == "/front_lidar/imu"
    assert env["MOLA_USE_FIXED_IMU_POSE"] == "true"
    assert (env["IMU_POSE_YAW"], env["IMU_POSE_PITCH"], env["IMU_POSE_ROLL"]) == ("-90", "0", "180")
    env, _ = replay.mola_env_and_cmd(tmp_path / "bag", tmp_path, (0.0, 0.0, 0.0), tmp_path / "o")
    assert "MOLA_IMU_TOPIC" not in env


def test_回放子命令收_IMU_参数(tmp_path, monkeypatch):
    seen = {}

    def run_mola(bag, prior, init, out, **kw):
        seen.update(kw)
        raise SystemExit(0)

    monkeypatch.setattr(replay, "run_mola", run_mola)
    Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
           sensor_height=0.0).save(tmp_path / "frames.json")
    with pytest.raises(SystemExit):
        tools.main(["replay", "--bag", str(tmp_path / "b"), "--prior", str(tmp_path), "--init",
                    "0,0,0", "--out", str(tmp_path / "o"), "--imu-topic", "/front_lidar/imu",
                    "--imu-ypr=-90,0,180"])
    assert seen["imu"] == imu.ImuPrior("/front_lidar/imu", (-90.0, 0.0, 180.0))
