"""原点,以及"还有多远"到"还要多少电"的换算。

**这个模块不碰后端、不碰时间。** 输入是坐标和系数,输出是数和结构 ——
所以里面每一条都能被穷举测试,而这套系数全是待真机标定的,能穷举才敢改。

**为什么不叫 `origin`。** ``app/gridmap.py`` 和 ``backends/local_nav.py`` 里
已经有一个 ``origin`` 了 —— 那是 ROS 占据栅格的左下角原点
(``origin_x`` / ``origin_y`` / ``origin_yaw``),是地图文件格式的一部分,
跟"狗回哪儿"毫无关系。两个东西同名,读代码的人早晚把它们当成一个。
这里一律叫 **home**。

**为什么存成地图旁边的 sidecar,不写进 ``<map_id>.yaml``。** 那个 yaml 是
slam_toolbox 生成的,重建图会原样覆盖 —— 写进去等于写在沙滩上。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_patrol.protocol.nav_types import Pose


class HomeError(Exception):
    """原点读不出来。"没标过"和"读坏了"都走这里,但话不一样。"""


@dataclass(frozen=True, slots=True)
class HomePoint:
    """原点。同时是三样东西:换电位、待命位、返航目标(spec §1.3)。"""

    map_id: str
    pose: Pose
    marked_at_ms: int
    note: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "pose": self.pose.to_wire(),
            "marked_at_ms": self.marked_at_ms,
            "note": self.note,
        }

    @classmethod
    def from_wire(cls, raw: Any) -> HomePoint:
        if not isinstance(raw, dict):
            raise HomeError(f"原点应为映射,实际为 {raw!r}")
        try:
            map_id = raw["map_id"]
            pose = Pose.from_wire(raw["pose"])
            marked = raw["marked_at_ms"]
        except (KeyError, ValueError, TypeError) as exc:
            raise HomeError(f"原点读不出来: {exc}") from exc
        if not isinstance(map_id, str) or not map_id:
            raise HomeError(f"原点里的 map_id 不是非空字符串: {map_id!r}")
        if not isinstance(marked, int) or isinstance(marked, bool):
            raise HomeError(f"原点里的 marked_at_ms 不是整数: {marked!r}")
        note = raw.get("note", "")
        return cls(map_id=map_id, pose=pose, marked_at_ms=marked,
                   note=note if isinstance(note, str) else "")


def home_path(maps_dir: Path | str, map_id: str) -> Path:
    """原点文件的位置 —— 就在地图旁边,跟着地图一起被拷走。"""
    return Path(maps_dir) / f"{map_id}.home.json"


def save_home(maps_dir: Path | str, home: HomePoint) -> None:
    """写原点。**先写临时文件,fsync,再改名。**

    写到一半断电,下次开机就是"原点不见了",而狗会因此拒绝起飞 —— 那还算好的;
    更坏的是写出半个 JSON,被当成"文件坏了"。原子写让这两种都不会发生。

    **``replace`` 一个人兑现不了上面这句承诺。** ``write_text`` 返回时数据
    只到了页缓存;``rename`` 在 ext4 上是有序的,但"有序"保证的是改名不早于
    写入落盘,**不保证两者都落了盘**。这台机器的日常工况就是热插拔换电池 ——
    断电不是意外分支,是操作流程本身。所以临时文件的字节必须先 ``fsync``
    到盘上,再让它顶替旧文件。

    只 fsync 文件、不 fsync 父目录:目录项没落盘的最坏结果是"改名丢了,
    还剩上一份完整的原点",这跟"原点是旧的"没有区别,而原点极少变。
    半个 JSON 才是要防的那一种。
    """
    target = home_path(maps_dir, home.map_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    try:
        # 先序列化再落盘: 序列化抛错的时候临时文件还没建, 已有的那份原封不动。
        text = json.dumps(home.to_wire(), ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def load_home(maps_dir: Path | str, map_id: str) -> HomePoint:
    """读原点。**读不到就抛错,绝不回一个零点。**

    (0, 0) 在地图里是一个真实存在的点。拿它当"没标过"的返回值,等于把狗派往
    一个谁也没标过的地方,而调用方看不出区别。
    """
    path = home_path(maps_dir, map_id)
    if not path.is_file():
        raise HomeError(f"地图 {map_id!r} 没标过原点(应有 {path.name})")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HomeError(f"{path.name} 读不出来: {exc}") from exc
    home = HomePoint.from_wire(raw)
    if home.map_id != map_id:
        # 文件名和内容对不上,说明有人拷贝改名过。信哪个都是猜。
        raise HomeError(f"{path.name} 里记的是 {home.map_id!r},跟要读的 {map_id!r} 对不上")
    return home


def forget_home(maps_dir: Path | str, map_id: str) -> None:
    """作废原点。**重建图之后必须调这个。**

    原点是标在坐标系上的。坐标系重建了,它就是错的 —— 而一个错的原点比没有
    原点危险得多:狗不会拒绝起飞,它会一声不吭地走过去。
    """
    home_path(maps_dir, map_id).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ReturnParams:
    """把"还有多远"换算成"还要多少电"的四个系数。

    **四个都是待真机标定的。** 现在的默认值一律往"更费电"的方向取 ——
    估低了的代价是狗在半路趴下,估高了的代价只是早回来一趟。
    """

    #: 巡航速度。``local_nav`` 的脉冲峰值是 max_fwd(0.50) × fwd_speed_mps(1.2)
    #: = 0.6 m/s,中间还有 settle 和转向,取三分之二。
    cruise_speed_mps: float = 0.4
    #: 耗电率。spec §1.3 空载续航 5±0.5h,取下限 4.5h → 100/4.5。
    drain_pct_per_hour: float = 22.3
    #: 直线距离不是走的距离 —— 绕柱子、绕货架、掉头。
    detour_factor: float = 1.4
    #: 就算原点就在脚下,起身、站定、对位也要电。**这个下限是返航线能成立的
    #: 前提**: 成本能取 0 的话,返航线就等于中止线,而中止先判,返航永远轮不到。
    floor_pct: float = 3.0


DEFAULT_RETURN_PARAMS = ReturnParams()


def estimate_cost_pct(distance_m: float,
                      params: ReturnParams = DEFAULT_RETURN_PARAMS) -> float:
    """走 ``distance_m`` 米大约要掉多少个点的电。

    **系数不合法就当场抛,不算。** 一个 ``cruise_speed_mps=0`` 算出来的是无穷,
    而无穷会让"电永远不够"这件事看起来像一个正常判定。
    """
    if distance_m < 0.0:
        raise ValueError(f"距离不能为负: {distance_m}")
    if params.cruise_speed_mps <= 0.0:
        raise ValueError(f"巡航速度必须为正: {params.cruise_speed_mps}")
    if params.drain_pct_per_hour <= 0.0:
        raise ValueError(f"耗电率必须为正: {params.drain_pct_per_hour}")
    if params.detour_factor < 1.0:
        raise ValueError(f"绕路系数不能小于 1: {params.detour_factor}")
    if params.floor_pct < 0.0:
        raise ValueError(f"电量下限不能为负: {params.floor_pct}")
    hours = distance_m * params.detour_factor / params.cruise_speed_mps / 3600.0
    return max(params.floor_pct, hours * params.drain_pct_per_hour)


def route_length_m(home: Pose, waypoints: Sequence[Pose]) -> float:
    """全程:原点 → 各点 → 回原点。**回来那一段必须算进去。**

    只算到最后一个点为止,等于假设狗可以停在场地尽头 —— 而它不能,它得回来换电池。
    """
    if not waypoints:
        return 0.0
    legs = [home, *waypoints, home]
    # B905: zip 必须显式写 strict。这里两个序列本来就差一个,只能 False。
    return sum(a.distance_to(b) for a, b in zip(legs, legs[1:], strict=False))
