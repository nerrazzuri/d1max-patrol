"""远程运维(W55):站点发来的运维命令在机器人上怎么做。

命令都走「地图类命令」那条路(站点发、代理回执里带数据)。要 root 的交给特权执行器
(:mod:`d1max_contract.privops`);读日志不经它(代理用户在 ``systemd-journal`` 组里)。

| 命令 | 回执 |
|---|---|
| ``ops_journal`` | ``{}`` → 能看哪几个服务;``{unit, hours?, bytes?}`` → 最近的日志(打码、截尾) |
| ``ops_diag`` | 诊断快照 |
| ``ops_restart`` | ``{unit}`` 重启一个我们的服务 |
| ``ops_reboot`` | 空闲才重启整机 |
| ``ops_debug`` | ``{minutes}`` 详细日志 N 分钟(0 = 关) |
| ``ops_cleanup`` | ``{}`` → 能清什么、多大;``{do: [...]}`` → 清 |
| ``ops_timesync`` | 立即对时 |
| ``cert_csr`` | 新私钥留在机器人上,回证书申请 |
| ``cert_install`` | ``{cert, registration}`` 核对后换上,代理随后重启 |
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_contract import debuglog
from d1max_contract.privops import PrivOps, PrivOpsError

#: 我们自己的服务(同 deploy/d1max-ops-runner 的 ROBOT_UNITS;测试核一致)。
OUR_UNITS = ("d1max-agent", "d1max-localizer", "d1max-obstacles", "d1max-persons",
             "d1max-lidar-merge", "d1max-nav2", "d1max-sidecar", "d1max-zenohd", "d1max-bind")
#: 能远程重启的:我们的全部(``d1max-bind`` 以 root 跑,代码在 root 独占目录里,重启它不是提权)。
RESTART_UNITS = OUR_UNITS
#: 能看日志的:我们的 + 远程运维相关的系统服务(谁 SSH 进来过、VPN、对时)。
JOURNAL_UNITS = OUR_UNITS + ("ssh", "wg-quick@d1max", "chrony", "systemd-timesyncd")
KINDS = ("ops_journal", "ops_diag", "ops_restart", "ops_reboot", "ops_debug", "ops_cleanup",
         "ops_timesync", "cert_csr", "cert_install")
#: 回执走 MQTT,站点 broker 单包上限 256 KB:日志最多 128 KiB,按编码后的大小再裁(同 proc_logs)。
MAX_JOURNAL = 128 * 1024
MAX_HOURS = 168
DEBUG_MAX_MIN = 120

_BEARER = re.compile(r"(?i)(bearer\s+|token[=:]\s*|password[=:]\s*|psk[=:]\s*)\S+")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_SECRET_KEY = re.compile(r"(?i)(PIN|PASS|KEY|TOKEN|SECRET|PSK)")

Run = Callable[[list[str], float], tuple[int, str]]


def run_cmd(argv: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        got = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return got.returncode, got.stdout + got.stderr


def redact(text: str) -> str:
    """日志打码:令牌、口令、预共享密钥的值;长串十六进制只留前 8 位。"""
    text = _BEARER.sub(lambda m: m.group(1) + "***", text)
    return _LONG_HEX.sub(lambda m: m.group(0)[:8] + "…", text)


def redact_env(text: str) -> dict[str, str]:
    """``/etc/d1max/env``:键名像密码、PIN、密钥、令牌的值一律打码(设备 PIN 在里面)。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        out[k] = "***" if _SECRET_KEY.search(k) else redact(v.strip().strip("\"'"))[:300]
    return out


@dataclass
class OpsDesk:
    robot_id: str
    priv: PrivOps | None
    data_root: Path
    busy: Callable[[], str]
    run: Run = run_cmd
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    tls_cert: Path = Path("/etc/d1max/tls/robot.crt")
    tls_ca: Path = Path("/etc/d1max/tls/ca.crt")
    env_file: Path = Path("/etc/d1max/env")
    releases: Any = None                        # ReleaseOps(可没有)
    debug_flag: Path | None = None
    extra_diag: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.debug_flag is None:
            self.debug_flag = self.data_root / "debug-until.json"

    # ------------------------------------------------------------ 入口
    def kinds(self) -> dict[str, dict[str, Any]]:
        """能力里报哪几项。要 root 的那几项没装执行器就不报。"""
        out: dict[str, dict[str, Any]] = {
            "ops_journal": {"units": list(JOURNAL_UNITS)}, "ops_diag": {},
            "ops_debug": {"max_minutes": DEBUG_MAX_MIN}, "ops_cleanup": {}}
        if self.priv is not None:
            for k in ("ops_restart", "ops_reboot", "ops_timesync", "cert_csr", "cert_install"):
                out[k] = {}
            out["ops_restart"] = {"units": list(OUR_UNITS)}
        return out

    def handle(self, kind: str, payload: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        fn = getattr(self, "_" + kind, None)
        if kind not in KINDS or fn is None:
            return "unsupported"
        try:
            return fn(payload)
        except PrivOpsError as exc:
            return f"priv_failed: {exc}"[:300]

    def _priv(self, action: str, args: dict[str, Any] | None = None,
              timeout_s: float = 60.0) -> str | dict[str, Any]:
        if self.priv is None:
            return "unsupported: 此机器人未安装特权执行器"
        got = self.priv.call(action, args, timeout_s=timeout_s)
        if not got.get("ok"):
            return f"priv_failed: {got.get('error') or '未知原因'}"[:300]
        return got

    # ------------------------------------------------------------ 日志
    def _ops_journal(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        unit = p.get("unit")
        if unit is None:
            return "", {"units": list(JOURNAL_UNITS)}
        if unit not in JOURNAL_UNITS:
            return f"payload: 不能看 {unit!r} 的日志"
        hours = p.get("hours", 6)
        size = p.get("bytes", MAX_JOURNAL)
        for v, hi in ((hours, MAX_HOURS), (size, MAX_JOURNAL)):
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= hi:
                return f"payload: hours 要 1–{MAX_HOURS}、bytes 要 1–{MAX_JOURNAL}"
        rc, out = self.run(["journalctl", "-u", f"{unit}.service", "--since", f"-{hours}h",
                            "--no-pager", "-o", "short-iso"], 60)
        if rc != 0 and not out.strip():
            return f"read_failed: journalctl 退出 {rc}"
        from d1max_agent.proc_logs import TEXT_BUDGET, _encoded
        raw = redact(out).encode("utf-8", "replace")
        truncated = len(raw) > size
        text = raw[-size:].decode("utf-8", "replace") if truncated else raw.decode("utf-8")
        while True:
            if truncated and "\n" in text[:-1]:
                text = text[text.index("\n") + 1:]              # 别从半行开始
            if _encoded(text) <= TEXT_BUDGET:
                break
            text, truncated = text[len(text) // 2:], True
        return "", {"unit": unit, "hours": hours, "truncated": truncated, "text": text}

    # ------------------------------------------------------------ 诊断快照
    def _ops_diag(self, p: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "", self.diag()

    def diag(self) -> dict[str, Any]:
        out: dict[str, Any] = {"robot_id": self.robot_id, "at_ms": self.now_ms(),
                               "host": platform.node(), "arch": platform.machine(),
                               "python": platform.python_version()}
        out["services"] = [self._unit_state(u) for u in OUR_UNITS]
        out["release"] = self._release_info()
        out["uptime_s"] = _read_float("/proc/uptime")
        out["load"] = _read_text("/proc/loadavg").split()[:3]
        out["memory"] = _meminfo()
        out["temps_c"] = _temps()
        out["disks"] = {"root": _disk(Path("/")), "data": _disk(self.data_root)}
        out["network"] = self._network()
        out["time"] = self._time_state()
        out["cert"] = self._cert_state()
        try:
            out["env"] = redact_env(self.env_file.read_text(encoding="utf-8"))
        except OSError as exc:
            out["env"] = {"error": f"无法读取:{exc}"[:200]}
        out["debug_until_ms"] = debuglog.until_ms(self.debug_flag)
        out["busy"] = self.busy()
        if self.extra_diag is not None:
            try:
                out.update(self.extra_diag())
            except Exception as exc:  # noqa: BLE001 —— 快照里多一项拿不到,不该整个拿不到
                out["extra_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out

    def _unit_state(self, unit: str) -> dict[str, Any]:
        rc, out = self.run(["systemctl", "show", f"{unit}.service", "-p", "LoadState,ActiveState,"
                            "SubState,NRestarts,ActiveEnterTimestamp,UnitFileState,ExecMainStatus"],
                           20)
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return {"unit": unit, "load": kv.get("LoadState", ""), "active": kv.get("ActiveState", ""),
                "sub": kv.get("SubState", ""), "enabled": kv.get("UnitFileState", ""),
                "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None,
                "since": kv.get("ActiveEnterTimestamp", ""), "exit": kv.get("ExecMainStatus", "")}

    def _release_info(self) -> dict[str, Any]:
        r = self.releases
        if r is None:
            return {}
        out: dict[str, Any] = {}
        for k in ("current", "previous"):
            try:
                out[k] = getattr(r, k)()
            except Exception:  # noqa: BLE001
                out[k] = None
        return out

    def _network(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        rc, txt = self.run(["ip", "-brief", "addr"], 10)
        if rc == 0:
            out["addrs"] = [line.split() for line in txt.splitlines() if line.strip()][:12]
        wl = _read_text("/proc/net/wireless").splitlines()[2:]
        out["wifi"] = [{"if": f.split()[0].rstrip(":"), "quality": f.split()[2].rstrip("."),
                        "signal_dbm": f.split()[3].rstrip(".")} for f in wl if len(f.split()) > 3]
        rc, txt = self.run(["iwgetid", "-r"], 5)
        if rc == 0 and txt.strip():
            out["ssid"] = txt.strip()[:64]
        return out

    def _time_state(self) -> dict[str, Any]:
        rc, txt = self.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], 10)
        out: dict[str, Any] = {"synced": txt.strip() == "yes" if rc == 0 else None}
        if shutil.which("chronyc"):
            rc, txt = self.run(["chronyc", "-n", "tracking"], 10)
            if rc == 0:
                out["chrony"] = {k.strip(): v.strip() for k, v in
                                 (line.split(":", 1) for line in txt.splitlines() if ":" in line)
                                 if k.strip() in ("Reference ID", "System time", "Leap status")}
        return out

    def _cert_state(self) -> dict[str, Any]:
        rc, txt = self.run(["openssl", "x509", "-in", str(self.tls_cert), "-noout", "-enddate",
                            "-subject"], 10)
        if rc != 0:
            return {"error": txt.strip()[:200]}
        out = {}
        for line in txt.splitlines():
            if line.startswith("notAfter="):
                out["not_after"] = line.split("=", 1)[1]
            elif line.startswith("subject="):
                out["subject"] = line.split("=", 1)[1].strip()
        return out

    # ------------------------------------------------------------ 动手的
    def _ops_restart(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        unit = p.get("unit")
        if unit not in RESTART_UNITS:
            return f"payload: 只能重启我们的服务,不认 {unit!r}"
        why = self.busy()                          # 重启定位、避障、旁路这些,走着的时候会出事
        if why:
            return f"busy: {why}"
        got = self._priv("restart", {"unit": unit}, timeout_s=200)
        return got if isinstance(got, str) else ("", {"unit": unit})

    def _ops_reboot(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        why = self.busy()
        if why:
            return f"busy: {why}"
        got = self._priv("reboot", {"delay_s": 5})
        return got if isinstance(got, str) else ("", {"delay_s": 5})

    def _ops_timesync(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        got = self._priv("timesync")
        if isinstance(got, str):
            return got
        return "", {"how": got.get("how", ""), "out": got.get("out", "")}

    def _ops_debug(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        m = p.get("minutes")
        if isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= DEBUG_MAX_MIN:
            return f"payload: minutes 要 0–{DEBUG_MAX_MIN}"
        until = self.now_ms() + m * 60_000 if m else 0
        debuglog.set_until(self.debug_flag, until)
        return "", {"until_ms": until}

    def _ops_cleanup(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        extra = self._extra_releases()
        items = {"releases": {"bytes": sum(_du(d) for d in extra.values()),
                              "detail": sorted(extra)},
                 "journal": {"bytes": self._journal_usage()}}
        do = p.get("do")
        if do is None:
            return "", {"items": items}
        if not isinstance(do, list) or not set(do) <= set(items):
            return f"payload: do 只能是 {sorted(items)} 里的"
        done: dict[str, Any] = {}
        if "releases" in do:
            for d in extra.values():
                shutil.rmtree(d, ignore_errors=True)
            done["releases"] = sorted(extra)
        if "journal" in do:
            got = self._priv("journal_vacuum", {"max_mb": 200}, timeout_s=120)
            if isinstance(got, str):
                return got
            done["journal"] = True
        return "", {"done": done}

    def _extra_releases(self) -> dict[str, Path]:
        """不是当前、也不是上一版的已装版本(可以删,退回用不到它们)。"""
        r = self.releases
        if r is None:
            return {}
        try:
            keep = {r.current(), r.previous()}
            root = r.layout.releases
        except Exception:  # noqa: BLE001
            return {}
        if not root.is_dir():
            return {}
        return {d.name: d for d in root.iterdir() if d.is_dir() and d.name not in keep
                and not d.name.startswith(".")}

    def _journal_usage(self) -> int | None:
        rc, txt = self.run(["journalctl", "--disk-usage"], 20)
        m = re.search(r"([\d.]+)([BKMGT])", txt) if rc == 0 else None
        if m is None:
            return None
        return int(float(m.group(1)) * 1024 ** "BKMGT".index(m.group(2)))

    # ------------------------------------------------------------ 证书
    def _staging(self) -> Path:
        d = self.data_root / "cert-new"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def _cert_csr(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        if self.priv is None:
            return "unsupported: 此机器人未安装特权执行器"
        d = self._staging()
        for n in ("robot.key", "robot.csr", "robot.crt", "registration.json"):
            (d / n).unlink(missing_ok=True)
        old = os.umask(0o077)
        try:
            rc, out = self.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                                "ec_paramgen_curve:P-256", "-out", str(d / "robot.key")], 30)
            if rc != 0:
                return f"keygen_failed: {out.strip()[-200:]}"
            rc, out = self.run(["openssl", "req", "-new", "-key", str(d / "robot.key"), "-subj",
                                f"/CN={self.robot_id}", "-out", str(d / "robot.csr")], 30)
            if rc != 0:
                return f"csr_failed: {out.strip()[-200:]}"
        finally:
            os.umask(old)
        return "", {"csr": (d / "robot.csr").read_text(encoding="ascii")}

    def _cert_install(self, p: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        why = self.busy()                          # 换上要重启代理:只在空闲时(外审 W55-1 第 16 条)
        if why:
            return f"busy: {why}"
        cert, reg = p.get("cert"), p.get("registration")
        if (not isinstance(cert, str) or "BEGIN CERTIFICATE" not in cert
                or not isinstance(reg, dict)):
            return "payload: 要 cert(PEM)和 registration"
        if reg.get("robot_id") != self.robot_id:
            return "payload: 登记文件不是这台机器人的"
        d = self.data_root / "cert-new"
        if not (d / "robot.key").is_file():
            return "no_key: 先要 cert_csr"
        (d / "robot.crt").write_text(cert, encoding="ascii")
        # 核:证书的公钥 = 留着的私钥的公钥;CN 是自己;链到站点 CA
        crt = str(d / "robot.crt")
        rc1, pub_c = self.run(["openssl", "x509", "-in", crt, "-noout", "-pubkey"], 10)
        rc2, pub_k = self.run(["openssl", "pkey", "-in", str(d / "robot.key"), "-pubout"], 10)
        if rc1 != 0 or rc2 != 0 or pub_c.strip() != pub_k.strip():
            return "cert_mismatch: 证书与机器人上的新私钥不匹配"
        rc, subj = self.run(["openssl", "x509", "-in", str(d / "robot.crt"), "-noout", "-subject",
                             "-nameopt", "RFC2253"], 10)
        if rc != 0 or f"CN={self.robot_id}" not in subj:
            return "cert_mismatch: 证书不是签给这台机器人的"
        rc, out = self.run(["openssl", "verify", "-CAfile", str(self.tls_ca), crt], 10)
        if rc != 0:
            return f"cert_untrusted: {out.strip()[-200:]}"
        (d / "registration.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        got = self._priv("cert_install", {}, timeout_s=60)
        return got if isinstance(got, str) else ("", {"installed": True})


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_float(path: str) -> float | None:
    try:
        return float(_read_text(path).split()[0])
    except (IndexError, ValueError):
        return None


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read_text("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                out[k] = int(v.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    return out


def _temps() -> dict[str, float]:
    out = {}
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (z / "type").read_text().strip()
            out[name] = int((z / "temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
    return out


def _disk(path: Path) -> dict[str, int] | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "used": u.used, "free": u.free}


def _du(path: Path) -> int:
    n = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                n += p.stat().st_size
        except OSError:
            pass
    return n
