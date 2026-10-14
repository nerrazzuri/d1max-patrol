"""建图打包(W09c1 决定 2、3、4):把一次建图的产物打成一个地图版本的文件 ——

- ``prior.mm``:MOLA 的局部地图(定位先验),按 ``prior_pack`` 打包(默认原样;``regroup`` 要回放验过
  再用);
- ``frames.json``:坐标换算,从建图轨迹标定;**「上」的正负号用点云判**(最密的那层水平面 —— 地面 ——
  要在雷达下面):光看轨迹分不出上下,新狗 C40011 的雷达就是 X 朝下装的,按「X 朝上」标出来的平面是
  镜像的,转向会反(2026-09-27 画 newdog2 的栅格时发现)。**再拿狗自己的里程核一遍手性**(对账 S2,
  :func:`handedness`):录包里有运控的里程(``/odom/mc_odom``),建图轨迹在地图平面上往左转的时候里程也
  得往左转;一直反着(镜像)就不出图、说清楚,核不了(没录包、没里程、转得太少)照出图,
  ``build.json`` 的 ``handedness`` 写着结论(2026-10-13 狗上建的 ``office-c40221:2`` 跟台式机拿同一个
  录包重建的上下镜像,W36);
- ``floor.pgm`` + ``floor.yaml``:按 ``frames.json`` 的平面画的规划栅格(:mod:`d1max_localizer.grid`);
  有录包就从里面逐帧读扫描打真射线(:func:`bag_scans`,跟着狗走的人清得掉),读不了退回模拟射线,
  ``build.json`` 的 ``grid.rays`` 写着用的哪种;
- ``coverage.json``:「哪里有图」—— 建图时走过的路(地图平面上每 :data:`COVERAGE_STEP_M` 一点);
- ``build.json``:怎么建的(来源、参数、各步耗时、标定说明)。

两步::func:`run_mapping` 用 MOLA 命令行在录包上建图(雷达里程计 + 存局部地图、轨迹、simplemap;边走
边建的 W09c2 用在线跑出来的同样三样);:func:`package` 把这三样打成版本文件。MOLA 的命令都经 ``run``
调(测试换成假的)。点云、栅格要 numpy(狗上用 ROS 系统 Python 里 apt 装的那份)。

**回环(可选,docs/建图回环.md)**:雷达里程计走一圈回到起点会差一点(漂移),同一面墙就画成两层。装了
回环工具(``mola_sm_loop_closure`` 的 ``mola-sm-lc-cli``,:func:`find_loop_closure`)就在打包前先跑
:func:`loop_close`:simplemap 进、改过关键帧位姿的 simplemap 出;再从它重出点云、先验(``sm2mm`` +
``pipelines/prior-from-simplemap.yaml``),轨迹按关键帧的改动挪(:func:`correct_traj`),打包用这三样。
没装、跑不成、没找到回环、改得离谱,都照原来的打包,``build.json`` 的 ``loop_closure`` 写着为什么。
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.geo import GEO_FILE
from d1max_contract.maps import GEOMETRY_FILES
from d1max_localizer.frames import Frames, calibrate
from d1max_localizer.replay import MOLA_SHARE, read_tum

#: 前雷达在狗身上的水平偏移(朝前、朝左,米;SDK 开发指南 2.10 节,前激光雷达 x = 404.3 mm)。
FRONT_LIDAR_IN_BASE = (0.4043, 0.0)
#: RS-Airy 装法上「上」「前」是哪根轴(「上」的正负号由点云判)。
SENSOR_UP_HINT = (1.0, 0.0, 0.0)
SENSOR_FORWARD_HINT = (0.0, 0.0, 1.0)
COVERAGE_STEP_M = 0.5
#: 真射线每几帧扫描取一帧、每帧每几个点取一个(newdog2:676 帧,读包 11 s、打射线 10 s)。
SCAN_EVERY = 3
SCAN_POINT_STRIDE = 4
#: 扫描的时刻跟轨迹上最近那一帧差多少以内才配得上。
SCAN_MATCH_S = 0.05
#: 逐帧扫描只认这个话题:MOLA 跟的就是它的坐标系(rslidar_head),点乘轨迹的位姿就进了地图。
SCAN_TOPIC = "/front_lidar"
#: 在前雷达系里的话题:前雷达、前后雷达合并(W09i,``merge-bag`` 写的)。MOLA 的机身系就是前雷达,
#: 打包画栅格能逐帧打真射线。
FRONT_FRAME_TOPICS = (SCAN_TOPIC, "/d1max/merged_lidar")
FILES = GEOMETRY_FILES
Runner = Callable[..., Any]

#: 回环工具的命令名;找法见 :func:`find_loop_closure`。
LC_CLI = "mola-sm-lc-cli"
#: 环境变量:给回环工具的路径,或者 ``off`` 不回环。
LC_ENV = "D1MAX_LOOP_CLOSURE"
#: 自己编的回环工具装在这儿(docs/建图回环.md:colcon ``--merge-install`` 装到这个前缀)。
LC_DEFAULT = Path("/opt/d1max/loop-closure/bin") / LC_CLI
PIPELINES = Path(__file__).with_name("pipelines")
#: 回环的参数(从上游 3D GICP 那份改的,改了什么写在文件头)。
LC_PIPELINE = PIPELINES / "loop-closure.yaml"
#: 从回环后的 simplemap 重出定位先验(跟 MOLA 建图存的局部地图同一个图层、同一个类)。
PRIOR_PIPELINE = PIPELINES / "prior-from-simplemap.yaml"
#: 回环把关键帧挪得超过 max(这么多米, 路程 × 这个比例),或者转得超过这么多度,就不用(多半是认错了
#: 地方的回环把图拉歪了)。只拦得住离谱的;办公室三份图(23–27 m)实测挪 0.06–0.24 m、转 0.7–2.3°。
LC_MAX_SHIFT_M = 1.0
LC_MAX_SHIFT_SHARE = 0.05
LC_MAX_TURN_DEG = 15.0
_LC_COUNT = re.compile(r"(?:Overall number of accepted loop-closures|Total accepted loop closures)"
                       r":\s*(\d+)")
#: 核手性(对账 S2)用的狗自己的里程:建图录包里录的(``app/mapping.py`` 的 ``RECORD_TOPICS``),
#: 运控的那个在前。
ODOM_TOPICS = ("/odom/mc_odom", "/odom/current_pose")
#: 每隔这么久比一次两边各转了多少(秒);这一段里程转过这么多才算「在转」(弧度,约 3°)。
HAND_STEP_S = 1.0
HAND_MIN_STEP_RAD = 0.05
#: 算进来的转向加起来不到这么多(弧度,约 57°)不下结论。建图走一圈转得远比这多(10-08、10-13 两份
#: 办公室录包:900°、1300° 上下)。
HAND_MIN_TURN_RAD = 1.0
#: 按转过的角度加权:同号的占这么多以上算对,反号的占这么多以上算镜像,中间说不清(照出图)。两份办公室
#: 录包台式机重建:同号 100%,相关 0.999。
HAND_AGREE = 0.8
#: 两边时刻没对齐的余量:在 ±这么多秒里找两边最像的那一下(录包里雷达「收到 − 报文头」比里程大
#: 2.7 s,10-08 那份;报文头两边是一个钟)。
HAND_MAX_LAG_S = 3.0
HAND_LAG_STEP_S = 0.1


class BuildError(RuntimeError):
    """建图打包不成(说人话的原因)。"""


class _ScanReadError(Exception):
    """读逐帧扫描出错(没有 ROS、包坏了):退回模拟射线。栅格自己出错不算这个。"""


@dataclass(frozen=True)
class PriorPack:
    """先验怎么打包:``none`` 原样;``regroup`` = ``mm-kf-regroup`` 合并关键帧并按体素降采样。"""
    mode: str = "none"
    voxel: float = 0.3
    extent: float = 0.5

    @classmethod
    def parse(cls, s: str) -> PriorPack:
        """``none`` 或 ``regroup:<体素米>:<范围倍数>``。"""
        if s == "none":
            return cls()
        parts = s.split(":")
        if parts[0] != "regroup" or len(parts) != 3:
            raise ValueError(f"先验打包写法不对:{s!r}(none 或 regroup:体素:范围倍数)")
        v, e = float(parts[1]), float(parts[2])
        if not (0 < v <= 2 and 0 < e <= 5):
            raise ValueError(f"先验打包参数超出范围:{s!r}")
        return cls("regroup", v, e)

    def label(self) -> str:
        return "none" if self.mode == "none" else f"regroup:{self.voxel:g}:{self.extent:g}"


def _plugin() -> list[str]:
    found = sorted(glob.glob("/opt/ros/humble/lib/**/libmola_metric_maps.so", recursive=True))
    return ["-l", found[0]] if found else []


def run_mapping(bag: Path, work: Path, *, lidar_topic: str = "/front_lidar",
                run: Runner = subprocess.run) -> dict[str, float]:
    """MOLA 命令行在录包上建图:``work`` 里得到 ``raw_prior.mm``、``traj.tum``、``map.simplemap``,
    还有 ``mola.json``(MOLA 的版本、流水线、参数 —— 打包抄进 ``build.json``,给回放复现)。回耗时。"""
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MOLA_LIDAR_TOPIC": lidar_topic,
           "MOLA_TF_BASE_LINK": ("rslidar_head" if lidar_topic in FRONT_FRAME_TOPICS
                                 else "base_link"),
           "MOLA_SAVE_MM": str(work / "raw_prior.mm"), "MOLA_LOCAL_MAP_MAX_SIZE": "0"}
    cmd = ["mola-lidar-odometry-cli", "-c", str(MOLA_SHARE / "pipelines/lidar3d-default.yaml"),
           "--state-estimator-param-file",
           str(MOLA_SHARE / "state-estimator-params/state-estimation-simple.yaml"),
           "--input-rosbag2", str(bag), "--lidar-sensor-label", lidar_topic,
           "--output-tum-path", str(work / "traj.tum"),
           "--output-simplemap", str(work / "map.simplemap"), "--progress-bar-period", "0"]
    ver = run(["dpkg-query", "-W", "-f=${Version}", "ros-humble-mola-lidar-odometry"],
              capture_output=True, text=True)
    (work / "mola.json").write_text(json.dumps({
        "version": (getattr(ver, "stdout", "") or "").strip()[:80],
        "pipeline": cmd[2], "state_estimator": cmd[4],
        "env": {k: env[k] for k in env if k.startswith("MOLA_")}}, ensure_ascii=False,
        indent=2) + "\n")
    t0 = time.monotonic()
    _run(run, cmd, env, work / "mola.log", "MOLA 建图")
    return {"mapping_s": round(time.monotonic() - t0, 1)}


def package(work: Path, out: Path, *, prior_pack: PriorPack | None = None,
            sensor_in_base: tuple[float, float] = FRONT_LIDAR_IN_BASE, source: str = "",
            run: Runner = subprocess.run, timings: dict[str, float] | None = None,
            bag: Path | None = None, lidar_topic: str = SCAN_TOPIC,
            read_scans: Callable[..., Iterator[Any]] | None = None,
            rtk_antenna: tuple[float, float] = (0.0, 0.0),
            clock_offset: Callable[[Path, str], float | None] | None = None,
            loop_closure: str | None = None,
            read_odom: Callable[[Path], tuple[str, list[tuple[float, float, float]]] | None]
            | None = None) -> list[str]:
    """``work`` 里的 ``raw_prior.mm``、``traj.tum``、``map.simplemap`` → ``out`` 里的版本文件。回
    文件名。给了 ``bag``(建图的那个录包)就从里面逐帧读扫描打真射线、读狗自己的里程核手性(镜像就抛
    :class:`BuildError`,一个版本文件都不写)。``loop_closure`` 是回环工具的路径
    (:func:`find_loop_closure`;None = 不回环):先回环(:func:`loop_close`),成了就从 ``work/lc``
    里回环后的三样打包。"""
    prior_pack = prior_pack or PriorPack()
    import numpy as np

    from d1max_localizer import grid

    for name in ("raw_prior.mm", "traj.tum", "map.simplemap"):
        if not (work / name).is_file():
            raise BuildError(f"建图没出 {name}")
    out.mkdir(parents=True, exist_ok=True)
    timings = dict(timings or {})
    n = len(read_tum(work / "traj.tum"))
    if n < 10:
        raise BuildError(f"建图轨迹只有 {n} 帧,太短")
    t0 = time.monotonic()
    src, lc_info = loop_close(work, loop_closure, run)
    if lc_info["ran"]:
        timings["loop_closure_s"] = round(time.monotonic() - t0, 1)
    traj = read_tum(src / "traj.tum")
    t0 = time.monotonic()
    points = _points(src, run)
    timings["points_s"] = round(time.monotonic() - t0, 1)
    sensor = np.array([p for _, p, _ in traj])
    frames, why = orient(points, traj)
    frames = frames.with_sensor_in_base(*sensor_in_base)
    hand = check_handedness(traj, frames, bag, lidar_topic, read_odom or bag_odom,
                            clock_offset or bag_clock_offset)
    if hand["verdict"] == "mirrored":
        raise BuildError(
            f"地图是镜像的,不出图:建图轨迹在地图平面上的转向跟狗自己的里程({hand['topic']})"
            f"反号 —— 按转过的角度算 {1 - hand['agree']:.0%} 反着(一共转了 {hand['turn_deg']}°)。"
            f"多半是「上」判反了({why});这样的图上转向会反、导航会往反方向拐。"
            "核雷达的装法和 frames 的标定(对账 S2)")
    frames.save(out / "frames.json")
    t0 = time.monotonic()
    body = np.array([frames.to_map2d(p, q)[:2] for _, p, q in traj])
    try:
        g, rays = _render(points, sensor, frames, body, traj, bag, lidar_topic,
                          read_scans or bag_scans)
    except grid.GridTooLarge as exc:
        raise BuildError(str(exc)) from exc
    grid.write(g, out / "floor")
    timings["grid_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    _pack_prior(src / "raw_prior.mm", out / "prior.mm", prior_pack, run, src)
    timings["prior_s"] = round(time.monotonic() - t0, 1)
    cov = coverage(traj, frames)
    (out / "coverage.json").write_text(json.dumps({"version": 1, "step_m": COVERAGE_STEP_M,
                                                   "path": cov}) + "\n")
    geo_info = _georeference(out, bag, traj, frames, rtk_antenna, lidar_topic,
                             clock_offset or bag_clock_offset)
    files = list(FILES)
    if geo_info.get("written"):
        files.append(GEO_FILE)
    (out / "build.json").write_text(json.dumps({
        "version": 1, "source": source, "builder": "mola-lidar-odometry", "prior_pack":
        prior_pack.label(), "frames": why, "frames_count": len(traj),
        "grid": {"res": g.res, "size": list(g.image.shape[::-1]), "origin": list(g.origin),
                 "rays": rays},
        "mola": _mola_info(work), "loop_closure": lc_info, "geo": geo_info, "handedness": hand,
        "timings_s": timings}, ensure_ascii=False, indent=2) + "\n")
    return files


def check_handedness(traj: Sequence[Any], frames: Frames, bag: Path | None, lidar_topic: str,
                     read_odom: Callable[[Path], tuple[str, list[tuple[float, float, float]]]
                                         | None],
                     clock_offset: Callable[[Path, str], float | None]) -> dict[str, Any]:
    """建图轨迹(按 ``frames`` 换到地图平面)的转向跟录包里狗自己的里程比(对账 S2)。回 ``build.json``
    的 ``handedness``:``verdict`` = ``ok`` / ``mirrored`` / ``unclear`` / ``skipped``(没录包、没
    里程、读不了),带比的是哪个话题、按哪个钟对的时刻。先按报文头的钟对(狗上两边是一个钟);两边时段
    对不上再按「收到的时刻」对:轨迹加上雷达的「收到 − 报文头」(``clock_offset``)。"""
    if bag is None:
        return {"verdict": "skipped", "why": "没给录包"}
    try:
        got = read_odom(bag)
    except Exception as exc:  # noqa: BLE001 —— 核不了照出图(没有 ROS、包坏了),写明为什么
        why = f"读不了录包里的里程:{type(exc).__name__}: {exc}"
        return {"verdict": "skipped", "why": why[:300]}
    if got is None:
        return {"verdict": "skipped", "why": f"录包里没有里程({'、'.join(ODOM_TOPICS)})"}
    topic, rows = got
    yaws = [(t, frames.to_map2d(p, q)[2]) for t, p, q in traj]
    res, clock = handedness(yaws, [(h, y) for h, _, y in rows]), "header"
    if res.pop("apart", False):
        off = clock_offset(bag, lidar_topic)
        if off is not None:
            res = handedness([(t + off, y) for t, y in yaws], [(r, y) for _, r, y in rows])
            res.pop("apart", None)
            clock = "receive"
    return {**res, "topic": topic, "clock": clock}


def handedness(map_yaw: Sequence[tuple[float, float]], odom_yaw: Sequence[tuple[float, float]]
               ) -> dict[str, Any]:
    """两串 ``(时刻, 朝向)``(同一个钟):地图平面上的转向跟里程的同不同号。每 :data:`HAND_STEP_S` 比
    一次各转了多少;两边时刻在 ±:data:`HAND_MAX_LAG_S` 里挪到最像(同号、反号里差得少的那个,不偏向
    哪个号);里程转得够的那几段按转过的角度加权数同号的占比(``agree``)。回 ``verdict``
    (``ok`` / ``mirrored`` / ``unclear``)、``why``、``agree``、``turn_deg``、``windows``、
    ``lag_s``、``corr``、``ratio``(地图转的 / 里程转的,中位数)。两边时段对不上时带 ``apart``。"""
    import numpy as np

    m = np.array(sorted(map_yaw), float).reshape(-1, 2)
    o = np.array(sorted(odom_yaw), float).reshape(-1, 2)
    if len(m) < 2 or len(o) < 2:
        return {"verdict": "unclear", "why": "帧太少", "apart": True}
    mu, ou = np.unwrap(m[:, 1]), np.unwrap(o[:, 1])
    lo = max(m[0, 0], o[0, 0]) + HAND_MAX_LAG_S
    hi = min(m[-1, 0], o[-1, 0]) - HAND_MAX_LAG_S
    if hi - lo < 10 * HAND_STEP_S:
        return {"verdict": "unclear", "apart": True,
                "why": f"两边的时段对不上(重叠 {max(0.0, hi - lo):.0f} s)"}
    g = np.arange(lo, hi, HAND_STEP_S)
    do = np.diff(np.interp(g, o[:, 0], ou))
    sel = np.abs(do) >= HAND_MIN_STEP_RAD
    turn = float(np.abs(do[sel]).sum())
    info: dict[str, Any] = {"turn_deg": round(math.degrees(turn)), "windows": int(sel.sum())}
    if turn < HAND_MIN_TURN_RAD:
        return {"verdict": "unclear", **info,
                "why": f"里程转得太少(算进来的 {info['turn_deg']}°,"
                       f"要 {math.degrees(HAND_MIN_TURN_RAD):.0f}°)"}
    # 挪到两边最像:同号、反号里差得少的那个算(不偏向哪个号);一样像取挪得少的
    best: tuple[float, float, Any] | None = None
    lags = sorted(np.arange(-HAND_MAX_LAG_S, HAND_MAX_LAG_S + 1e-9, HAND_LAG_STEP_S), key=abs)
    for lag in lags:
        dm = np.diff(np.interp(g + lag, m[:, 0], mu))
        e = float(min(((dm - do) ** 2).sum(), ((dm + do) ** 2).sum()))
        if best is None or e < best[1] - 1e-9 * (1.0 + best[1]):
            best = (float(lag), e, dm)
    assert best is not None
    lag, _, dm = best
    w = np.abs(do[sel])
    agree = float((w * (np.sign(dm[sel]) == np.sign(do[sel]))).sum() / w.sum())
    corr = (float(np.corrcoef(dm, do)[0, 1]) if np.std(dm) > 1e-9 and np.std(do) > 1e-9
            else None)
    info.update(agree=round(agree, 3), lag_s=round(lag, 1) + 0.0,
                corr=None if corr is None else round(corr, 3),
                ratio=round(float(np.median(dm[sel] / do[sel])), 3))
    if agree >= HAND_AGREE:
        return {"verdict": "ok", **info, "why": "转向跟里程同号"}
    if agree <= 1 - HAND_AGREE:
        return {"verdict": "mirrored", **info, "why": "转向跟里程反号:地图是镜像的"}
    return {"verdict": "unclear", **info, "why": "转向跟里程一阵同号一阵反号:说不清(时刻对不齐?)"}


def bag_odom(bag: Path, topics: Sequence[str] = ODOM_TOPICS
             ) -> tuple[str, list[tuple[float, float, float]]] | None:
    """录包里狗自己的里程(``nav_msgs/Odometry``):``topics`` 里第一个录了的 → ``(话题, [(报文头时刻,
    收到时刻, 朝向)])``;一个都没录是 None。要 ROS 的系统 Python(``rosbag2_py``)。"""
    import rosbag2_py
    from nav_msgs.msg import Odometry
    from rclpy.serialization import deserialize_message

    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("", ""))
    have = {t.name for t in r.get_all_topics_and_types()}
    topic = next((t for t in topics if t in have), None)
    if topic is None:
        return None
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    rows = []
    while r.has_next():
        _, raw, t_ns = r.read_next()
        m = deserialize_message(raw, Odometry)
        q = m.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        rows.append((m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, t_ns * 1e-9, yaw))
    return topic, rows


def find_loop_closure(setting: str = "auto", *, env: Mapping[str, str] | None = None,
                      which: Callable[[str], str | None] = shutil.which,
                      default: Path = LC_DEFAULT) -> str | None:
    """回环工具在哪(没有是 None —— 回环是可选的一步,找不到就不回环,不报错)。``setting``:``off``
    不回环;``auto`` 依次看环境变量 :data:`LC_ENV`(路径或 ``off``)、``PATH``(apt 装进
    ``/opt/ros/humble/bin`` 的那种)、:data:`LC_DEFAULT`(自己编、装在自己前缀下的);别的当路径。给的
    路径不是能跑的程序也当没有。"""
    env = os.environ if env is None else env
    if setting == "auto":
        setting = env.get(LC_ENV, "").strip() or "auto"
    if setting == "off":
        return None
    if setting != "auto":
        return setting if _executable(Path(setting)) else None
    found = which(LC_CLI)
    if found:
        return found
    return str(default) if _executable(default) else None


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


class _NoLoopClosure(Exception):
    """回环这一步不用了(说人话的原因):退回原来的打包。"""


def loop_close(work: Path, cli: str | None, run: Runner = subprocess.run
               ) -> tuple[Path, dict[str, Any]]:
    """回环(可选):``work/map.simplemap`` → 回环工具 → ``work/lc/map.simplemap``;导出前后的关键帧
    位姿(``sm-cli export-keyframes``),轨迹按它挪(:func:`correct_traj`),从回环后的 simplemap 重出
    先验(``sm2mm``)。回 ``(打包用哪个目录, build.json 的 loop_closure)``:成了是 ``work/lc``,不成
    (没有工具、跑不成、没找到回环、挪得太多)是 ``work`` 原样。"""
    if not cli:
        return work, {"ran": False, "why": f"没有回环工具({LC_CLI})"}
    lc = work / "lc"
    shutil.rmtree(lc, ignore_errors=True)
    lc.mkdir(parents=True)
    info: dict[str, Any] = {"ran": True, "used": False, "cli": cli, "version": _lc_version(cli),
                            "pipeline": str(LC_PIPELINE)}
    timings: dict[str, float] = {}
    info["timings_s"] = timings
    try:
        _loop_close(work, lc, cli, run, info, timings)
    except (_NoLoopClosure, BuildError) as exc:
        info["why"] = str(exc)[:400]
        return work, info
    except Exception as exc:  # noqa: BLE001 —— 可选的一步:出什么错都退回原来的打包,记下来
        info["why"] = f"回环出错: {type(exc).__name__}: {exc}"[:400]
        return work, info
    info["used"] = True
    return lc, info


def _loop_close(work: Path, lc: Path, cli: str, run: Runner, info: dict[str, Any],
                timings: dict[str, float]) -> None:
    prefix = Path(cli).parent.parent
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(
        p for p in (str(prefix / "lib"), os.environ.get("LD_LIBRARY_PATH", "")) if p)}
    t0 = time.monotonic()
    # 它往当前目录写调试文件(sm_lc__initial.tum、sm_lc__final.tum;参数文件管不了):在 lc/ 里跑
    _run(run, [cli, "-i", str(work / "map.simplemap"), "-o", str(lc / "map.simplemap"),
               "-p", str(LC_PIPELINE)], env, lc / "lc.log", "回环", cwd=lc)
    timings["lc_s"] = round(time.monotonic() - t0, 1)
    counts = _LC_COUNT.findall((lc / "lc.log").read_text(errors="replace"))
    if not counts:
        raise _NoLoopClosure("回环日志里没有回环数(工具的版本不对?)")
    info["closures"] = int(counts[-1])
    if not info["closures"]:
        raise _NoLoopClosure("没找到回环")
    if not (lc / "map.simplemap").is_file():
        raise _NoLoopClosure("回环没出 simplemap")
    t0 = time.monotonic()
    for name, sm in (("kf_before.tum", work / "map.simplemap"),
                     ("kf_after.tum", lc / "map.simplemap")):
        _run(run, ["sm-cli", "export-keyframes", str(sm), "--output", str(lc / name)], None,
             lc / "sm-cli.log", "导出关键帧")
    before, after = read_tum(lc / "kf_before.tum"), read_tum(lc / "kf_after.tum")
    fixed = correct_traj(read_tum(work / "traj.tum"), before, after)
    timings["keyframes_s"] = round(time.monotonic() - t0, 1)
    info.update(_shifts(before, after))
    limit = max(LC_MAX_SHIFT_M, LC_MAX_SHIFT_SHARE * info["path_m"])
    if info["max_shift_m"] > limit or info["max_turn_deg"] > LC_MAX_TURN_DEG:
        raise _NoLoopClosure(
            f"回环把关键帧挪得太多(最多 {info['max_shift_m']:.2f} m / {info['max_turn_deg']:.1f}°,"
            f"上限 {limit:.2f} m / {LC_MAX_TURN_DEG:g}°):多半认错了地方,不用")
    (lc / "traj.tum").write_text("".join(
        f"{t:.6f} " + " ".join(f"{v:.6f}" for v in (*p, *q)) + "\n" for t, p, q in fixed))
    t0 = time.monotonic()
    _run(run, ["sm2mm", *_plugin(), "-i", str(lc / "map.simplemap"), "-o",
               str(lc / "raw_prior.mm"), "-p", str(PRIOR_PIPELINE), "--no-progress-bar"], None,
         lc / "prior.log", "从回环后的 simplemap 出先验")
    timings["prior_s"] = round(time.monotonic() - t0, 1)
    if not (lc / "raw_prior.mm").is_file():
        raise _NoLoopClosure("从回环后的 simplemap 出先验没出文件")


def _lc_version(cli: str) -> str:
    """回环工具的版本:我们编的记在 ``share/mola_sm_loop_closure/d1max-build.txt``(源码的提交号;
    ``package.xml`` 里的版本号跟提交对不上),没有就看 ``package.xml``。"""
    share = Path(cli).parent.parent / "share" / "mola_sm_loop_closure"
    try:
        return (share / "d1max-build.txt").read_text().strip().splitlines()[0][:120]
    except (OSError, IndexError):
        pass
    try:
        m = re.search(r"<version>([^<]+)</version>", (share / "package.xml").read_text())
    except OSError:
        m = None
    return m.group(1) if m else "?"


Pose = tuple[float, tuple[float, float, float], tuple[float, float, float, float]]


def correct_traj(traj: Sequence[Pose], before: Sequence[Pose], after: Sequence[Pose]
                 ) -> list[Pose]:
    """轨迹按关键帧的改动挪:关键帧 k 的改动 ``C_k = T_after_k · T_before_k⁻¹``(左乘);一帧落在关键帧
    k、k+1 之间,两边各自挪一份(``C_k·T``、``C_{k+1}·T``),位置按时刻线性插、朝向按时刻插(四元数
    nlerp,相邻两个改动差得很小)。第一个关键帧之前跟第一个、最后一个之后跟最后一个。关键帧前后对不上
    (个数、时刻)报 :class:`BuildError`。"""
    import numpy as np

    from d1max_localizer.frames import mat_to_quat, quat_to_mat

    if not before or len(before) != len(after) or any(
            abs(a[0] - b[0]) > 1e-3 for a, b in zip(before, after, strict=True)):
        raise BuildError(f"回环前后的关键帧对不上({len(before)} 个 / {len(after)} 个)")
    stamps = np.array([t for t, _, _ in before])
    corr = []
    for (_, pb, qb), (_, pa, qa) in zip(before, after, strict=True):
        R = np.array(quat_to_mat(qa)) @ np.array(quat_to_mat(qb)).T
        corr.append((R, np.array(pa, float) - R @ np.array(pb, float)))

    def moved(k: int, p: Any, Rp: Any) -> tuple[Any, Any]:
        R, t = corr[k]
        q = np.array(mat_to_quat(tuple(tuple(r) for r in R @ Rp)))
        return R @ p + t, q / np.linalg.norm(q)

    out: list[Pose] = []
    for t, p, q in traj:
        P, Rp = np.array(p, float), np.array(quat_to_mat(q))
        k = int(np.searchsorted(stamps, t, side="right")) - 1
        if k < 0 or k >= len(stamps) - 1:
            pos, quat = moved(min(max(k, 0), len(stamps) - 1), P, Rp)
        else:
            w = float((t - stamps[k]) / (stamps[k + 1] - stamps[k]))
            (p0, q0), (p1, q1) = moved(k, P, Rp), moved(k + 1, P, Rp)
            q1 = q1 if float(q0 @ q1) >= 0 else -q1
            pos = (1 - w) * p0 + w * p1
            quat = (1 - w) * q0 + w * q1
            quat = quat / np.linalg.norm(quat)
        out.append((t, (float(pos[0]), float(pos[1]), float(pos[2])),
                    (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))))
    return out


def _shifts(before: Sequence[Pose], after: Sequence[Pose]) -> dict[str, Any]:
    """回环改了多少:关键帧挪的最大、最后一个、中位数(米),转的最大(度),路程(米,回环前)。"""
    import numpy as np

    from d1max_localizer.frames import quat_to_mat

    pb = np.array([p for _, p, _ in before], float)
    pa = np.array([p for _, p, _ in after], float)
    d = np.linalg.norm(pa - pb, axis=1)
    turn = []
    for (_, _, qb), (_, _, qa) in zip(before, after, strict=True):
        c = (np.trace(np.array(quat_to_mat(qb)).T @ np.array(quat_to_mat(qa))) - 1) / 2
        turn.append(math.degrees(math.acos(max(-1.0, min(1.0, float(c))))))
    return {"keyframes": len(before),
            "path_m": round(float(np.linalg.norm(np.diff(pb, axis=0), axis=1).sum()), 2),
            "max_shift_m": round(float(d.max()), 3), "end_shift_m": round(float(d[-1]), 3),
            "median_shift_m": round(float(np.median(d)), 3),
            "max_turn_deg": round(max(turn), 2)}


def bag_clock_offset(bag: Path, topic: str, n: int = 200) -> float | None:
    """录包里雷达消息「本机收到的时刻 − 消息头的时刻」的中位数(秒):轨迹(消息头的钟)挪到代理的钟
    (``rtk.jsonl`` 用的)要加的量。读不了(没有 ROS、没有这个话题)是 None。"""
    try:
        import rosbag2_py

        from d1max_localizer.livemap import cdr_stamp
    except ImportError:
        return None
    try:
        storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
        r = rosbag2_py.SequentialReader()
        r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
               rosbag2_py.ConverterOptions("", ""))
        r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
        diffs = []
        while r.has_next() and len(diffs) < n:
            _, raw, t_ns = r.read_next()
            diffs.append(t_ns * 1e-9 - cdr_stamp(raw))
    except Exception:  # noqa: BLE001 —— 算不出来就按 0,build.json 里写着
        return None
    if not diffs:
        return None
    return sorted(diffs)[len(diffs) // 2]


def _georeference(out: Path, bag: Path | None, traj: Sequence[Any], frames: Frames,
                  antenna: tuple[float, float], topic: str,
                  clock_offset: Callable[[Path, str], float | None]) -> dict[str, Any]:
    """录包里有 ``rtk.jsonl``(代理录包时记的 RTK 解)就配经纬度,配得上写 ``geo.json``
    (W09e 决定 6)。"""
    from d1max_localizer.geo import georeference, read_rtk
    (out / GEO_FILE).unlink(missing_ok=True)
    rows = read_rtk(bag / "rtk.jsonl") if bag is not None else []
    if not rows:
        return {"written": False, "why": "录包里没有 RTK 解"}
    assert bag is not None
    off = clock_offset(bag, topic)
    try:                                              # geo.json 是可选的:配准出错不许让整次建图失败
        g, why = georeference(traj, frames, rows, antenna_in_base=antenna,
                              clock_offset_s=off or 0.0)
    except Exception as exc:  # noqa: BLE001
        return {"written": False, "why": f"配准出错: {type(exc).__name__}: {exc}"[:200],
                "clock_offset_s": off}
    if g is None:
        return {"written": False, "why": why, "clock_offset_s": off}
    (out / GEO_FILE).write_text(json.dumps(g.to_wire(), indent=2) + "\n")
    return {"written": True, "why": why, "rms_m": g.rms_m, "pairs": g.pairs,
            "clock_offset_s": off}


def _render(points: Any, sensor: Any, frames: Frames, body: Any, traj: Sequence[Any],
            bag: Path | None, topic: str, read_scans: Callable[..., Iterator[Any]]
            ) -> tuple[Any, str]:
    """画栅格:有录包用逐帧扫描的真射线,读不了(没有 ROS、话题不对、包坏了)退回模拟射线。回
    (栅格, 用的哪种)。"""
    from d1max_localizer import grid

    if bag is None:
        why = "没给录包"
    elif topic not in FRONT_FRAME_TOPICS:
        why = f"话题 {topic} 不在前雷达系里"
    else:
        used = [0]

        def counted() -> Iterator[Any]:
            it = iter(read_scans(bag, traj, topic))
            while True:
                try:
                    s = next(it)
                except StopIteration:
                    return
                except Exception as exc:
                    raise _ScanReadError(f"{type(exc).__name__}: {exc}") from exc
                used[0] += 1
                yield s
        try:
            g = grid.render(points, sensor, frames, scans=counted(), body_path=body)
        except _ScanReadError as exc:
            why = f"读不了逐帧扫描:{exc}"[:300]
        else:
            if used[0]:
                return g, f"scans:{used[0]}"
            why = "录包里没有配得上轨迹的扫描"
    return grid.render(points, sensor, frames, body_path=body), f"synthetic:{why}"


def _mola_info(work: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((work / "mola.json").read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def cloud_xyz(msg: Any) -> Any:
    """``sensor_msgs/PointCloud2`` → (N,3) 的 x、y、z(按字段偏移取,别的字段类型不管),丢掉 NaN。"""
    import numpy as np

    off = {f.name: f.offset for f in msg.fields}
    order = ">" if getattr(msg, "is_bigendian", False) else "<"
    dt = np.dtype({"names": ["x", "y", "z"], "formats": [order + "f4"] * 3,
                   "offsets": [off["x"], off["y"], off["z"]], "itemsize": msg.point_step})
    a = np.frombuffer(bytes(msg.data), dtype=dt)
    xyz = np.stack([a["x"], a["y"], a["z"]], 1).astype(float)
    return xyz[np.isfinite(xyz).all(1)]


def bag_scans(bag: Path, traj: Sequence[Any], topic: str = SCAN_TOPIC, *,
              every: int = SCAN_EVERY) -> Iterator[tuple[Any, Any]]:
    """从录包逐帧读扫描(每 ``every`` 帧取一帧),按时刻配上轨迹里的位姿,回 ``(雷达位置, 这一帧的点)``
    (MOLA 系)。要 ROS 的系统 Python(``rosbag2_py``)。"""
    import numpy as np
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    from d1max_localizer.frames import quat_to_mat

    stamps = np.array([t for t, _, _ in traj])
    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("", ""))
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    i = -1
    while r.has_next():
        _, raw, _ = r.read_next()
        i += 1
        if i % every:
            continue
        m = deserialize_message(raw, PointCloud2)
        st = m.header.stamp.sec + m.header.stamp.nanosec * 1e-9
        k = int(np.clip(np.searchsorted(stamps, st), 1, len(stamps) - 1))
        k = k - 1 if abs(stamps[k - 1] - st) <= abs(stamps[k] - st) else k
        if abs(stamps[k] - st) > SCAN_MATCH_S:
            continue
        _, p, q = traj[k]
        R = np.array(quat_to_mat(q))
        xyz = cloud_xyz(m)[::SCAN_POINT_STRIDE]
        yield np.array(p, float), xyz @ R.T + np.array(p, float)


def orient(points: Any, traj: Sequence[Any]) -> tuple[Frames, str]:
    """从轨迹标 ``frames``;「上」的正负号用点云判:以雷达为准的相对高度里,最密的那层水平面(地面)要在
    雷达下面,在上面就把「上」翻过来重标。回 ``(frames, 说明)``。"""
    import numpy as np

    from d1max_localizer import grid

    poses = [(p, q) for _, p, q in traj]
    sensor = np.array([p for p, _ in poses])
    note = ""
    for hint in (SENSOR_UP_HINT, tuple(-c for c in SENSOR_UP_HINT)):
        f, why = calibrate(poses, sensor_up=hint, forward_hint=SENSOR_FORWARD_HINT, explain=True)
        pts = np.asarray(points, float)
        q = grid.level(pts[:: max(1, len(pts) // 400000)], f)
        rel = q[:, 2] - float(np.median(grid.level(sensor, f)[:, 2]))
        hist, edges = np.histogram(rel, bins=60, range=(-1.5, 1.5))
        k = int(np.argmax(hist))
        peak = float((edges[k] + edges[k + 1]) / 2)
        if peak < 0:
            return f, f"{why};地面在雷达下方 {-peak:.2f} m{note}"
        note = f"(按装法的「上」{_fmt(hint)} 标出来地面在雷达上方 {peak:.2f} m:雷达是倒装的,翻过来)"
    raise BuildError("点云里找不到雷达下方的地面:标不出「上」")


def coverage(traj: Sequence[Any], frames: Frames, step: float = COVERAGE_STEP_M
             ) -> list[list[float]]:
    """建图时走过的路:地图平面上(狗身中心)每 ``step`` 米一点。"""
    out: list[list[float]] = []
    for _, p, q in traj:
        x, y, _ = frames.to_map2d(p, q)
        if not out or math.hypot(x - out[-1][0], y - out[-1][1]) >= step:
            out.append([round(x, 2), round(y, 2)])
    return out


def _points(work: Path, run: Runner) -> Any:
    """simplemap → 点云(``sm2mm`` 出原始点图,``mm2ply -b`` 导出 x、y、z)。"""
    import numpy as np

    _run(run, ["sm2mm", "-i", str(work / "map.simplemap"), "-o", str(work / "points.mm"),
               "--no-progress-bar"], None, work / "sm2mm.log", "simplemap 转点云")
    _run(run, ["mm2ply", *_plugin(), "-i", str(work / "points.mm"), "-o",
               str(work / "points"), "-b", "--export-fields", "x,y,z"], None,
         work / "mm2ply.log", "导出点云")
    plys = sorted(work.glob("points*.ply"))
    if not plys:
        raise BuildError("导出点云没出文件")
    with open(plys[0], "rb") as fh:
        start = fh.read(4096)
    try:
        head = start.index(b"end_header\n") + len(b"end_header\n")
    except ValueError:
        raise BuildError("点云文件头不对") from None
    # 不先整个读成 bytes 再转(内审小 9:峰值是点云文件的好几倍)
    return np.fromfile(plys[0], dtype="<f4", offset=head).reshape(-1, 3).astype(float)


def _pack_prior(raw: Path, out: Path, pack: PriorPack, run: Runner, work: Path) -> None:
    if pack.mode == "none":
        out.unlink(missing_ok=True)
        try:
            os.link(raw, out)                      # 几百 MB:不再拷一份(内审小 10)
        except OSError:
            shutil.copy2(raw, out)
        return
    _run(run, ["mm-kf-regroup", *_plugin(), "-i", str(raw), "-o", str(out), "--decimate-voxel",
               f"{pack.voxel:g}", "--extent-factor", f"{pack.extent:g}"], None,
         work / "regroup.log", "先验打包")
    if not out.is_file():
        raise BuildError("先验打包没出文件")


def _run(run: Runner, cmd: list[str], env: dict[str, str] | None, log: Path, what: str,
         cwd: Path | None = None) -> None:
    kw = {"cwd": str(cwd)} if cwd is not None else {}
    with open(log, "w") as fh:
        r = run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, **kw)
    if getattr(r, "returncode", 0) != 0:
        tail = log.read_text(errors="replace")[-300:] if log.exists() else ""
        raise BuildError(f"{what}失败(退出码 {r.returncode}):{tail}")


def _fmt(v: Sequence[float]) -> str:
    return "(" + ", ".join(f"{c:g}" for c in v) + ")"
