"""W68:Super-LIO 定位的纯计算部分(初值换算、坐标换算、参数文件、点云套图打分、PCD 读写)。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from nexwar_localizer import slioloc as L
from nexwar_localizer.frames import Frames, quat_to_mat

#: C40221 前雷达(Airy):雷达 +X 朝下、+Z 朝前;IMU +Y 朝上、-Z 朝前(11-02 起步加速度核过)。
#: 外参按 Super-LIO 的读法(列优先):雷达系 → IMU 系。
R_IL = np.array([-0.017043, -0.999836, 0.006192, -0.999855, 0.017041, -0.000495,
                 0.00039, -0.006199, -0.999981]).reshape(3, 3).T
EXT = (R_IL.tolist(), [0.004279, 0.004176, -0.004436])
#: Super-LIO 建的图:世界系 Z 朝上;雷达系里「上」是 -X、「朝前」是 +Z。
FR = Frames(up=(0.0, 0.0, 1.0), sensor_up=(-1.0, 0.0, 0.0), sensor_forward=(0.0, 0.0, 1.0),
            sensor_height=0.2, sensor_in_base=(0.4043, 0.0))


def _heading(R) -> float:
    R = np.asarray(R)
    return math.atan2(R[1, 0], R[0, 0])


def test_机身在原点朝前_IMU的初值朝向是负90度():
    """跟 Super-LIO 建图起步时一样(``odom_robo`` 的 yaw -90):IMU 的 x 轴朝机身右边。"""
    init = L.init_param(*L.imu_pose(FR, EXT, 0.0, 0.0, 0.0))
    assert len(init) == 6 and all(isinstance(v, float) for v in init)
    assert init[3] == init[4] == 0.0                  # 横滚、俯仰交给它自己按重力对
    assert init[5] == pytest.approx(-90.0, abs=1.5)
    assert init[0] == pytest.approx(0.4043, abs=0.02)  # IMU 在雷达里,雷达在机身前 0.40 m


@pytest.mark.parametrize("x,y,yaw", [(0.0, 0.0, 0.0), (3.5, -2.0, 1.2), (-10.0, 7.0, -2.9)])
def test_初值换算来回一致(x, y, yaw):
    p, R = L.imu_pose(FR, EXT, x, y, yaw)
    lp, lq = L.lidar_pose(p, L.mat_quat(R), EXT)
    bx, by, byaw = FR.to_map2d(lp, lq)
    assert (bx, by) == pytest.approx((x, y), abs=1e-6)
    assert math.remainder(byaw - yaw, 2 * math.pi) == pytest.approx(0.0, abs=1e-6)
    init = L.init_param(p, R)
    assert math.radians(init[5]) == pytest.approx(_heading(R), abs=1e-9)
    assert init[:3] == pytest.approx(tuple(p))


def test_IMU位姿换雷达位姿_乘外参():
    p, q = (1.0, 2.0, 0.3), (0.0, 0.0, math.sin(0.4), math.cos(0.4))
    lp, lq = L.lidar_pose(p, q, EXT)
    Rw = np.array(quat_to_mat(q))
    assert lp == pytest.approx(tuple(Rw @ np.array(EXT[1]) + np.array(p)))
    assert np.allclose(np.array(quat_to_mat(lq)), Rw @ R_IL, atol=1e-4)   # 外参不严格正交


def test_重定位参数文件_初值是小数_地图按源码目录给相对路径(tmp_path):
    base = ("/**:\n  ros__parameters:\n    lio.map.save_map: true\n"
            '    lio.map.save_map_dir: "map/default"\n    lio.map.map_name: "map.pcd"\n'
            "    lio.eva.timer: true\n")
    src = tmp_path / "opt/superlio/ws/src/Super-LIO/src/super_lio"
    mapdir = tmp_path / "maps/m1/v3"
    txt = L.reloc_config(base, map_dir=mapdir, src_dir=src, init=(1, 2, 0, 0, 0, -90))
    assert "lio.map.save_map: false" in txt and "save_map: true" not in txt
    assert '"../../../../../../../maps/m1/v3"' in txt
    assert f'lio.map.map_name: "{L.MAP_FILE}"' in txt
    assert "lio.relocation.init_pose: [1.0, 2.0, 0.0, 0.0, 0.0, -90.0]" in txt
    assert "lio.relocation.update_map: false" in txt
    assert txt.count("lio.relocation.init_pose") == 1


def test_源码目录_机器人上与开发机两种装法(tmp_path):
    for sub in ("ws/src/Super-LIO/src/super_lio", "src/Super-LIO/src/super_lio"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / "CMakeLists.txt").write_text("")
        assert L.source_dir(tmp_path) == d
        (d / "CMakeLists.txt").unlink()
    with pytest.raises(L.SlioLocError, match="源码目录"):
        L.source_dir(tmp_path / "nothing")


def test_节点参数_话题改到定位命名空间(tmp_path):
    argv = L.node_argv(Path("/x/relocation_node"), tmp_path / "c.yaml")
    assert argv[0] == "/x/relocation_node"
    assert f"/lio/odom:={L.NS}/lio/odom" in argv
    assert "lio.ros.lidar_topic:=/front_lidar" in argv
    assert L.POSE_TOPIC == f"{L.NS}/lio/odom"


def test_PCD来回(tmp_path):
    xyz = np.random.default_rng(1).uniform(-5, 5, (1000, 3)).astype(np.float32)
    f = tmp_path / "m.pcd"
    L.write_pcd(f, xyz)
    head = f.read_bytes()[:200].decode("ascii", "replace")
    assert "FIELDS x y z intensity" in head and "DATA binary" in head
    assert np.allclose(L.read_pcd(f), xyz)


def test_体素降采样():
    xyz = np.array([[0.01, 0.01, 0.0], [0.02, 0.02, 0.0], [1.0, 1.0, 1.0]])
    assert len(L.voxel_down(xyz, 0.25)) == 2


def _room() -> np.ndarray:
    """一个 20×12 m 的房间:地、四面墙,中间两根柱子(点距 0.1 m)。"""
    g = np.arange(0, 1, 0.1)
    pts = []
    for x in np.arange(-10, 10, 0.1):
        for y in np.arange(-6, 6, 0.1):
            pts.append((x, y, 0.0))
        for z in np.arange(0, 3, 0.1):
            pts += [(x, -6.0, z), (x, 6.0, z)]
    for y in np.arange(-6, 6, 0.1):
        for z in np.arange(0, 3, 0.1):
            pts += [(-10.0, y, z), (10.0, y, z)]
    for cx, cy in ((-3.0, 2.0), (4.0, -1.5)):
        for z in np.arange(0, 3, 0.1):
            pts += [(cx + a, cy + b, z) for a in g * 0.5 for b in (0.0, 0.5)]
    return np.array(pts)


def test_套图打分_位姿对就高_偏1米就低():
    room = _room()
    fit = L.MapFit(L.voxel_down(room, 0.1))
    # 雷达离地 0.5 m(雷达系这里就用单位阵:套图只管点和位姿)
    p, q = (1.0, 0.5, 0.5), (0.0, 0.0, 0.0, 1.0)
    rng = np.random.default_rng(2)
    world = room[rng.choice(len(room), 3000, replace=False)]
    scan = world - np.array(p)                        # 雷达系里的点
    good = fit.score(scan, p, q)
    off = fit.score(scan, (2.0, 1.5, 0.5), q)        # 两个方向都偏 1 m
    turned = fit.score(scan, p, (0.0, 0.0, math.sin(math.radians(5)), math.cos(math.radians(5))))
    assert good > 0.95
    assert off < 0.3
    assert turned < good - 0.2


def test_套图打分_地面不算():
    """位姿偏了,地面上的点照样落在地面上:算进去会把错锁掩盖掉。"""
    room = _room()
    fit = L.MapFit(L.voxel_down(room, 0.1))
    floor = room[room[:, 2] == 0.0][::7]
    p = (1.0, 0.5, 0.5)
    assert fit.score(floor - np.array(p), (3.0, 2.5, 0.5), (0.0, 0.0, 0.0, 1.0)) is None


def test_套图打分_近处机身的点和太远的点不算():
    fit = L.MapFit(L.voxel_down(_room(), 0.25))
    body = np.full((500, 3), 0.1)                      # 机身上的点(离雷达 0.17 m)
    assert fit.score(body, (0.0, 0.0, 0.2), (0.0, 0.0, 0.0, 1.0)) is None   # 没有能算的点


def test_质量配对_点云配上后面那帧位姿_其余帧沿用_配不上的扔掉():
    room = _room()
    sc = L.ScanScorer()
    p, q = (1.0, 0.5, 0.5), (0.0, 0.0, 0.0, 1.0)
    scan = room[::13] - np.array(p)
    assert sc.pose(10.0, p, q) == 0.0                   # 还没地图、没打过分
    sc.set_fit(L.MapFit(L.voxel_down(room, 0.1)))
    sc.scan(10.0, scan)
    assert sc.pose(9.95, p, q) == 0.0                    # 比点云早的位姿:不是这帧的
    assert sc.pose(10.0, p, q) == 0.0                    # 上一帧扫描的结束 = 这一帧的开始:也不是
    good = sc.pose(10.1, p, q)                            # 扫描结束时刻的位姿
    assert good > 0.95
    assert sc.pose(10.2, (5.0, 5.0, 0.5), q) == good      # 没新点云:沿用
    sc.scan(11.0, scan)
    assert sc.pose(11.4, (3.0, 2.5, 0.5), q) == good     # 隔太久:配不上,扔掉
    sc.scan(12.0, scan)
    assert sc.pose(12.1, (3.0, 2.5, 0.5), q) < 0.3


def test_W68_外审_套图分过期_一直配不上就归零_换图清零():
    """codex r1 #2:原来最后一次的分没有有效期,一直配不上(或打不出分)也照用旧的高分。"""
    room = _room()
    sc = L.ScanScorer()
    p, q = (1.0, 0.5, 0.5), (0.0, 0.0, 0.0, 1.0)
    scan = room[::13] - np.array(p)
    sc.set_fit(L.MapFit(L.voxel_down(room, 0.1)))
    sc.scan(10.0, scan)
    assert sc.pose(10.1, p, q) > 0.95
    t = 10.1
    for _ in range(40):                                   # 点云一直来、一直配不上
        t += 0.1
        sc.scan(t - 0.5, scan)
        last = sc.pose(t, p, q)
    assert last == 0.0
    sc.scan(t, scan)
    assert sc.pose(t + 0.1, p, q) > 0.95                  # 又配上了
    sc.set_fit(None)
    assert sc.pose(t + 0.2, p, q) == 0.0                  # 换图(新图还没载好):旧分作废


def test_W68_外审r2_打分途中换了图_旧图的分不写回():
    """codex r2 #2:rclpy 线程在算旧图的分,这时换图清零;算完原来照样写回 0.99。"""
    sc = L.ScanScorer()

    class 旧图:
        def score(self, *_a):
            sc.set_fit(None)                              # 打分途中 asyncio 那边换了图
            return 0.99

    sc.set_fit(旧图())
    sc.scan(10.0, np.zeros((10, 3)))
    assert sc.pose(10.1, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)) == 0.0
    assert sc.pose(10.2, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)) == 0.0


def _旧的打分(fit, scan, p, q):
    """W71 之前的做法(整帧先乘旋转再筛、再抽):拿来比对,改快以后分数一分不差。"""
    from nexwar_localizer import superlio as S
    a = np.asarray(scan, dtype=np.float64).reshape(-1, 3)
    a = a[np.isfinite(a).all(axis=1)]
    r2 = (a * a).sum(axis=1)
    a = a[(r2 >= S.RANGE_MIN_M ** 2) & (r2 <= S.RANGE_MAX_M ** 2)]
    w = a @ np.array(quat_to_mat(q)).T + np.asarray(p, dtype=np.float64)
    w = w[w[:, 2] > float(p[2]) - L.FIT_FLOOR_BELOW_M]
    if len(w) < L.FIT_MIN_POINTS:
        return None
    if len(w) > L.FIT_POINTS:
        w = w[np.linspace(0, len(w) - 1, L.FIT_POINTS).astype(np.int64)]
    v = np.floor(w / fit.res).astype(np.int64)
    k = L._keys((v[:, None, :] + L._NEIGH[None, :, :]).reshape(-1, 3)).reshape(len(v), -1)
    i = np.searchsorted(fit._occ, k)
    hit = (i < len(fit._occ)) & (fit._occ[np.minimum(i, len(fit._occ) - 1)] == k)
    return float(hit.any(axis=1).mean())


def test_W71_套图打分只转抽出来的点_分数跟整帧先转一样_整帧不过矩阵乘(monkeypatch):
    """11-03 商场:定位器主进程 8 个线程各 10–14%(OpenBLAS 线程池)。打分先把整帧 8.6 万个点乘旋转、
    再抽 2000 个;改成先只算高度筛地面、抽好再转。"""
    rng = np.random.default_rng(7)
    room = _room()
    fit = L.MapFit(L.voxel_down(room, 0.1))
    scan = rng.uniform(-9, 9, size=(30000, 3)) * np.array([1.0, 0.6, 0.2]) + np.array([0, 0, 0.5])
    scan[::97] = np.nan
    for yaw, p in ((0.0, (0.0, 0.0, 0.4)), (0.7, (1.5, -2.0, 0.3)), (-2.4, (-4.0, 1.0, 0.5))):
        q = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
        assert fit.score(scan, p, q) == _旧的打分(fit, scan, p, q)
    calls = []
    real = L._to_map

    def 记(pts, rot, p):
        calls.append(len(pts))
        return real(pts, rot, p)
    monkeypatch.setattr(L, "_to_map", 记)
    fit.score(scan, (0.0, 0.0, 0.4), (0.0, 0.0, 0.0, 1.0))
    assert calls and max(calls) <= L.FIT_POINTS, calls
