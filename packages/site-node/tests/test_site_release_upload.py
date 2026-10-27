"""W44 第四期:网页上传升级包(再输一次密码、照旧验签、说人话)、等充完电空闲时再换(排队)。"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import urllib.request
from types import SimpleNamespace

import pytest
from test_site_api import PW, 站
from test_site_releases import NAME, 做包

from d1max_contract import relsign
from d1max_site.db import SiteDB
from d1max_site.release_queue import MIN_BATTERY, ReleaseQueue


def _签(d, key):
    raw = json.loads((d / "release.json").read_text())
    raw["signature"] = relsign.sign(raw, key)
    (d / "release.json").write_text(json.dumps(raw))


def _打包(d) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(d, arcname=d.name)
    return buf.getvalue()


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, releases={})
    s.accounts.add("gina", PW, role="guard")
    home = tmp_path / "home"
    home.mkdir()
    relsign.keygen(tmp_path / "keys" / "rel.key", tmp_path / "keys" / "rel.pub")
    pub = str(tmp_path / "keys" / "rel.pub")
    (home / "site.json").write_text(json.dumps({"release_pubkey": pub}))
    s.api.home = home
    s.key = tmp_path / "keys" / "rel.key"
    yield s
    s.close()


def _登(s, name="alice"):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _传(s, tok, data: bytes, pw: str | None = PW):
    r = urllib.request.Request(s.api.url + "/api/releases/upload", data=data, method="POST")
    r.add_header("Content-Type", "application/octet-stream")
    r.add_header("Authorization", f"Bearer {tok}")
    if pw is not None:
        r.add_header("X-D1Max-Confirm", pw)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_上传登记(站点, tmp_path):
    s = 站点
    d = 做包(tmp_path)
    _签(d, s.key)
    tok = _登(s)
    code, got = _传(s, tok, _打包(d))
    assert code == 200, got
    assert got["release"]["name"] == NAME
    assert [r["name"] for r in s.req("GET", "/api/releases", token=tok)[1]["releases"]] == [NAME]
    code, got = _传(s, tok, _打包(d))
    assert code == 409 and "已经有了" in got["error"]


def test_签名不对拒(站点, tmp_path):
    s = 站点
    d = 做包(tmp_path)
    relsign.keygen(tmp_path / "other.key", tmp_path / "other.pub")
    _签(d, tmp_path / "other.key")
    code, got = _传(s, _登(s), _打包(d))
    assert code == 400 and "签名对不上" in got["error"]


def test_包坏了说人话(站点, tmp_path):
    s = 站点
    d = 做包(tmp_path, sha="0" * 64)
    _签(d, s.key)
    code, got = _传(s, _登(s), _打包(d))
    assert code == 400 and "坏了" in got["error"]
    code, got = _传(s, _登(s), b"not a tarball")
    assert code == 400


def test_包里有往外跳的路径拒(站点, tmp_path):
    s = 站点
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"bad"))
    code, _ = _传(s, _登(s), buf.getvalue())
    assert code == 400
    assert not (tmp_path / "evil.txt").exists()


def test_要再输密码_保安不行(站点, tmp_path):
    s = 站点
    d = 做包(tmp_path)
    _签(d, s.key)
    assert _传(s, _登(s), _打包(d), pw=None)[0] == 403
    assert _传(s, _登(s), _打包(d), pw="wrong-wrong-wrong")[0] == 403
    assert _传(s, _登(s, "gina"), _打包(d))[0] == 403


# ------------------------------------------------------------ 等充完电空闲时换


class 假派遣:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.clients: dict = {}
        self.fresh: set[str] = set()

    def _fresh(self, c) -> bool:
        return c is not None and c.rid in self.fresh

    async def map_command(self, rid, kind, payload, *, issued_by):
        self.sent.append((rid, kind, payload))
        return {"ack": {"result": "accepted"}}


def _狗(d, rid="A", battery=90.0, task=None):
    d.clients[rid] = SimpleNamespace(rid=rid, status=SimpleNamespace(task=task),
                                     telemetry=SimpleNamespace(battery_pct=battery))
    d.fresh.add(rid)


def test_充完电空闲才换_换了出队(tmp_path):
    d = 假派遣()
    q = ReleaseQueue(SiteDB(tmp_path / "s.db"), d, now_ms=lambda: 1, payload=lambda n: {"name": n})
    q.add("A", NAME, by="alice")
    assert [r["robot_id"] for r in q.list()] == ["A"]
    asyncio.run(q.tick())
    assert d.sent == []                                     # 不在线
    _狗(d, battery=MIN_BATTERY - 1)
    asyncio.run(q.tick())
    assert d.sent == []                                     # 电不够
    _狗(d, battery=95, task={"kind": "patrol"})
    asyncio.run(q.tick())
    assert d.sent == []                                     # 有任务
    _狗(d, battery=95)
    asyncio.run(q.tick())
    assert d.sent == [("A", "release_activate", {"name": NAME})]
    assert q.list() == []
    asyncio.run(q.tick())
    assert len(d.sent) == 1


def test_接口_等充电时换是排队(站点, tmp_path):
    s = 站点
    d = 做包(tmp_path)
    _签(d, s.key)
    tok = _登(s)
    assert _传(s, tok, _打包(d))[0] == 200
    code, got = s.req("POST", "/api/robots/A/release", {"action": "activate", "name": NAME,
                                                        "when": "charging"}, token=tok)
    assert code == 200 and got["queued"] is True
    assert [r["robot_id"] for r in s.api.release_queue.list()] == ["A"]
