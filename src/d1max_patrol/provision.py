"""狗上开通、出厂自检(商业化 A4)。

**开通**(:func:`provision`,``d1max-patrol provision --code '<开通码>'``,以 root 跑):

1. 解开站点给的开通码(站点接口地址、站点服务证书指纹、MQTT 地址、狗的编号、一次性令牌);
2. 连站点接口,**先核站点服务证书的指纹**(钉住:对不上就断,令牌一个字节都不发 ——
   冒充的站点拿不到);
3. 拿令牌领证书包(站点那头给了就作废);
4. 放到 ``/etc/d1max/``:``tls/ca.crt``、``tls/robot.crt``、``tls/robot.key``(0600)、
   ``registration.json``、``evidence-pub.key``,属主是代理的账号(代理自己要读私钥);
   先写临时文件再换名;
5. ``/etc/d1max/env`` 里填 ``D1MAX_SITE_MQTT``(别的行原样不动)。

已经开通过**别的**狗编号的,不盖(加 ``--force`` 才盖):一台狗只该有一个身份。

**出厂自检**(:func:`selfcheck`,``d1max-patrol selfcheck``):逐条 PASS / WARN / FAIL,
有 FAIL 退出码 1。证书包、私钥权限、发行公钥、证据公钥、``env`` 必填项、用自己的证书连得上
站点的 MQTT、对时、盘、代理服务。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ETC = Path("/etc/d1max")
#: 证书包里的文件 → 狗上放哪儿(相对 ``/etc/d1max``)、权限。
PLACES = {"ca.crt": ("tls/ca.crt", 0o644), "robot.crt": ("tls/robot.crt", 0o644),
          "robot.key": ("tls/robot.key", 0o600), "registration.json": ("registration.json", 0o644),
          "evidence-pub.key": ("evidence-pub.key", 0o644)}
REQUIRED = ("ca.crt", "robot.crt", "robot.key", "registration.json")


class ProvisionError(RuntimeError):
    pass


def decode(code: str) -> dict[str, Any]:
    """开通码(``D1MAX1.<base64url(JSON)>``,跟站点 ``d1max_site.provision`` 同一个格式)。"""
    code = code.strip()
    if not code.startswith("D1MAX1."):
        raise ProvisionError("不是开通码(要以 D1MAX1. 开头)")
    body = code[len("D1MAX1."):]
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError) as exc:
        raise ProvisionError("开通码坏了(抄错了?)") from exc
    for k in ("api", "fp", "mqtt", "id", "t"):
        if not isinstance(d.get(k), str) or not d[k]:
            raise ProvisionError(f"开通码里缺 {k}")
    return d


def claim(code: dict[str, Any], *, timeout_s: float = 20.0) -> dict[str, bytes]:
    """连站点接口、**先核证书指纹**、再拿令牌领证书包。回 ``{文件名: 内容}``。"""
    u = urlparse(code["api"])
    if u.scheme != "https" or not u.hostname:
        raise ProvisionError(f"站点接口地址不对:{code['api']}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                     # 不靠 CA 链:靠下面钉住的指纹
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout_s, context=ctx)
    try:
        try:
            conn.connect()
        except OSError as exc:
            raise ProvisionError(f"连不上站点 {u.hostname}:{u.port}:{exc}") from exc
        der = conn.sock.getpeercert(binary_form=True) or b""
        got = hashlib.sha256(der).hexdigest()
        if got != code["fp"].lower():
            raise ProvisionError(f"站点证书指纹对不上(开通码里是 {code['fp'][:16]}…,"
                                 f"连上的是 {got[:16]}…):不是那个站点,没发令牌")
        body = json.dumps({"robot_id": code["id"], "token": code["t"]}).encode()
        conn.request("POST", "/api/enroll/claim", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        d = {}
    if resp.status != 200:
        raise ProvisionError(f"站点不给:{d.get('error') or resp.status}")
    files = {k: base64.b64decode(v) for k, v in (d.get("files") or {}).items() if k in PLACES}
    missing = [n for n in REQUIRED if n not in files]
    if missing:
        raise ProvisionError(f"站点给的证书包不全:缺 {'、'.join(missing)}")
    return files


def _owner(user: str | None) -> tuple[int, int] | None:
    if not user:
        return None
    import pwd
    try:
        p = pwd.getpwnam(user)
    except KeyError as exc:
        raise ProvisionError(f"没有用户 {user}(代理用哪个账号跑:装机脚本的 D1MAX_USER)") from exc
    return p.pw_uid, p.pw_gid


def _write(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)                           # 先收紧再写(私钥从写下去那一刻就是 0600)
        if owner is not None:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_env(env: Path, key: str, value: str) -> None:
    """``env`` 里把 ``key=`` 那一行换成新值(没有就加在最后);别的行原样不动。"""
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    mode = env.stat().st_mode & 0o777 if env.is_file() else 0o640
    st = env.stat() if env.is_file() else None
    _write(env, ("\n".join(out) + "\n").encode("utf-8"), mode,
           (st.st_uid, st.st_gid) if st is not None else None)


def provision(code_text: str, *, etc: Path = ETC, user: str | None = "robot", force: bool = False,
              claimer: Callable[[dict[str, Any]], dict[str, bytes]] = claim) -> str:
    """开通。回狗的编号。"""
    code = decode(code_text)
    reg = etc / "registration.json"
    if reg.is_file() and not force:
        try:
            old = json.loads(reg.read_text(encoding="utf-8")).get("robot_id")
        except (OSError, ValueError):
            old = None
        if old and old != code["id"]:
            raise ProvisionError(f"这台狗已经开通成 {old} 了,开通码是给 {code['id']} 的:"
                                 "一台狗只该有一个身份;确定要换加 --force")
    owner = _owner(user)
    files = claimer(code)
    for name, data in files.items():
        rel, mode = PLACES[name]
        _write(etc / rel, data, mode, owner)
    set_env(etc / "env", "D1MAX_SITE_MQTT", code["mqtt"])
    return code["id"]


# ------------------------------------------------------------ 出厂自检


@dataclass
class Check:
    level: str            # PASS / WARN / FAIL
    name: str
    detail: str


def _env(etc: Path) -> dict[str, str]:
    out = {}
    p = etc / "env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    return out


def _mqtt_tls(url: str, etc: Path, timeout_s: float = 10.0) -> str:
    """用自己的证书跟站点 MQTT 做一次 TLS 握手。回空串 = 通了。"""
    u = urlparse(url)
    if not u.hostname:
        return f"地址不对:{url}"
    try:
        ctx = ssl.create_default_context(cafile=str(etc / "tls" / "ca.crt"))
        ctx.check_hostname = True
        ctx.load_cert_chain(str(etc / "tls" / "robot.crt"), str(etc / "tls" / "robot.key"))
        with socket.create_connection((u.hostname, u.port or 8883), timeout=timeout_s) as s, \
                ctx.wrap_socket(s, server_hostname=u.hostname):
            return ""
    except (OSError, ssl.SSLError) as exc:
        return str(exc)[:200]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def selfcheck(*, etc: Path = ETC, user: str | None = "robot",
              mqtt_probe: Callable[[str, Path], str] = _mqtt_tls,
              run: Callable[[list[str]], tuple[int, str]] = _run,
              disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> list[Check]:
    out: list[Check] = []
    rid = ""
    try:
        rid = json.loads((etc / "registration.json").read_text(encoding="utf-8"))["robot_id"]
        out.append(Check("PASS", "注册文件", f"狗的编号 {rid}"))
    except (OSError, ValueError, KeyError) as exc:
        out.append(Check("FAIL", "注册文件", f"读不了 registration.json:{exc}(还没开通?)"))
    for name in ("ca.crt", "robot.crt", "robot.key"):
        p = etc / "tls" / name
        if not p.is_file():
            out.append(Check("FAIL", f"证书 {name}", "没有(还没开通?)"))
    key = etc / "tls" / "robot.key"
    if key.is_file():
        st = key.stat()
        bad = st.st_mode & 0o077
        own = _owner(user) if user else None
        if bad:
            out.append(Check("FAIL", "私钥权限", f"{oct(st.st_mode & 0o777)}:别的用户读得到"))
        elif own is not None and st.st_uid != own[0]:
            out.append(Check("FAIL", "私钥权限", f"属主不是 {user}:代理读不了自己的私钥"))
        else:
            out.append(Check("PASS", "私钥权限", oct(st.st_mode & 0o777)))
    pub = etc / "release-pub.pem"
    extra = etc / "release-pub.d"
    if pub.is_file() or (extra.is_dir() and any(extra.glob("*.pem"))):
        out.append(Check("PASS", "发行公钥", "站点下发的升级会验签"))
    else:
        out.append(Check("WARN", "发行公钥",
                         "没有:站点下发的升级会被拒(包里带上 deploy/release-pub.pem 重装)"))
    out.append(Check("PASS", "证据公钥", "照片、录像封着存") if (etc / "evidence-pub.key").is_file()
               else Check("WARN", "证据公钥", "没有:照片、录像明文存在狗上(站点没配证据私钥?)"))
    env = _env(etc)
    for k in ("D1MAX_SITE_MQTT", "D1MAX_MAP", "D1MAX_HOME"):
        if env.get(k):
            out.append(Check("PASS", f"env {k}", env[k]))
        else:
            out.append(Check("FAIL", f"env {k}", "没填:代理不起"))
    if env.get("D1MAX_SITE_MQTT") and all((etc / "tls" / n).is_file()
                                          for n in ("ca.crt", "robot.crt", "robot.key")):
        why = mqtt_probe(env["D1MAX_SITE_MQTT"], etc)
        out.append(Check("FAIL", "连站点", f"{env['D1MAX_SITE_MQTT']}:{why}") if why
                   else Check("PASS", "连站点", f"用自己的证书连上了 {env['D1MAX_SITE_MQTT']}"))
    rc, txt = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    out.append(Check("PASS", "对时", "已同步") if rc == 0 and txt == "yes"
               else Check("WARN", "对时", f"没同步({txt or rc}):录像、告警的时间会不准"))
    try:
        u = disk_usage(Path("/"))
        free = u.free / u.total
        out.append(Check("PASS" if free >= 0.15 else "WARN", "盘", f"还剩 {free:.0%}"))
    except OSError as exc:
        out.append(Check("WARN", "盘", f"量不了:{exc}"))
    rc, txt = run(["systemctl", "is-active", "d1max-agent"])
    out.append(Check("PASS", "代理服务", "在跑") if txt == "active"
               else Check("WARN", "代理服务", f"{txt or rc}:开通、填好 env 以后 "
                                               "sudo systemctl restart d1max-agent"))
    return out
