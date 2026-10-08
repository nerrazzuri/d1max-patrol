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
