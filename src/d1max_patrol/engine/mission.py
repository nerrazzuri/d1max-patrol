"""巡检任务的定义格式与 YAML 往返。

格式见设计 spec §6.1,以及子规范 `2026-09-02-巡检App设计.md` §7.1 加的
``check`` 字段 —— 那是给 VLM 的判读依据,写成自由文本而不是枚举,因为每个
点位要看的东西天差地别,枚举一定不够用。

**路线是快照,不是引用。** 从厂商路径拉下来之后就固化在这个文件里。
别人在 App 里改了路径,历史报告不该跟着变。

**为什么读写的就是 YAML 文件本身,不引数据库:** 手写的任务和页面上编的
任务必须是同一份东西,随时能互相切换,也能进 git 看 diff。多一层存储就多
一个"页面上看到的和文件里写的不一样"的失败模式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from d1max_patrol.protocol.nav_types import Pose

#: 第一版支持的动作类型。见设计 spec §6.1。
ACTION_TYPES = frozenset({"dwell", "photo", "light", "head"})

#: 机器上的两路相机。见清单 #29。
CAMERAS = frozenset({"front", "back"})

#: 单点失败之后怎么办。
ON_WAYPOINT_FAILED = frozenset({"abort", "skip", "retry_then_skip"})

#: 定位丢了之后怎么办。默认先暂停试重置,试满次数再中止(主规范 §6.4)。
ON_LOC_LOST = frozenset({"pause_then_abort", "abort"})

#: 控制权被拿走之后怎么办。默认暂停 —— 上装抢走控制权是可以人工夺回的
#: (清单 #46/#47),不必直接判整趟失败。
ON_CONTROL_LOST = frozenset({"pause", "abort"})

#: 点位名里绝对不能出现的东西 —— 它会成为照片文件名的一部分。
_NAME_FORBIDDEN = ("/", "\\", "..", "\x00")


class MissionError(ValueError):
    """任务定义不合法。

    消息里**必须带上出错的那个值本身** —— "动作类型不认识"帮不了任何人,
    "动作类型不认识: teleport"才能让人直接去文件里找到那一行。
    """


@dataclass(frozen=True, slots=True)
class Action:
    """点位上的一个动作。

    四种动作共用一个结构而不是做成四个类:它要往返 YAML,而 YAML 里就是
    一个带 ``type`` 的扁平映射。分成四个类只会让加载和导出两头都多一层分发。
    """

    type: str
    seconds: float = 0.0
    camera: str = ""
    on: bool = True
    pitch: float = 0.0
    yaw: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        """只导出这种动作用得上的字段 —— 全导出会让 YAML 变得没法读。"""
        out: dict[str, Any] = {"type": self.type}
        if self.type == "dwell":
            out["seconds"] = self.seconds
        elif self.type == "photo":
            out["camera"] = self.camera
        elif self.type == "light":
            out["on"] = self.on
        elif self.type == "head":
            out["pitch"] = self.pitch
            out["yaw"] = self.yaw
        return out


@dataclass(frozen=True, slots=True)
class MissionWaypoint:
    """一个巡检点位。"""

    name: str
    pose: Pose
    check: str = ""
    actions: tuple[Action, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "pose": self.pose.to_wire()}
        if self.check:
            out["check"] = self.check
        if self.actions:
            out["actions"] = [a.to_wire() for a in self.actions]
        return out


@dataclass(frozen=True, slots=True)
class Policy:
    """跑这个任务时的策略。默认值取自设计 spec §6.1 的样例。"""

    waypoint_timeout_s: float = 120.0
    on_waypoint_failed: str = "retry_then_skip"
    waypoint_retry: int = 1
    battery_return_pct: float = 25.0
    #: 中止线。**25 不是 15。** 厂商的强制趴窝线是单块电池 10%(硬件手册
    #: 2.3.3「内部异常保护」),15 离它只剩 5 个点,而这 5 个点要覆盖:发现、
    #: 告警、人走过去、把狗弄回来。不够。
    battery_abort_pct: float = 25.0
    on_loc_lost: str = "pause_then_abort"
    on_control_lost: str = "pause"
    loops: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "waypoint_timeout_s": self.waypoint_timeout_s,
            "on_waypoint_failed": self.on_waypoint_failed,
            "waypoint_retry": self.waypoint_retry,
            "battery_return_pct": self.battery_return_pct,
            "battery_abort_pct": self.battery_abort_pct,
            "on_loc_lost": self.on_loc_lost,
            "on_control_lost": self.on_control_lost,
            "loops": self.loops,
        }


@dataclass(frozen=True, slots=True)
class Mission:
    """一份完整的巡检任务。"""

    mission: str
    map_id: str
    waypoints: tuple[MissionWaypoint, ...]
    policy: Policy = field(default_factory=Policy)
    route_source: str = "inline"
    route_path_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mission": self.mission, "map_id": self.map_id}
        if self.route_source != "inline" or self.route_path_id:
            out["route"] = {"source": self.route_source,
                            "path_id": self.route_path_id}
        out["waypoints"] = [w.to_wire() for w in self.waypoints]
        out["policy"] = self.policy.to_wire()
        return out


# --------------------------------------------------------------------- 校验


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise MissionError(msg)


def _num(raw: Any, key: str, where: str, default: float | None = None) -> float:
    if key not in raw:
        _require(default is not None, f"{where} 缺少必填字段 {key}")
        return float(default)  # type: ignore[arg-type]
    value = raw[key]
    # bool 是 int 的子类,不拦住的话 ``seconds: true`` 会变成 1.0。
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{where} 的 {key} 应为数字,实际为 {value!r}")
    return float(value)


def _parse_action(raw: Any, where: str) -> Action:
    _require(isinstance(raw, dict), f"{where} 的动作应为映射,实际为 {raw!r}")
    kind = raw.get("type")
    _require(isinstance(kind, str) and kind in ACTION_TYPES,
             f"{where} 的动作类型不认识: {kind!r}(支持 {sorted(ACTION_TYPES)})")
    if kind == "photo":
        camera = raw.get("camera")
        _require(isinstance(camera, str) and camera in CAMERAS,
                 f"{where} 的 photo 动作没说清楚哪个相机: {camera!r}"
                 f"(支持 {sorted(CAMERAS)})")
        return Action(type="photo", camera=camera)  # type: ignore[arg-type]
    if kind == "dwell":
        seconds = _num(raw, "seconds", f"{where} 的 dwell 动作")
        _require(seconds >= 0, f"{where} 的 dwell 时长不能是负数: {seconds}")
        return Action(type="dwell", seconds=seconds)
    if kind == "light":
        on = raw.get("on", True)
        _require(isinstance(on, bool), f"{where} 的 light.on 应为真假值,实际为 {on!r}")
        return Action(type="light", on=on)
    return Action(type="head",
                  pitch=_num(raw, "pitch", f"{where} 的 head 动作", 0.0),
                  yaw=_num(raw, "yaw", f"{where} 的 head 动作", 0.0))


def _parse_waypoint(raw: Any, index: int) -> MissionWaypoint:
    where = f"第 {index + 1} 个点位"
    _require(isinstance(raw, dict), f"{where} 应为映射,实际为 {raw!r}")
    name = raw.get("name")
    _require(isinstance(name, str) and name.strip(), f"{where} 缺少 name")
    assert isinstance(name, str)  # 上一行已经保证,这行只是给类型检查看的
    for bad in _NAME_FORBIDDEN:
        _require(bad not in name,
                 f"{where} 的点位名不能含 {bad!r}: {name!r} —— "
                 f"点位名会成为照片文件名的一部分")
    try:
        pose = Pose.from_wire(raw.get("pose"))
    except ValueError as exc:
        raise MissionError(f"{where}({name}) 的 pose 不合法: {exc}") from exc

    check = raw.get("check", "")
    _require(isinstance(check, str), f"{where}({name}) 的 check 应为字符串")

    actions_raw = raw.get("actions", []) or []
    _require(isinstance(actions_raw, list),
             f"{where}({name}) 的 actions 应为列表,实际为 {actions_raw!r}")
    actions = tuple(_parse_action(a, f"{where}({name})") for a in actions_raw)
    return MissionWaypoint(name=name, pose=pose, check=check, actions=actions)


def _parse_policy(raw: Any) -> Policy:
    if raw is None:
        return Policy()
    _require(isinstance(raw, dict), f"policy 应为映射,实际为 {raw!r}")
    base = Policy()
    got: dict[str, Any] = {}
    for key in ("waypoint_timeout_s", "battery_return_pct", "battery_abort_pct"):
        if key in raw:
            got[key] = _num(raw, key, "policy")
    if "waypoint_retry" in raw:
        value = raw["waypoint_retry"]
        _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                 f"policy.waypoint_retry 应为非负整数,实际为 {value!r}")
        got["waypoint_retry"] = value
    if "loops" in raw:
        value = raw["loops"]
        _require(isinstance(value, int) and not isinstance(value, bool) and value >= 1,
                 f"policy.loops 应为正整数,实际为 {value!r}")
        got["loops"] = value
    if "on_waypoint_failed" in raw:
        value = raw["on_waypoint_failed"]
        _require(value in ON_WAYPOINT_FAILED,
                 f"policy.on_waypoint_failed 不认识: {value!r}"
                 f"(支持 {sorted(ON_WAYPOINT_FAILED)})")
        got["on_waypoint_failed"] = value
    for key, allowed in (("on_loc_lost", ON_LOC_LOST),
                         ("on_control_lost", ON_CONTROL_LOST)):
        if key in raw:
            value = raw[key]
            # 词表在这里卡死,而不是等到出事那一刻在安全规则表里才发现不认识
            # —— 那时候狗已经在外面了。
            _require(value in allowed,
                     f"policy.{key} 不认识: {value!r}(支持 {sorted(allowed)})")
            got[key] = value

    policy = replace(base, **got)
    _require(policy.battery_abort_pct <= policy.battery_return_pct,
             f"中止电量({policy.battery_abort_pct})不能高于返航电量"
             f"({policy.battery_return_pct}) —— 那样永远轮不到返航")
    _require(policy.waypoint_timeout_s > 0,
             f"policy.waypoint_timeout_s 必须为正,实际为 {policy.waypoint_timeout_s}")
    return policy


def parse_mission(raw: Any) -> Mission:
    """从已经解出来的映射构任务。页面提交的 JSON 也走这里。"""
    _require(isinstance(raw, dict), f"任务定义应为映射,实际为 {raw!r}")

    name = raw.get("mission")
    _require(isinstance(name, str) and name.strip(), "缺少 mission(任务名)")
    map_id = raw.get("map_id")
    _require(isinstance(map_id, str) and map_id.strip(), "缺少 map_id(地图名)")

    route = raw.get("route") or {}
    _require(isinstance(route, dict), f"route 应为映射,实际为 {route!r}")
    route_source = route.get("source", "inline")
    _require(route_source in ("inline", "vendor_path"),
             f"route.source 不认识: {route_source!r}")
    route_path_id = route.get("path_id", "")
    _require(isinstance(route_path_id, str),
             f"route.path_id 应为字符串,实际为 {route_path_id!r}")

    waypoints_raw = raw.get("waypoints")
    _require(isinstance(waypoints_raw, list), "缺少 waypoints(点位列表)")
    assert isinstance(waypoints_raw, list)
    _require(bool(waypoints_raw), "至少要有一个点位")
    waypoints = tuple(_parse_waypoint(w, i) for i, w in enumerate(waypoints_raw))

    seen: set[str] = set()
    for wp in waypoints:
        _require(wp.name not in seen,
                 f"点位名重复: {wp.name} —— 照片按点位名归档,重名会互相覆盖")
        seen.add(wp.name)

    return Mission(
        mission=name,           # type: ignore[arg-type]
        map_id=map_id,          # type: ignore[arg-type]
        waypoints=waypoints,
        policy=_parse_policy(raw.get("policy")),
        route_source=route_source,
        route_path_id=route_path_id,
    )


# --------------------------------------------------------------------- 往返


def load_mission(path: Path) -> Mission:
    """从 YAML 文件读一份任务。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MissionError(f"读不了任务文件 {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MissionError(f"任务文件 {path} 不是合法的 YAML: {exc}") from exc
    return parse_mission(raw)


def dump_mission(mission: Mission) -> str:
    """导成 YAML 文本。

    ``allow_unicode=True`` 是硬要求:不然 ``check`` 里的中文变成一串
    ``\\uXXXX``,人就没法在编辑器里改了 —— 而"手写和页面编的是同一份东西"
    正是不引数据库的全部理由。
    """
    return yaml.safe_dump(mission.to_wire(), allow_unicode=True,
                          sort_keys=False, default_flow_style=False)


def save_mission(mission: Mission, path: Path) -> None:
    """写回 YAML 文件。先写临时文件再 ``os.replace``。

    理由跟 ``archive.write_state`` 一样:就地覆盖时断电会留下半个文件,
    而半个任务文件是跑不了的 —— 现场丢的是整条路线。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dump_mission(mission), encoding="utf-8")
    os.replace(tmp, path)
