"""W55 远程运维(站点这边):机器人运维接口、证书自动续签、错误记录、站点级运维、App 分发、
工程师 VPN、诊断包。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_site_api import PW, 站

from d1max_contract import relsign
from d1max_contract.privops import SimOps
from d1max_site.app_dist import AppDistError, AppStore
from d1max_site.ca import SiteCA
from d1max_site.db import SiteDB
from d1max_site.health import support_bundle
from d1max_site.permissions import MAINTAIN, allowed
from d1max_site.registry import Registry
from d1max_site.remote_ops import CLIENT_ERR_KEEP_MS, CertRenewer, ClientErrors
from d1max_site.vpn import VpnError, VpnState


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.api.home = tmp_path
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _假狗(s, monkeypatch, reply):
    sent = []

    async def 回(robot_id, kind, payload, *, issued_by):
        sent.append((robot_id, kind, payload, issued_by))
        return {"ack": reply}
    monkeypatch.setattr(s.disp, "map_command", 回)
    return sent


def test_运维权限_只有管理员和工程师():
    assert allowed("admin", MAINTAIN) and allowed("engineer", MAINTAIN)
    assert not any(allowed(r, MAINTAIN) for r in ("guard", "owner", "viewer"))


def test_机器人运维接口_读日志_保安不行_拒了说原因(站点, monkeypatch):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    sent = _假狗(s, monkeypatch, {"result": "accepted", "reason": "",
                                "data": {"unit": "d1max-agent", "text": "x\n"}})
    assert s.req("GET", "/api/robots/A/ops/journal?unit=d1max-agent", token=gina)[0] == 403
    code, d = s.req("GET", "/api/robots/A/ops/journal?unit=d1max-agent&hours=3", token=alice)
    assert code == 200 and d["text"] == "x\n"
    assert sent[-1][1:] == ("ops_journal", {"unit": "d1max-agent", "hours": 3}, "alice")
    assert s.req("GET", "/api/robots/A/ops/nope", token=alice)[0] == 404
    assert s.req("GET", "/api/robots/A/ops/restart", token=alice)[0] == 405
    _假狗(s, monkeypatch, {"result": "rejected", "reason": "busy: 正在执行任务 p1"})
    code, d = s.req("POST", "/api/robots/A/ops/restart", {"unit": "d1max-agent"}, token=alice)
    assert code == 409 and "busy" in d["error"]


def test_重启整机要再输密码(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    sent = _假狗(s, monkeypatch, {"result": "accepted", "reason": "", "data": {"delay_s": 5}})
    assert s.req("POST", "/api/robots/A/ops/reboot", {}, token=alice)[0] == 403
    assert s.req("POST", "/api/robots/A/ops/reboot", {"confirm_password": "bad"},
                 token=alice)[0] == 403
    code, d = s.req("POST", "/api/robots/A/ops/reboot", {"confirm_password": PW}, token=alice)
    assert code == 200 and d == {"delay_s": 5}
    assert sent == [("A", "ops_reboot", {}, "alice")], "密码不往机器人那边带"


def test_错误记录_登录着的都能报_限条数_留14天(站点, tmp_path):
    s = 站点
    s.api.client_errors = ClientErrors(s.db, now_ms=lambda: 1_000_000_000)
    gina = _登(s, "gina")
    items = [{"kind": "exception", "page": "duty", "message": "boom", "app_version": "1.0.0+3"}]
    assert s.req("POST", "/api/client-errors", {"client": "app", "items": items},
                 token=gina) == (200, {"stored": 1})
    bad = {"client": "x", "items": items}
    assert s.req("POST", "/api/client-errors", bad, token=gina)[0] == 400
    assert s.req("POST", "/api/client-errors", {"client": "web", "items": items * 201},
                 token=gina)[0] == 400
    ce = s.api.client_errors
    got = ce.recent(0)
    assert got[0]["account"] == "gina" and got[0]["message"] == "boom"
    ce._now = lambda: 1_000_000_000 + CLIENT_ERR_KEEP_MS + 1
    ce.prune()
    assert ce.recent(0) == []


def test_站点级运维_没装执行器明说_装了交请求回202_结果之后查(站点):
    s = 站点
    alice = _登(s, "alice")
    assert s.req("POST", "/api/system/restart", {"confirm_password": PW}, token=alice)[0] == 409
    s.api.priv = SimOps()
    assert s.req("POST", "/api/system/restart", {}, token=alice)[0] == 403
    code, d = s.req("POST", "/api/system/restart", {"confirm_password": PW}, token=alice)
    assert code == 202 and d["id"]
    assert s.api.priv.calls == [("restart_site", {})]
    code, d = s.req("GET", "/api/system/restart", token=alice)
    assert code == 200 and d["last"]["by"] == "alice" and d["last"]["result"]["ok"]
    assert s.req("POST", "/api/system/push-test", {}, token=alice)[0] == 409
    gina = _登(s, "gina")
    assert s.req("GET", "/api/system/vpn", token=gina)[0] == 403


def _upload(s, path, data, headers, token):
    r = urllib.request.Request(s.api.url + path, data=data, method="POST")
    for k, v in {**headers, "Authorization": f"Bearer {token}"}.items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_站点升级包_收下要密码_交给执行器(站点):
    s = 站点
    alice = _登(s, "alice")
    s.api.priv = SimOps()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b'{"name": "x"}'
        info = tarfile.TarInfo("2026-10-31-abcdef12/release.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    pkg = buf.getvalue()
    code, _ = _upload(s, "/api/system/update", pkg, {"X-D1Max-Confirm": "bad"}, alice)
    assert code == 403 and not list((s.api.home / "updates").glob("*.tar.gz"))
    code, d = _upload(s, "/api/system/update", b"junk", {"X-D1Max-Confirm": PW}, alice)
    assert code == 400
    code, d = _upload(s, "/api/system/update", pkg, {"X-D1Max-Confirm": PW}, alice)
    assert code == 202, d
    (action, args), = s.api.priv.calls
    assert action == "update" and (s.api.home / "updates" / args["package"]).read_bytes() == pkg


def _signed_app(tmp: Path, data: bytes, version: str, name: str = "nw.apk"):
    keys = tmp / "keys"
    if not (keys / "pub.pem").exists():
        relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    man = {"name": name, "version": version, "requires_mission_schema": 1,
           "content_sha256": hashlib.sha256(data).hexdigest()}
    man["signature"] = relsign.sign(man, keys / "priv.pem")
    return man, keys / "pub.pem"


def test_App安装包_验签_只收更新的_问有没有新的(tmp_path):
    man, pub = _signed_app(tmp_path, b"apk1", "1.0.0+3")
    st = AppStore(tmp_path / "apps", pubkey=pub)
    assert st.check("android", 1) == {"available": False}
    meta = st.add("android", b"apk1", man, now_ms=5)
    assert meta["build"] == 3 and st.check("android", 2)["available"]
    assert st.check("android", 2)["sha256"] == meta["sha256"], "App 下载完要核对指纹"
    assert not st.check("android", 3)["available"]
    with pytest.raises(AppDistError, match="只收更新的"):
        st.add("android", b"apk1", man, now_ms=6)
    with pytest.raises(AppDistError, match="不匹配"):
        st.add("android", b"other", _signed_app(tmp_path, b"apk2", "1.0.1+4")[0], now_ms=7)
    bad = dict(_signed_app(tmp_path, b"apk2", "1.0.1+4")[0], version="1.0.1+5")
    with pytest.raises(AppDistError, match="签名"):
        st.add("android", b"apk2", bad, now_ms=8)
    with pytest.raises(AppDistError, match="版本"):
        st.add("android", b"apk2", dict(man, version="v2"), now_ms=9)
    st.add("android", b"apk2", _signed_app(tmp_path, b"apk2", "1.0.1+4")[0], now_ms=10)
    f, m = st.file("android")
    assert f.read_bytes() == b"apk2" and sorted(p.name for p in f.parent.iterdir()) == \
        ["latest.json", "nw.apk"]


def test_App安装包_接口_上传问下载(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    man, pub = _signed_app(tmp_path, b"APKDATA", "1.2.0+12")
    s.api.apps = AppStore(tmp_path / "apps", pubkey=pub)
    hdr = {"X-App-Manifest": base64.b64encode(json.dumps(man).encode()).decode()}
    assert _upload(s, "/api/app-update/upload?platform=android", b"APKDATA", hdr, gina)[0] == 403
    code, d = _upload(s, "/api/app-update/upload?platform=android", b"APKDATA", hdr, alice)
    assert code == 200 and d["build"] == 12
    code, d = s.req("GET", "/api/app-update?platform=android&build=10", token=gina)
    assert code == 200 and d["available"] and d["version"] == "1.2.0+12"
    r = urllib.request.Request(s.api.url + "/api/app-update/file/android")
    r.add_header("Authorization", f"Bearer {gina}")
    with urllib.request.urlopen(r, timeout=15) as resp:
        assert resp.read() == b"APKDATA"


def test_工程师VPN_要到期时间_到点收回_地址能再分_只放行到22端口(tmp_path):
    v = VpnState(tmp_path / "vpn")
    v.init(endpoint="example.org")
    v.add("robot", "A")
    with pytest.raises(VpnError, match="到期"):
        v.add("engineer", "eng-x")
    with pytest.raises(VpnError, match="到期"):
        v.add("phone", "p1", expires_ms=5)
    got = v.add("engineer", "eng-x", expires_ms=1000)
    assert got["address"] == "172.29.70.200"
    conf = v.server_conf()
    assert "-s 172.29.70.200/32 -p tcp --dport 22 -j ACCEPT" in conf
    assert conf.index("--dport 22") < conf.index("-j DROP"), "先放行工程师,再丢别的"
    assert "net.ipv4.conf.%i.forwarding=1" in conf and "ip_forward" not in conf
    assert v.expire(999) == [] and v.expire(1000) == ["eng-x"]
    assert "172.29.70.200" not in v.server_conf()
    again = v.add("engineer", "eng-y", expires_ms=2000)
    assert again["address"] == "172.29.70.200", "到点的地址再分"
    gen = v.write_apply()
    f = v.root / "apply.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert v.pending, "改过、还没装上"
    v.applied(gen)
    assert not v.pending


def test_外审W55_1_VPN装不成一直待生效_期间又改过的不清(tmp_path):
    v = VpnState(tmp_path / "vpn")
    v.init(endpoint="vpn.example.com")
    gen = v.write_apply()
    v.add("engineer", "eng-x", expires_ms=1000)          # 装的同时又改了
    v.applied(gen)
    assert v.pending, "装上的是老的那一份:还得再装"
    v.applied(v.write_apply())
    assert not v.pending
    v.expire(1000)
    assert v.pending, "收回了工程师:要重装"


class _Disp:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.clients = {"A": SimpleNamespace(capabilities=SimpleNamespace(tasks={"cert_csr": {}}))}
        self.calls = []
        self.install = {"result": "accepted"}

    async def map_command(self, rid, kind, payload, *, issued_by):
        self.calls.append((rid, kind))
        if kind == "cert_csr":
            k = self.tmp / "new.key"
            subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                            "ec_paramgen_curve:P-256", "-out", str(k)], check=True,
                           capture_output=True)
            subprocess.run(["openssl", "req", "-new", "-key", str(k), "-subj", f"/CN={rid}",
                            "-out", str(self.tmp / "new.csr")], check=True, capture_output=True)
            return {"ack": {"result": "accepted",
                            "data": {"csr": (self.tmp / "new.csr").read_text()}}}
        self.cert = payload["cert"]
        return {"ack": self.install}


class _Alerts:
    def __init__(self):
        self.raised, self.resolved = [], []

    def raise_alert(self, **kw):
        self.raised.append(kw)

    def has_open(self, rid, kind):
        return bool(self.raised)

    def resolve_all(self, rid, kind, who):
        self.resolved.append((rid, kind))


def _ca_reg(tmp: Path, expires_in_ms: int, now: int):
    ca = SiteCA(tmp / "ca")
    ca.init("estate-1")
    b = ca.issue_robot("A", days=30, now_ms=now)
    db = SiteDB(tmp / "site.db")
    reg = Registry(db, site_id="estate-1")
    reg.enroll("A", fingerprint=b.fingerprint, issued_at=now, expires_at=now + expires_in_ms)
    _核过身份(db, "A")
    return ca, reg, db


def _核过身份(db, rid):
    """W63:这台核过设备身份(领证书时答过题),才给续签。"""
    with db.tx() as c:
        c.execute("INSERT INTO device_ids(serial, device_pub, hw_id, first_ms, last_ms) "
                  "VALUES (?, 'pub', ?, 0, 0)", (rid, "e" * 64))


def test_W63_没核过设备身份的_不自动续签_手动续签只给中性的话_不告警(tmp_path):
    from d1max_site.attest import NEUTRAL
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    with db.tx() as c:
        c.execute("DELETE FROM device_ids")
    d, al = _Disp(tmp_path), _Alerts()
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    assert r.due() == []
    with pytest.raises(Exception, match=NEUTRAL):
        asyncio.run(r.renew("A", by="alice"))
    assert d.calls == [] and al.raised == []
    db.close()


def test_证书自动续签_快到期的才续_签了换上_登记表换指纹(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    d, al = _Disp(tmp_path), _Alerts()
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    assert r.due() == ["A"]
    old = reg.get("A").fingerprint
    got = asyncio.run(r.renew("A", by="alice"))
    assert d.calls == [("A", "cert_csr"), ("A", "cert_install")]
    assert reg.get("A").fingerprint == got["fingerprint"] != old
    assert reg.get("A").expires_at == now + 365 * 86_400_000
    pub_c = subprocess.run(["openssl", "x509", "-noout", "-pubkey"], input=d.cert, text=True,
                           capture_output=True).stdout
    pub_k = subprocess.run(["openssl", "pkey", "-pubout", "-in", str(tmp_path / "new.key")],
                           text=True, capture_output=True).stdout
    assert pub_c == pub_k, "签的是机器人自己那把新私钥"
    assert al.raised == []
    db.close()


def test_证书自动续签_机器人不肯换_报P2_远的不续(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    d, al = _Disp(tmp_path), _Alerts()
    d.install = {"result": "rejected", "reason": "cert_mismatch: x"}
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    with pytest.raises(Exception, match="未换上"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert al.raised[0]["kind"] == "cert_renew_failed"
    far = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al,
                      now_ms=lambda: now - 300 * 86_400_000)
    assert far.due() == []
    db.close()


def test_CA签申请_主体不是这台拒_格式不对拒(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    k = tmp_path / "k.key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-out", str(k)], check=True, capture_output=True)
    subprocess.run(["openssl", "req", "-new", "-key", str(k), "-subj", "/CN=B", "-out",
                    str(tmp_path / "b.csr")], check=True, capture_output=True)
    from d1max_site.ca import CAError
    with pytest.raises(CAError, match="主体"):
        ca.sign_robot_csr("A", (tmp_path / "b.csr").read_text(), days=10, now_ms=now)
    with pytest.raises(CAError, match="格式"):
        ca.sign_robot_csr("A", "junk", days=10, now_ms=now)
    db.close()


def test_诊断包_带时间段_事件命令操作记录_另取的原样放进去(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    (tmp_path / "site.json").write_text('{"site_id": "s", "push": {"secret": "zzz"}}')
    db.query("INSERT INTO audit(at, actor, action, target, status, detail, remote) "
             "VALUES (?,?,?,?,?,?,?)", (5_000_000, "alice", "login", "", 200, "{}", ""))
    out = support_bundle(tmp_path / "b.tar.gz", home=tmp_path, db=db, health=None,
                         now_ms=5_000_000 + 3_600_000, hours=2, journal=lambda u, h: f"{u} {h}h\n",
                         extra={"robots/A/diag.json": b'{"ok": 1}', "client_errors.json": b"[]"})
    with tarfile.open(out) as tf:
        names = {m.name.removeprefix("d1max-support/") for m in tf.getmembers()}
        audit = json.loads(tf.extractfile("d1max-support/audit.json").read())
        log = tf.extractfile("d1max-support/logs/wg-quick@d1max.log").read()
    assert {"robots/A/diag.json", "client_errors.json", "events.json", "commands.json",
            "audit.json", "logs/chrony.log"} <= names
    assert audit[0]["actor"] == "alice" and log == b"wg-quick@d1max 2h\n"
    db.close()


def test_命令行签App包_站点认(tmp_path):
    from d1max_patrol.cli import main
    assert main(["release", "keygen", str(tmp_path / "p.pem"), str(tmp_path / "pub.pem")]) == 0
    f = tmp_path / "nw-1.0.0.apk"
    f.write_bytes(b"APK!")
    assert main(["release", "sign-app", str(f), "--version", "1.0.0", "--key",
                 str(tmp_path / "p.pem")]) == 2, "版本没写构建号"
    assert main(["release", "sign-app", str(f), "--version", "1.0.0+7", "--key",
                 str(tmp_path / "p.pem")]) == 0
    man = json.loads((tmp_path / "nw-1.0.0.apk.manifest.json").read_text())
    st = AppStore(tmp_path / "apps", pubkey=tmp_path / "pub.pem")
    assert st.add("android", b"APK!", man, now_ms=1)["build"] == 7


def test_自动续签早于体检告警_剩40天就续(tmp_path):
    """体检剩 30 天以内报「证书快到期」;自动续签要更早动手,告警才只在自动续签不成时出现。"""
    from d1max_site import health, remote_ops
    assert remote_ops.RENEW_WITHIN_DAYS > health.CERT_WARN_DAYS
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 40 * 86_400_000, now)
    r = CertRenewer(dispatcher=_Disp(tmp_path), registry=reg, ca=ca, alerts=_Alerts(),
                    now_ms=lambda: now)
    assert r.due() == ["A"]
    db.close()



def test_外审W55_1_续签_机器人拒绝换_旧证书照常认_挂着的删掉_隔天再试(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    old = reg.get("A")
    d, al = _Disp(tmp_path), _Alerts()
    d.install = {"result": "rejected", "reason": "cert_mismatch: x"}
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    with pytest.raises(Exception, match="未换上"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert reg.get("A").fingerprint == old.fingerprint and reg.get("A").expires_at == old.expires_at
    assert reg.pending("A") is None
    assert r.due() == ["A"], "登记表没动:还该续"
    db.close()


def test_外审W55_1_续签_回执丢了_新证书挂着_机器人拿它连上来就转正(tmp_path):
    from d1max_site.intake import IntakeServer
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    old = reg.get("A").fingerprint
    d, al = _Disp(tmp_path), _Alerts()

    async def lost(rid, kind, payload, *, issued_by, _orig=d.map_command):
        if kind == "cert_install":
            d.cert = payload["cert"]
            raise TimeoutError("no ack")
        return await _orig(rid, kind, payload, issued_by=issued_by)
    d.map_command = lost
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    with pytest.raises(Exception, match="未回执"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert reg.get("A").fingerprint == old, "没确认:登记表不动,旧证书照常认"
    assert reg.pending("A") is not None and r.due() == [], "挂着一张:别再签一张顶掉它"
    der = subprocess.run(["openssl", "x509", "-outform", "DER"], input=d.cert.encode(),
                         capture_output=True, check=True).stdout
    it = IntakeServer.__new__(IntakeServer)
    it.db, it._now = db, lambda: now
    assert it.robot_for(der) == "A", "拿新证书连上来:认,并转正"
    assert reg.get("A").fingerprint != old and reg.pending("A") is None
    db.close()


def test_外审W55_1_续签_机器人正忙_不告警_一小时后再试(tmp_path):
    from d1max_site import remote_ops
    now = [1_800_000_000_000]
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now[0])
    d, al = _Disp(tmp_path), _Alerts()
    d.install = {"result": "rejected", "reason": "busy: 正在执行任务 t1"}
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now[0])

    async def go():
        r.tick()
        await asyncio.sleep(0.5)
    asyncio.run(go())
    assert al.raised == [] and r._wait["A"] == remote_ops.BUSY_RETRY_MS
    assert reg.pending("A") is None
    db.close()


def test_外审W55_1_钟不准的时候_对时和读诊断放行():
    from d1max_site.temporal import READ, SAFE, temporal_class
    assert temporal_class("ops_timesync") == SAFE
    assert temporal_class("ops_diag") == READ and temporal_class("ops_journal") == READ
    assert temporal_class("ops_reboot") not in (SAFE, READ)



def test_外审W55_2_I2_续签挂着时解绑重登_迟到的转正不覆盖新身份(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    old_fp = reg.get("A").fingerprint
    reg.stage("A", fingerprint="sha256:new", issued_at=now, expires_at=now + 365 * 86_400_000,
              now_ms=now, old_fingerprint=old_fp)
    reg.revoke("A")
    reg.enroll("A", fingerprint="sha256:fresh", issued_at=now, expires_at=now + 30 * 86_400_000)
    assert reg.pending("A") is None, "解绑、重新登记:挂着的作废"
    reg.stage("A", fingerprint="sha256:new2", issued_at=now, expires_at=now + 1,
              now_ms=now, old_fingerprint=old_fp)               # 拿旧身份发起的(迟到)
    assert reg.promote("A", "sha256:new2") is False
    assert reg.get("A").fingerprint == "sha256:fresh"
    db.close()


def test_外审W55_2_C3_机器人上执行器结果未知_挂着的新证书留着(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    d, al = _Disp(tmp_path), _Alerts()
    d.install = {"result": "rejected", "reason": "priv_pending: abc123 结果未知"}
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    with pytest.raises(Exception, match="未及时回复"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert reg.pending("A") is not None, "可能已经换上了:留着,连上来时转正"
    db.close()


def test_外审W55_3_N2_老的挂起证书表没有旧指纹列_打开就补上(tmp_path):
    import sqlite3
    db_path = tmp_path / "site.db"
    SiteDB(db_path).close()
    con = sqlite3.connect(db_path)                     # 做成第二轮那版的样子:没有 old_fingerprint
    con.execute("DROP TABLE cert_pending")
    con.execute("CREATE TABLE cert_pending (robot_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, "
                "created_ms INTEGER NOT NULL)")
    con.execute("INSERT INTO cert_pending VALUES ('x', 'sha256:a', 1, 2, 3)")
    con.commit()
    con.close()
    db = SiteDB(db_path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(cert_pending)")}
    assert "old_fingerprint" in cols
    assert db.query("SELECT old_fingerprint FROM cert_pending")[0]["old_fingerprint"] == ""
    db.close()


def test_外审W55_3_I2_拿到申请以后解绑重登_这次续签作废_新身份不动(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    d, al = _Disp(tmp_path), _Alerts()
    orig = d.map_command

    async def 中途重登(rid, kind, payload, *, issued_by):
        got = await orig(rid, kind, payload, issued_by=issued_by)
        if kind == "cert_csr":                          # 申请拿到了,这时管理员解绑、重新登记
            reg.revoke("A")
            reg.enroll("A", fingerprint="sha256:fresh", issued_at=now,
                       expires_at=now + 30 * 86_400_000)
        return got
    d.map_command = 中途重登
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    with pytest.raises(Exception, match="解绑或重新登记"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert reg.get("A").fingerprint == "sha256:fresh" and reg.pending("A") is None
    assert ("A", "cert_install") not in d.calls, "作废了就不往机器人上装"
    db.close()


def test_外审W55_4_N2_老库里挂着的证书_补列后照样能转正(tmp_path):
    import sqlite3
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    old_fp = reg.get("A").fingerprint
    db.close()
    con = sqlite3.connect(tmp_path / "site.db")
    con.execute("DROP TABLE cert_pending")
    con.execute("CREATE TABLE cert_pending (robot_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, "
                "created_ms INTEGER NOT NULL)")
    con.execute("INSERT INTO cert_pending VALUES ('A', 'sha256:new', ?, ?, ?)",
                (now, now + 365 * 86_400_000, now))
    con.commit()
    con.close()
    db = SiteDB(tmp_path / "site.db")
    reg = Registry(db, site_id="estate-1")
    assert reg.pending("A")["old_fingerprint"] == old_fp
    assert reg.promote("A", "sha256:new") and reg.get("A").fingerprint == "sha256:new"
    db.close()


def test_外审W55_4_N4_接收口先转正_回执后到_也算续签成功(tmp_path):
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    d, al = _Disp(tmp_path), _Alerts()
    orig = d.map_command

    async def 回执晚到(rid, kind, payload, *, issued_by):
        got = await orig(rid, kind, payload, issued_by=issued_by)
        if kind == "cert_install":                       # 机器人换上、先连上接收口转正了
            fp = reg.pending("A")["fingerprint"]
            assert reg.promote("A", fp)
        return got
    d.map_command = 回执晚到
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=al, now_ms=lambda: now)
    got = asyncio.run(r.renew("A", by="site:cert"))
    assert got["fingerprint"] == reg.get("A").fingerprint and al.raised == []
    db.close()


# ------------------------------------------------------------------ W58 长跑查出来的

def test_W58_续签失败_真告警簿里记得上_成功后自动关掉(tmp_path):
    """原来假告警簿什么参数都收,站点真用的 AlertDesk 不收 now_ms:告警从来没记上。"""
    from d1max_site.alert_store import AlertDesk
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    desk = AlertDesk(db, now_ms=lambda: now)
    d = _Disp(tmp_path)
    d.install = {"result": "rejected", "reason": "disk full"}
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=desk, now_ms=lambda: now)
    with pytest.raises(Exception, match="disk full"):
        asyncio.run(r.renew("A", by="site:cert"))
    assert desk.has_open("A", "cert_renew_failed")
    d.install = {"result": "accepted"}
    asyncio.run(r.renew("A", by="site:cert"))
    assert not desk.has_open("A", "cert_renew_failed")


def test_W58_钟差还不知道_当忙_过一会儿再试_不报续签失败(tmp_path):
    """站点刚起来、机器人刚连上时钟差未知,派命令被拒:不算续签失败,按「忙」稍后重试。"""
    from d1max_site import remote_ops
    from d1max_site.alert_store import AlertDesk
    from d1max_site.dispatcher import SkewUnknown
    now = 1_800_000_000_000
    ca, reg, db = _ca_reg(tmp_path, 10 * 86_400_000, now)
    desk = AlertDesk(db, now_ms=lambda: now)
    d = _Disp(tmp_path)

    async def refuse(rid, kind, payload, *, issued_by):
        raise SkewUnknown(f"{rid} 的钟差未知")
    d.map_command = refuse
    r = CertRenewer(dispatcher=d, registry=reg, ca=ca, alerts=desk, now_ms=lambda: now)
    asyncio.run(r._auto("A"))
    assert r._wait["A"] == remote_ops.BUSY_RETRY_MS
    assert not desk.has_open("A", "cert_renew_failed")
