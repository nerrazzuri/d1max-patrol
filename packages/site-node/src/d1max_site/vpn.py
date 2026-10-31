"""站点 VPN(W45,决策 71):站点主机跑 WireGuard,机器人(上装 V2 的 RK3588)
和管理员、经理、保安的手机连进来。

为什么:机器人在 4G 上(WiFi 断了、在现场边上)、手机在外面,都要连回站点;**不经公司服务器**,
画面不出家门。
WiFi 和 4G 来回切时路由器的外网地址会变,WireGuard 自己重新握手;隧道里的地址不变,代理的连接和视频都走
隧道,所以不断。现场的路由器只开一个 UDP 端口(缺省 51820),站点的网页、登录页不直接对外。

这里管的是**密钥和配置**(纯 Python 的 X25519,不依赖 ``wg``、``cryptography``):

- ``init``:站点的密钥对、对外地址(域名或公网 IP + 端口)、隧道网段 :data:`SUBNET`(站点是 ``.1``)。
- ``add robot|phone <名字>``:给一台机器人(``.10`` 起)、一部手机(``.100`` 起)分地址、
  生成密钥和预共享密钥,写出它的配置文件 ``peers/<名字>.conf``
  (机器人拷到 ``/etc/wireguard/d1max.conf``,手机用 WireGuard 应用导入)。
- ``remove``:删掉一个(站点配置里没了它;地址不马上给新来的,免得旧配置还在谁手里时撞上)。
- ``server_conf``:站点自己的 ``wg-quick`` 配置(``/etc/wireguard/d1max.conf``)。

状态在 ``<站点目录>/vpn/state.json``,文件、目录都只许站点用户读(600 / 700):里面有私钥。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: 隧道网段。跟上装的 172.29.66/67 一个系列,避开现场 WiFi 常见的 192.168.0/1.x、10.0.0.x。
SUBNET = ipaddress.ip_network("172.29.70.0/24")
SITE_HOST = 1
#: 各类从哪个号开始分、到哪为止。
RANGES = {"robot": (10, 99), "phone": (100, 199), "engineer": (200, 250)}
#: W55:工程师的地址到期就收回、可以再分(别的种类删了不马上再分,免得旧配置还在谁手里时撞上)。
REUSABLE = frozenset({"engineer"})
#: 隧道里的转发规则单独一条链,增删工程师、重装配置时整条清掉重建,不留旧规则。
CHAIN = "D1MAX-VPN"
DEFAULT_PORT = 51820
KEEPALIVE_S = 25
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class VpnError(ValueError):
    """VPN 配置写错了、没初始化、重名(命令行说清楚)。"""


# ------------------------------------------------------------ X25519(RFC 7748)

_P = 2**255 - 19
_A24 = 121665


def _clamp(k: bytes) -> int:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(bytes(b), "little")


def x25519(k: bytes, u: bytes) -> bytes:
    """RFC 7748 第 5 节的 X25519(蒙哥马利阶梯,常数步数)。"""
    if len(k) != 32 or len(u) != 32:
        raise ValueError("X25519 要 32 字节")
    scalar = _clamp(k)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        kt = (scalar >> t) & 1
        swap ^= kt
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = kt
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def public_key(private: bytes) -> bytes:
    return x25519(private, (9).to_bytes(32, "little"))


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def keypair() -> tuple[str, str]:
    """WireGuard 格式的(私钥, 公钥),都是 44 个字符的 base64。"""
    priv = bytearray(secrets.token_bytes(32))
    priv[0] &= 248
    priv[31] = (priv[31] & 127) | 64
    return _b64(bytes(priv)), _b64(public_key(bytes(priv)))


def public_of(private_b64: str) -> str:
    return _b64(public_key(base64.b64decode(private_b64)))


# ------------------------------------------------------------ 状态

class VpnState:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / "state.json"

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(d, dict):
            raise VpnError(f"{self.path} 已损坏")
        return d

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """外审 V213-I2:读—改—存(连写 peer 配置)整段跨进程互斥。不锁的话两个命令拿同一份旧状态各存
        各的,删掉的手机凭据会被旧副本恢复回来。"""
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _save(self, d: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        tmp = self.path.with_name(f"state.json.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(d, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _changed(d: dict[str, Any]) -> None:
        """改了谁能连(外审 W55-1 第 10 条):记一笔「待生效」,装成功以后才清;没装成一直重试。"""
        d["gen"] = int(d.get("gen", 0)) + 1
        d["apply_pending"] = True

    @property
    def pending(self) -> bool:
        return bool(self._load().get("apply_pending"))

    def mark_pending(self) -> None:
        """站点起来时调一次:按状态文件重装一遍(恢复备份、改过系统配置之后都对得上)。"""
        with self._locked():
            d = self._load()
            if "server" in d:
                d["apply_pending"] = True
                self._save(d)

    def apply_doc(self) -> dict[str, Any]:
        """给特权执行器的结构化数据(它自己生成 wg-quick 配置和防火墙规则,不收命令行)。"""
        d = self._need()
        s = d["server"]
        return {"address": f"{SUBNET[SITE_HOST]}/{SUBNET.prefixlen}", "port": s["port"],
                "private": s["private"],
                "peers": [{"name": p["name"], "kind": p["kind"], "public": p["public"],
                           "psk": p["psk"], "address": f"{SUBNET[p['host']]}/32"}
                          for p in d["peers"]]}

    def write_apply(self) -> int:
        """写 ``<vpn>/apply.json``(0600),回这一份对应的修订号(装成功后交给 :meth:`applied`)。"""
        with self._locked():
            d = self._need()
            gen = int(d.get("gen", 0))
            f = self.root / "apply.json"
            tmp = f.with_name(f"apply.json.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.apply_doc(), ensure_ascii=False))
            os.replace(tmp, f)
            return gen

    def applied(self, gen: int) -> None:
        """修订号 ``gen`` 那一份装成功了:期间没再改过就清掉「待生效」。"""
        with self._locked():
            d = self._load()
            if int(d.get("gen", 0)) == gen:
                d["apply_pending"] = False
                self._save(d)

    def _need(self) -> dict[str, Any]:
        d = self._load()
        if "server" not in d:
            raise VpnError("尚未 init:先 d1max-site vpn init --endpoint <域名或公网 IP>")
        return d

    def init(self, *, endpoint: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
        with self._locked():
            return self._init(endpoint=endpoint, port=port)

    def _init(self, *, endpoint: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
        if "server" in self._load():
            raise VpnError("已 init 过了(站点密钥不覆盖;真要重来先删掉 vpn 目录,"
                           "所有配置都要重发)")
        if not endpoint or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", endpoint):
            raise VpnError("--endpoint 要是域名或公网 IP(机器人、手机从外面连站点用的地址)")
        if not 1 <= port <= 65535:
            raise VpnError("端口要在 1–65535")
        priv, pub = keypair()
        d = {"server": {"private": priv, "public": pub, "endpoint": endpoint, "port": port},
             "peers": [], "used": []}
        self._changed(d)
        self._save(d)
        return {"public": pub, "address": str(SUBNET[SITE_HOST])}

    def add(self, kind: str, name: str, *, expires_ms: int | None = None) -> dict[str, Any]:
        with self._locked():
            return self._add(kind, name, expires_ms)

    def _add(self, kind: str, name: str, expires_ms: int | None = None) -> dict[str, Any]:
        if kind not in RANGES:
            raise VpnError(f"种类要是 {'/'.join(RANGES)}")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise VpnError("名字要是 1–64 个字母、数字、点、下划线、减号(字母或数字开头)")
        d = self._need()
        if any(p["name"] == name for p in d["peers"]):
            raise VpnError(f"{name} 已存在")
        if (kind == "engineer") != (expires_ms is not None):
            raise VpnError("工程师要有到期时间,别的种类不要")
        lo, hi = RANGES[kind]
        now_used = {p["host"] for p in d["peers"]}
        used = set(d.get("used", [])) | now_used
        host = next((h for h in range(lo, hi + 1)
                     if h not in (now_used if kind in REUSABLE else used)), None)
        if host is None:
            raise VpnError(f"{kind} 的地址分完了({lo}–{hi})")
        priv, pub = keypair()
        peer = {"name": name, "kind": kind, "host": host, "private": priv, "public": pub,
                "psk": _b64(secrets.token_bytes(32))}
        if expires_ms is not None:
            peer["expires_ms"] = int(expires_ms)
        d["peers"].append(peer)
        d["used"] = sorted(used | {host})
        self._changed(d)
        self._save(d)
        self._write_peer(d, peer)
        return {"name": name, "kind": kind, "address": str(SUBNET[host]),
                "conf": str(self.root / "peers" / f"{name}.conf"), "expires_ms": expires_ms}

    def expire(self, now_ms: int) -> list[str]:
        """W55:到点的工程师收回(配置文件一起删)。回收回了谁;站点随后重装 VPN 配置。"""
        with self._locked():
            d = self._load()
            gone = [p["name"] for p in d.get("peers", [])
                    if isinstance(p.get("expires_ms"), int) and p["expires_ms"] <= now_ms]
            if not gone:
                return []
            d["peers"] = [p for p in d["peers"] if p["name"] not in gone]
            self._changed(d)
            self._save(d)
            for n in gone:
                (self.root / "peers" / f"{n}.conf").unlink(missing_ok=True)
            return gone

    @property
    def ready(self) -> bool:
        return bool(self._load().get("server"))

    def remove(self, name: str) -> None:
        with self._locked():
            return self._remove(name)

    def _remove(self, name: str) -> None:
        d = self._need()
        if not any(p["name"] == name for p in d["peers"]):
            raise VpnError(f"没有 {name}")
        d["peers"] = [p for p in d["peers"] if p["name"] != name]
        self._changed(d)
        self._save(d)
        (self.root / "peers" / f"{name}.conf").unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        d = self._load()
        return [{"name": p["name"], "kind": p["kind"], "address": str(SUBNET[p["host"]]),
                 "public": p["public"], "expires_ms": p.get("expires_ms")}
                for p in d.get("peers", [])]

    def server_conf(self) -> str:
        d = self._need()
        s = d["server"]
        out = ["# d1max 站点 VPN(W45):/etc/wireguard/d1max.conf,wg-quick@d1max",
               "[Interface]", f"Address = {SUBNET[SITE_HOST]}/{SUBNET.prefixlen}",
               f"ListenPort = {s['port']}", f"PrivateKey = {s['private']}",
               *self._forward_rules(d), ""]
        for p in d["peers"]:
            out += [f"# {p['kind']} {p['name']}", "[Peer]", f"PublicKey = {p['public']}",
                    f"PresharedKey = {p['psk']}", f"AllowedIPs = {SUBNET[p['host']]}/32", ""]
        return "\n".join(out)

    @staticmethod
    def _forward_rules(d: dict[str, Any]) -> list[str]:
        """W55:隧道里谁能连谁。只放行**工程师 → 隧道里的机器人、站点的 TCP 22**(SSH),别的
        隧道内转发一律丢(手机连不到机器人)。规则单独一条链,起停都整条清掉重建。只开这一个接口的转发。"""
        eng = [p for p in d.get("peers", []) if p.get("kind") == "engineer"]
        up = ["sysctl -q -w net.ipv4.conf.%i.forwarding=1",
              f"iptables -N {CHAIN} 2>/dev/null || true", f"iptables -F {CHAIN}",
              f"iptables -C FORWARD -i %i -j {CHAIN} 2>/dev/null || "
              f"iptables -I FORWARD -i %i -j {CHAIN}",
              f"iptables -A {CHAIN} -o %i -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
        up += [f"iptables -A {CHAIN} -o %i -s {SUBNET[p['host']]}/32 -p tcp --dport 22 -j ACCEPT"
               for p in eng]
        up += [f"iptables -A {CHAIN} -j DROP"]
        down = [f"iptables -D FORWARD -i %i -j {CHAIN} 2>/dev/null || true",
                f"iptables -F {CHAIN} 2>/dev/null || true",
                f"iptables -X {CHAIN} 2>/dev/null || true"]
        return [f"PostUp = {r}" for r in up] + [f"PostDown = {r}" for r in down]

    def peer_conf(self, name: str) -> str:
        d = self._need()
        p = next((x for x in d["peers"] if x["name"] == name), None)
        if p is None:
            raise VpnError(f"没有 {name}")
        return self._peer_text(d, p)

    @staticmethod
    def _peer_text(d: dict[str, Any], p: dict[str, Any]) -> str:
        s = d["server"]
        where = "/etc/wireguard/d1max.conf(wg-quick@d1max)" if p["kind"] == "robot" \
            else "WireGuard 应用里导入"
        if p["kind"] == "engineer":
            where += f";到期自动失效(站点时间 {p.get('expires_ms')} ms);可 SSH 到机器人、站点主机"
        return "\n".join([
            f"# d1max 站点 VPN(W45):{p['kind']} {p['name']} —— {where}", "[Interface]",
            f"Address = {SUBNET[p['host']]}/32", f"PrivateKey = {p['private']}", "",
            "[Peer]", f"PublicKey = {s['public']}", f"PresharedKey = {p['psk']}",
            # 只把隧道网段走隧道:上网、厂家的东西照旧走路由器
            f"AllowedIPs = {SUBNET}", f"Endpoint = {s['endpoint']}:{s['port']}",
            f"PersistentKeepalive = {KEEPALIVE_S}", ""])

    def _write_peer(self, d: dict[str, Any], p: dict[str, Any]) -> None:
        peers = self.root / "peers"
        peers.mkdir(parents=True, exist_ok=True)
        os.chmod(peers, 0o700)
        f = peers / f"{p['name']}.conf"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self._peer_text(d, p))
