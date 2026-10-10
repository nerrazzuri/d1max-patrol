"""站点健康与诊断(商业化 A1)。

- **体检**(:meth:`HealthDesk.checks`,``GET /api/health``,管理员):库能写、事件循环没卡、MQTT 连着、
  站点盘和备份盘够不够、备份和校验、推送、狗在线几台、证书还有多久到期(站点服务证书、每只狗的)、
  有没有卡住的后台道、版本。每一项 ``ok`` + 说人话的 ``detail``。
- **探活**(:meth:`HealthDesk.healthz`,``GET /healthz``,**不用登录**):外面的监控、以后的热备探它。
  只回好不好、版本,**不给细节**(不登录的人看不到站点的情况)。好回 200,不好回 503。
- **自己盯着**(:meth:`HealthDesk.tick`,每小时一拍):证书 30 天内到期、站点盘剩不到 10%、后台道卡住
  超过 5 分钟 → 报 **P2**,好了自动解决;按当前状态每拍对账(报、解决失败了下一拍再来)。
- **指标历史**(:meth:`HealthDesk.record`,每 5 分钟一笔,留 30 天):盘、狗在线、开着的 P1/P2、狗上
  待传的、事件循环延迟。诊断包里导成 CSV;以后手机画趋势用。
- **诊断包**(:func:`support_bundle`,``d1max-site support-bundle``、``GET /api/support-bundle``):
  一个 tar.gz,给我们远程看问题用。**不带**密钥、令牌、库文件、照片录像、狗的位置;站点配置里像口令、
  密钥的值打码;日志里长串十六进制、``Bearer`` 令牌打码。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import platform
import re
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_site.alert_sources import SITE

log = logging.getLogger(__name__)

#: 证书剩不到这么多天报 P2。
CERT_WARN_DAYS = 30
#: 盘剩不到这么多(比例)报 P2;探活也算不好。
DISK_LOW = 0.10
#: 事件循环多久没转算卡住(秒)。
LOOP_STUCK_S = 5.0
#: 后台道一轮跑了多久算卡住(秒)。
LANE_STUCK_S = 300.0
#: 多久记一笔指标、留多久。
METRICS_EVERY_MS = 5 * 60_000
METRICS_KEEP_MS = 30 * 86_400_000
#: 多久对一次账(报、解决 P2)。
TICK_EVERY_MS = 3600_000

#: 探活只看这几项:坏了站点就不能用。
CRITICAL = ("db", "loop", "broker", "disk")


def _openssl_enddate(cert: Path) -> int | None:
    """证书到期时刻(毫秒);读不了回 ``None``。"""
    try:
        got = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", str(cert)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"notAfter=(.+)", got.stdout)
    if got.returncode != 0 or m is None:
        return None
    try:
        return int(time.mktime(time.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z"))
                   - time.timezone) * 1000
    except ValueError:
        return None


def site_version() -> str:
    try:
        from importlib.metadata import version
        return version("d1max-site-node")
    except Exception:  # noqa: BLE001 - 装法不一样拿不到版本:不挡体检
        return "unknown"


class HealthDesk:
    def __init__(self, db: Any, *, home: Path, now_ms: Callable[[], int],
                 dispatcher: Any = None, backup: Any = None, push: Any = None,
                 loop_lag: Callable[[], float | None] | None = None,
                 lanes: Callable[[], dict[str, float]] | None = None,
                 disk_usage: Callable[[Path], Any] = shutil.disk_usage,
                 cert_end: Callable[[Path], int | None] = _openssl_enddate,
                 monotonic: Callable[[], float] = time.monotonic,
                 keys: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.db = db
        self.home = Path(home)
        self._now = now_ms
        self.dispatcher = dispatcher
        self.backup = backup
        self.push = push
        self._loop_lag = loop_lag
        #: 正在跑的后台道:名字 → 这一轮起跑的单调钟秒。
        self._lanes = lanes
        self._disk = disk_usage
        self._cert_end = cert_end
        self._mono = monotonic
        #: 站点密钥自检(A3,``keyvault.status``)。
        self._keys = keys
        #: 告警台(站点主程序接线程安全的那个):P2。
        self.alerts: Any = None
        self._next_tick = 0
        self._next_record = 0

    # ------------------------------------------------------------ 体检

    def _free(self, path: Path) -> float | None:
        try:
            u = self._disk(path)
            return u.free / u.total if u.total else None
        except OSError:
            return None

    def _certs(self) -> list[tuple[str, int | None]]:
        """(谁, 到期时刻):站点服务证书、每只狗的证书(吊销了的不算)。"""
        out = [("站点服务证书", self._cert_end(self.home / "ca" / "server" / "server.crt"))]
        revoked = {r["robot_id"] for r in self.db.query(
            "SELECT robot_id FROM robots WHERE revoked=1")} if self._has("robots") else set()
        issued = self.home / "ca" / "issued"
        if issued.is_dir():
            for d in sorted(issued.iterdir()):
                crt = d / "robot.crt"
                if crt.is_file() and d.name not in revoked:
                    out.append((f"狗 {d.name} 的证书", self._cert_end(crt)))
        return out

    def _has(self, table: str) -> bool:
        return bool(self.db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)))

    def checks(self) -> dict[str, dict[str, Any]]:
        now = self._now()
        out: dict[str, dict[str, Any]] = {}
        try:
            with self.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('health_ping', ?)", (str(now),))
            out["db"] = {"ok": True, "detail": "库能写"}
        except Exception as exc:  # noqa: BLE001
            out["db"] = {"ok": False, "detail": f"库写不进去:{exc}"[:200]}
        lag = self._loop_lag() if self._loop_lag is not None else None
        out["loop"] = ({"ok": True, "detail": "没接(命令行)"} if lag is None else
                       {"ok": lag < LOOP_STUCK_S, "value": round(lag, 2),
                        "detail": f"事件循环 {lag:.1f} 秒前转过"
                                  + ("" if lag < LOOP_STUCK_S else ":卡住了")})
        t = getattr(self.dispatcher, "_t", None) if self.dispatcher is not None else None
        conn = getattr(t, "connected", None)
        out["broker"] = ({"ok": True, "detail": "没接(命令行)"} if conn is None else
                         {"ok": bool(conn), "detail": "连着 MQTT" if conn else "MQTT 断了"})
        free = self._free(self.home)
        out["disk"] = ({"ok": False, "detail": "量不了站点盘"} if free is None else
                       {"ok": free >= DISK_LOW, "value": round(free, 3),
                        "detail": f"站点盘还剩 {free:.0%}"})
        dest = getattr(self.backup, "dest", None) if self.backup is not None else None
        if dest is not None:
            bfree = self._free(Path(dest))
            st = self.backup.status()
            ver = st.get("verify") or {}
            ok = bfree is not None and bfree >= DISK_LOW and not st.get("stale") \
                and ver.get("ok", True)
            out["backup"] = {"ok": ok, "value": bfree,
                             "detail": "备份盘" + (f"还剩 {bfree:.0%}" if bfree is not None
                                                   else "量不了")
                             + (";备份过期了" if st.get("stale") else "")
                             + (";上次校验没过" if ver and not ver.get("ok") else "")}
        else:
            out["backup"] = {"ok": False, "detail": "没配备份:站点主机坏了数据就没了"}
        if self.push is not None:
            v = self.push.view()
            out["push"] = {"ok": bool(v["configured"]) and not v["error"],
                           "detail": (v["why_off"] or v["error"] or
                                      f"推送已配,{v['devices']} 台手机登记了")}
        if self.dispatcher is not None:
            clients = getattr(self.dispatcher, "clients", {})
            online = [rid for rid, c in clients.items()
                      if c.status is not None and c.status.online]
            out["robots"] = {"ok": True, "value": len(online),
                             "detail": f"{len(online)}/{len(clients)} 只狗在线"}
        soon, worst = [], None
        for who, end in self._certs():
            if end is None:
                soon.append(f"{who}读不了到期时间")
                continue
            days = (end - now) / 86_400_000
            worst = days if worst is None else min(worst, days)
            if days < CERT_WARN_DAYS:
                soon.append(f"{who}还有 {max(days, 0):.0f} 天到期")
        out["certs"] = {"ok": not soon, "value": None if worst is None else round(worst, 1),
                        "detail": ";".join(soon)[:300] or (f"证书最近的还有 {worst:.0f} 天到期"
                                                          if worst is not None else "没有证书")}
        if self._keys is not None:
            bad = [f"{r['name']}:{r['problem']}" for r in self._keys() if r["problem"]]
            out["keys"] = {"ok": not bad, "detail": ";".join(bad)[:300] or "站点密钥都在、权限对"}
        lanes = self._lanes() if self._lanes is not None else {}
        stuck = [n for n, t0 in lanes.items() if self._mono() - t0 > LANE_STUCK_S]
        out["lanes"] = {"ok": not stuck,
                        "detail": ("卡住了:" + "、".join(stuck)) if stuck else "后台道都在转"}
        return out

    def healthz(self) -> tuple[bool, dict[str, Any]]:
        """探活:只回好不好、版本(不登录的人看不到细节)。"""
        c = self.checks()
        ok = all(c[k]["ok"] for k in CRITICAL if k in c)
        return ok, {"ok": ok, "version": site_version()}

    def view(self) -> dict[str, Any]:
        c = self.checks()
        return {"ok": all(v["ok"] for v in c.values()),
                "critical_ok": all(c[k]["ok"] for k in CRITICAL if k in c),
                "checks": c, "version": site_version(), "now_ms": self._now()}

    # ------------------------------------------------------------ 自己盯着、记指标

    def tick(self) -> None:
        """每小时按体检结果对账 P2(证书快到期、站点盘紧、后台道卡住);每 5 分钟记一笔指标。
        站点杂事线程里调(告警走线程安全的那个告警台)。"""
        now = self._now()
        if now >= self._next_record:
            self._next_record = now + METRICS_EVERY_MS
            self.record()
        if now < self._next_tick or self.alerts is None:
            return
        c = self.checks()
        done = True
        for kind, key, title in (("cert_expiring", "certs", "证书快到期了"),
                                 ("site_disk_low", "disk", "站点盘快满了"),
                                 ("lane_stuck", "lanes", "站点后台有活卡住了")):
            try:
                open_ = self.alerts.has_open(SITE, kind)
                if not c[key]["ok"] and not open_:
                    self.alerts.raise_alert(kind=kind, robot=SITE, title=title,
                                            detail=c[key]["detail"][:300])
                elif c[key]["ok"] and open_:
                    self.alerts.resolve_all(SITE, kind, who="site:health")
            except Exception:
                log.exception("体检告警 %s 没对上(下一拍再来)", kind)
                done = False
        if done:
            self._next_tick = now + TICK_EVERY_MS

    def record(self) -> None:
        """记一笔指标(留 30 天)。"""
        now = self._now()
        vals: dict[str, float] = {}
        free = self._free(self.home)
        if free is not None:
            vals["site_disk_free"] = free
        lag = self._loop_lag() if self._loop_lag is not None else None
        if lag is not None:
            vals["loop_lag_s"] = lag
        if self.dispatcher is not None:
            clients = getattr(self.dispatcher, "clients", {})
            vals["robots_online"] = sum(1 for c in clients.values()
                                        if c.status is not None and c.status.online)
            backlog = 0
            for st in getattr(self.dispatcher, "storage", {}).values():
                backlog += getattr(st[0], "backlog_files", 0) or 0
            vals["upload_backlog"] = backlog
        if self._has("alerts"):
            for lv in ("P1", "P2"):
                vals[f"alerts_open_{lv}"] = self.db.query(
                    "SELECT COUNT(*) AS n FROM alerts WHERE level=? AND resolved_ms IS NULL",
                    (lv,))[0]["n"]
        with self.db.tx() as c:
            c.executemany("INSERT INTO metrics(ts, key, value) VALUES (?,?,?)",
                          [(now, k, float(v)) for k, v in vals.items()])
            c.execute("DELETE FROM metrics WHERE ts<?", (now - METRICS_KEEP_MS,))

    def metrics(self, *, key: str | None = None, since_ms: int = 0) -> list[dict[str, Any]]:
        q, args = "SELECT ts, key, value FROM metrics WHERE ts>=?", [since_ms]
        if key:
            q += " AND key=?"
            args.append(key)
        return [dict(r) for r in self.db.query(q + " ORDER BY ts, key LIMIT 50000", tuple(args))]


# ------------------------------------------------------------ 诊断包

_SECRET_KEY = re.compile(r"(secret|password|passwd|token|key|pin|credential)", re.I)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_cfg(v: Any, key: str = "") -> Any:
    """站点配置打码:键名像口令、密钥的值换成 ``***``(``*_file`` 这种路径不打:它不是密钥本身)。"""
    if isinstance(v, dict):
        return {k: redact_cfg(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [redact_cfg(x, key) for x in v]
    if isinstance(v, str) and _SECRET_KEY.search(key) and not key.endswith(("_file", "_dir")):
        return "***" if v else ""
    return v


def redact_text(s: str) -> str:
    """日志打码:``Bearer`` 令牌、长串十六进制(令牌哈希、密钥、指纹)只留前 8 位。"""
    s = _BEARER.sub(r"\1***", s)
    return _LONG_HEX.sub(lambda m: m.group(0)[:8] + "…", s)


def _journal(unit: str, hours: int) -> str:
    try:
        got = subprocess.run(["journalctl", "-u", unit, "--since", f"-{hours}h", "--no-pager",
                              "-o", "short-iso"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(读不了 {unit} 的日志:{exc})\n"
    if got.returncode != 0 and not got.stdout:
        return f"(读不了 {unit} 的日志:{got.stderr.strip()[:300]})\n"
    return got.stdout


def support_bundle(out: Path, *, home: Path, db: Any, health: HealthDesk | None,
                   hours: int = 24, now_ms: int,
                   journal: Callable[[str, int], str] = _journal) -> Path:
    """写诊断包 ``out``(tar.gz)。回 ``out``。"""
    files: dict[str, bytes] = {}

    def put(name: str, obj: Any) -> None:
        files[name] = (obj if isinstance(obj, bytes) else
                       json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode())
    put("README.txt", ("D1 Max 站点诊断包。不含密钥、令牌、库文件、照片录像、狗的位置;"
                       "站点配置里像口令、密钥的值打了码,"
                       "日志里长串十六进制只留前 8 位。\n").encode())
    put("versions.json", {"site": site_version(), "python": platform.python_version(),
                          "os": platform.platform(), "generated_ms": now_ms,
                          "schema": (db.query("SELECT value FROM meta WHERE key='schema'")
                                     or [{"value": "?"}])[0]["value"]})
    if health is not None:
        try:
            put("health.json", health.view())
        except Exception as exc:  # noqa: BLE001
            put("health.json", {"error": str(exc)[:300]})
    try:
        cfg = json.loads((home / "site.json").read_text(encoding="utf-8"))
        put("site.json", redact_cfg(cfg))
    except (OSError, ValueError) as exc:
        put("site.json", {"error": f"读不了:{exc}"})
    tables = [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    put("db_counts.json", {t: db.query(f'SELECT COUNT(*) AS n FROM "{t}"')[0]["n"]
                           for t in tables})
    since = now_ms - 7 * 86_400_000
    if "alerts" in tables:
        put("alerts_7d.json", [dict(r) for r in db.query(
            "SELECT key, kind, level, robot, title, count, first_ms, last_ms, acked_ms, "
            "resolved_ms, escalated FROM alerts WHERE last_ms>=? ORDER BY last_ms DESC "
            "LIMIT 2000", (since,))])
    if "robots" in tables:
        put("robots.json", [{k: r[k] for k in r.keys() if k in (
            "robot_id", "enrolled_at", "revoked", "manual_only")} for r in db.query(
            "SELECT * FROM robots")])
    if health is not None and health.dispatcher is not None:
        live = []
        for rid, c in getattr(health.dispatcher, "clients", {}).items():
            st, caps, tel = c.status, c.capabilities, c.telemetry
            live.append({"robot_id": rid,
                         "online": bool(st and st.online),
                         "adapter": getattr(caps, "adapter_id", None),
                         "tasks": sorted(getattr(caps, "tasks", {}) or {}),
                         "battery_pct": getattr(tel, "battery_pct", None)})
        put("robots_live.json", live)
    if "metrics" in tables:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "key", "value"])
        for r in db.query("SELECT ts, key, value FROM metrics ORDER BY ts, key"):
            w.writerow([r["ts"], r["key"], r["value"]])
        files["metrics.csv"] = buf.getvalue().encode()
    for unit in ("d1max-site", "d1max-mosquitto"):
        files[f"logs/{unit}.log"] = redact_text(journal(unit, hours)).encode()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tarfile.open(tmp, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"d1max-support/{name}")
            info.size, info.mtime, info.mode = len(data), now_ms // 1000, 0o600
            tar.addfile(info, io.BytesIO(data))
    tmp.replace(out)
    return out
