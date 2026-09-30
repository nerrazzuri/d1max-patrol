"""建图编排:录一段包 → 离线建一个地图版本。

录包的命令行逐字照抄 ``docs/建图定位与巡检管线.md`` §2 (a);建图从 W09c1 起换成 MOLA(slam_toolbox
那条退役,W08 决定 5):起一个建图脚本(``deploy/d1max-map-build`` → ``d1max-loc build``,在 ROS 的
系统 Python 里跑),它在录包上跑 MOLA 建图、打成地图版本的几个文件(``d1max_contract.maps`` 的
``GEOMETRY_FILES``)。日志照旧由进程管理器落盘,站点经 ``proc_log`` 看尾巴。

两件事值得单独说:

**为什么建图是离线的。** 先录一段 mcap 包,再在录包上建 —— 慢多少都无所谓,反正没人等着(边走边建是
W09c2)。

**为什么建图在隔离域里。** MOLA 命令行直接读录包、本不上 ROS 网;万一哪个环节起了 ROS 节点,也只在
本机的隔离域里(``ROS_DOMAIN_ID=93`` + ``rmw_fastrtps_cpp`` + ``ROS_LOCALHOST_ONLY=1``),不会跟实时
链路互相看见 —— 录包里的旧 ``/tf`` 放出来会污染实时定位。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import signal
from dataclasses import dataclass, replace
from pathlib import Path

from d1max_agent.engine.homing import forget_home
from d1max_contract.maps import GEOMETRY_FILES
from d1max_patrol.app.procs import ProcError, ProcManager, ProcSpec

log = logging.getLogger(__name__)

#: 录包按这么大切成一个个文件(W00c5d:点云几十 MB/s,十分钟不切就过了站点单文件 8 GiB 的上限,
#: 永远传不上去)。逐字照抄文档 §2 (a)。
RECORD_SPLIT_BYTES = 2 * 1024 ** 3

#: 录包录哪几个话题。逐字照抄文档 §2 (a)。
RECORD_TOPICS: tuple[str, ...] = (
    "/front_lidar", "/tf", "/tf_static", "/odom/mc_odom", "/odom/current_pose",
)

#: 名字里只许有这些 —— 它要当目录名用,还要拼进命令行。**字母或数字打头**(跟站点的契约一样):
#: 重建要先删同名的旧版本目录,``..`` 就删到上一层去了;点开头的留给中间目录,``-`` 开头会被当成选项。
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: 录包进程停的时候多给一会儿。mcap 要把索引写完才算一个完整的包,
#: 强杀出来的包放不回去,而那一段路是走不回来的。
_RECORD_GRACE_S = 15.0

#: 建图最多跑多久。一段走动录几分钟,MOLA 离线建图比实时慢,再加打包(点云、栅格),给足一小时。
REBUILD_TIMEOUT_S = 3600.0

#: 建图脚本在狗上的位置(跟着这一版走)。
MAP_BUILDER = Path("/opt/d1max/current/deploy/d1max-map-build")
#: ROS 包装脚本:source ROS 再执行后面的命令。代理的服务里没有 source ROS,``ros2``、``mola-cli``
#: 都不在 PATH 上(W09c2 发现:录包原来直接起 ``ros2``,真狗上起不来)。
ROS_WRAPPER = Path("/opt/d1max/current/deploy/d1max-ros")
#: 在线建图(W09c2):直接起 ``mola-cli``(不经 ``ros2 launch`` —— 停 launch 时底下的 mola-cli 成了
#: 孤儿、没存盘,开发机上实测),用 MOLA 自带的 ROS 2 配置。
MOLA_CLI = "/opt/ros/humble/lib/mola_launcher/mola-cli"
#: 建图预览(W09f):跟在线建图一起起的小进程(``d1max_localizer.livemap``),攒一张俯视栅格给手机看。
LIVE_PREVIEW = Path("/opt/d1max/current/deploy/d1max-live-preview")
MOLA_LO_SHARE = "/opt/ros/humble/share/mola_lidar_odometry"
#: 在线建图停的时候给多久存盘(开发机 4 分半的走动:simplemap 200 MB + 局部地图 150 MB,3.5 s;
#: Orin 上盘慢,给足)。
LIVE_SAVE_GRACE_S = 300.0
#: 在线建图起完等这么久再看它还在不在(插件、路径、QoS 不对的话它起来就退;W09c2 内审应修 2)。
LIVE_SETTLE_S = 3.0
#: 在线建图的 ROS 命名空间:跟定位器的 MOLA(同一份配置、同一个域)不串 —— 位姿话题、重定位服务、
#: /initialpose 这几个是绝对名,命名空间管不到,逐个改名(W09c2 内审阻断 1;开发机上核过)。
LIVE_NS = "/d1max_mapping"
_LIVE_RENAMES = ("/initialpose", "/relocalize_near_pose", "/lidar_odometry/pose",
                 "/lidar_odometry/pose_quality")


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
    #: 建图脚本(``d1max-loc build`` 的包装)。
    map_builder: Path = MAP_BUILDER
    #: 先验怎么打包(W09c 决定 4):``none`` 原样,``regroup:<体素米>:<范围倍数>`` 压缩 —— 压缩参数要
    #: 用回放在这块场地的录包上验过再换。
    prior_pack: str = "none"
    ros_wrapper: Path = ROS_WRAPPER
    #: 在线建图订雷达的 QoS。厂商前雷达发的是 reliable(newdog2 录包的 metadata,depth 100);
    #: best_effort 订的话开发机上放包实测 18% 的帧在传输里丢了、图上出重影,reliable 99.5%。
    live_qos: str = "reliable"
    live_preview: Path = LIVE_PREVIEW
    #: RTK 天线在狗身上的水平位置(朝前、朝左,米;W09e 配经纬度用,真机量了再配)。
    rtk_antenna: tuple[float, float] = (0.0, 0.0)


class MappingOrchestrator:
    """把建图那几步串起来。自己不认识 ROS,只生成 ``ProcSpec`` 交给进程管理器。

    一次只做一件事:正在录包就不能重建,正在重建就不能录包。它们抢的是同一
    条链路和同一批话题,并行只会两边都出错图。
    """

    RECORD = "bagrecord"
    BUILD = "mapbuild"
    #: 打包边走边建的:单独一个名字、单独一份日志 —— 打不成退回从录包建时,不盖掉打不成的原因。
    PACK = "mappack"
    LIVE = "molamap"
    #: 建图预览(W09f):尽力而为,起不来、停不掉都不碍录包与在线建图。
    PREVIEW = "mapview"

    def __init__(self, procs: ProcManager, cfg: MappingConfig) -> None:
        self._procs = procs
        self._cfg = cfg
        self._phase = "idle"
        self._bag: Path | None = None
        self._map_id = ""
        self._error = ""
        #: 正在边走边建的图号(W09c2);没有是 None。
        self._live: str | None = None
        #: 这一趟的预览没起来的原因(W09f);起来了是空串。
        self.preview_error = ""

    @property
    def log_dir(self) -> Path:
        """子进程(录包、重建)的日志目录(W00c6g)。"""
        return self._procs.log_dir

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
        """版本文件齐的目录才算一张图(建到一半的、中间目录都不算)。"""
        maps_dir = self._cfg.maps_dir
        if not maps_dir.is_dir():
            return []
        return sorted(p.name for p in maps_dir.iterdir()
                      if p.is_dir() and _SAFE_NAME.match(p.name)
                      and all((p / f).is_file() for f in GEOMETRY_FILES))

    # ------------------------------------------------------------------ 录包

    @property
    def live_map(self) -> str | None:
        """正在边走边建的图号(W09c2);没有是 None。"""
        return self._live

    async def start_record(self, name: str, *, live_map_id: str | None = None) -> Path:
        """开录。返回包的落盘路径。给了 ``live_map_id`` 就同时起在线建图(W09c2 边走边建):MOLA
        建图模式订实时域的雷达,停的时候把局部地图、轨迹、simplemap 存进这张图的中间目录,之后
        :meth:`package` 打包(不用再在录包上跑一遍)。"""
        self._require_idle("录包")
        _check_name(name, "包名")
        if live_map_id is not None:
            _check_name(live_map_id, "图名")
        bag = self._cfg.bags_dir / name
        if bag.exists():
            raise MappingArgError(f"{name} 这个包已经在了,换个名字 —— 覆盖等于丢一段路")
        self._cfg.bags_dir.mkdir(parents=True, exist_ok=True)
        if live_map_id is not None:
            out, work = self._out(live_map_id)
            for d in (out, work.parent):                   # 同 rebuild:旧的产物、中间文件不混进来
                shutil.rmtree(d, ignore_errors=True)
            work.mkdir(parents=True)
            forget_home(self._cfg.maps_dir, live_map_id)   # 坐标系要重建了
        await self._start(self.spec_for_record(bag))
        if live_map_id is not None:
            spec = self.spec_for_live(live_map_id)
            (work / "mola.json").write_text(json.dumps(
                {"mode": "live", "pipeline": spec.argv[-1], "env": dict(spec.env)},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                await self._start(spec)
                await asyncio.sleep(LIVE_SETTLE_S)
                if self.LIVE not in self._procs.running():
                    raise MappingError("在线建图起来就退了,日志见 "
                                       f"{self._procs.log_path(self.LIVE)}")
            except MappingError:
                await self._procs.stop(self.LIVE)
                await self._procs.stop(self.RECORD, term_grace_s=_RECORD_GRACE_S)
                raise
        self.preview_error = ""
        if live_map_id is not None:
            try:
                await self._start(self.spec_for_preview(live_map_id, name))
            except MappingError as exc:
                self.preview_error = str(exc)
                log.warning("建图预览起不来(录包、在线建图照旧):%s", exc)
        self._phase = "recording"
        self._bag = bag
        self._live = live_map_id
        self._error = ""
        return bag

    async def stop_record(self) -> Path:
        """停录。返回刚录完的那个包。在线建图一起停(给足存盘的时间)。"""
        if self._phase != "recording":
            raise MappingError(f"现在没在录包(当前 {self._phase})")
        bag = self._bag
        assert bag is not None
        stops = [self._procs.stop(self.RECORD, term_grace_s=_RECORD_GRACE_S)]
        if self._live is not None:
            stops.append(self._procs.stop(self.LIVE, term_grace_s=LIVE_SAVE_GRACE_S))
            stops.append(self._stop_preview())
        # 一个停不掉,另一个照样停(外审阻断 1:不许留一个进程在后台一直写盘)
        got = await asyncio.gather(*stops, return_exceptions=True)
        self._phase = "idle"
        self._live = None
        for g in got:
            if isinstance(g, BaseException):
                raise g
        return bag

    async def _stop_preview(self) -> None:
        """预览停不掉只记一行:它不写要紧的东西,不碍停录。"""
        try:
            await self._procs.stop(self.PREVIEW)
        except Exception as exc:  # noqa: BLE001 —— 尽力而为
            log.warning("建图预览停不掉:%s", exc)

    # ------------------------------------------------------------------ 重建

    async def rebuild(self, bag: Path, map_id: str) -> Path:
        """离线建一个地图版本。返回版本文件所在的目录。

        这一步要跑几分钟到几十分钟。调用方应该把它丢到后台,靠 ``phase`` 和 ``last_error`` 看进展。
        """
        self._require_idle("重建")
        _check_name(map_id, "图名")
        bag = Path(bag)
        if not bag.exists():
            raise MappingArgError(f"包不存在:{bag}")

        # 坐标系要被重建了,标在旧坐标系上的原点从这一刻起就是错的。
        # 放在**开始之前**作废:重建崩在半路时图可能已经覆盖了一半,
        # 那时候留着旧原点是最坏的一种。
        self._cfg.maps_dir.mkdir(parents=True, exist_ok=True)
        forget_home(self._cfg.maps_dir, map_id)
        return await self._run_build(bag, map_id, reuse=False)

    async def package(self, bag: Path, map_id: str) -> Path:
        """把边走边建存下的(中间目录里的局部地图、轨迹、simplemap)打成版本文件(W09c2):建图脚本
        ``--reuse``,不在录包上再跑一遍 MOLA(录包只拿来读逐帧扫描打真射线)。返回版本文件的目录。"""
        self._require_idle("打包")
        _check_name(map_id, "图名")
        return await self._run_build(Path(bag), map_id, reuse=True)

    async def _run_build(self, bag: Path, map_id: str, *, reuse: bool) -> Path:
        self._phase = "rebuilding"
        self._bag = bag
        self._map_id = map_id
        self._error = ""
        out, work = self._out(map_id)
        name = self.PACK if reuse else self.BUILD
        started = False
        try:
            # 打包按文件名收:上一次同名的产物、中间目录里旧的点云混进来,这一版就是拼出来的。
            # 从录包建:中间目录整个清(上一次没建成留着查的那份,几百 MB,不留到下一次之后);
            # 打包在线建好的:中间目录就是原料,只清产物。
            for d in (out,) if reuse else (out, work.parent):
                shutil.rmtree(d, ignore_errors=True)
            spec = self.spec_for_build(bag, map_id)
            if reuse:
                spec = replace(spec, name=name, argv=(*spec.argv, "--reuse"))
            await self._start(spec)
            started = True
            await self._wait(name, REBUILD_TIMEOUT_S)
            # 建成了:中间文件(simplemap、点云)不留;没建成的留着 MOLA 的日志查原因。
            shutil.rmtree(work.parent, ignore_errors=True)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # 超时的那条路上进程还挂着:不收掉,下一次建图撞名字直接失败,现场看不出为什么。
            if started:
                await self._procs.stop(name)
            self._phase = "idle"
        return out

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
            argv=(str(self._cfg.ros_wrapper), "ros2", "bag", "record", "-s", "mcap", "-b",
                  str(RECORD_SPLIT_BYTES),
                  "-o", str(bag), *RECORD_TOPICS),
            env=self._live_env(),
            # 假设(待真机验证): rosbag2 humble 起来时会打这一行。接真机时
            # 用 runs/logs/bagrecord.log 核一下,不对就改这个正则。
            ready_pattern=r"Listening for topics|Recording",
            ready_timeout_s=30.0,
        )

    def spec_for_live(self, map_id: str) -> ProcSpec:
        """在线建图(W09c2):``mola-cli`` + MOLA 自带的 ROS 2 配置,走**实时域**(订的是真机现在的
        雷达)。要的开关都给环境变量(``ros2 launch`` 原来就是这么传的);停的时候(SIGTERM)存局部
        地图、轨迹、simplemap 到这张图的中间目录。"""
        _, work = self._out(map_id)
        share = MOLA_LO_SHARE
        return ProcSpec(
            name=self.LIVE,
            argv=(str(self._cfg.ros_wrapper), MOLA_CLI,
                  f"{share}/mola-cli-launchs/lidar_odometry_ros2.yaml"),
            env=self._live_env() | {
                "MOLA_LIDAR_TOPIC": self._cfg.lidar_topic, "MOLA_USE_FIXED_LIDAR_POSE": "true",
                "MOLA_WITH_GUI": "false", "MOLA_LOCALIZ_USE_REP105": "false",
                "MOLA_MAPPING_ENABLED": "true", "MOLA_LIDAR_QOS_RELIABILITY": self._cfg.live_qos,
                "MOLA_STATE_ESTIMATOR_YAML":
                    f"{share}/state-estimator-params/state-estimation-simple.yaml",
                "MOLA_GENERATE_SIMPLEMAP": "true",
                "MOLA_SIMPLEMAP_OUTPUT": str(work / "map.simplemap"),
                "MOLA_SAVE_TRAJECTORY": "true",
                "MOLA_TUM_TRAJECTORY_OUTPUT": str(work / "traj.tum"),
                "MOLA_SAVE_MM": str(work / "raw_prior.mm"), "MOLA_LOCAL_MAP_MAX_SIZE": "0",
                # 不跟定位器串:换命名空间、不发 /tf 与位姿、不往 ROS 上发越来越大的地图
                "ROS_ARGS": json.dumps(["-r", f"__ns:={LIVE_NS}", *(
                    x for n in _LIVE_RENAMES for x in ("-r", f"{n}:={LIVE_NS}{n}"))]),
                "MOLA_LOCALIZATION_PUBLISH_TF": "false",
                # 位姿发(W09f 预览要):改过名、在 LIVE_NS 下,跟定位器的不串
                "MOLA_LOCALIZATION_PUBLISH_ODOM_MSGS": "true",
                "MOLA_ROS2_TRANSFORM_PUBLISH_PERIOD": "0",
                "MOLA_ROS2_PUBLISH_MAPS_PERIOD": "86400"},
            ready_pattern="",
            stop_signal=signal.SIGINT,          # SIGTERM 它不存盘(见 ProcSpec.stop_signal)
        )

    def preview_running(self) -> bool:
        """建图预览进程还在不在(W09f 内审应修 5:起来之后才退的,开录时记不下原因)。
        在事件循环里调(会摘掉已退的进程)。"""
        return self.PREVIEW in self._procs.running()

    def preview_dir(self, map_id: str) -> Path:
        """建图预览的快照写在哪(W09f):这张图的中间目录底下。"""
        return self._out(map_id)[1] / "preview"

    def spec_for_preview(self, map_id: str, run: str) -> ProcSpec:
        """建图预览(W09f):订实时域的雷达与在线建图的位姿,快照写进 :meth:`preview_dir`。
        ``run`` 是这一趟的号(录包名),手机据此知道换了一趟。"""
        return ProcSpec(
            name=self.PREVIEW,
            argv=(str(self._cfg.live_preview), "--out", str(self.preview_dir(map_id)),
                  "--run", run, "--lidar-topic", self._cfg.lidar_topic,
                  "--pose-topic", f"{LIVE_NS}/lidar_odometry/pose"),
            env=self._live_env(),
            ready_pattern="",
        )

    def spec_for_build(self, bag: Path, map_id: str) -> ProcSpec:
        """建图脚本:MOLA 在录包上建图 + 打成地图版本的文件(W09c1)。"""
        out, work = self._out(map_id)
        cfg = self._cfg
        return ProcSpec(
            name=self.BUILD,
            argv=(str(cfg.map_builder), "--bag", str(bag), "--out", str(out),
                  "--work", str(work), "--lidar-topic", cfg.lidar_topic,
                  "--prior-pack", cfg.prior_pack,
                  "--rtk-antenna", f"{cfg.rtk_antenna[0]:g},{cfg.rtk_antenna[1]:g}"),
            env=self._offline_env(),
            ready_pattern="",       # 跑完自己退
        )

    # ------------------------------------------------------------------ 内部

    def _live_env(self) -> dict[str, str]:
        return {"ROS_DOMAIN_ID": self._cfg.live_domain,
                "RMW_IMPLEMENTATION": "rmw_zenoh_cpp"}

    def _offline_env(self) -> dict[str, str]:
        """隔离域三件套。少一件就还在跟实时链路搅在一起 —— 见模块开头。"""
        return {"ROS_DOMAIN_ID": self._cfg.offline_domain,
                "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
                "ROS_LOCALHOST_ONLY": "1"}

    def _out(self, map_id: str) -> tuple[Path, Path]:
        """版本文件放哪、中间文件放哪(点开头的目录:不会跟哪张图同名)。"""
        return self._cfg.maps_dir / map_id, self._cfg.maps_dir / ".work" / map_id

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


def _check_name(name: str, what: str) -> None:
    if not name or not _SAFE_NAME.match(name):
        raise MappingArgError(f"{what}只能用字母、数字、下划线、点和横杠(字母或数字打头),"
                              f"给的是 {name!r}")


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
