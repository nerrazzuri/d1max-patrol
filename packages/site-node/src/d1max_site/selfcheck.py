"""站点主机装完自检(RC 加固 H6):``d1max-site selfcheck``,逐条 PASS / WARN / FAIL,有 FAIL 退出码 1。

狗上早就有出厂自检(``d1max-patrol selfcheck``),站点这头原来靠人照着清单一条条看。目标是

    干净的 Ubuntu → install-site.sh → 填配置、放密钥 → 起服务 → selfcheck

中间不再有「还要记得手动敲这几条」。跟站点里的体检(``/api/health``)不是一回事:体检是站点**跑着的
时候**自己看自己;自检是**从外面**看这台主机装对了没有,站点起不来的时候也能跑。

- FAIL:站点现在用不了,或者一出事就救不回来(密钥缺了、服务没起、探活不通、证书过期)。
- WARN:能用,但少了一块(没管理员、没配备份、没配推送、读不到日志、证书快到期)。
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_site import keyvault

USER = "d1max-site"
UNITS = ("d1max-mosquitto", "d1max-site")
RELEASE_PUB = Path("/etc/d1max-site/release-pub.pem")
CERT_WARN_DAYS = 30


@dataclass
class Check:
    level: str            # PASS / WARN / FAIL
    name: str
    detail: str


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _tcp(host: str, port: int, timeout_s: float = 3.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return ""
    except OSError as exc:
        return str(exc)[:200]


def _healthz(port: int, ca: Path, timeout_s: float = 5.0) -> tuple[int, dict[str, Any] | str]:
    """问本机的探活。证书用站点自己的 CA 验(不核主机名:127.0.0.1 不一定在证书里)。"""
    try:
        ctx = ssl.create_default_context(cafile=str(ca))
        ctx.check_hostname = False
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", timeout=timeout_s,
                                    context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, ""
    except (OSError, ssl.SSLError, ValueError) as exc:
        return 0, str(exc)[:200]


def _disk_free(path: Path) -> float:
    u = shutil.disk_usage(path)
    return u.free / u.total


def selfcheck(home: Path, cfg: dict[str, Any], *, api_port: int = 8443, user: str = USER,
              run: Callable[[list[str]], tuple[int, str]] = _run,
              tcp: Callable[[str, int], str] = _tcp,
              healthz: Callable[[int, Path], tuple[int, Any]] = _healthz,
              release_pub: Path = RELEASE_PUB, keys_root: Path | None = None,
              disk_free: Callable[[Path], float] = _disk_free) -> list[Check]:
    out: list[Check] = []

    def add(level: str, name: str, detail: str) -> None:
        out.append(Check(level, name, detail))

    # ---- 站点身份、证书
    site_id = str(cfg.get("site_id") or "")
    if site_id:
        add("PASS", "站点身份", site_id)
    else:
        add("FAIL", "站点身份", "site.json 里没有 site_id")
    crt = home / "ca" / "server" / "server.crt"
    if not crt.is_file():
        add("FAIL", "站点证书", f"没有 {crt}(未 init,也未恢复?)")
    else:
        expired, _ = run(["openssl", "x509", "-in", str(crt), "-noout", "-checkend", "0"])
        soon, _ = run(["openssl", "x509", "-in", str(crt), "-noout", "-checkend",
                       str(CERT_WARN_DAYS * 86400)])
        if expired != 0:
            add("FAIL", "站点证书", "已过期(或者无法读取):机器人和手机都无法连接")
        elif soon != 0:
            add("WARN", "站点证书", f"不到 {CERT_WARN_DAYS} 天就到期")
        else:
            add("PASS", "站点证书", f"{CERT_WARN_DAYS} 天内不到期")

    # ---- 密钥
    for row in keyvault.status(cfg, root=keys_root):
        if row["problem"]:
            add("FAIL", f"密钥 {row['name']}", row["problem"])
        elif row["present"]:
            add("PASS", f"密钥 {row['name']}", f"{row['mode']} 指纹 {row['fingerprint']}")
    add("PASS", "发行公钥", "登记软件版本时会验签") if release_pub.is_file() else add(
        "WARN", "发行公钥", f"没有 {release_pub}:无法登记要下发给机器人的软件版本")

    # ---- 账号、备份、推送
    try:
        con = sqlite3.connect(f"file:{home / 'site.db'}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM accounts WHERE role='admin' AND disabled=0"
                            ).fetchone()[0]
        finally:
            con.close()
        add("PASS", "管理员", f"{n} 个") if n else add(
            "WARN", "管理员", "一个都没有:d1max-site add-admin <名字>")
    except sqlite3.Error as exc:
        add("FAIL", "站点库", f"打不开 {home / 'site.db'}:{exc}")
    dest = cfg.get("backup_dir")
    if not dest:
        add("WARN", "备份", "site.json 未配置 backup_dir:主机损坏将导致数据丢失")
    elif not Path(dest).is_dir():
        add("FAIL", "备份", f"备份目录 {dest} 不在(备份盘未挂上?)")
    else:
        add("PASS", "备份", str(dest))
    add("PASS", "推送", "配了") if cfg.get("push") else add(
        "WARN", "推送", "未配置:App 不在前台时手机无法接收 P1(docs/告警推送配置.md)")

    # ---- 这台主机
    rc, groups = run(["id", "-nG", user])
    if rc != 0:
        add("FAIL", "站点用户", f"没有 {user} 此用户")
    elif "systemd-journal" not in groups.split():
        add("WARN", "读日志", f"{user} 不在 systemd-journal 组:诊断包里没有站点日志(重跑装机脚本)")
    else:
        add("PASS", "读日志", "诊断包读得到站点日志")
    for unit in UNITS:
        _, active = run(["systemctl", "is-active", unit])
        _, enabled = run(["systemctl", "is-enabled", unit])
        if active != "active":
            add("FAIL", f"服务 {unit}", f"{active or '没起'}:systemctl start {unit}")
        elif enabled != "enabled":
            add("WARN", f"服务 {unit}", "正在运行,但未设置开机自启")
        else:
            add("PASS", f"服务 {unit}", "正在运行,开机自启")
    port = int(cfg.get("broker_port") or 8883)
    why = tcp("127.0.0.1", port)
    add("FAIL", "机器人连的端口", f"{port} 无法连接:{why}") if why else add(
        "PASS", "机器人连的端口", f"{port} 开着")
    code, body = healthz(api_port, home / "ca" / "ca.crt")
    if code == 200 and isinstance(body, dict) and body.get("ok"):
        add("PASS", "探活", f"站点 {body.get('version') or ''} 回好")
    elif code == 0:
        add("FAIL", "探活", f"https://127.0.0.1:{api_port}/healthz 不通:{body}")
    else:
        add("FAIL", "探活", f"站点回 {code}:要紧的体检项未通过(值班台 System 页看是哪一项)")
    _, chrony = run(["systemctl", "is-active", "chrony"])
    add("PASS", "对时服务", "chrony 正在运行(机器人跟此主机对时)") if chrony == "active" else add(
        "WARN", "对时服务", f"chrony {chrony or '没起'}:机器人时钟未同步时,命令会被当成过期的")
    try:
        free = disk_free(home)
        add("PASS" if free >= 0.15 else "WARN", "盘", f"还剩 {free:.0%}")
    except OSError as exc:
        add("WARN", "盘", f"无法测量:{exc}")
    return out


def render(checks: list[Check]) -> str:
    lines = [f"[{c.level}] {c.name}: {c.detail}" for c in checks]
    fails = sum(c.level == "FAIL" for c in checks)
    warns = sum(c.level == "WARN" for c in checks)
    lines.append(f"FAIL {fails} 条,WARN {warns} 条" + ("" if fails else ":能用了"
                 + (",WARN 的看一眼" if warns else "")))
    return "\n".join(lines)
