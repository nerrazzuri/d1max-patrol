"""配网服务的状态机(W42 §4–§7)。不碰网络、不起线程:网络经 ``SiteLink``,路由器经 ``Router``,
时间经 ``now_ms``;``tick()`` 由服务主循环每秒叫一次。所有入口在一把锁里。

状态(存 ``/etc/d1max/bind/state.json``,0600,里面有庄园 WiFi 密码和轮询口令):

- ``unbound``:没绑定。热点那一侧能 ``/bind``。
- ``pending``:有了 WiFi 和站点,正在连站点(``phase=connecting``)或者等站点批准(``waiting``)。
- ``bound``:绑好了。热点那一侧只能看状态、提 WiFi 改动、拿解绑令解绑。
- ``wiping``:正在清机(断电了开机接着清)。清干净了回 ``unbound``。

热点那一侧的每个请求都要带绑定码;错 :data:`MAX_FAILS` 次锁 :data:`LOCK_MS`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any

from d1max_contract import ed25519
from d1max_contract.binding import (
    BindingError,
    check_digits,
    check_wifi,
    code_hash,
    verify_unbind,
)
from d1max_patrol.bind.paths import BindPaths, read_json, write_json
from d1max_patrol.bind.router import Router
from d1max_patrol.bind.sitelink import LinkError

log = logging.getLogger(__name__)

MAX_FAILS = 5
LOCK_MS = 10 * 60_000
#: 挂申请、轮询多久一次。
POLL_MS = 5_000
#: 绑好以后多久问一次站点(顺带知道连不连得上;被吊销了就清机)。
PROBE_MS = 60_000
#: 最近这么久内跟站点通过 = 「连得上」(决定 WiFi 改动走哪条路)。
ONLINE_MS = 2 * 60_000
#: 在线时提的改动没人确认,多久作废;确认换过去以后多久连不上站点就退回;离线试新 WiFi 多久。
CHANGE_TTL_MS = 24 * 3600_000
APPLY_GRACE_MS = 5 * 60_000
TRIAL_MS = 30 * 60_000


class BindRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def cert_fp(pem: bytes) -> str:
    """PEM 证书 → ``sha256:<DER 的十六进制>``(跟站点登记表一个写法)。"""
    text = pem.decode("ascii", "replace")
    try:
        body = text.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----")[0]
        der = base64.b64decode("".join(body.split()), validate=True)
    except (IndexError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(der).hexdigest()


class BindCore:
    def __init__(self, paths: BindPaths, *, router: Router, site: Any,
                 provision: Callable[[str], str], wipe: Callable[[], list[str]],
                 start_services: Callable[[], None], now_ms: Callable[[], int],
                 version: str = "") -> None:
        self.paths = paths
        self.router = router
        self.site = site
        self._provision = provision
        self._wipe = wipe
        self._start = start_services
        self._now = now_ms
        self.version = version
        self._lock = threading.RLock()
        dev = read_json(paths.device, None)
        if not isinstance(dev, dict) or not dev.get("serial") or not dev.get("code_hash"):
            raise RuntimeError(f"{paths.device} 没有或不全:这台还没烧写(d1max-patrol factory burn)")
        self.device = dev
        self.s: dict[str, Any] = read_json(paths.state, None) or {"state": "unbound"}
        self._fails = 0
        self._locked_until = 0
        self._next_poll = 0
        self._next_probe = 0
        self._next_wipe = 0
        self._link = "none"

    # ------------------------------------------------------------ 小工具

    @property
    def serial(self) -> str:
        return str(self.device["serial"])

    def _save(self) -> None:
        write_json(self.paths.state, self.s)

    def _check_code(self, code: Any) -> None:
        now = self._now()
        if now < self._locked_until:
            raise BindRefused(429, f"绑定码错太多次了,{(self._locked_until - now) // 60_000 + 1} "
                                   "分钟后再试")
        try:
            ok = hmac.compare_digest(code_hash(code), str(self.device["code_hash"]))
        except BindingError:
            ok = False
        if not ok:
            self._fails += 1
            if self._fails >= MAX_FAILS:
                self._fails, self._locked_until = 0, now + LOCK_MS
            raise BindRefused(403, "绑定码不对:请扫机器人身上的贴纸")
        self._fails = 0

    def _my_cert(self) -> str:
        try:
            return cert_fp(self.paths.robot_cert.read_bytes())
        except OSError:
            return ""

    def _err(self, msg: str) -> None:
        self.s["error"] = msg[:300]

    # ------------------------------------------------------------ 热点那一侧(手机)

    def status(self, code: Any) -> dict[str, Any]:
        with self._lock:
            self._check_code(code)
            return self._status()

    def _status(self) -> dict[str, Any]:
        s = self.s
        out: dict[str, Any] = {"serial": self.serial, "state": s["state"],
                               "phase": s.get("phase", ""), "error": s.get("error", ""),
                               "site_name": (s.get("site") or {}).get("name", ""),
                               "wifi": self._wifi_view()}
        req = s.get("request")
        if s["state"] == "pending" and req:
            out["check"] = req.get("check", "")
        ch = s.get("change")
        if ch:
            out["change"] = {"id": ch["id"], "ssid": ch["ssid"], "mode": ch["mode"],
                             "phase": ch["phase"], "expires_ms": ch["expires_ms"]}
        if s.get("change_result"):
            out["change_result"] = s["change_result"]
        return out

    def bind(self, code: Any, body: Any) -> dict[str, Any]:
        """``{"wifi": {"ssid", "psk"}, "site": {"url", "fp", "name"}}``:还没绑定(或正在绑)才行。"""
        with self._lock:
            self._check_code(code)
            if self.s["state"] in ("bound", "wiping"):
                raise BindRefused(409, "这台已经绑在一个站点上了:要换站点,先在原来的站点上解绑")
            if not isinstance(body, dict):
                raise BindRefused(400, "要 JSON")
            w, site = body.get("wifi"), body.get("site")
            try:
                ssid, psk = check_wifi((w or {}).get("ssid"), (w or {}).get("psk", ""))
            except BindingError as exc:
                raise BindRefused(400, str(exc)) from exc
            if not isinstance(site, dict) or not str(site.get("url", "")).startswith("https://") \
                    or len(str(site.get("fp", "")).removeprefix("sha256:")) != 64:
                raise BindRefused(400, "站点地址、证书指纹不对(在手机上重新选站点)")
            self.router.connect_wifi(ssid, psk)
            self.s = {"state": "pending", "phase": "connecting",
                      "wifi": {"ssid": ssid, "psk": psk},
                      "site": {"url": str(site["url"]), "fp": str(site["fp"]).lower(),
                               "name": str(site.get("name") or "")[:64]},
                      "request": None, "error": ""}
            self._next_poll = 0
            self._save()
            return self._status()

    def cancel(self, code: Any) -> dict[str, Any]:
        with self._lock:
            self._check_code(code)
            if self.s["state"] != "pending":
                raise BindRefused(409, "现在没有在绑定")
            self.router.forget_wifi()
            self.s = {"state": "unbound", "error": ""}
            self._save()
            return self._status()

    def wifi(self, code: Any, body: Any) -> dict[str, Any]:
        """绑好以后换 WiFi(§6):连得上站点就挂着等确认;连不上就先试新 WiFi,30 分钟没确认退回。"""
        with self._lock:
            self._check_code(code)
            if self.s["state"] != "bound":
                raise BindRefused(409, "还没绑定:绑定时就会填 WiFi")
            try:
                ssid, psk = check_wifi((body or {}).get("ssid"), (body or {}).get("psk", ""))
            except (BindingError, AttributeError) as exc:
                raise BindRefused(400, str(exc)) from exc
            now = self._now()
            self._revert_if_trying()
            online = now - int(self.s.get("site_ok_ms") or 0) <= ONLINE_MS
            ch = {"id": secrets.token_hex(6), "ssid": ssid, "psk": psk, "created_ms": now,
                  "mode": "online" if online else "trial",
                  "phase": "waiting" if online else "trying",
                  "expires_ms": now + (CHANGE_TTL_MS if online else TRIAL_MS)}
            if not online:
                self._prev_wifi_from_current(ch)
                self.router.connect_wifi(ssid, psk)
                self._next_probe = 0
            self.s["change"] = ch
            self.s.pop("change_result", None)
            self._save()
            return self._status()

    def unbind_token(self, code: Any, token: Any) -> dict[str, Any]:
        """工程师拿公司签的解绑令解(站点坏了、没备份,§7)。"""
        with self._lock:
            self._check_code(code)
            if self.s["state"] == "unbound":
                raise BindRefused(409, "这台本来就没绑定")
            try:
                pub = ed25519.pub_from_pem(self.paths.unbind_pub.read_bytes())
            except (OSError, ValueError) as exc:
                raise BindRefused(409, "这台没装解绑公钥:找工程师重装") from exc
            try:
                tok = verify_unbind(str(token or ""), pub, serial=self.serial, now_ms=self._now())
            except BindingError as exc:
                raise BindRefused(403, str(exc)) from exc
            used = read_json(self.paths.used_tokens, []) or []
            if tok.nonce in used:
                raise BindRefused(403, "这张解绑令用过了:请公司重新签一张")
            write_json(self.paths.used_tokens, [*used, tok.nonce][-500:])
            log.warning("解绑令解绑(%s),开始清机", tok.nonce)
            self._begin_wipe()
            return self._status()

    # ------------------------------------------------------------ 代理那一侧(站点经 MQTT 发来的)

    def net_view(self) -> dict[str, Any]:
        """代理放进遥测 ``net`` 的那一块。"""
        with self._lock:
            return {"wifi": self._wifi_view()}

    def _wifi_view(self) -> dict[str, Any]:
        ch = self.s.get("change")
        view = None
        if ch and ch["phase"] in ("waiting", "trying"):
            view = {"id": ch["id"], "ssid": ch["ssid"], "mode": ch["mode"],
                    "expires_ms": ch["expires_ms"]}
        return {"ssid": (self.s.get("wifi") or {}).get("ssid", ""), "link": self._link,
                "change": view}

    def site_unbind(self, cert: Any) -> str:
        """站点上点了解绑(§5.3)。证书对得上才清
        (老的 ``unbind`` 命令晚到、机器人已经重新绑过了不清)。
        回空串 = 收下了;别的是拒绝原因。"""
        with self._lock:
            if self.s["state"] != "bound":
                return "not_bound"
            mine = self._my_cert()
            if not mine or not hmac.compare_digest(mine, str(cert or "").lower()):
                return "cert_mismatch"
            log.warning("站点叫解绑,开始清机")
            self._begin_wipe()
            return ""

    def wifi_confirm(self, cid: Any, approve: Any) -> str:
        """站点上确认 / 不同意 WiFi 改动。回空串 = 做了;别的是拒绝原因。"""
        with self._lock:
            ch = self.s.get("change")
            if not ch or ch["id"] != cid or ch["phase"] not in ("waiting", "trying"):
                return "no_such_change"
            now = self._now()
            if not approve:
                self._finish_change("rejected", revert=ch["phase"] == "trying")
            elif ch["phase"] == "trying":
                self._commit_change()                    # 已经在新 WiFi 上连着站点了
            else:
                self._prev_wifi_from_current(ch)
                self.router.connect_wifi(ch["ssid"], ch["psk"])
                ch.update(phase="applying", applied_ms=now)
                self._next_probe = now + POLL_MS
                self._save()
            return ""

    # ------------------------------------------------------------ WiFi 改动

    def _prev_wifi_from_current(self, ch: dict[str, Any]) -> None:
        ch["prev"] = dict(self.s.get("wifi") or {"ssid": "", "psk": ""})

    def _commit_change(self) -> None:
        ch = self.s.pop("change")
        self.s["wifi"] = {"ssid": ch["ssid"], "psk": ch["psk"]}
        self.s["change_result"] = {"id": ch["id"], "result": "applied", "at_ms": self._now()}
        self._save()

    def _finish_change(self, result: str, *, revert: bool) -> None:
        ch = self.s.pop("change")
        if revert:
            prev = ch.get("prev") or self.s.get("wifi") or {}
            if prev.get("ssid"):
                self.router.connect_wifi(prev["ssid"], prev.get("psk", ""))
            else:
                self.router.forget_wifi()
        self.s["change_result"] = {"id": ch["id"], "result": result, "at_ms": self._now()}
        self._save()

    def _revert_if_trying(self) -> None:
        """新提一个改动之前:正在试 / 正在换的那个先退回去(以新提的为准)。"""
        ch = self.s.get("change")
        if ch is None:
            return
        self._finish_change("replaced", revert=ch["phase"] in ("trying", "applying"))

    def _tick_change(self, now: int) -> None:
        ch = self.s.get("change")
        if ch is None:
            return
        if ch["phase"] == "waiting" and now > ch["expires_ms"]:
            self._finish_change("expired", revert=False)
        elif ch["phase"] == "trying" and now > ch["expires_ms"]:
            log.warning("试新 WiFi %s 30 分钟没人确认 / 连不上站点,退回", ch["ssid"])
            self._finish_change("expired", revert=True)
        elif ch["phase"] == "applying":
            if int(self.s.get("site_ok_ms") or 0) > ch["applied_ms"]:
                self._commit_change()
            elif now - ch["applied_ms"] > APPLY_GRACE_MS:
                log.warning("换到 %s 以后 5 分钟连不上站点,退回", ch["ssid"])
                self._finish_change("unreachable", revert=True)

    # ------------------------------------------------------------ 清机

    def _begin_wipe(self) -> None:
        self.s = {"state": "wiping", "error": ""}
        self._save()

    # ------------------------------------------------------------ 每拍

    def tick(self) -> None:
        with self._lock:
            now = self._now()
            try:
                self._link = self.router.status().link
            except Exception as exc:  # noqa: BLE001 - 路由器一时问不到,当没线
                log.debug("问路由器状态失败:%s", exc)
                self._link = "none"
            st = self.s["state"]
            if st == "wiping":
                if now < self._next_wipe:
                    return
                self._next_wipe = now + 30_000            # 没清干净:30 秒后整套再来一遍
                failed = self._wipe()
                if failed:
                    self._err("清机没清干净,会接着清:" + ";".join(failed))
                    self._save()
                else:
                    self.s = {"state": "unbound", "error": ""}
                    self._save()
                    log.warning("清机完成,回到没绑定")
                return
            if st == "pending" and now >= self._next_poll:
                self._next_poll = now + POLL_MS
                self._tick_pending()
            elif st == "bound":
                if now >= self._next_probe:
                    self._next_probe = now + PROBE_MS
                    self._probe(now)
                self._tick_change(now)

    def _probe(self, now: int) -> None:
        try:
            st = self.site.cert_state(self.s["site"], self.serial, self._my_cert())
        except LinkError as exc:
            log.debug("问站点失败:%s", exc)
            if self.s.get("change", {}).get("phase") == "applying":
                self._next_probe = now + POLL_MS        # 换 WiFi 的那 5 分钟问勤一点
            return
        self.s["site_ok_ms"] = now
        if st == "revoked":
            log.warning("站点说这台已经解绑了(当时不在线),开始清机")
            self._begin_wipe()
            return
        self._save()

    def _tick_pending(self) -> None:
        s = self.s
        site = s["site"]
        try:
            if s.get("request") is None:
                nonce = secrets.token_hex(16)
                r = self.site.request(site, {"serial": self.serial, "nonce": nonce,
                                             "model": str(self.device.get("model") or ""),
                                             "version": self.version})
                s["request"] = {"id": r["request_id"], "secret": r["poll_secret"],
                                "check": check_digits(nonce)}
                s["phase"], s["error"] = "waiting", ""
                self._save()
                return
            req = s["request"]
            r = self.site.poll(site, req["id"], req["secret"])
        except LinkError as exc:
            if exc.status in (409, 429, 400):
                self._err(str(exc))
            else:
                self._err(f"连不上站点:{exc}")
            self._save()
            return
        except (KeyError, TypeError) as exc:
            self._err(f"站点回的东西看不懂:{exc}")
            self._save()
            return
        state = r.get("state")
        if state == "approved" and isinstance(r.get("code"), str):
            try:
                self._provision(r["code"])
            except Exception as exc:  # noqa: BLE001 - 原因给手机看,下一拍再领
                self._err(f"领证书没成:{exc}")
                self._save()
                return
            self.s = {"state": "bound", "wifi": s["wifi"], "site": site,
                      "site_ok_ms": self._now(), "error": ""}
            self._save()
            log.warning("绑定完成:站点 %s", site.get("name") or site["url"])
            self._start()
        elif state in ("rejected", "expired", "replaced", "done"):
            why = {"rejected": "站点上拒绝了", "expired": "24 小时没人批准,申请作废了",
                   "replaced": "这台又挂了一条新申请", "done": "开通码领过了"}[state]
            if state == "done" and self.paths.robot_cert.is_file():
                self.s = {"state": "bound", "wifi": s["wifi"], "site": site,
                          "site_ok_ms": self._now(), "error": ""}
                self._save()
                self._start()
                return
            self.router.forget_wifi()
            self.s = {"state": "unbound", "error": why + ":要重新绑定就再扫一次贴纸"}
            self._save()
        else:
            if s.get("error"):
                s["error"] = ""
                self._save()
