"""建图编排:录包 → 用建图脚本(MOLA)离线建一个地图版本(W09c1)。

**这里一个 ROS 进程都不真起。** 断言的对象是**生成出来的 ``ProcSpec``** ——
命令行、环境变量。理由有两条:一是 CI 上没有 ROS;二是就算有,一次真建图
要跑几分钟,那不是单元测试该干的事。建图脚本本身(``d1max-loc build``)在
``packages/localizer/tests/test_loc_build.py`` 里测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d1max_agent.engine.homing import HomeError, HomePoint, load_home, save_home
from d1max_patrol.app.mapping import (
    REBUILD_TIMEOUT_S,
    RECORD_TOPICS,
    MappingConfig,
    MappingError,
    MappingOrchestrator,
)
from d1max_patrol.app.procs import ProcError, ProcSpec
from d1max_patrol.protocol.nav_types import Pose


class FakeProcs:
    """假进程管理器。只记账,不起进程。"""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self.log_dir = log_dir
        self.started: list[ProcSpec] = []
        self.stopped: list[str] = []
        #: 名字 → 退出码。没写的都当 0。
        self.exit_codes: dict[str, int] = {}
        #: 这些名字一起就报错。
        self.start_fails: set[str] = set()

    def running(self) -> list[str]:
        return sorted({s.name for s in self.started} - set(self.stopped))

    def log_path(self, name: str) -> Path:
        return self._log_dir / f"{name}.log"

    async def start(self, spec: ProcSpec) -> None:
        if spec.name in self.start_fails:
            raise ProcError(f"{spec.name} 起不来(测试里安排的)")
        if spec.name == "bagrecord":
            # 真的 ``ros2 bag record`` 会把 ``-o`` 那个目录建出来,列表要靠它。
            Path(spec.argv[spec.argv.index("-o") + 1]).mkdir(parents=True)
        if spec.name in ("mapbuild", "mappack"):
            # 真的建图脚本会在 ``--work`` 里落 MOLA 的日志、中间文件。
            work = Path(spec.argv[spec.argv.index("--work") + 1])
            work.mkdir(parents=True, exist_ok=True)        # 边走边建的:中间目录本来就在
            (work / "mola.log").write_text("建图日志", encoding="utf-8")
        self.started.append(spec)

    async def stop(self, name: str, *, term_grace_s: float = 3.0) -> None:
        self.stopped.append(name)

    async def wait(self, name: str, timeout_s: float | None = None) -> int:
        return self.exit_codes.get(name, 0)


@pytest.fixture(autouse=True)
def _不等在线建图起稳(monkeypatch):
    import d1max_patrol.app.mapping as M
    monkeypatch.setattr(M, "LIVE_SETTLE_S", 0.0)


@pytest.fixture
def procs(tmp_path) -> FakeProcs:
    return FakeProcs(tmp_path / "logs")


@pytest.fixture
def cfg(tmp_path) -> MappingConfig:
    return MappingConfig(
        bags_dir=tmp_path / "bags",
        maps_dir=tmp_path / "maps",
        map_builder=Path("/opt/d1max/current/deploy/d1max-map-build"),
    )


@pytest.fixture
def orch(procs, cfg) -> MappingOrchestrator:
    return MappingOrchestrator(procs, cfg)


@pytest.fixture
def bag(cfg) -> Path:
    """一个"已经录好"的包。重建要它在。"""
    path = cfg.bags_dir / "walk3d-0901"
    path.mkdir(parents=True)
    (path / "metadata.yaml").write_text("x", encoding="utf-8")
    return path


def _argv(specs: list[ProcSpec], name: str) -> str:
    return " ".join(next(s for s in specs if s.name == name).argv)


# ------------------------------------------------------------------ 命令


def test_建图起的是建图脚本_录包_输出_中间目录_雷达话题_先验打包都给了(orch, cfg, tmp_path):
    spec = orch.spec_for_build(tmp_path / "b", "m1")
    assert spec.name == "mapbuild"
    assert spec.argv == ("/opt/d1max/current/deploy/d1max-map-build",
                         "--bag", str(tmp_path / "b"),
                         "--out", str(cfg.maps_dir / "m1"),
                         "--work", str(cfg.maps_dir / ".work" / "m1"),
                         "--lidar-topic", "/front_lidar",
                         "--prior-pack", "none")
    assert spec.ready_pattern == "", "跑完自己退,没有「起来了」这回事"


def test_先验打包和雷达话题是可配的(procs, cfg, tmp_path):
    from dataclasses import replace
    other = MappingOrchestrator(procs, replace(cfg, prior_pack="regroup:0.3:0.5",
                                               lidar_topic="/rear_lidar"))
    argv = other.spec_for_build(tmp_path / "b", "m1").argv
    assert argv[argv.index("--prior-pack") + 1] == "regroup:0.3:0.5"
    assert argv[argv.index("--lidar-topic") + 1] == "/rear_lidar"


def test_建图在隔离域里_不跟实时链路搅在一起(orch, tmp_path):
    """MOLA 命令行直接读录包,本不上 ROS 网;万一哪个环节起了 ROS 节点,也只在本机的隔离域里。"""
    env = orch.spec_for_build(tmp_path / "b", "m1").env
    assert env["ROS_DOMAIN_ID"] == "93"
    assert env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
    assert env["ROS_LOCALHOST_ONLY"] == "1"


def test_录包用的是实时域(orch, tmp_path):
    """录包要听真机现在发的话题,那是实时域。"""
    spec = orch.spec_for_record(tmp_path / "b")
    assert spec.env["ROS_DOMAIN_ID"] == "24"
    assert spec.env["RMW_IMPLEMENTATION"] == "rmw_zenoh_cpp"


def test_域是可配的(procs, cfg, tmp_path):
    from dataclasses import replace
    other = MappingOrchestrator(procs, replace(cfg, live_domain="7", offline_domain="77"))
    assert other.spec_for_record(tmp_path / "b").env["ROS_DOMAIN_ID"] == "7"
    assert other.spec_for_build(tmp_path / "b", "m1").env["ROS_DOMAIN_ID"] == "77"


def test_录包录的是那五个话题(orch, tmp_path):
    line = _argv([orch.spec_for_record(tmp_path / "b")], "bagrecord")
    for topic in RECORD_TOPICS:
        assert topic in line
    assert "-s mcap" in line, "mcap 是文档定的格式,换了格式回放的工具就对不上"


# ------------------------------------------------------------------ 状态机


async def test_录包会起进程并进到recording(orch, procs):
    path = await orch.start_record("w1")
    assert orch.phase == "recording"
    assert procs.running() == ["bagrecord"]
    assert path.name == "w1"


async def test_录着包的时候再录会被拒(orch):
    await orch.start_record("w1")
    with pytest.raises(MappingError):
        await orch.start_record("w2")


async def test_没在录的时候停录会被拒(orch):
    with pytest.raises(MappingError):
        await orch.stop_record()


async def test_停录给足时间收尾(orch, procs, monkeypatch):
    """mcap 要把索引写完才算一个完整的包,强杀出来的包放不回去 ——
    而那一段路是走不回来的。"""
    grace: list[float] = []

    async def fake_stop(name, *, term_grace_s=3.0):
        grace.append(term_grace_s)
        procs.stopped.append(name)

    monkeypatch.setattr(procs, "stop", fake_stop)
    await orch.start_record("w1")
    await orch.stop_record()
    assert grace and grace[0] >= 10.0


async def test_停录之后又能录了(orch):
    await orch.start_record("w1")
    await orch.stop_record()
    assert orch.phase == "idle"
    await orch.start_record("w2")


async def test_重名的包不给覆盖(orch, cfg):
    (cfg.bags_dir / "w1").mkdir(parents=True)
    with pytest.raises(MappingError):
        await orch.start_record("w1")


async def test_包名不合法被拒(orch):
    for bad in ("../x", "a/b", "", "w 1"):
        with pytest.raises(MappingError):
            await orch.start_record(bad)


async def test_重建的时候录包会被拒(orch, procs, bag, monkeypatch):
    """两件事抢的是同一批话题,并行只会两边都出错图。"""
    seen: list[str] = []

    async def fake_wait(name, timeout_s=None):
        seen.append(orch.phase)
        with pytest.raises(MappingError):
            await orch.start_record("w9")
        return 0

    monkeypatch.setattr(procs, "wait", fake_wait)
    await orch.rebuild(bag, "m1")
    assert seen and seen[0] == "rebuilding"


async def test_包不存在就重建会被拒并说清楚(orch, tmp_path):
    with pytest.raises(MappingError, match="不存在"):
        await orch.rebuild(tmp_path / "没有这个包", "m1")


async def test_图名不合法被拒_一个目录也不删(orch, bag, cfg):
    """重建要先删掉同名的旧版本目录:图名是 ``..`` 就删到上一层去了。"""
    keep = cfg.maps_dir / "keep"
    keep.mkdir(parents=True)
    for bad in ("../x", "a/b", "", "..", ".", ".work", "-x"):
        with pytest.raises(MappingError):
            await orch.rebuild(bag, bad)
    assert keep.is_dir() and cfg.bags_dir.is_dir()


# ------------------------------------------------------------------ 重建全程


async def test_重建起建图脚本_等它跑完_收掉(orch, procs, bag, monkeypatch):
    waited: list[tuple[str, float | None]] = []

    async def fake_wait(name, timeout_s=None):
        waited.append((name, timeout_s))
        return 0

    monkeypatch.setattr(procs, "wait", fake_wait)
    await orch.rebuild(bag, "m1")
    assert [s.name for s in procs.started] == ["mapbuild"]
    assert waited == [("mapbuild", REBUILD_TIMEOUT_S)]
    assert orch.phase == "idle" and procs.running() == []


async def test_重建返回的是版本文件的目录(orch, bag, cfg):
    assert await orch.rebuild(bag, "m1") == cfg.maps_dir / "m1"


async def test_上一次同名的产物和中间文件先清掉(orch, procs, bag, cfg, monkeypatch):
    """打包按文件名收:上一次留下的 ``floor.pgm``、中间目录里旧的点云混进来,这一版就是拼出来的。"""
    old_out, old_work = cfg.maps_dir / "m1", cfg.maps_dir / ".work" / "m1"
    for d in (old_out, old_work):
        d.mkdir(parents=True)
        (d / "floor.pgm").write_text("旧的")
    other = cfg.maps_dir / "m2"
    other.mkdir()
    (other / "floor.pgm").write_text("别的图")
    seen: list[bool] = []

    async def fake_wait(name, timeout_s=None):
        seen.append(old_out.exists() or (old_work / "floor.pgm").exists())
        return 0

    monkeypatch.setattr(procs, "wait", fake_wait)
    await orch.rebuild(bag, "m1")
    assert seen == [False], "起建图之前就清掉了"
    assert (other / "floor.pgm").read_text() == "别的图"


async def test_中间文件_建成了就删_没建成留着查_下一次整个清掉(orch, procs, bag, cfg):
    """中间目录里有 simplemap、点云,几百 MB:建成了就不留;没建成留着 MOLA 的日志查原因。"""
    procs.exit_codes["mapbuild"] = 1
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert (cfg.maps_dir / ".work" / "m1" / "mola.log").is_file()
    procs.exit_codes.clear()
    await orch.rebuild(bag, "m2")
    assert not (cfg.maps_dir / ".work").exists()


async def test_重建失败时把进程收掉_回到闲着(orch, procs, bag):
    procs.start_fails = {"mapbuild"}
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert procs.running() == []
    assert orch.phase == "idle"


async def test_建图退出码不为零算失败_说日志在哪(orch, procs, bag):
    procs.exit_codes["mapbuild"] = 1
    with pytest.raises(MappingError, match="mapbuild.*mapbuild.log"):
        await orch.rebuild(bag, "m1")
    assert procs.running() == []
    assert orch.phase == "idle"


async def test_失败原因留得下来(orch, procs, bag):
    """重建要跑几分钟,HTTP 那边早返回了,失败原因只能留着等人来问。"""
    procs.exit_codes["mapbuild"] = 3
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert "mapbuild" in orch.last_error


async def test_重建成功会清掉上一次的失败(orch, procs, bag):
    procs.exit_codes["mapbuild"] = 3
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    procs.exit_codes.clear()
    await orch.rebuild(bag, "m2")
    assert orch.last_error == ""


async def test_重建图会把旧原点作废(orch, cfg, bag):
    # 原点是标在坐标系上的。坐标系重建了,它就是错的。
    save_home(cfg.maps_dir, HomePoint(map_id="m1",
                                      pose=Pose.from_xy_yaw(1.0, 2.0),
                                      marked_at_ms=1))
    await orch.rebuild(bag, "m1")
    with pytest.raises(HomeError):
        load_home(cfg.maps_dir, "m1")


async def test_重建一张图不动别的图的原点(orch, cfg, bag):
    save_home(cfg.maps_dir, HomePoint(map_id="m2",
                                      pose=Pose.from_xy_yaw(1.0, 2.0),
                                      marked_at_ms=1))
    await orch.rebuild(bag, "m1")
    assert load_home(cfg.maps_dir, "m2").map_id == "m2"


async def test_重建半路失败了原点照样作废(orch, cfg, bag, monkeypatch):
    # 作废在重建**开始前**发生。重建崩在半路的时候,图很可能已经被覆盖了
    # 一半 —— 那时候留着旧原点是最坏的一种。往安全的那一边错。
    save_home(cfg.maps_dir, HomePoint(map_id="m1",
                                      pose=Pose.from_xy_yaw(1.0, 2.0),
                                      marked_at_ms=1))

    async def boom(name, timeout_s=None):
        raise RuntimeError("重建炸了")

    monkeypatch.setattr(orch, "_wait", boom)
    with pytest.raises(RuntimeError):
        await orch.rebuild(bag, "m1")
    with pytest.raises(HomeError):
        load_home(cfg.maps_dir, "m1")


# ------------------------------------------------------------------ 列表


def test_没有目录也列得出来是空的(orch):
    """app 要能在什么都还没建的时候起来。"""
    assert orch.list_bags() == []
    assert orch.list_maps() == []


def test_列图只算文件齐的版本目录(orch, cfg):
    from d1max_contract.maps import GEOMETRY_FILES
    for name, files in (("a", GEOMETRY_FILES), ("b", GEOMETRY_FILES[:-1]),
                        (".work", GEOMETRY_FILES)):
        d = cfg.maps_dir / name
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("x", encoding="utf-8")
    assert orch.list_maps() == ["a"], "建到一半的、中间目录都不算"


async def test_列包列的是录出来的那些(orch):
    await orch.start_record("w1")
    await orch.stop_record()
    assert orch.list_bags() == ["w1"]


def test_状态里有相位也有清单(orch):
    wire = orch.to_wire()
    assert wire["phase"] == "idle"
    assert wire["bags"] == [] and wire["maps"] == []
    assert wire["last_error"] == ""


# ------------------------------------------------------------------ HTTP


def test_日志目录_编排照进程管理器的报(orch, procs):
    """W00c6g:站点要看录包、重建子进程的日志,代理经编排找到日志目录。"""
    assert orch.log_dir == procs.log_dir


# ------------------------------------------------------------------ 边走边建(W09c2)


def test_录包经_ROS_包装脚本起(orch, cfg, tmp_path):
    """代理的服务里没有 source ROS:``ros2`` 不在 PATH 上。经 ``deploy/d1max-ros`` 起。"""
    spec = orch.spec_for_record(tmp_path / "b")
    assert spec.argv[0] == str(cfg.ros_wrapper) and spec.argv[1:4] == ("ros2", "bag", "record")


def test_在线建图默认订_reliable(tmp_path):
    """厂商前雷达发的是 reliable;best_effort 订的话开发机上丢 18% 的帧、图出重影。"""
    assert MappingConfig(bags_dir=tmp_path, maps_dir=tmp_path).live_qos == "reliable"


def test_在线建图的进程_实时域_存盘的环境变量都指向中间目录(orch, cfg):
    spec = orch.spec_for_live("m1")
    work = cfg.maps_dir / ".work" / "m1"
    assert spec.name == "molamap"
    assert spec.argv[0] == str(cfg.ros_wrapper) and spec.argv[1].endswith("mola-cli")
    assert spec.argv[2].endswith("lidar_odometry_ros2.yaml")
    env = spec.env
    assert env["ROS_DOMAIN_ID"] == "24" and env["RMW_IMPLEMENTATION"] == "rmw_zenoh_cpp"
    assert env["MOLA_LIDAR_TOPIC"] == "/front_lidar" and env["MOLA_MAPPING_ENABLED"] == "true"
    assert env["MOLA_WITH_GUI"] == "false" and env["MOLA_USE_FIXED_LIDAR_POSE"] == "true"
    assert env["MOLA_SIMPLEMAP_OUTPUT"] == str(work / "map.simplemap")
    assert env["MOLA_TUM_TRAJECTORY_OUTPUT"] == str(work / "traj.tum")
    assert env["MOLA_SAVE_MM"] == str(work / "raw_prior.mm")
    assert env["MOLA_GENERATE_SIMPLEMAP"] == "true" and env["MOLA_SAVE_TRAJECTORY"] == "true"
    assert env["MOLA_LOCAL_MAP_MAX_SIZE"] == "0"
    assert env["MOLA_STATE_ESTIMATOR_YAML"].endswith("state-estimation-simple.yaml")
    assert env["MOLA_LIDAR_QOS_RELIABILITY"] == cfg.live_qos
    import signal
    assert spec.stop_signal == signal.SIGINT, "SIGTERM 它不存盘(真实链路上踩到)"


def test_在线建图跟定位器的_MOLA_不串_话题服务换到自己的命名空间_不发_TF(orch):
    """内审阻断 1:两个 MOLA 同一份配置、同一个域 —— 位姿话题、/tf、重定位服务、/initialpose 都同名,
    定位器会收到建图那边的位姿、重定位请求可能落到建图的 MOLA 上。"""
    import json
    env = orch.spec_for_live("m1").env
    args = json.loads(env["ROS_ARGS"])
    assert args[:2] == ["-r", "__ns:=/d1max_mapping"]
    remaps = dict(a.split(":=") for a in args[1::2])
    for name in ("/initialpose", "/relocalize_near_pose", "/lidar_odometry/pose",
                 "/lidar_odometry/pose_quality"):
        assert remaps[name] == "/d1max_mapping" + name, name
    assert env["MOLA_LOCALIZATION_PUBLISH_TF"] == "false"
    # W09f:位姿发(预览要),在自己的命名空间里,不跟定位器串;/tf 照旧不发
    assert env["MOLA_LOCALIZATION_PUBLISH_ODOM_MSGS"] == "true"
    assert env["MOLA_ROS2_TRANSFORM_PUBLISH_PERIOD"] == "0"
    assert float(env["MOLA_ROS2_PUBLISH_MAPS_PERIOD"]) >= 3600, "不往 ROS 上发越来越大的地图"


async def test_边走边建_录包的同时起在线建图_先清旧的_原点作废(orch, procs, cfg):
    save_home(cfg.maps_dir, HomePoint(map_id="m1", pose=Pose.from_xy_yaw(1.0, 2.0),
                                      marked_at_ms=1))
    old = cfg.maps_dir / ".work" / "m1"
    old.mkdir(parents=True)
    (old / "traj.tum").write_text("旧的")
    (cfg.maps_dir / "m1").mkdir()
    await orch.start_record("w1", live_map_id="m1")
    assert [s.name for s in procs.started] == ["bagrecord", "molamap", "mapview"]
    assert orch.phase == "recording" and orch.live_map == "m1"
    assert not (old / "traj.tum").exists() and old.is_dir(), "清了,再建好给 MOLA 写"
    assert not (cfg.maps_dir / "m1").exists()
    with pytest.raises(HomeError):
        load_home(cfg.maps_dir, "m1")


async def test_边走边建_停录两个一起停_在线建图给足存盘时间(orch, procs, monkeypatch):
    grace = {}

    async def fake_stop(name, *, term_grace_s=3.0):
        grace[name] = term_grace_s
        procs.stopped.append(name)
    monkeypatch.setattr(procs, "stop", fake_stop)
    await orch.start_record("w1", live_map_id="m1")
    await orch.stop_record()
    assert set(grace) == {"bagrecord", "molamap", "mapview"} and grace["molamap"] >= 120
    assert orch.phase == "idle" and orch.live_map is None


async def test_在线建图起来就退了_录包也收掉_说清楚(orch, procs, monkeypatch):
    """内审应修 2:原来起来就算,MOLA 一起来就退(插件、路径、QoS 不对)要等人走完一圈停下才知道。"""
    real = procs.start

    async def 起来就退(spec):
        await real(spec)
        if spec.name == "molamap":
            procs.stopped.append("molamap")               # FakeProcs:当它退了
    monkeypatch.setattr(procs, "start", 起来就退)
    with pytest.raises(MappingError, match="起来就退了"):
        await orch.start_record("w1", live_map_id="m1")
    assert procs.running() == [] and orch.phase == "idle"


async def test_开录时在中间目录记下在线建图的参数_打包抄进_build_json(orch, cfg):
    """内审小 1:``--reuse`` 打包时中间目录里没有 mola.json,build.json 里不知道是在线建的、
    用的什么参数。"""
    import json
    await orch.start_record("w1", live_map_id="m1")
    m = json.loads((cfg.maps_dir / ".work" / "m1" / "mola.json").read_text())
    assert m["mode"] == "live" and m["env"]["MOLA_LIDAR_QOS_RELIABILITY"] == cfg.live_qos
    assert m["pipeline"].endswith("lidar_odometry_ros2.yaml")


async def test_在线建图起不来_录包也收掉_说清楚(orch, procs):
    procs.start_fails = {"molamap"}
    with pytest.raises(MappingError, match="molamap"):
        await orch.start_record("w1", live_map_id="m1")
    assert procs.running() == [] and orch.phase == "idle"


# ------------------------------------------------------------------ 建图预览(W09f)


def test_预览进程_实时域_写到中间目录_订建图那边的位姿(orch, cfg):
    spec = orch.spec_for_preview("m1", "w1")
    assert spec.name == "mapview"
    assert spec.argv[0] == str(cfg.live_preview)
    a = spec.argv

    def arg(k):
        return a[a.index(k) + 1]
    assert arg("--out") == str(cfg.maps_dir / ".work" / "m1" / "preview")
    assert orch.preview_dir("m1") == cfg.maps_dir / ".work" / "m1" / "preview"
    assert arg("--run") == "w1"
    assert arg("--lidar-topic") == cfg.lidar_topic
    assert arg("--pose-topic") == "/d1max_mapping/lidar_odometry/pose"
    assert spec.env["ROS_DOMAIN_ID"] == "24" and spec.env["RMW_IMPLEMENTATION"] == "rmw_zenoh_cpp"
    assert spec.ready_pattern == "", "不等它就绪"
    assert MappingConfig(bags_dir=cfg.bags_dir, maps_dir=cfg.maps_dir).live_preview == Path(
        "/opt/d1max/current/deploy/d1max-live-preview")


async def test_只录包不起预览(orch, procs):
    await orch.start_record("w1")
    assert [s.name for s in procs.started] == ["bagrecord"]
    assert orch.preview_error == ""


async def test_预览起不来_开录照样成_记下原因(orch, procs):
    procs.start_fails = {"mapview"}
    await orch.start_record("w1", live_map_id="m1")
    assert orch.phase == "recording" and orch.live_map == "m1"
    assert procs.running() == ["bagrecord", "molamap"]
    assert "mapview" in orch.preview_error


async def test_在线建图起不来_预览不起(orch, procs):
    procs.start_fails = {"molamap"}
    with pytest.raises(MappingError):
        await orch.start_record("w1", live_map_id="m1")
    assert "mapview" not in [s.name for s in procs.started]


async def test_预览停不掉不碍停录(orch, procs, monkeypatch):
    stopped = []

    async def fake_stop(name, *, term_grace_s=3.0):
        stopped.append(name)
        if name == "mapview":
            raise OSError("停不了")
    await orch.start_record("w1", live_map_id="m1")
    monkeypatch.setattr(procs, "stop", fake_stop)
    bag = await orch.stop_record()
    assert bag.name == "w1" and set(stopped) == {"bagrecord", "molamap", "mapview"}
    assert orch.phase == "idle"


async def test_再开一趟_上一趟预览起不来的原因清掉(orch, procs):
    procs.start_fails = {"mapview"}
    await orch.start_record("w1", live_map_id="m1")
    await orch.stop_record()
    procs.start_fails = set()
    procs.stopped.clear()                                 # FakeProcs 按名字记「停过」
    await orch.start_record("w2", live_map_id="m1")
    assert orch.preview_error == ""


async def test_打包在线建好的_不重跑_MOLA_不清中间目录(orch, procs, cfg, bag, monkeypatch):
    work = cfg.maps_dir / ".work" / "m1"
    work.mkdir(parents=True)
    (work / "traj.tum").write_text("在线建的")
    seen = []
    real = procs.start

    async def 看(spec):
        seen.append((work / "traj.tum").exists())
        await real(spec)
    monkeypatch.setattr(procs, "start", 看)
    out = await orch.package(bag, "m1")
    assert seen == [True], "打包起来的时候在线建好的还在"
    spec = procs.started[-1]
    assert spec.name == "mappack" and spec.argv[-1] == "--reuse", "自己一份日志:退回重建不盖掉它"
    assert spec.argv[spec.argv.index("--work") + 1] == str(work)
    assert out == cfg.maps_dir / "m1" and orch.phase == "idle"
    assert not work.exists(), "打好了:中间文件不留"


async def test_打包失败留着中间目录_说日志在哪(orch, procs, cfg, bag):
    work = cfg.maps_dir / ".work" / "m1"
    work.mkdir(parents=True)
    procs.exit_codes["mappack"] = 2
    with pytest.raises(MappingError, match="mappack"):
        await orch.package(bag, "m1")
    assert work.is_dir() and orch.phase == "idle"


async def test_停录一个停不掉_另一个照样停_回到闲着(orch, procs, monkeypatch):
    """外审阻断 1:停录时录包那边炸了,在线建图也要尽力收掉。"""
    stopped = []

    async def fake_stop(name, *, term_grace_s=3.0):
        stopped.append(name)
        if name == "bagrecord":
            raise OSError("停不了")
    await orch.start_record("w1", live_map_id="m1")
    monkeypatch.setattr(procs, "stop", fake_stop)
    with pytest.raises(OSError):
        await orch.stop_record()
    assert set(stopped) == {"bagrecord", "molamap", "mapview"} and orch.phase == "idle"


# ------------------------------------------------------------------ 停录部分失败(外审复查阻断)


async def test_停录部分失败_服务层跟编排一起回到不在录_边走边建的照样待打包(orch, procs, cfg,
                                                          monkeypatch, tmp_path):
    """外审复查:编排先回闲着再抛其中一个停止异常,服务层原来只有正常返回才清 ``recording`` ——
    上层「在录」、下层「闲着」,之后停不了、也开不了,只能重启。按真的语义:两个停止都做过、编排已回
    闲着,再抛。"""
    from d1max_agent.mapping import DONE, LIVE, MappingService
    svc = MappingService(orch, bags_root=cfg.bags_dir, maps_out=tmp_path / "outbox" / "maps")
    await svc.start("yard", target=("m1", "5"), task_id="t1")
    bag = cfg.bags_dir / svc.last_bag
    real = procs.stop

    async def 录包停不干净(name, *, term_grace_s=3.0):
        await real(name, term_grace_s=term_grace_s)
        if name == "bagrecord":
            raise OSError("mcap 索引没写完")
    monkeypatch.setattr(procs, "stop", 录包停不干净)
    with pytest.raises(OSError):
        await svc.stop()
    assert set(procs.stopped) >= {"bagrecord", "molamap"} and orch.phase == "idle"
    assert not svc.recording and svc.live is None, "上下层一致:都不在录"
    assert svc.pending == (bag.name, "m1", "5") and svc.pending_task == "t1", "照样待打包(不丢)"
    assert (bag / DONE).is_file() and (bag / LIVE).is_file()
    monkeypatch.setattr(procs, "stop", real)
    with pytest.raises(Exception, match="还在打包"):
        await svc.start("yard2")                       # 收尾之前不开新的(上一趟还没交代)
    svc.pending = None                                 # (收尾在代理的后台槽里做,这里当它收完了)
    await svc.start("yard2")
    assert svc.recording and orch.phase == "recording"


async def test_停录部分失败_只录包的_之后马上能再开(orch, procs, cfg, monkeypatch, tmp_path):
    from d1max_agent.mapping import MappingService
    svc = MappingService(orch, bags_root=cfg.bags_dir, maps_out=tmp_path / "outbox" / "maps")
    await svc.start("yard")
    real = procs.stop

    async def 停不干净(name, *, term_grace_s=3.0):
        await real(name, term_grace_s=term_grace_s)
        raise OSError("停不了")
    monkeypatch.setattr(procs, "stop", 停不干净)
    with pytest.raises(OSError):
        await svc.stop()
    monkeypatch.setattr(procs, "stop", real)
    assert not svc.recording and svc.pending is None
    await svc.start("yard2")
    assert svc.recording


async def test_编排说没在录_服务层也不在录(orch, cfg, tmp_path):
    """上下层万一对不上(编排已经闲着),停录也要让服务层回到不在录,不卡住。"""
    from d1max_agent.mapping import MappingService
    svc = MappingService(orch, bags_root=cfg.bags_dir, maps_out=tmp_path / "outbox" / "maps")
    await svc.start("yard")
    await orch.stop_record()                           # 下层先停了
    with pytest.raises(Exception, match="没在录"):
        await svc.stop()
    assert not svc.recording
    await svc.start("yard2")
