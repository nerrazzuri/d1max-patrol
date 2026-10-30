"""W42 出厂工具(烧写、检验、重做贴纸)、配网服务的两个口(热点 HTTPS、代理套接字)、清机。"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
from pathlib import Path

import pytest

from d1max_contract import binding as b
from d1max_contract import ed25519
from d1max_patrol.bind import factory as fa
from d1max_patrol.bind import server as sv
from d1max_patrol.bind import wipe as wp
from d1max_patrol.bind.core import BindCore
from d1max_patrol.bind.paths import BindPaths, read_json, write_json
from d1max_patrol.bind.router import FakeRouter


@pytest.fixture
def 出厂(tmp_path):
    paths = BindPaths(etc=tmp_path / "etc", data_root=tmp_path / "data")
    pub = tmp_path / "unbind-pub.pem"
    pub.write_bytes(ed25519.public_pem(ed25519.public_key(ed25519.new_seed())))
    router = FakeRouter()
    st = fa.burn(paths, "D1M-000123", router=router, unbind_pub=pub)
    return paths, router, st, pub


def test_烧写_只存哈希_热点写进路由器_检验对得上(出厂, tmp_path):
    paths, router, st, _ = 出厂
    dev = read_json(paths.device)
    assert dev["serial"] == "D1M-000123" and dev["hotspot_ssid"] == "NW-000123"
    assert st.code not in paths.device.read_text(), "机器人里只存哈希"
    assert router.hotspot == ("NW-000123", b.HOTSPOT_PSK) and st.hotspot_psk == b.HOTSPOT_PSK
    assert paths.tls_key.stat().st_mode & 0o077 == 0
    env = paths.env.read_text()
    assert "D1MAX_MAP=unmapped:0" in env and "D1MAX_SITE_MQTT=\n" in env
    assert "D1MAX_BIND_HOST=192.168.1.2" in env
    assert fa.check(paths, st.encode()) == []
    [txt] = fa.label_files(st, tmp_path / "label")
    assert b.pretty_code(st.code) in txt.read_text() and txt.stat().st_mode & 0o077 == 0
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "sticker_label", Path(__file__).resolve().parents[1] / "tools" / "sticker_label.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    svg = mod.render(txt)
    assert svg.suffix == ".svg" and "<svg" in svg.read_text()


def test_烧写过了不重来_检验抓得到贴错的(出厂):
    paths, router, st, pub = 出厂
    with pytest.raises(fa.FactoryError, match="烧写过了"):
        fa.burn(paths, "D1M-000123", router=router, unbind_pub=pub)
    wrong = b.Sticker(st.serial, b.new_code(), st.hotspot_ssid, st.hotspot_psk, st.fingerprint)
    assert any("绑定码" in x for x in fa.check(paths, wrong.encode()))
    other = b.Sticker("D1M-9", st.code, "NW-9", st.hotspot_psk, "0" * 32)
    bad = fa.check(paths, other.encode())
    assert len(bad) == 3
    assert fa.check(paths, "乱码")[0].startswith("贴纸读不出来")


def test_重做贴纸_旧的作废_绑着不让(出厂):
    paths, router, st, _ = 出厂
    new = fa.rekey(paths, router=router)
    assert new.code != st.code and new.hotspot_psk == st.hotspot_psk == b.HOTSPOT_PSK
    assert new.fingerprint != st.fingerprint and new.hotspot_ssid == st.hotspot_ssid
    assert fa.check(paths, st.encode()) and fa.check(paths, new.encode()) == []
    write_json(paths.state, {"state": "bound"})
    with pytest.raises(fa.FactoryError, match="先在站点上解绑"):
        fa.rekey(paths, router=router)


def test_清机_删客户数据_留出厂的_env回出厂值(出厂, tmp_path):
    paths, router, st, _ = 出厂
    (paths.data_root / "agent" / "maps").mkdir(parents=True)
    (paths.data_root / "agent" / "maps" / "m.pgm").write_bytes(b"x")
    (paths.data_root / "clips").mkdir()
    outbox = tmp_path / "usb" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "a.jpg").write_bytes(b"x")
    (paths.etc / "tls").mkdir()
    (paths.etc / "tls" / "robot.key").write_bytes(b"k")
    for n in ("registration.json", "evidence-pub.key", "payload.json", "release-pub.pem"):
        (paths.etc / n).write_text("x")
    from d1max_patrol.provision import set_env
    set_env(paths.env, "D1MAX_SITE_MQTT", "mqtts://s:8883")
    set_env(paths.env, "D1MAX_MAP", "estate:3")
    set_env(paths.env, "D1MAX_HAL", "d1max")
    router.connect_wifi("Estate", "12345678")
    ran = []
    p2 = BindPaths(etc=paths.etc, data_root=paths.data_root, extra_data=(outbox,))
    assert wp.wipe(p2, router=router, run=lambda cmd: ran.append(list(cmd)) or 0,
                   trim=lambda m: ran.append(["fstrim", m]) or (0, "")) == []
    assert list(paths.data_root.iterdir()) == [] and list(outbox.iterdir()) == []
    assert paths.data_root.is_dir(), "目录本身留着(属主、权限是装机脚本定的)"
    assert not (paths.etc / "tls").exists() and not (paths.etc / "registration.json").exists()
    assert not (paths.etc / "evidence-pub.key").exists()
    for keep in ("payload.json", "release-pub.pem", "device.json", "unbind-pub.pem"):
        assert (paths.etc / keep).exists(), keep
    assert paths.tls_key.exists()
    env = paths.env.read_text()
    assert "D1MAX_SITE_MQTT=\n" in env and "D1MAX_MAP=unmapped:0" in env
    assert "D1MAX_HAL=d1max" in env, "设备配置留着"
    assert router.ssid == ""
    assert ["systemctl", "stop", "d1max-agent"] in ran
    assert ["journalctl", "--vacuum-time=1s"] in ran
    assert any(c[0] == "fstrim" for c in ran)


def test_清机_一步失败照做完别的_报出来(出厂):
    paths, router, *_ = 出厂
    (paths.data_root).mkdir(parents=True, exist_ok=True)
    (paths.data_root / "x").write_text("x")
    failed = wp.wipe(paths, router=router,
                     run=lambda cmd: 1 if cmd[0] == "journalctl" else 0, trim=lambda m: (0, ""))
    assert failed == ["清系统日志"]
    assert not (paths.data_root / "x").exists()


def _https(core, paths):
    srv = sv.make_https(core, "127.0.0.1", 0, paths.tls_cert, paths.tls_key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _req(port, fp32, method, path, code, body=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
    conn.connect()
    der = conn.sock.getpeercert(binary_form=True)
    assert hashlib.sha256(der).hexdigest()[:32] == fp32, "贴纸上的指纹钉得住"
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                 headers={"X-Bind-Code": code, "Content-Type": "application/json"})
    r = conn.getresponse()
    d = json.loads(r.read())
    conn.close()
    return r.status, d


def test_热点口和代理套接字(出厂, tmp_path):
    paths, router, st, _ = 出厂
    core = BindCore(paths, router=router, site=None, provision=str, wipe=list,
                    start_services=lambda: None, now_ms=lambda: 1)
    srv = _https(core, paths)
    port = srv.server_address[1]
    sock = tmp_path / "bind.sock"
    unix = sv.make_unix(core, sock)
    threading.Thread(target=unix.serve_forever, daemon=True).start()
    try:
        idle = socket.create_connection(("127.0.0.1", port))   # 外审 I6:不握手的连接堵不住别人
        assert _req(port, st.fingerprint, "GET", "/status", "WRONG")[0] == 403
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)),
                             server_hostname="x") as raw:   # 外审 C5:负数长度拒
            raw.sendall(b"POST /bind HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n"
                        + b"x" * 4096)
            assert raw.recv(64).startswith(b"HTTP/1.0 400")
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)),
                             server_hostname="x") as raw:   # 分块发送的拒(不当成空的)
            raw.sendall(b"POST /bind HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"2\r\n{}\r\n0\r\n\r\n")
            assert raw.recv(64).startswith(b"HTTP/1.0 411")
        idle.close()
        code, d = _req(port, st.fingerprint, "GET", "/status", st.code)
        assert code == 200 and d["state"] == "unbound" and d["serial"] == "D1M-000123"
        code, d = _req(port, st.fingerprint, "POST", "/bind", st.code,
                       {"wifi": {"ssid": "Estate", "psk": "12345678"},
                        "site": {"url": "https://s:8443", "fp": "ab" * 32, "name": "庄园"}})
        assert code == 200 and d["state"] == "pending"
        assert _req(port, st.fingerprint, "POST", "/nope", st.code, {})[0] == 404
        assert sock.stat().st_mode & 0o777 == 0o660
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(sock))
            f = s.makefile("rwb")
            for msg in ({"op": "net"}, {"op": "unbind", "cert": "x"}, {"op": "?"}):
                f.write(json.dumps(msg).encode() + b"\n")
                f.flush()
                out = json.loads(f.readline())
                if msg["op"] == "net":
                    assert out["net"]["wifi"]["ssid"] == "Estate"
                else:
                    assert "error" in out
    finally:
        srv.shutdown()
        unix.shutdown()


def test_外审I8_服务停不下就不删(出厂):
    paths, router, *_ = 出厂
    paths.data_root.mkdir(parents=True, exist_ok=True)
    (paths.data_root / "x").write_text("x")
    failed = wp.wipe(paths, router=router,
                     run=lambda cmd: 1 if cmd[:2] == ["systemctl", "stop"] else 0,
                     trim=lambda m: (0, ""))
    assert failed and "数据还没删" in failed[0]
    assert (paths.data_root / "x").exists()


def test_外审I9_fstrim给挂载点(tmp_path):
    assert wp.mountpoint(tmp_path / "a" / "b") == wp.mountpoint(tmp_path)
    assert wp.mountpoint(Path("/")) == Path("/")


def test_外审2D_trim真出错不算清完_盘不支持不卡住(出厂):
    paths, router, *_ = 出厂
    paths.data_root.mkdir(parents=True, exist_ok=True)
    ok = lambda cmd: 0  # noqa: E731
    assert wp.wipe(paths, router=router, run=ok,
                   trim=lambda m: (1, "fstrim: /: the discard operation is not supported")) == []
    bad = wp.wipe(paths, router=router, run=ok,
                  trim=lambda m: (1, "fstrim: /: FITRIM ioctl failed: I/O error"))
    assert bad and "fstrim" in bad[0]


def test_外审2C_清机标记挡住所有业务服务():
    from pathlib import Path as P
    deploy = P(__file__).resolve().parents[1] / "deploy"
    for u in wp.UNITS:
        text = (deploy / f"{u}.service").read_text(encoding="utf-8")
        assert "ConditionPathExists=!/etc/d1max/bind/wiping" in text, u


def test_外审2E_重做贴纸_配网服务停不下就不换(出厂, tmp_path, monkeypatch, capsys):
    import argparse

    from d1max_patrol.bind import cli as bc
    from d1max_patrol.bind import wipe as wpm
    paths, router, st, _ = 出厂
    before = read_json(paths.device)["code_hash"]
    calls = []

    def 跑(cmd):
        calls.append(list(cmd))
        return 1 if cmd[:2] == ["systemctl", "stop"] else 0      # 在跑,停不下
    monkeypatch.setattr(wpm, "_run", 跑)
    args = argparse.Namespace(etc=str(paths.etc), data_root=str(paths.data_root), outbox="",
                              router="fake", no_service=False, factory_command="rekey",
                              out=str(tmp_path / "out"))
    assert bc._factory(args) == 1
    assert read_json(paths.device)["code_hash"] == before, "停不下就不换"
    assert "停不下来" in capsys.readouterr().err
