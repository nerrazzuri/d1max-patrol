"""狗上证据加密(W30b,决策 51),狗这一头:照片直接封(明文不落盘、返回原名);录像段封着挪进发件箱;
有站点公钥才封,没有照旧明文、能力里报没封。"""
# ruff: noqa: F811  (录是从 test_recording 借来的夹具)

from __future__ import annotations

import pytest
from test_recording import _段, 录  # noqa: F401

from d1max_agent.engine.archive import RunArchive
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_agent.recording import STAGING
from d1max_contract import evseal as E
from d1max_patrol.protocol.nav_types import Pose


@pytest.fixture
def 钥匙():
    priv, pub = E.keypair()
    yield priv, E.Sealer(pub)
    RunArchive.sealer = None


def _一趟(tmp_path):
    m = Mission(mission="m1", map_id="m", policy=Policy(photo_optional=True),
                waypoints=(MissionWaypoint(name="P1", pose=Pose.from_xy_yaw(0, 0)),))
    return RunArchive(tmp_path / "runs", m)


def test_配了公钥_照片直接封_盘上只有d1e_返回原名_站点解得开(tmp_path, 钥匙):
    priv, sealer = 钥匙
    RunArchive.sealer = sealer
    arch = _一趟(tmp_path)
    p = arch.save_photo("P1", "front", b"\xff\xd8 face " * 500)
    arch.close()
    assert p.name.endswith(".jpg"), "事件、汇总里记原名"
    files = [x.name for x in p.parent.iterdir()]
    assert files == [p.name + ".d1e"], files
    E.open_file(p.with_name(p.name + ".d1e"), tmp_path / "out.jpg", priv)
    assert (tmp_path / "out.jpg").read_bytes() == b"\xff\xd8 face " * 500


def test_没配公钥_照旧明文(tmp_path):
    RunArchive.sealer = None
    arch = _一趟(tmp_path)
    p = arch.save_photo("P1", "front", b"plain")
    arch.close()
    assert p.read_bytes() == b"plain"


def test_封不了_照片算存不下_不留半截(tmp_path, 钥匙):
    class 坏:
        def seal_bytes(self, data, dst):
            raise E.SealError("openssl 不在")
    RunArchive.sealer = 坏()
    arch = _一趟(tmp_path)
    with pytest.raises(OSError, match="openssl"):
        arch.save_photo("P1", "front", b"x")
    arch.close()
    assert not list((arch.path / "photos").iterdir()), "封不了:照片目录里什么都不留(不落明文)"


def test_录像段封着挪进发件箱_暂存明文删掉(录, 钥匙):
    priv, sealer = 钥匙
    r = 录
    r.sealer = sealer
    r.start()
    _段(r, "front", "20261009T010000Z", 4000)
    _段(r, "front", "20261009T010100Z")                   # 正在写:不碰
    r.step()
    seg = r.out / "front" / "20261009T010000Z"
    assert [p.name for p in seg.iterdir()] == ["video.mp4.d1e"]
    assert not (r.root / STAGING / "front" / "20261009T010000Z.mp4").exists()
    E.open_file(seg / "video.mp4.d1e", r.root / "x.mp4", priv)
    assert (r.root / "x.mp4").stat().st_size == 4000


def test_代理_有公钥文件能力报封了_没有报没封(tmp_path, 钥匙):
    from test_agent_main import _args

    from d1max_agent import main as agent_main
    priv, _ = 钥匙
    pubf = tmp_path / "pub.key"
    pubf.write_text(E.public_key(priv).hex())
    for given, want in ((pubf, True), (tmp_path / "没有", False)):
        a = _args(tmp_path, "--evidence-pub", str(given))
        asm = agent_main.build(a)
        try:
            assert asm.runtime.evidence_sealed is want
            assert (RunArchive.sealer is not None) is want
        finally:
            asm.stop()


# ------------------------------------------------------------ W30b 外审 1


class 坏封:
    def seal_file(self, src, dst):
        raise E.SealError("openssl 坏了")


def test_外审1_录像段封不上_留在暂存不进发件箱_记着_修好了下一拍封(录, 钥匙):
    _priv, sealer = 钥匙
    r = 录
    r.sealer = 坏封()
    r.start()
    _段(r, "front", "20261009T010000Z", 400)
    _段(r, "front", "20261009T010100Z")                   # 正在写
    r.step()
    assert not list(r.out.rglob("video.mp4*")), "封不上:不进发件箱(不传明文)"
    assert (r.root / STAGING / "front" / "20261009T010000Z.mp4").exists()
    assert list(r.seal_failed) == ["front/20261009T010000Z"]
    r.sealer = sealer
    r.step()
    assert not r.seal_failed
    assert [p.name for p in (r.out / "front" / "20261009T010000Z").iterdir()] == ["video.mp4.d1e"]


def test_外审1_封不上的段也算配额_攒多了从最旧的删_报一次(录):
    r = 录
    r.sealer = 坏封()
    r.start()
    for m in range(6):
        _段(r, "front", f"20261009T01{m:02d}00Z", 3000)    # 配额 10000:扣着的 5 段 15000
    r.step()
    left = sorted(p.stem for p in (r.root / STAGING / "front").iterdir())
    assert left == ["20261009T010200Z", "20261009T010300Z", "20261009T010400Z",
                    "20261009T010500Z"], left              # 删了最旧两段;最新那段正在写,不碰
    assert [k for k, _ in r.events].count("recording_dropped") == 1
    assert sorted(r.seal_failed) == [f"front/20261009T01{m:02d}00Z" for m in (2, 3, 4)]


def test_复查_代理_遥测的盘况带封不上的件数_没配加密不带():
    from types import SimpleNamespace

    from d1max_agent.runtime import AgentRuntime
    n = [(2, "openssl 坏了")]
    rt = SimpleNamespace(_unsealed=lambda: n[0], _unsealed_told=0,
                         recorder=SimpleNamespace(seal_failed={"front/x": "坏了"}))
    assert AgentRuntime._unsealed_now(rt) == 3
    n[0] = (0, "")
    rt.recorder.seal_failed = {}
    assert AgentRuntime._unsealed_now(rt) == 0, "都封好了报 0(站点据此解决告警)"
    rt._unsealed = None
    assert AgentRuntime._unsealed_now(rt) is None
