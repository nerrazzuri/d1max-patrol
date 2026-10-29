"""代理 ↔ Nav2 转接节点的报文(W47,`docs/W47-导航换Nav2-设计.md`)。

转接节点(ROS 那一侧,跟 Nav2 一起起)跟代理在同一台机器上,经 Unix 套接字说话:
**一行一个 JSON**,每行最多 :data:`MAX_LINE` 字节;代理监听、转接节点来连,第一行是 ``hello``。
跟定位桥同一套写法。

**Nav2 看到的一切都由代理送过去**(时钟、里程、``map→odom``、障碍栅格):时间全在代理的一个钟上,
绕开狗的传感器钟(比系统钟慢约 230 天);Nav2 看到的跟代理的守卫看到的是同一份。
Nav2 一律 ``use_sim_time``,转接节点按代理送来的时刻发 ``/clock``。

代理 → 转接节点:

- ``hello {proto}``
- ``map {req, map_id, map_version, dir, nogo, speed}``:地图目录(``floor.pgm/.yaml``)。
  ``nogo`` 是禁行区多边形(地图系,米)的列表,烧成致命格;``speed`` 是 ``[{poly, mps}]`` 限速区。
  区域改了再发一遍。
- ``tick {t_ms, odom, vel, map_odom}``:每拍一条。
  ``t_ms`` 代理的钟(毫秒,严格递增);``odom`` = ``[x, y, yaw]`` 里程;
  ``vel`` = ``[vx, vy, wz]`` 实测速度(机身系);
  ``map_odom`` = ``[x, y, yaw]`` 地图系里里程原点在哪。定位可信时才有,不可信是 ``null``:
  Nav2 那边 TF 过期,自己也会停。``speed`` = 此刻许跑多快(米/秒,代理按「看得清就快」算的),
  转接节点转成 Nav2 的 ``/speed_limit``。
- ``grid {t_ms, pose, res, size, occ, known}``:感知栅格(机身系、行 = 前、列 = 左,中心在机身)。
  ``pose`` 是拍下这一帧时的里程 ``[x, y, yaw]``;位图同障碍桥(``obsbridge.pack_bits``)。
- ``goal {goal_id, x, y, yaw, via?}`` / ``cancel {goal_id}``。``via`` = 路网上的途经点
  ``[[x, y], …]``(协议 4,W48):有就走 ``NavigateThroughPoses``(途经点只是路过),没有走
  ``NavigateToPose``。
- ``hb {seq}``

转接节点 → 代理:

- ``hello {proto, name, version}``
- ``ready {ok, reason}``:Nav2 各节点起来了、地图收下了才 ``ok``。
- ``cmd {t_ms, vx, vy, wz, goal_id?, src?}``:Nav2 出的速度(机身系),``t_ms`` 是它那时候的 ``/clock``;
  ``goal_id`` = 出这个速度时 Nav2 正在走的目标(协议 2,外审 R7:代理不拿别的目标的速度开车);
  ``src`` = ``ctl``(控制器)/ ``beh``(脱困动作,BackUp 等;协议 3,复查第三轮 P4),缺省 ``ctl``。
- ``goal_state {goal_id, state, reason}``:
  ``accepted`` / ``succeeded`` / ``aborted`` / ``canceled`` / ``rejected``。
- ``path {goal_id, pts}``:Nav2 这一次规划出的路径(地图系,抽稀到最多 :data:`MAX_PATH_PTS` 个点),
  每次重规划发一次;站点地图上画。
- ``hb {seq}``
"""

from __future__ import annotations

import json
import math
from typing import Any

from d1max_contract.errors import ContractError

#: 2(W47 外审 R7):``cmd`` 带 ``goal_id``;3(复查第三轮 P4):``cmd`` 带 ``src``;
#: 4(W48):``goal`` 可带 ``via``。两头同一个发布包里,对不上握手就拒(代理退回自己的规划)。
PROTO = 4
#: ``cmd.src``:控制器 / 脱困动作。
CMD_SRCS = ("ctl", "beh")
#: 途经点最多几个(路网最多 500 个点)。
MAX_VIA = 500
MAX_LINE = 256 * 1024
GOAL_STATES = ("accepted", "succeeded", "aborted", "canceled", "rejected")
#: 每种报文必须有的字段(多出来的拒:加字段要升协议)。
FIELDS: dict[str, tuple[set[str], set[str]]] = {
    # 类型: (必须的, 可选的)
    "hello": ({"proto"}, {"name", "version"}),
    "map": ({"req", "map_id", "map_version", "dir"}, {"nogo", "speed"}),
    "tick": ({"t_ms", "odom", "vel"}, {"map_odom", "speed"}),
    "grid": ({"t_ms", "pose", "res", "size", "occ", "known"}, set()),
    "goal": ({"goal_id", "x", "y", "yaw"}, {"via"}),
    "cancel": ({"goal_id"}, set()),
    "hb": ({"seq"}, set()),
    "ready": ({"ok"}, {"reason"}),
    "cmd": ({"t_ms", "vx", "vy", "wz"}, {"goal_id", "src"}),
    "goal_state": ({"goal_id", "state"}, {"reason"}),
    "path": ({"goal_id", "pts"}, set()),
    "error": ({"reason"}, set()),
}
#: 只许代理发的、只许转接节点发的。
FROM_AGENT = frozenset({"hello", "map", "tick", "grid", "goal", "cancel", "hb", "error"})
FROM_BRIDGE = frozenset({"hello", "ready", "cmd", "goal_state", "path", "hb", "error"})
#: Nav2 规划出的路径最多带这么多点(转接节点抽稀;站点地图上画)。
MAX_PATH_PTS = 256


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ContractError(f"Nav2 转接:{what} 要是有限数:{str(v)[:40]!r}")
    return float(v)


def _int(v: Any, what: str, *, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ContractError(f"Nav2 转接:{what} 要是不小于 {lo} 的整数:{v!r}")
    return v


def _triple(v: Any, what: str) -> None:
    if not isinstance(v, list) or len(v) != 3:
        raise ContractError(f"Nav2 转接:{what} 要是三个数 [x, y, yaw]")
    for i, x in enumerate(v):
        _num(x, f"{what}[{i}]")


def _poly(p: Any, what: str) -> None:
    if not isinstance(p, list) or not 3 <= len(p) <= 256:
        raise ContractError(f"Nav2 转接:{what} 要是 3–256 个点")
    for q in p:
        if not isinstance(q, list) or len(q) != 2:
            raise ContractError(f"Nav2 转接:{what} 的点要是 [x, y]")
        _num(q[0], what)
        _num(q[1], what)


def check(msg: dict[str, Any]) -> dict[str, Any]:
    """查一条报文的形状;不对抛 :class:`ContractError`。"""
    t = msg.get("type")
    if t not in FIELDS:
        raise ContractError(f"Nav2 转接:不认识的报文 {str(t)[:20]!r}")
    need, opt = FIELDS[t]
    keys = set(msg) - {"type"}
    if need - keys or keys - need - opt:
        raise ContractError(f"Nav2 转接:{t} 的字段不对:缺 {sorted(need - keys)}、"
                            f"多 {sorted(keys - need - opt)}")
    if t == "hello":
        _int(msg["proto"], "proto", lo=1)
    elif t == "map":
        _int(msg["req"], "req", lo=1)
        for k in ("map_id", "map_version", "dir"):
            if not isinstance(msg[k], str) or len(msg[k]) > 512:
                raise ContractError(f"Nav2 转接:map.{k} 要是字符串")
        for i, p in enumerate(msg.get("nogo", [])):
            _poly(p, f"nogo[{i}]")
        for i, z in enumerate(msg.get("speed", [])):
            if not isinstance(z, dict) or set(z) != {"poly", "mps"}:
                raise ContractError("Nav2 转接:speed 每项要是 {poly, mps}")
            _poly(z["poly"], f"speed[{i}]")
            if _num(z["mps"], f"speed[{i}].mps") <= 0:
                raise ContractError("Nav2 转接:限速要是正数")
    elif t == "tick":
        _int(msg["t_ms"], "t_ms")
        _triple(msg["odom"], "odom")
        _triple(msg["vel"], "vel")
        if msg.get("map_odom") is not None:
            _triple(msg["map_odom"], "map_odom")
        if "speed" in msg and not 0 < _num(msg["speed"], "speed") <= 5:
            raise ContractError("Nav2 转接:speed 要在 (0, 5] 米/秒")
    elif t == "grid":
        _int(msg["t_ms"], "t_ms")
        _triple(msg["pose"], "pose")
        if not 0 < _num(msg["res"], "res") <= 1:
            raise ContractError("Nav2 转接:res 要在 (0, 1] 米")
        _int(msg["size"], "size", lo=1)
        if msg["size"] > 400:
            raise ContractError("Nav2 转接:size 最多 400")
        for k in ("occ", "known"):
            if not isinstance(msg[k], str):
                raise ContractError(f"Nav2 转接:{k} 要是 base64 字符串")
    elif t in ("goal", "cancel", "goal_state"):
        _int(msg["goal_id"], "goal_id", lo=1)
        if t == "goal":
            for k in ("x", "y", "yaw"):
                _num(msg[k], k)
            via = msg.get("via", [])
            if not isinstance(via, list) or len(via) > MAX_VIA:
                raise ContractError(f"Nav2 转接:goal.via 要是最多 {MAX_VIA} 个点的列表")
            for q in via:
                if not isinstance(q, list) or len(q) != 2:
                    raise ContractError("Nav2 转接:途经点要是 [x, y]")
                _num(q[0], "via.x")
                _num(q[1], "via.y")
        if t == "goal_state" and msg["state"] not in GOAL_STATES:
            raise ContractError(f"Nav2 转接:state 要是 {'/'.join(GOAL_STATES)}")
    elif t == "path":
        _int(msg["goal_id"], "goal_id", lo=1)
        pts = msg["pts"]
        if not isinstance(pts, list) or len(pts) > MAX_PATH_PTS:
            raise ContractError(f"Nav2 转接:path.pts 要是最多 {MAX_PATH_PTS} 个点")
        for q in pts:
            if not isinstance(q, list) or len(q) != 2:
                raise ContractError("Nav2 转接:path 的点要是 [x, y]")
            _num(q[0], "path.x")
            _num(q[1], "path.y")
    elif t == "hb":
        _int(msg["seq"], "seq")
    elif t == "ready":
        if not isinstance(msg["ok"], bool):
            raise ContractError("Nav2 转接:ready.ok 要是 true/false")
    elif t == "cmd":
        _int(msg["t_ms"], "t_ms")
        if "goal_id" in msg and msg["goal_id"] is not None:
            _int(msg["goal_id"], "goal_id")
        if "src" in msg and msg["src"] not in CMD_SRCS:
            raise ContractError(f"Nav2 转接:cmd.src 要是 {'/'.join(CMD_SRCS)}")
        for k in ("vx", "vy", "wz"):
            _num(msg[k], k)
    for k in ("reason", "name", "version"):
        if k in msg and (not isinstance(msg[k], str) or len(msg[k]) > 300):
            raise ContractError(f"Nav2 转接:{k} 要是字符串、最多 300 字")
    return msg


def encode(msg: dict[str, Any]) -> bytes:
    check(msg)
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()
    if len(line) + 1 > MAX_LINE:
        raise ContractError(f"Nav2 转接:一行太长({len(line)} 字节)")
    return line + b"\n"


def parse(line: bytes, *, allowed: frozenset[str] | None = None) -> dict[str, Any]:
    if len(line) > MAX_LINE:
        raise ContractError("Nav2 转接:一行太长")
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContractError(f"Nav2 转接:不是 JSON:{exc}") from None
    if not isinstance(msg, dict):
        raise ContractError("Nav2 转接:一行要是 JSON 对象")
    check(msg)
    if allowed is not None and msg["type"] not in allowed:
        raise ContractError(f"Nav2 转接:这一头不该收到 {msg['type']}")
    return msg
