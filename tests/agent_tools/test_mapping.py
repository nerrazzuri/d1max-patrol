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
        if spec.name == "mapbuild":
            # 真的建图脚本会在 ``--work`` 里落 MOLA 的日志、中间文件。
            work = Path(spec.argv[spec.argv.index("--work") + 1])
            work.mkdir(parents=True, exist_ok=True)        # 边走边建的:中间目录本来就在
            (work / "mola.log").write_text("建图日志", encoding="utf-8")
        self.started.append(spec)

    async def stop(self, name: str, *, term_grace_s: float = 3.0) -> None:
        self.stopped.append(name)

    async def wait(self, name: str, timeout_s: float | None = None) -> int:
        return self.exit_codes.get(name, 0)


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


async def test_边走边建_录包的同时起在线建图_先清旧的_原点作废(orch, procs, cfg):
    save_home(cfg.maps_dir, HomePoint(map_id="m1", pose=Pose.from_xy_yaw(1.0, 2.0),
                                      marked_at_ms=1))
    old = cfg.maps_dir / ".work" / "m1"
    old.mkdir(parents=True)
    (old / "traj.tum").write_text("旧的")
    (cfg.maps_dir / "m1").mkdir()
    await orch.start_record("w1", live_map_id="m1")
    assert [s.name for s in procs.started] == ["bagrecord", "molamap"]
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
    assert set(grace) == {"bagrecord", "molamap"} and grace["molamap"] >= 120
    assert orch.phase == "idle" and orch.live_map is None


async def test_在线建图起不来_录包也收掉_说清楚(orch, procs):
    procs.start_fails = {"molamap"}
    with pytest.raises(MappingError, match="molamap"):
        await orch.start_record("w1", live_map_id="m1")
    assert procs.running() == [] and orch.phase == "idle"


async def test_打包在线建好的_不重跑_MOLA_不清中间目录(orch, procs, cfg, bag):
    work = cfg.maps_dir / ".work" / "m1"
    work.mkdir(parents=True)
    (work / "traj.tum").write_text("在线建的")
    out = await orch.package(bag, "m1")
    spec = procs.started[-1]
    assert spec.name == "mapbuild" and spec.argv[-1] == "--reuse"
    assert spec.argv[spec.argv.index("--work") + 1] == str(work)
    assert out == cfg.maps_dir / "m1" and orch.phase == "idle"
    assert not work.exists(), "打好了:中间文件不留"


async def test_打包失败留着中间目录_说日志在哪(orch, procs, cfg, bag):
    work = cfg.maps_dir / ".work" / "m1"
    work.mkdir(parents=True)
    procs.exit_codes["mapbuild"] = 2
    with pytest.raises(MappingError, match="mapbuild"):
        await orch.package(bag, "m1")
    assert work.is_dir() and orch.phase == "idle"
