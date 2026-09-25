"""建图编排:录包 → 隔离域重建 → 存图。

**这里一个 ROS 进程都不真起。** 断言的对象是**生成出来的 ``ProcSpec``** ——
命令行、环境变量、起的顺序。理由有两条:一是 CI 和这台开发机上根本没有
ROS;二是就算有,一次真重建要跑几分钟,那不是单元测试该干的事。

真机上验的是另一件事(命令敲下去到底能不能跑通),那个由
``docs/建图定位与巡检管线.md`` 和现场手册负责 —— 这一组测试保证的是
"app 发出去的命令和文档里那串**一字不差**"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d1max_agent.engine.homing import HomeError, HomePoint, load_home, save_home
from d1max_patrol.app.mapping import (
    RECORD_TOPICS,
    MappingConfig,
    MappingError,
    MappingOrchestrator,
)
from d1max_patrol.app.procs import ProcError, ProcSpec
from d1max_patrol.protocol.nav_types import Pose

_PARAMS = """\
slam_toolbox:
  ros__parameters:
    mode: mapping
    scan_topic: /scan_3d
    repo: <REPO>
"""


class FakeProcs:
    """假进程管理器。只记账,不起进程。"""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
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
    params = tmp_path / "mapper_3d.yaml"
    params.write_text(_PARAMS, encoding="utf-8")
    return MappingConfig(
        bags_dir=tmp_path / "bags",
        maps_dir=tmp_path / "maps",
        params_template=params,
        work_dir=tmp_path / "work",
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


# ------------------------------------------------------------------ 域与 RMW


def test_重建用的是隔离域和FastDDS(orch, tmp_path):
    """#63:zenoh 传不了 latched 话题,``/map`` 根本发不出去。离线重建整个绕开它。"""
    for spec in orch.specs_for_rebuild(tmp_path / "b", "m1"):
        assert spec.env["ROS_DOMAIN_ID"] == "93"
        assert spec.env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
        assert spec.env["ROS_LOCALHOST_ONLY"] == "1"


def test_录包用的是实时域(orch, tmp_path):
    """录包要听真机现在发的话题,那是实时域,不能跟着重建跑去 93。"""
    spec = orch.spec_for_record(tmp_path / "b")
    assert spec.env["ROS_DOMAIN_ID"] == "24"
    assert spec.env["RMW_IMPLEMENTATION"] == "rmw_zenoh_cpp"


def test_存图也在隔离域里(orch):
    """存图问的是重建那个 slam_toolbox,跑错域就是问了个不存在的服务。"""
    for spec in orch.specs_for_save("m1"):
        assert spec.env["ROS_DOMAIN_ID"] == "93"
        assert spec.env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"


def test_域是可配的(procs, cfg, tmp_path):
    other = MappingOrchestrator(
        procs, MappingConfig(bags_dir=cfg.bags_dir, maps_dir=cfg.maps_dir,
                             live_domain="7", offline_domain="8"))
    assert other.spec_for_record(tmp_path / "b").env["ROS_DOMAIN_ID"] == "7"
    assert other.specs_for_rebuild(tmp_path / "b", "m")[0].env["ROS_DOMAIN_ID"] == "8"


# ------------------------------------------------------------------ 命令本身


def test_重建要起的四个进程一个不少(orch, tmp_path):
    names = {s.name for s in orch.specs_for_rebuild(tmp_path / "b", "m1")}
    assert names == {"slam", "static_tf", "pc2scan", "bagplay"}


def test_重建全程开着仿真时间(orch, tmp_path):
    """放包放的是过去的时间戳,不开 use_sim_time 建出来的图是错的。"""
    specs = orch.specs_for_rebuild(tmp_path / "b", "m1")
    for name in ("slam", "pc2scan"):
        assert "use_sim_time:=true" in _argv(specs, name)


def test_放包的进程最后起(orch, tmp_path):
    """先起 slam 再放包 —— 反过来前几秒的数据就喂给空气了,而那是图的起点。"""
    assert orch.specs_for_rebuild(tmp_path / "b", "m1")[-1].name == "bagplay"


def test_放包还留了四秒的余量(orch, tmp_path):
    """``--delay 4`` 是"最后起"之外的第二道保险,文档 §2 里就有。"""
    specs = orch.specs_for_rebuild(tmp_path / "b", "m1")
    assert "--delay 4" in _argv(specs, "bagplay")
    assert "--clock" in _argv(specs, "bagplay")


def test_点云压平接的是雷达话题(orch, tmp_path):
    specs = orch.specs_for_rebuild(tmp_path / "b", "m1")
    line = _argv(specs, "pc2scan")
    assert "cloud_in:=/front_lidar" in line
    assert "scan:=/scan_3d" in line


def test_雷达外参照抄文档(orch, tmp_path):
    """#64 的根治手段是把 ``--yaw`` 改成 3.14159,但默认得跟文档一致。"""
    line = _argv(orch.specs_for_rebuild(tmp_path / "b", "m1"), "static_tf")
    assert "--x 0.2" in line and "--z 0.18" in line
    assert "--yaw 0" in line
    assert "--frame-id base_link --child-frame-id rslidar_head" in line


def test_雷达朝向可以一行配置改掉(procs, cfg, tmp_path):
    """#64 真机验证之后要改的就这一个数,不该去动命令行拼接。"""
    from dataclasses import replace
    other = MappingOrchestrator(procs, replace(cfg, lidar_yaw=3.14159))
    assert "--yaw 3.14159" in _argv(
        other.specs_for_rebuild(tmp_path / "b", "m1"), "static_tf")


def test_录包录的是那五个话题(orch, tmp_path):
    line = _argv([orch.spec_for_record(tmp_path / "b")], "bagrecord")
    for topic in RECORD_TOPICS:
        assert topic in line
    assert "-s mcap" in line, "mcap 是文档定的格式,换了格式回放的工具就对不上"


def test_存图两步都在(orch):
    """``map_saver_cli`` 出 pgm+yaml 给导航用,``serialize_map`` 出 posegraph
    给定位用。少了后者,slam_toolbox 的 localization 模式就没得读。"""
    names = [s.name for s in orch.specs_for_save("m1")]
    assert names == ["map_saver", "serialize"]
    joined = " ".join(" ".join(s.argv) for s in orch.specs_for_save("m1"))
    assert "map_saver_cli" in joined
    assert "/slam_toolbox/serialize_map" in joined


def test_存图路径里不带反斜杠(orch):
    """ROS 的服务参数是 YAML,反斜杠在里面是转义符 —— Windows 路径会被吃掉。"""
    argv = " ".join(orch.specs_for_save("m1")[-1].argv)
    assert "\\" not in argv


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


async def test_图名不合法被拒(orch, bag):
    for bad in ("../x", "a/b", ""):
        with pytest.raises(MappingError):
            await orch.rebuild(bag, bad)


# ------------------------------------------------------------------ 重建全程


async def test_重建按顺序起了那四个(orch, procs, bag):
    await orch.rebuild(bag, "m1")
    started = [s.name for s in procs.started]
    assert started[:4] == ["slam", "static_tf", "pc2scan", "bagplay"]


async def test_重建完了会存图也会序列化(orch, procs, bag):
    await orch.rebuild(bag, "m1")
    started = [s.name for s in procs.started]
    assert started[-2:] == ["map_saver", "serialize"]


async def test_重建完了回到闲着并且把进程都收了(orch, procs, bag):
    await orch.rebuild(bag, "m1")
    assert orch.phase == "idle"
    assert procs.running() == []


async def test_重建返回的是图的路径前缀(orch, bag, cfg):
    assert await orch.rebuild(bag, "m1") == cfg.maps_dir / "m1"


async def test_重建失败时把起的进程都收掉(orch, procs, bag):
    """留一个 slam_toolbox 在隔离域里空转,下一次重建会因为名字撞了直接
    失败,而现场根本看不出为什么。"""
    procs.start_fails = {"bagplay"}
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert procs.running() == []
    assert orch.phase == "idle"


async def test_放包退出码不为零算失败(orch, procs, bag):
    procs.exit_codes["bagplay"] = 1
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert procs.running() == []


async def test_存图失败也算失败(orch, procs, bag):
    """图没落盘就说建好了,是把"没有图"伪装成"有图"。"""
    procs.exit_codes["map_saver"] = 2
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert "serialize" not in [s.name for s in procs.started]


async def test_失败原因留得下来(orch, procs, bag):
    """重建要跑几分钟,HTTP 那边早返回了,失败原因只能留着等人来问。"""
    procs.exit_codes["bagplay"] = 3
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert "bagplay" in orch.last_error


async def test_重建成功会清掉上一次的失败(orch, procs, bag):
    procs.exit_codes["bagplay"] = 3
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


# ------------------------------------------------------------------ 参数文件


async def test_参数文件被挪到不带空格的路径下(orch, procs, bag, cfg):
    """本仓库路径里有空格("D1 Max"),而 ROS 传参数文件对这个很敏感。"""
    await orch.rebuild(bag, "m1")
    line = _argv(procs.started, "slam")
    params = line.split("--params-file ")[1].split(" ")[0]
    assert " " not in params
    assert Path(params).is_file()


async def test_参数文件里的REPO被换成真路径(orch, procs, bag, cfg):
    await orch.rebuild(bag, "m1")
    text = (cfg.work_dir / "mapper_3d.yaml").read_text(encoding="utf-8")
    assert "<REPO>" not in text


async def test_参数模板不在就直接失败(procs, cfg, bag):
    from dataclasses import replace
    orch = MappingOrchestrator(
        procs, replace(cfg, params_template=cfg.bags_dir / "没有这个.yaml"))
    with pytest.raises(MappingError):
        await orch.rebuild(bag, "m1")
    assert procs.started == [], "参数都没准备好就不该起进程"


# ------------------------------------------------------------------ 列表


def test_没有目录也列得出来是空的(orch):
    """app 要能在什么都还没建的时候起来。"""
    assert orch.list_bags() == []
    assert orch.list_maps() == []


def test_列图只算有posegraph的(orch, cfg):
    cfg.maps_dir.mkdir(parents=True)
    for name in ("a.pgm", "a.yaml", "a.posegraph", "b.pgm"):
        (cfg.maps_dir / name).write_text("x", encoding="utf-8")
    assert orch.list_maps() == ["a"], "只有 pgm 的图定位模式读不了,不算数"


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
