"""W45 站点 VPN(决策 71):WireGuard 密钥(纯 Python X25519)、站点、机器人、手机的配置、
分地址、吊销。"""

from __future__ import annotations

import base64
import os

import pytest

from d1max_site import vpn


def test_X25519_对得上RFC7748的测试向量():
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    want = "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    assert vpn.x25519(k, u).hex() == want
    # 7.1 的 Alice:公钥 = X25519(私钥, 9)
    a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    want = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert vpn.public_key(a).hex() == want


def test_密钥是WireGuard那种base64_44个字符():
    priv, pub = vpn.keypair()
    assert len(priv) == len(pub) == 44 and len(base64.b64decode(pub)) == 32
    assert base64.b64decode(priv)
    assert vpn.public_of(priv) == pub


def test_初始化_加机器人加手机_分地址_出配置_文件600(tmp_path):
    st = vpn.VpnState(tmp_path / "vpn")
    st.init(endpoint="home.example.net", port=51820)
    r = st.add("robot", "C40221")
    p = st.add("phone", "alice-phone")
    assert r["address"] == "172.29.70.10" and p["address"] == "172.29.70.100"
    srv = st.server_conf()
    assert "Address = 172.29.70.1/24" in srv and "ListenPort = 51820" in srv
    assert srv.count("[Peer]") == 2 and "AllowedIPs = 172.29.70.10/32" in srv
    conf = st.peer_conf("C40221")
    assert "Address = 172.29.70.10/32" in conf and "Endpoint = home.example.net:51820" in conf
    assert "AllowedIPs = 172.29.70.0/24" in conf, "只走隧道网段:上网照旧走路由器"
    assert "PersistentKeepalive = 25" in conf and "PresharedKey = " in conf
    for f in (tmp_path / "vpn").rglob("*"):
        if f.is_file():
            assert os.stat(f).st_mode & 0o077 == 0, f
    st2 = vpn.VpnState(tmp_path / "vpn")                     # 重开:还在
    assert [x["name"] for x in st2.list()] == ["C40221", "alice-phone"]


def test_重名拒_没初始化拒_名字写错拒(tmp_path):
    st = vpn.VpnState(tmp_path / "vpn")
    with pytest.raises(vpn.VpnError, match="init"):
        st.add("robot", "C40221")
    st.init(endpoint="1.2.3.4")
    st.add("robot", "C40221")
    with pytest.raises(vpn.VpnError, match="已经有了"):
        st.add("phone", "C40221")
    for bad in ("", "a b", "x" * 65, "../x"):
        with pytest.raises(vpn.VpnError):
            st.add("phone", bad)
    with pytest.raises(vpn.VpnError):
        st.add("printer", "p1")
    with pytest.raises(vpn.VpnError):
        st.init(endpoint="1.2.3.4")                           # 已经初始化过:不覆盖站点密钥


def test_删了的地址不复用_站点配置里没了它(tmp_path):
    st = vpn.VpnState(tmp_path / "vpn")
    st.init(endpoint="1.2.3.4")
    st.add("robot", "A")
    st.add("robot", "B")
    st.remove("A")
    assert "172.29.70.10/32" not in st.server_conf()
    assert st.add("robot", "C")["address"] == "172.29.70.12", \
        "删掉的地址不马上给新来的(旧配置还在谁手里)"
    with pytest.raises(vpn.VpnError):
        st.remove("没有这个")


def test_命令行_init_add_list_server_conf(tmp_path, capsys):
    from d1max_site.main import main
    home = tmp_path / "site"
    home.mkdir()
    assert main(["--home", str(home), "vpn", "init", "--endpoint", "home.example.net"]) == 0
    assert main(["--home", str(home), "vpn", "add", "robot", "C40221"]) == 0
    out = capsys.readouterr().out
    assert "172.29.70.10" in out and "C40221.conf" in out
    assert (home / "vpn" / "peers" / "C40221.conf").exists()
    assert main(["--home", str(home), "vpn", "list"]) == 0
    assert "C40221" in capsys.readouterr().out
    assert main(["--home", str(home), "vpn", "server-conf"]) == 0
    assert "[Interface]" in capsys.readouterr().out


def test_新站点的证书里就带隧道地址():
    from d1max_site.ca import LOCAL_NAMES
    assert "172.29.70.1" in LOCAL_NAMES


def test_老站点证书里没有隧道地址_init说清楚_要明着重签_重签打新指纹(tmp_path, capsys):
    from d1max_site.main import cmd_fingerprint, main
    home = tmp_path / "site"
    assert main(["--home", str(home), "init", "--site-id", "s1", "--hostname", "site.lan"]) == 0
    ext = home / "ca" / "server" / "server_ext.cnf"
    ext.write_text(ext.read_text().replace(",IP:172.29.70.1", ""))   # 当它是老站点建的
    capsys.readouterr()
    assert main(["--home", str(home), "vpn", "init", "--endpoint", "1.2.3.4"]) == 0
    out = capsys.readouterr().out
    assert "reissue-cert" in out and "手机" in out
    old = cmd_fingerprint(home)
    assert main(["--home", str(home), "vpn", "reissue-cert"]) == 0
    out = capsys.readouterr().out
    new = cmd_fingerprint(home)
    assert new != old and new in out and "重新添加站点" in out
    assert "IP:172.29.70.1" in ext.read_text()
