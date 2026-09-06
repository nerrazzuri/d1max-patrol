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
    """写原点。**先写临时文件再改名。**

    写到一半断电,下次开机就是"原点不见了",而狗会因此拒绝起飞 —— 那还算好的;
    更坏的是写出半个 JSON,被当成"文件坏了"。原子写让这两种都不会发生。
    """
    target = home_path(maps_dir, home.map_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    try:
        # 先序列化再落盘: 序列化抛错的时候临时文件还没建, 已有的那份原封不动。
        text = json.dumps(home.to_wire(), ensure_ascii=False, indent=2)
        tmp.write_text(text, encoding="utf-8")
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
