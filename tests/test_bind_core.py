"""W42 配网服务的状态机:绑定、换 WiFi(站点确认)、解绑清机、解绑令。网络、路由器、时间都是假的。"""

from __future__ import annotations

import base64

import pytest

from d1max_contract import binding as b
from d1max_contract import ed25519
from d1max_patrol.bind import core as c
from d1max_patrol.bind.paths import BindPaths, read_json, write_json
from d1max_patrol.bind.router import FakeRouter
from d1max_patrol.bind.sitelink import LinkError

CODE = "7KQ27M9XD4HTPR8W"
SITE = {"url": "https://192.168.1.10:8443", "fp": "ab" * 32, "name": "庄园"}
PEM = (b"-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"cert-der") +
       b"\n-----END CERTIFICATE-----\n")


class 假站点:
    def __init__(self):
        self.up = True
        self.answer = {"state": "pending"}
        self.cert = "active"
        self.requests = []

    def _up(self):
        if not self.up:
            raise LinkError("无法连接")

    def request(self, site, body):
        self._up()
        self.requests.append(body)
        return {"request_id": "r1", "poll_secret": "s1"}

    def poll(self, site, rid, secret):
        self._up()
        return self.answer

    def cert_state(self, site, serial, cert):
        self._up()
        return self.cert


class 台:
    def __init__(self, tmp_path, *, has_4g=False):
        self.paths = BindPaths(etc=tmp_path / "etc", data_root=tmp_path / "data")
        self.paths.data_root.mkdir(parents=True)
        write_json(self.paths.device, {"serial": "D1M-1", "code_hash": b.code_hash(CODE),
                                       "model": "D1 Max"})
        self.t = 1_000_000
        self.router = FakeRouter(reachable={"Estate", "New"}, has_4g=has_4g)
        self.site = 假站点()
        self.provisioned, self.started, self.wiped = [], 0, 0
        self.prov_fail = 0
        self.wipe_fails: list[str] = []
        self.core = c.BindCore(self.paths, router=self.router, site=self.site,
                               provision=self._prov, wipe=self._wipe,
                               start_services=self._start, now_ms=lambda: self.t, version="9")

    def _prov(self, code):
        self.provisioned.append(code)
        if self.prov_fail:
            self.prov_fail -= 1
            raise RuntimeError("连接已断开")
        self.paths.robot_cert.parent.mkdir(parents=True, exist_ok=True)
        self.paths.robot_cert.write_bytes(PEM)
        return "D1M-1"

    def _wipe(self):
        self.wiped += 1
        return list(self.wipe_fails)

    def _start(self):
        self.started += 1

    def tick(self, dt=0):
        self.t += dt
        self.core.tick()

    def bound(self):
        self.core.bind(CODE, {"wifi": {"ssid": "Estate", "psk": "12345678"}, "site": SITE})
        self.tick()
        self.site.answer = {"state": "approved", "code": "D1MAX1.x"}
        self.tick(c.POLL_MS)
        assert self.core.s["state"] == "bound"


@pytest.fixture
def 机(tmp_path):
    return 台(tmp_path)


def test_绑定码错五次锁十分钟(机):
    for _ in range(c.MAX_FAILS):
        with pytest.raises(c.BindRefused) as e:
            机.core.status("0000000000000000")
        assert e.value.status == 403
    with pytest.raises(c.BindRefused) as e:
        机.core.status(CODE)
    assert e.value.status == 429
    机.t += c.LOCK_MS + 1
    assert 机.core.status(CODE.lower())["state"] == "unbound"


def test_绑定_挂申请_等批_批了领证书_起服务(机):
    st = 机.core.bind(CODE, {"wifi": {"ssid": "Estate", "psk": "12345678"}, "site": SITE})
    assert st["state"] == "pending" and st["phase"] == "connecting"
    assert 机.router.ssid == "Estate"
    机.tick()
    assert 机.site.requests[0]["serial"] == "D1M-1" and 机.site.requests[0]["version"] == "9"
    st = 机.core.status(CODE)
    assert st["phase"] == "waiting"
    assert st["check"] == b.check_digits(机.site.requests[0]["nonce"])
    机.tick(1000)                      # 没到 5 秒不问
    机.site.answer = {"state": "approved", "code": "D1MAX1.x"}
    机.tick(c.POLL_MS)
    assert 机.provisioned == ["D1MAX1.x"] and 机.started == 1
    st = 机.core.status(CODE)
    assert st["state"] == "bound" and st["site_name"] == "庄园"
    s = read_json(机.paths.state)
    assert s["wifi"]["psk"] == "12345678"
    assert 机.paths.state.stat().st_mode & 0o077 == 0, "里面有 WiFi 密码:只许 root 读"


def test_绑定_站点连不上记原因_拒了回没绑定(机):
    机.site.up = False
    机.core.bind(CODE, {"wifi": {"ssid": "Estate", "psk": ""}, "site": SITE})
    机.tick()
    assert "无法连接站点" in 机.core.status(CODE)["error"]
    机.site.up = True
    机.tick(c.POLL_MS)
    机.site.answer = {"state": "rejected"}
    机.tick(c.POLL_MS)
    st = 机.core.status(CODE)
    assert st["state"] == "unbound" and "拒绝" in st["error"]
    assert 机.router.ssid == ""


def test_绑好以后不能再绑_不能取消(机):
    机.bound()
    with pytest.raises(c.BindRefused) as e:
        机.core.bind(CODE, {"wifi": {"ssid": "X", "psk": ""}, "site": SITE})
    assert e.value.status == 409
    with pytest.raises(c.BindRefused):
        机.core.cancel(CODE)


@pytest.mark.parametrize("body", [
    {"wifi": {"ssid": "", "psk": ""}, "site": SITE},
    {"wifi": {"ssid": "E", "psk": "123"}, "site": SITE},
    {"wifi": {"ssid": "E", "psk": ""}, "site": {"url": "http://x", "fp": "ab" * 32}},
    {"wifi": {"ssid": "E", "psk": ""}, "site": {"url": "https://x", "fp": "ab"}},
    [], None])
def test_绑定_形状不对400(机, body):
    with pytest.raises(c.BindRefused) as e:
        机.core.bind(CODE, body)
    assert e.value.status == 400


def test_换WiFi_连得上站点_挂着等确认_确认后换_连上站点才算(机):
    机.bound()
    st = 机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})
    ch = st["change"]
    assert ch["mode"] == "online" and ch["phase"] == "waiting"
    assert 机.router.ssid == "Estate", "未确认不换"
    v = 机.core.net_view()["wifi"]
    assert v["change"]["id"] == ch["id"] and v["change"]["ssid"] == "New"
    assert 机.core.wifi_confirm("bogus", True) == "no_such_change"
    assert 机.core.wifi_confirm(ch["id"], True) == ""
    assert 机.router.ssid == "New"
    assert 机.core.net_view()["wifi"]["change"] is None, "换的时候不再挂着等确认"
    机.tick(c.POLL_MS)                                  # 问到站点了
    st = 机.core.status(CODE)
    assert "change" not in st and st["change_result"]["result"] == "applied"
    assert st["wifi"]["ssid"] == "New"


def test_换WiFi_确认后5分钟连不上站点_退回旧的(机):
    机.bound()
    ch = 机.core.wifi(CODE, {"ssid": "Bad", "psk": "abcdefgh"})["change"]
    机.core.wifi_confirm(ch["id"], True)
    机.site.up = False
    for _ in range(70):
        机.tick(c.POLL_MS)
    st = 机.core.status(CODE)
    assert st["change_result"]["result"] == "unreachable"
    assert 机.router.ssid == "Estate" and st["wifi"]["ssid"] == "Estate"


def test_换WiFi_在线时不同意_或者24小时没人管_作废(机):
    机.bound()
    ch = 机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})["change"]
    assert 机.core.wifi_confirm(ch["id"], False) == ""
    assert 机.core.status(CODE)["change_result"]["result"] == "rejected"
    assert 机.router.ssid == "Estate"
    机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})
    机.t += c.CHANGE_TTL_MS + 1
    机.site.up = False
    机.tick()
    assert 机.core.status(CODE)["change_result"]["result"] == "expired"
    assert 机.router.ssid == "Estate"


def test_换WiFi_连不上站点_先试新的_确认了留下(机):
    机.bound()
    机.site.up = False
    机.t += c.ONLINE_MS + 1
    st = 机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})
    assert st["change"]["mode"] == "trial" and st["change"]["phase"] == "trying"
    assert 机.router.ssid == "New", "离线:先试新的"
    assert 机.core.net_view()["wifi"]["change"]["mode"] == "trial"
    机.site.up = True
    机.tick()
    assert 机.core.wifi_confirm(st["change"]["id"], True) == ""
    st = 机.core.status(CODE)
    assert st["change_result"]["result"] == "applied" and st["wifi"]["ssid"] == "New"


def test_换WiFi_试新的30分钟没确认_退回旧的(机):
    机.bound()
    机.site.up = False
    机.t += c.ONLINE_MS + 1
    机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})
    机.tick(c.TRIAL_MS + 1)
    st = 机.core.status(CODE)
    assert st["change_result"]["result"] == "expired" and 机.router.ssid == "Estate"


def test_换WiFi_还没绑定不行(机):
    with pytest.raises(c.BindRefused) as e:
        机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})
    assert e.value.status == 409


def test_站点叫解绑_证书对得上才清机_清完回没绑定(机):
    机.bound()
    assert 机.core.site_unbind("sha256:" + "0" * 64) == "cert_mismatch"
    assert 机.core.s["state"] == "bound"
    assert 机.core.site_unbind(c.cert_fp(PEM)) == ""
    assert 机.core.s["state"] == "wiping"
    assert read_json(机.paths.state)["state"] == "wiping", "先记下来:断电了开机接着清"
    机.wipe_fails = ["删 /x"]
    机.tick()
    assert 机.core.s["state"] == "wiping" and 机.wiped == 1
    机.tick(1000)
    assert 机.wiped == 1, "未清干净:隔 30 秒再来"
    机.wipe_fails = []
    机.tick(30_000)
    assert 机.core.s["state"] == "unbound" and 机.wiped == 2


def test_不在线时被解绑_问站点知道了自己清(机):
    机.bound()
    机.site.cert = "unknown"
    机.tick(c.PROBE_MS)
    assert 机.core.s["state"] == "bound", "站点不认识此机器人(站点重装了):不动"
    机.site.cert = "revoked"
    机.tick(c.PROBE_MS)
    assert 机.core.s["state"] == "wiping"


def test_解绑令_一次性_签名序列号要对(机):
    seed = ed25519.new_seed()
    机.paths.unbind_pub.write_bytes(ed25519.public_pem(ed25519.public_key(seed)))
    with pytest.raises(c.BindRefused, match="本来就未绑定"):
        机.core.unbind_token(CODE, "x")
    机.bound()
    other = b.sign_unbind(seed, "D1M-2", now_ms=机.t, hours=24)
    with pytest.raises(c.BindRefused, match="不是给此机器人"):
        机.core.unbind_token(CODE, other)
    tok = b.sign_unbind(seed, "D1M-1", now_ms=机.t, hours=24)
    机.core.unbind_token(CODE, tok)
    assert 机.core.s["state"] == "wiping"
    机.tick()
    机.bound()
    with pytest.raises(c.BindRefused, match="用过了"):
        机.core.unbind_token(CODE, tok)


def test_没烧写过_起不来(tmp_path):
    with pytest.raises(RuntimeError, match="factory burn"):
        c.BindCore(BindPaths(etc=tmp_path), router=FakeRouter(), site=None,
                   provision=str, wipe=list, start_services=lambda: None, now_ms=lambda: 0)


def test_外审I7_领证没成_下一次轮询再领(机):
    机.core.bind(CODE, {"wifi": {"ssid": "Estate", "psk": "12345678"}, "site": SITE})
    机.tick()
    机.prov_fail = 1
    机.site.answer = {"state": "approved", "code": "D1MAX1.a"}
    机.tick(c.POLL_MS)
    assert 机.core.s["state"] == "pending" and "领证书失败" in 机.core.s["error"]
    机.site.answer = {"state": "approved", "code": "D1MAX1.b"}
    机.tick(c.POLL_MS)
    assert 机.core.s["state"] == "bound" and 机.provisioned == ["D1MAX1.a", "D1MAX1.b"]


def test_外审I8_清机期间有标记_清完删掉(机):
    机.bound()
    机.core.site_unbind(c.cert_fp(PEM))
    assert 机.paths.wiping_marker.exists(), "服务单元看到它就不起"
    机.tick()
    assert not 机.paths.wiping_marker.exists() and 机.core.s["state"] == "unbound"


def test_外审I13_改动最后怎样了_进遥测(机):
    机.bound()
    ch = 机.core.wifi(CODE, {"ssid": "New", "psk": "abcdefgh"})["change"]
    机.core.wifi_confirm(ch["id"], False)
    v = 机.core.net_view()["wifi"]
    assert v["change"] is None and v["result"] == {"id": ch["id"], "result": "rejected"}
    assert b.wifi_view({"wifi": v})["result"] == v["result"]


def test_扫附近WiFi_同名留最强_强的在前_空名不列_要绑定码(机):
    from d1max_patrol.bind.router import WifiSeen
    机.router.nearby = (WifiSeen("B", -80, True), WifiSeen("A", -50, False),
                        WifiSeen("B", -60, True), WifiSeen("", -40, True))
    with pytest.raises(c.BindRefused):
        机.core.scan("0000000000000000")
    assert 机.core.scan(CODE)["networks"] == [
        {"ssid": "A", "signal": -50, "secured": False},
        {"ssid": "B", "signal": -60, "secured": True}]


# ------------------------------------------------------------------ W57 换解绑钥匙、作废、台账

def _pub(seed):
    return ed25519.public_pem(ed25519.public_key(seed))


def test_W57_解绑令_新钥匙随发行装进来就认_出厂那把照认(机):
    old, new = ed25519.new_seed(), ed25519.new_seed()
    机.paths.unbind_pub.write_bytes(_pub(old))
    机.bound()
    with pytest.raises(c.BindRefused, match="签名错误"):
        机.core.unbind_token(CODE, b.sign_unbind(new, "D1M-1", now_ms=机.t, hours=24))
    d = 机.paths.etc / "unbind-pub.d"
    d.mkdir()
    (d / "2026-11.pem").write_bytes(_pub(new))
    机.core.unbind_token(CODE, b.sign_unbind(new, "D1M-1", now_ms=机.t, hours=24))
    assert 机.core.s["state"] == "wiping"


def test_W57_解绑令_作废名单里的钥匙不认_出厂那把也能作废(机):
    old, new = ed25519.new_seed(), ed25519.new_seed()
    机.paths.unbind_pub.write_bytes(_pub(old))
    d = 机.paths.etc / "unbind-pub.d"
    d.mkdir()
    (d / "new.pem").write_bytes(_pub(new))
    (机.paths.etc / "unbind-revoked.txt").write_text(
        f"# 2026-11-02 出厂那把泄露\n{b.key_id(ed25519.public_key(old))}\n")
    机.bound()
    with pytest.raises(c.BindRefused, match="签名错误|作废"):
        机.core.unbind_token(CODE, b.sign_unbind(old, "D1M-1", now_ms=机.t, hours=24))
    机.core.unbind_token(CODE, b.sign_unbind(new, "D1M-1", now_ms=机.t, hours=24))
    assert 机.core.s["state"] == "wiping"


def test_W57_签解绑令_记台账(tmp_path, capsys):
    import argparse
    import csv

    from d1max_patrol.bind.cli import add_args, run
    ap = argparse.ArgumentParser()
    add_args(ap.add_subparsers(dest="command", required=True))
    k = tmp_path / "unbind.key"
    assert run(ap.parse_args(["unbind-token", "keygen", "--private", str(k), "--public",
                              str(tmp_path / "unbind-pub.pem")])) == 0
    ledger = tmp_path / "解绑令台账.csv"
    argv = ["unbind-token", "sign", "--private", str(k), "--serial", "D1M-1"]
    assert run(ap.parse_args([*argv, "--ledger", str(ledger)])) == 1, \
        "记台账要写申请人、核对人、用途"
    assert run(ap.parse_args([*argv, "--ledger", str(ledger), "--requester", "客户 A 管理员",
                              "--checker", "liang", "--purpose", "站点主机损坏无备份"])) == 0
    tok = capsys.readouterr().out.strip().splitlines()[-1]
    rows = list(csv.DictReader(ledger.open(encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["serial"] == "D1M-1" and rows[0]["checker"] == "liang"
    assert rows[0]["nonce"] and rows[0]["nonce"] in b.verify_unbind(
        tok, ed25519.pub_from_pem((tmp_path / "unbind-pub.pem").read_bytes()), serial="D1M-1",
        now_ms=int(__import__("time").time() * 1000)).nonce
