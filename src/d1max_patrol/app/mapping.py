"""建图编排:录一段包 → 在隔离域里离线重建 → 存图。

这个模块把 ``docs/建图定位与巡检管线.md`` §2 那串命令编码成 ``ProcSpec``。
**命令行和环境变量是逐字照抄那份文档的** —— 现场是照着文档一条条敲通的,
这里改了而文档没改,下次现场按文档敲就会跟 app 跑出不一样的结果。改一处
要两处一起改。

三件事值得单独说:

**为什么重建是离线的。** ``/front_lidar`` 是 3D 点云,压平成 2D、喂给
slam_toolbox、跑回环,这些都比实时慢。现场的做法是先录一段 mcap 包(机器
上不占内存,流到笔记本),回来再放包重建 —— 慢多少都无所谓,反正没人等着。

**为什么重建要换域、换 RMW。** 实时域走的是 zenoh,而 zenoh 传不了 latched
话题(现场坑清单 #63),``/map`` 根本发不出去。重建时整个绕开它:
``ROS_DOMAIN_ID=93`` + ``rmw_fastrtps_cpp`` + ``ROS_LOCALHOST_ONLY=1``,
三条缺一不可 —— 换了域但没换 RMW,还是 zenoh;换了 RMW 但没换域,会和实时
链路上的节点互相看见,放包放出来的旧 ``/tf`` 会污染实时定位。

**为什么放包的进程最后起。** ``ros2 bag play`` 一起来就开始吐数据,slam
还没订阅上的话,前几秒就喂给空气了 —— 而前几秒恰恰是建图的起点。文档里的
``--delay 4`` 是同一个道理的第二道保险。
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from d1max_patrol.app.procs import ProcError, ProcManager, ProcSpec
from d1max_patrol.engine.homing import forget_home

#: 录包录哪几个话题。逐字照抄文档 §2 (a)。
RECORD_TOPICS: tuple[str, ...] = (
    "/front_lidar", "/tf", "/tf_static", "/odom/mc_odom", "/odom/current_pose",
)

#: 名字里只许有这些 —— 它要当目录名用,还要拼进命令行。
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

#: 录包进程停的时候多给一会儿。mcap 要把索引写完才算一个完整的包,
#: 强杀出来的包放不回去,而那一段路是走不回来的。
_RECORD_GRACE_S = 15.0

#: 放包最多跑多久。一段走动录几分钟,重建比实时慢,给足一小时。
REBUILD_TIMEOUT_S = 3600.0

#: 存图两步各自最多多久。
_SAVE_TIMEOUT_S = 120.0

#: 发一条初始位姿最多等多久。``ros2 topic pub --once`` 发完自己就退了,
#: 给的时间够它把 ROS 上下文起起来即可。
_INITPOSE_TIMEOUT_S = 30.0


class MappingError(RuntimeError):
    """建图这条流程上的问题:状态不对、进程起不来、图没存下来。"""


class MappingArgError(MappingError, ValueError):
    """调用方给错了:名字不合法、包不在、名字撞了。

    单独分一类是为了让外壳能把它翻成 400 而不是 409 —— 这两件事对页面上的
    人是不一样的:一个是"你填错了",一个是"现在不行,等会儿再来"。
    多继承 ``ValueError`` 是刻意的:外壳那边"参数不对 → 400"的规则已经在了,
    不必为建图再加一条。
    """


@dataclass(frozen=True, slots=True)
class MappingConfig:
    """建图要用到的路径和几个域参数。"""

    bags_dir: Path
    maps_dir: Path
    #: 实时域。录包和实时定位都在这个域上。
    live_domain: str = "24"
    #: 离线域。重建单独占一个,免得和实时链路互相看见。
    offline_domain: str = "93"
    lidar_topic: str = "/front_lidar"
    #: slam_toolbox 的参数模板。会被复制到一个**路径不带空格**的地方再传给
    #: ROS —— 本仓库的路径里有空格("D1 Max"),而 ROS 对这个很敏感。
    params_template: Path = Path("config/params/mapper_3d.yaml")
    work_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    #: 雷达外参。清单 #64:速腾前雷达疑似朝后装,建出的图内部朝向差 180°,
    #: 根治就是把这个设成 3.14159 重建一次。默认留着文档里的 0 ——
    #: 改这里等于改文档 §2,两处要一起改。
    lidar_yaw: float = 0.0
    #: 定位地图的坐标系名。发 ``/initialpose`` 时要填,逐字照抄文档 §4。
    loc_frame: str = "loc_map"


class MappingOrchestrator:
    """把建图那几步串起来。自己不认识 ROS,只生成 ``ProcSpec`` 交给进程管理器。

    一次只做一件事:正在录包就不能重建,正在重建就不能录包。它们抢的是同一
    条链路和同一批话题,并行只会两边都出错图。
    """

    RECORD = "bagrecord"
    SLAM = "slam"
    STATIC_TF = "static_tf"
    PC2SCAN = "pc2scan"
    BAGPLAY = "bagplay"
    MAP_SAVER = "map_saver"
    SERIALIZE = "serialize"
    INITPOSE = "initialpose"

    def __init__(self, procs: ProcManager, cfg: MappingConfig) -> None:
        self._procs = procs
        self._cfg = cfg
        self._phase = "idle"
        self._bag: Path | None = None
        self._map_id = ""
        self._error = ""

    # ------------------------------------------------------------------ 状态

    @property
    def phase(self) -> str:
        """idle | recording | rebuilding。"""
        return self._phase

    @property
    def bag(self) -> Path | None:
        """当前正在录、或者正在重建的那个包。"""
        return self._bag

    @property
    def maps_dir(self) -> Path:
        """存好的地图放在哪。页面要画底图,得知道去哪儿读那对 pgm/yaml。"""
        return self._cfg.maps_dir

    @property
    def last_error(self) -> str:
        """上一次失败的原因。

        重建要跑几分钟,HTTP 那边早就返回了,失败原因只能留在这儿等人来问。
        """
        return self._error

    def to_wire(self) -> dict[str, object]:
        return {
            "phase": self._phase,
            "bag": self._bag.name if self._bag else "",
            "map_id": self._map_id,
            "last_error": self._error,
            "bags": self.list_bags(),
            "maps": self.list_maps(),
        }

    def list_bags(self) -> list[str]:
        return _list_dirs(self._cfg.bags_dir)

    def list_maps(self) -> list[str]:
        """有 ``.posegraph`` 的才算一张图 —— 只有 pgm 的定位模式读不了。"""
        maps_dir = self._cfg.maps_dir
        if not maps_dir.is_dir():
            return []
        return sorted(p.stem for p in maps_dir.glob("*.posegraph"))

    # ------------------------------------------------------------------ 录包

    async def start_record(self, name: str) -> Path:
        """开录。返回包的落盘路径。"""
        self._require_idle("录包")
        _check_name(name, "包名")
        bag = self._cfg.bags_dir / name
        if bag.exists():
            raise MappingArgError(f"{name} 这个包已经在了,换个名字 —— 覆盖等于丢一段路")
        self._cfg.bags_dir.mkdir(parents=True, exist_ok=True)
        await self._start(self.spec_for_record(bag))
        self._phase = "recording"
        self._bag = bag
        self._error = ""
        return bag

    async def stop_record(self) -> Path:
        """停录。返回刚录完的那个包。"""
        if self._phase != "recording":
            raise MappingError(f"现在没在录包(当前 {self._phase})")
        bag = self._bag
        assert bag is not None
        await self._procs.stop(self.RECORD, term_grace_s=_RECORD_GRACE_S)
        self._phase = "idle"
        return bag

    # ------------------------------------------------------------------ 重建

    async def rebuild(self, bag: Path, map_id: str) -> Path:
        """离线重建一张图。返回图的路径前缀(不带扩展名)。

        这一步要跑几分钟到几十分钟。调用方(HTTP)应该把它丢到后台,靠
        ``phase`` 和 ``last_error`` 看进展。
        """
        self._require_idle("重建")
        _check_name(map_id, "图名")
        bag = Path(bag)
        if not bag.exists():
            raise MappingArgError(f"包不存在:{bag}")

        self._phase = "rebuilding"
        self._bag = bag
        self._map_id = map_id
        self._error = ""
        self._cfg.maps_dir.mkdir(parents=True, exist_ok=True)
        # 坐标系要被重建了,标在旧坐标系上的原点从这一刻起就是错的。
        # 放在**开始之前**作废:重建崩在半路时图可能已经覆盖了一半,
        # 那时候留着旧原点是最坏的一种。
        forget_home(self._cfg.maps_dir, map_id)
        started: list[str] = []
        try:
            self._write_params()
            for spec in self.specs_for_rebuild(bag, map_id):
                await self._start(spec)
                started.append(spec.name)
            await self._wait(self.BAGPLAY, REBUILD_TIMEOUT_S)
            for spec in self.specs_for_save(map_id):
                await self._start(spec)
                started.append(spec.name)
                await self._wait(spec.name, _SAVE_TIMEOUT_S)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # 起了什么就收什么。留一个 slam_toolbox 在隔离域里空转,下一次
            # 重建会因为名字撞了直接失败,而现场根本看不出为什么。
            for name in reversed(started):
                await self._procs.stop(name)
            self._phase = "idle"
        return self._cfg.maps_dir / map_id

    # -------------------------------------------------------------- 初始位姿

    async def publish_initial_pose(self, x: float, y: float,
                                   yaw: float = 0.0) -> None:
        """告诉定位"狗现在大概在这儿"。文档 §4 的最后一条命令。

        机器不在图原点上开机时,slam_toolbox 的定位模式要有人给一个初始猜测
        才收敛得回来。这件事**只能由人在看板上点** —— 自己猜一个位置的代价
        是真的撞上去(同 ``LocalNavBackend.reset_localization`` 的理由)。

        录包和重建期间不许发:那两个阶段要么正在实时域上录、要么整个跑在隔离
        域里,这一条发出去只会搅乱正在进行的事。
        """
        self._require_idle("发初始位姿")
        await self._start(self.spec_for_initial_pose(x, y, yaw))
        try:
            await self._wait(self.INITPOSE, _INITPOSE_TIMEOUT_S)
        finally:
            # 超时的那条路上进程还挂着。不收掉的话,下次再发会撞名字,
            # 而现场只会看到"发不出去",看不出是被上一次卡住的。
            await self._procs.stop(self.INITPOSE)

    def spec_for_initial_pose(self, x: float, y: float, yaw: float) -> ProcSpec:
        """文档 §4 最后那条 ``ros2 topic pub``。走**实时域** —— 定位跑在那儿。

        消息体用 ``json.dumps`` 生成,不手写那串花括号:JSON 是 YAML 的子集,
        ``ros2 topic pub`` 照收,而手拼一层套一层的映射是这类命令唯一真正会
        写错的地方 —— 少一个花括号,报的错和位姿一点关系都没有。

        朝向要写成四元数:绕 Z 轴转 ``yaw`` 就是
        ``z = sin(yaw/2), w = cos(yaw/2)``。``w`` 必须写出来,默认值 0 是一个
        非法姿态,发下去定位会当场炸。
        """
        message = json.dumps({
            "header": {"frame_id": self._cfg.loc_frame},
            "pose": {"pose": {
                "position": {"x": x, "y": y, "z": 0.0},
                "orientation": {"z": math.sin(yaw / 2.0),
                                "w": math.cos(yaw / 2.0)},
            }},
        })
        return ProcSpec(
            name=self.INITPOSE,
            argv=("ros2", "topic", "pub", "--once", "/initialpose",
                  "geometry_msgs/msg/PoseWithCovarianceStamped", message),
            env=self._live_env(),
            ready_pattern="",       # 发一条就退,没有"起来了"这回事
        )

    async def snapshot(self) -> dict[str, object]:
        """给 HTTP 用的状态。做成协程是为了走线程桥,和别的读法保持一致。"""
        return self.to_wire()

    async def plan_rebuild(self, bag_name: str, map_id: str) -> Path:
        """把"包名 + 图名"验一遍,返回包的路径。

        单独抽出来是因为 :meth:`rebuild` 要跑几分钟,HTTP 那边等不起,只能
        丢到后台。可"名字填错了"必须**当场**告诉人,不能等几分钟之后让他
        去状态里翻 ``last_error``。所以先同步验一遍,再丢后台。
        """
        self._require_idle("重建")
        _check_name(map_id, "图名")
        _check_name(bag_name, "包名")
        bag = self._cfg.bags_dir / bag_name
        if not bag.exists():
            raise MappingArgError(f"包不存在:{bag}")
        return bag

    # ------------------------------------------------------------------ 命令

    def spec_for_record(self, bag: Path) -> ProcSpec:
        """文档 §2 (a)。录包走**实时域**,它要听的是真机现在发的话题。"""
        return ProcSpec(
            name=self.RECORD,
            argv=("ros2", "bag", "record", "-s", "mcap", "-o", str(bag),
                  *RECORD_TOPICS),
            env=self._live_env(),
            # 假设(待真机验证): rosbag2 humble 起来时会打这一行。接真机时
            # 用 runs/logs/bagrecord.log 核一下,不对就改这个正则。
            ready_pattern=r"Listening for topics|Recording",
            ready_timeout_s=30.0,
        )

    def specs_for_rebuild(self, bag: Path, map_id: str) -> list[ProcSpec]:
        """文档 §2 (b)。四个进程,**放包的排最后**。"""
        cfg = self._cfg
        env = self._offline_env()
        params = self._params_path()
        return [
            ProcSpec(
                name=self.SLAM,
                argv=("ros2", "run", "slam_toolbox", "async_slam_toolbox_node",
                      "--ros-args", "--params-file", str(params),
                      "-p", "use_sim_time:=true"),
                env=env,
                # 假设(待真机验证): 就绪串取节点名。slam_toolbox 一起来就会
                # 打带节点名的日志行。
                ready_pattern=r"slam_toolbox",
                ready_timeout_s=60.0,
            ),
            ProcSpec(
                name=self.STATIC_TF,
                argv=("ros2", "run", "tf2_ros", "static_transform_publisher",
                      "--x", "0.2", "--y", "0", "--z", "0.18",
                      "--roll", "0", "--pitch", "0",
                      "--yaw", _num(cfg.lidar_yaw),
                      "--frame-id", "base_link",
                      "--child-frame-id", "rslidar_head"),
                env=env,
                # 假设(待真机验证): static_transform_publisher 起来后会打
                # "Spinning until stopped"。
                ready_pattern=r"Spinning until stopped|static_transform",
                ready_timeout_s=20.0,
            ),
            ProcSpec(
                name=self.PC2SCAN,
                argv=("ros2", "run", "pointcloud_to_laserscan",
                      "pointcloud_to_laserscan_node", "--ros-args",
                      "-r", f"cloud_in:={cfg.lidar_topic}",
                      "-r", "scan:=/scan_3d",
                      "-p", "target_frame:=base_link",
                      "-p", "min_height:=0.1", "-p", "max_height:=1.5",
                      "-p", "range_min:=0.3", "-p", "range_max:=20.0",
                      "-p", "angle_min:=-3.14159", "-p", "angle_max:=3.14159",
                      "-p", "angle_increment:=0.0087",
                      "-p", "use_sim_time:=true"),
                env=env,
                # 假设(待真机验证): 同上,取节点名。
                ready_pattern=r"pointcloud_to_laserscan",
                ready_timeout_s=30.0,
            ),
            ProcSpec(
                name=self.BAGPLAY,
                argv=("ros2", "bag", "play", str(bag), "--clock",
                      "--delay", "4", "--topics",
                      cfg.lidar_topic, "/tf", "/tf_static"),
                env=env,
                ready_pattern="",       # 起了就算,它自己会退
            ),
        ]

    def specs_for_save(self, map_id: str) -> list[ProcSpec]:
        """文档 §2 (c)。**两步都要**。

        ``map_saver_cli`` 出 pgm + yaml 给导航用,``serialize_map`` 出
        posegraph 给定位用。少了后者,slam_toolbox 的 localization 模式
        没得读,这张图就只能看不能用。
        """
        prefix = (self._cfg.maps_dir / map_id).resolve()
        env = self._offline_env()
        return [
            ProcSpec(
                name=self.MAP_SAVER,
                argv=("ros2", "run", "nav2_map_server", "map_saver_cli",
                      "-f", str(prefix), "--ros-args",
                      "-p", "save_map_timeout:=15.0"),
                env=env,
                ready_pattern="",
            ),
            ProcSpec(
                name=self.SERIALIZE,
                argv=("ros2", "service", "call", "/slam_toolbox/serialize_map",
                      "slam_toolbox/srv/SerializePoseGraph",
                      f"{{filename: '{_posix(prefix)}'}}"),
                env=env,
                ready_pattern="",
            ),
        ]

    # ------------------------------------------------------------------ 内部

    def _live_env(self) -> dict[str, str]:
        return {"ROS_DOMAIN_ID": self._cfg.live_domain,
                "RMW_IMPLEMENTATION": "rmw_zenoh_cpp"}

    def _offline_env(self) -> dict[str, str]:
        """隔离域三件套。少一件就还在跟实时链路搅在一起 —— 见模块开头。"""
        return {"ROS_DOMAIN_ID": self._cfg.offline_domain,
                "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
                "ROS_LOCALHOST_ONLY": "1"}

    def _params_path(self) -> Path:
        return self._cfg.work_dir / self._cfg.params_template.name

    def _write_params(self) -> None:
        """把参数模板复制到不带空格的路径下,顺便把 ``<REPO>`` 换成真路径。"""
        src = self._cfg.params_template
        if not src.is_file():
            raise MappingError(f"参数模板不在:{src}")
        text = src.read_text(encoding="utf-8")
        text = text.replace("<REPO>", str(Path.cwd()).replace("\\", "/"))
        dst = self._params_path()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")

    def _require_idle(self, what: str) -> None:
        if self._phase != "idle":
            raise MappingError(f"现在在{_CN[self._phase]},不能{what}")

    async def _start(self, spec: ProcSpec) -> None:
        try:
            await self._procs.start(spec)
        except ProcError as exc:
            raise MappingError(str(exc)) from exc

    async def _wait(self, name: str, timeout_s: float) -> None:
        try:
            code = await self._procs.wait(name, timeout_s)
        except ProcError as exc:
            raise MappingError(str(exc)) from exc
        if code != 0:
            raise MappingError(f"{name} 退出码 {code},日志见 {self._procs.log_path(name)}")


_CN = {"idle": "闲着", "recording": "录包", "rebuilding": "重建"}


def _posix(path: Path) -> str:
    """路径写成正斜杠。

    ROS 的服务参数是一段 YAML,反斜杠在里面是转义符 —— Windows 路径原样塞
    进去会被吃掉一半。
    """
    return str(path).replace("\\", "/")


def _num(value: float) -> str:
    """角度写成命令行参数。整数不留 ``.0`` —— 和文档里的写法对得上。"""
    return f"{value:g}"


def _check_name(name: str, what: str) -> None:
    if not name or not _SAFE_NAME.match(name):
        raise MappingArgError(f"{what}只能用字母、数字、下划线、点和横杠,给的是 {name!r}")


def _list_dirs(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


__all__ = [
    "RECORD_TOPICS",
    "REBUILD_TIMEOUT_S",
    "MappingArgError",
    "MappingConfig",
    "MappingError",
    "MappingOrchestrator",
]
