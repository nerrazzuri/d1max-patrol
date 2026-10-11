"""P1 告警推到手机(商业化 A6,决策 53:聚合推送用极光 JPush,只推标题)。

手机 App 不在前台、被系统清掉以后,站点直接连不到它;要走手机厂商的推送通道。极光把华为、小米、OPPO、
vivo、荣耀这些厂商的通道接好了,站点只调极光一个接口。

- **谁收**:手机登录站点(或值守令牌)以后,把自己的推送号(极光的 ``registration_id``)报给站点
  (``POST /api/push/devices``),记在 ``push_devices``;退出时注销(``POST /api/push/devices/remove``),
  停用了的账号不推。
- **推什么**:只推 **P1**(决策 53),只推**标题**(「<站点> · P1:<告警标题>」),不带照片、位置;点开
  通知进 App 看详情。新起一条推一次;没人确认、升到下一档再推一次(同一条同一档只推一次)。
- **怎么推**:告警写库的同一个事务里排进 ``push_outbox``(告警记下了,推送就一定在队里);站点循环里
  单独一条道往外发(:meth:`PushDesk.tick`),发不出去退避重试;已经有人确认、解决了的不发;一个
  小时还没发出去的作废(那时候再推没意义)。
- **发不出去**(没外网、密钥不对、极光那边出错)连续 ``STUCK_MS`` 报 **P2**「告警推送发不出去」,发出去
  一条就自动解决。这条 P2 自己不推。
- **配置**:``site.json`` 里 ``push``:``{"provider": "jpush", "app_key": "…"}``;Master Secret 放
  ``/etc/d1max-site/jpush.secret``(``push.secret_file`` 可改;跟别的密钥一样不进 ``site.json``)。
  没配就不推,值守汇总里明说。
- **多通道**(RC 加固 H9,决策 56):极光(带国内厂商通道)和 **FCM**(带 Google 服务的手机,马来西亚的
  首个客户)并列,以后加 APNs。``push`` 写成
  ``{"jpush": {...}, "fcm": {"service_account_file": "…"}}``;
  老写法(平铺的 ``app_key``)照认,算极光。手机登记推送号时带 ``provider``;一部手机(一次登录)两个
  通道都登记了、站点也都配了,只走一个(先 FCM),不推两遍。一条告警在某个通道发完了就记下
  (``push_outbox.done``),别的通道没发出去重试时不重发它。
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_site.alert_sources import SITE

log = logging.getLogger(__name__)

JPUSH_URL = "https://api.jpush.cn/v3/push"
DEFAULT_SECRET = Path("/etc/d1max-site/jpush.secret")
FCM_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_FCM_ACCOUNT = Path("/etc/d1max-site/fcm-service-account.json")
#: 认得的通道;一部手机两个都登记了时,排前面的先用。
PROVIDERS = ("fcm", "jpush")
#: 推送号最长多少(FCM 的令牌一百六十多个字符,留余量)。
REG_MAX = {"jpush": 128, "fcm": 512}
#: 一次最多推给多少台(极光一次最多 1000 个推送号)。
BATCH = 1000
#: 多久还没发出去就作废(毫秒)。
EXPIRE_MS = 3600_000
#: 发不出去连续这么久报 P2(毫秒)。
STUCK_MS = 10 * 60_000
#: 退避:第 n 次失败等 min(MAX, BASE * 2^n) 秒。
BACKOFF_BASE_S, BACKOFF_MAX_S = 10, 300


class PushError(Exception):
    """推送没发出去(网络、密钥、极光那边)。"""


class JPush:
    """极光 REST v3。``send`` 失败抛 :class:`PushError`。"""

    def __init__(self, app_key: str, master_secret: str, *, url: str = JPUSH_URL,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 timeout_s: float = 10.0) -> None:
        self.app_key = app_key
        self._auth = base64.b64encode(f"{app_key}:{master_secret}".encode()).decode()
        self.url = url
        self._open = opener
        self.timeout_s = timeout_s

    def send(self, reg_ids: list[str], title: str, body: str, extras: dict[str, str]) -> str:
        """推给这几台。回极光的消息号。"""
        payload = {
            "platform": ["android", "ios"],
            "audience": {"registration_id": reg_ids},
            "notification": {
                "android": {"alert": body, "title": title, "priority": 2, "category": "alarm",
                            "extras": extras},
                "ios": {"alert": {"title": title, "body": body}, "sound": "default",
                        "extras": extras},
            },
            "options": {"time_to_live": EXPIRE_MS // 1000},
        }
        req = urllib.request.Request(
            self.url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": f"Basic {self._auth}",
                     "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                got = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
            raise PushError(f"极光回 {exc.code}:{detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PushError(f"连不上极光:{exc}") from exc
        if "msg_id" not in got:
            raise PushError(f"极光的回复看不懂:{str(got)[:200]}")
        return str(got["msg_id"])


def openssl_rs256(private_key_pem: str, data: bytes) -> bytes:
    """RS256 签名。站点不带密码库,跟 CA 一样调系统的 ``openssl``;**私钥走标准输入,不落盘**。"""
    with tempfile.NamedTemporaryFile(prefix="d1max-fcm-") as f:
        f.write(data)
        f.flush()
        try:
            out = subprocess.run(["openssl", "dgst", "-sha256", "-sign", "/dev/stdin", f.name],
                                 input=private_key_pem.encode(), capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PushError(f"openssl 签名没成:{exc}") from exc
    if out.returncode != 0 or not out.stdout:
        raise PushError("openssl 签名没成:服务账号的私钥不对")
    return out.stdout


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Fcm:
    """Firebase Cloud Messaging HTTP v1。一条消息发一台;``send`` 有没发出去的抛 :class:`PushError`。

    - 鉴权:服务账号签一张一小时的 JWT 换访问令牌,快到期再换。
    - 通知带 ``tag``(告警 + 档):同一条重发,手机上**盖掉**原来那条,不叠成两条 —— 所以一批里有几台
      没发出去、整批重试时,已经收到的手机不会多响一次。
    - 令牌失效了的(App 卸了、清了数据):记在 :meth:`take_dead` 里,站点把它从名单上删掉。
    """

    batch = 500

    def __init__(self, project_id: str, client_email: str, private_key_pem: str, *,
                 token_uri: str = "https://oauth2.googleapis.com/token",
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 signer: Callable[[str, bytes], bytes] = openssl_rs256,
                 now_s: Callable[[], float] | None = None, timeout_s: float = 10.0) -> None:
        import time
        self.project_id = project_id
        self.client_email = client_email
        self._key = private_key_pem
        self.token_uri = token_uri
        self.url = FCM_URL.format(project=urllib.parse.quote(project_id, safe=""))
        self._open = opener
        self._sign = signer
        self._now = now_s or time.time
        self.timeout_s = timeout_s
        self._access = ("", 0.0)
        self._dead: list[str] = []

    def take_dead(self) -> list[str]:
        dead, self._dead = self._dead, []
        return dead

    def _token(self) -> str:
        tok, until = self._access
        now = self._now()
        if tok and now < until - 60:
            return tok
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({"iss": self.client_email, "scope": FCM_SCOPE,
                                     "aud": self.token_uri, "iat": int(now),
                                     "exp": int(now) + 3600}).encode())
        signing = f"{head}.{claims}".encode()
        jwt = f"{head}.{claims}.{_b64url(self._sign(self._key, signing))}"
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt}).encode()
        req = urllib.request.Request(self.token_uri, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                got = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PushError(f"FCM 鉴权回 {exc.code}:服务账号不对,或者站点的钟不准") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PushError(f"连不上 Google 鉴权:{exc}") from exc
        tok = str(got.get("access_token") or "")
        if not tok:
            raise PushError("FCM 鉴权的回复里没有访问令牌")
        self._access = (tok, now + float(got.get("expires_in") or 3600))
        return tok

    def send(self, reg_ids: list[str], title: str, body: str, extras: dict[str, str]) -> str:
        tag = (extras.get("alert_key", "") + "#" + extras.get("tier", ""))[:60]
        failed, last, why = 0, "", ""
        for reg in reg_ids:
            msg = {"message": {
                "token": reg,
                "notification": {"title": title, "body": body},
                "data": dict(extras),
                "android": {"priority": "HIGH", "ttl": f"{EXPIRE_MS // 1000}s",
                            "collapse_key": tag, "notification": {"tag": tag, "sound": "default"}},
            }}
            req = urllib.request.Request(
                self.url, data=json.dumps(msg, ensure_ascii=False).encode(), method="POST",
                headers={"Authorization": f"Bearer {self._token()}",
                         "Content-Type": "application/json"})
            try:
                with self._open(req, timeout=self.timeout_s) as resp:
                    last = str(json.loads(resp.read() or b"{}").get("name") or "")
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:400].decode("utf-8", "replace") if exc.fp else ""
                if exc.code == 404 or "UNREGISTERED" in detail:
                    self._dead.append(reg)          # 这台手机上的 App 没了:不算发送失败
                    continue
                if exc.code == 401:
                    self._access = ("", 0.0)
                failed += 1
                why = f"FCM 回 {exc.code}:{detail[:200]}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                failed += 1
                why = f"连不上 FCM:{exc}"
        if failed:
            raise PushError(f"{why}({failed} / {len(reg_ids)} 台没发出去)")
        return last


def _jpush(p: dict[str, Any]) -> tuple[Any, str]:
    key = str(p.get("app_key") or "")
    if not key:
        return None, "push.app_key 没填"
    sf = Path(p.get("secret_file") or DEFAULT_SECRET)
    try:
        secret = sf.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"读不了极光 Master Secret {sf}:{exc}"
    if not secret:
        return None, f"{sf} 是空的"
    return JPush(key, secret), ""


def _fcm(p: dict[str, Any]) -> tuple[Any, str]:
    sf = Path(p.get("service_account_file") or DEFAULT_FCM_ACCOUNT)
    try:
        acct = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"读不了 FCM 服务账号 {sf}:{exc}"
    need = {k: str(acct.get(k) or "") for k in ("project_id", "client_email", "private_key")} \
        if isinstance(acct, dict) else {}
    if not need or not all(need.values()):
        return None, f"{sf} 不是服务账号文件(要有 project_id、client_email、private_key)"
    return Fcm(need["project_id"], need["client_email"], need["private_key"],
               token_uri=str(acct.get("token_uri") or "https://oauth2.googleapis.com/token")), ""


def load_senders(cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """按 ``site.json`` 造推送通道。回 ({通道名: 通道}, 没配 / 配不好的原因,几条用分号连)。"""
    p = cfg.get("push") or {}
    if not p:
        return {}, "没配推送(site.json 的 push)"
    parts: dict[str, dict[str, Any]] = {}
    if "app_key" in p or "provider" in p:            # 老写法:平铺的就是极光
        if p.get("provider", "jpush") != "jpush":
            return {}, f"不认识的推送通道 {p.get('provider')!r}(认得:{'、'.join(PROVIDERS)})"
        parts["jpush"] = p
    for name in PROVIDERS:
        if isinstance(p.get(name), dict):
            parts[name] = p[name]
    unknown = [k for k, v in p.items() if isinstance(v, dict) and k not in PROVIDERS]
    out: dict[str, Any] = {}
    why = [f"不认识的推送通道 {k!r}" for k in unknown]
    for name, conf in parts.items():
        sender, bad = (_jpush if name == "jpush" else _fcm)(conf)
        if sender is None:
            why.append(bad)
        else:
            out[name] = sender
    if not parts and not why:
        why.append("push 里没有认得的通道")
    return out, ";".join(why)


def load_sender(cfg: dict[str, Any]) -> tuple[Any, str]:
    """只要极光那一个(老接口)。"""
    out, why = load_senders(cfg)
    return out.get("jpush"), "" if "jpush" in out else why


def enqueue(tx: Any, alert: Any, now_ms: int) -> None:
    """告警写库的那个事务里调:没确认、没解决的 P1,这一条这一档还没排过就排上。"""
    if getattr(alert.level, "name", str(alert.level)) != "P1" or alert.acked_ms is not None \
            or alert.resolved_ms is not None:
        return
    tx.execute("INSERT OR IGNORE INTO push_outbox(alert_key, tier, robot, title, created_ms, "
               "next_ms) VALUES (?,?,?,?,?,?)",
               (alert.key, int(alert.escalated), alert.robot, alert.title[:120], now_ms, now_ms))


class PushDesk:
    def __init__(self, db: Any, *, now_ms: Callable[[], int], sender: Any = None,
                 senders: dict[str, Any] | None = None, site_name: str = "",
                 why_off: str = "") -> None:
        self.db = db
        self._now = now_ms
        #: 通道名 → 通道。``sender``(老参数)算极光。
        self.senders: dict[str, Any] = dict(senders or {})
        if sender is not None:
            self.senders.setdefault("jpush", sender)
        self.site_name = site_name
        #: 没配、配不好的原因(值守汇总里明说;配好了一个、另一个没配好也说)。
        self.why_off = why_off
        #: 告警台(站点主程序接上):发不出去报 P2。
        self.alerts: Any = None
        self.last_error = ""

    # ------------------------------------------------------------ 手机登记

    @property
    def sender(self) -> Any:
        """老接口:有没有配(任何一个通道)。"""
        return next(iter(self.senders.values()), None)

    def register(self, account: str, reg_id: str, platform: str, *, session: str = "",
                 provider: str = "jpush") -> bool:
        """登记一部手机。``session``:登记它的那次登录的令牌哈希。

        推送订阅的生命周期(A 阶段外审 I2):**那次登录明确退出**(``Accounts.logout``)、账号停用 /
        改角色 / 重设口令、手机自己注销,才不再推;**登录闲置过期不影响**(App 被系统清掉以后照样要
        收到 P1)。登记跟退出在同一个库里串行:那次登录在这个事务里已经不在了(退出了、被撤了)就不登记,
        回 False —— 迟到的登记不会把撤掉的授权又加回来。"""
        if provider not in PROVIDERS:
            raise ValueError(f"provider 要是 {' / '.join(PROVIDERS)}")
        reg_id = str(reg_id).strip()
        if not reg_id or len(reg_id) > REG_MAX[provider] or not reg_id.isascii():
            raise ValueError("registration_id 不对")
        if platform not in ("android", "ios", "harmony"):
            raise ValueError("platform 要是 android / ios / harmony")
        with self.db.tx() as c:
            if session and c.execute("SELECT 1 FROM sessions WHERE token_hash=? AND name=?",
                                     (session, account)).fetchone() is None:
                return False
            c.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms, session, "
                      "provider) VALUES (?,?,?,?,?,?) ON CONFLICT(reg_id) DO UPDATE SET "
                      "account=excluded.account, platform=excluded.platform, "
                      "updated_ms=excluded.updated_ms, session=excluded.session, "
                      "provider=excluded.provider",
                      (reg_id, account, platform, self._now(), session, provider))
        return True

    def unregister(self, account: str, reg_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM push_devices WHERE reg_id=? AND account=?", (reg_id, account))

    def drop_account(self, account: str) -> None:
        """账号停用了:它的手机不再收。"""
        with self.db.tx() as c:
            c.execute("DELETE FROM push_devices WHERE account=?", (account,))

    def view(self) -> dict[str, Any]:
        n = self.db.query("SELECT COUNT(*) AS n FROM push_devices")[0]["n"]
        waiting = self.db.query("SELECT COUNT(*) AS n FROM push_outbox WHERE sent_ms IS NULL "
                                "AND dropped=''")[0]["n"]
        by = {r["provider"]: r["n"] for r in self.db.query(
            "SELECT provider, COUNT(*) AS n FROM push_devices GROUP BY provider")}
        #: 登记了、站点却没配那个通道的手机:收不到,要让人看见
        orphan = sum(n for prov, n in by.items() if prov not in self.senders)
        return {"configured": bool(self.senders), "why_off": self.why_off, "devices": n,
                "waiting": waiting, "error": self.last_error,
                "providers": sorted(self.senders), "devices_by_provider": by,
                "devices_unreachable": orphan}

    # ------------------------------------------------------------ 发

    def _devices(self) -> dict[str, list[str]]:
        """推给谁,按通道分:名单里的、账号没停用的、站点配了那个通道的。退出登录、停用、改权限时名单里
        的行已经删了(A 阶段外审 I2);**登录闲置过期不删**:App 被系统清掉、半小时没请求,照样推。

        同一次登录(同一部手机)两个通道都登记了:只走 :data:`PROVIDERS` 里排前面的那个,不推两遍。"""
        rows = self.db.query(
            "SELECT d.reg_id, d.provider, d.session FROM push_devices d "
            "JOIN accounts a ON a.name = d.account "
            "WHERE a.disabled = 0 AND d.session != '' ORDER BY d.reg_id")   # 没绑登录的老数据不推
        best: dict[str, str] = {}
        for r in rows:
            if r["provider"] in self.senders and (
                    r["session"] not in best
                    or PROVIDERS.index(r["provider"]) < PROVIDERS.index(best[r["session"]])):
                best[r["session"]] = r["provider"]
        out: dict[str, list[str]] = {}
        for r in rows:
            if best.get(r["session"]) == r["provider"]:
                out.setdefault(r["provider"], []).append(r["reg_id"])
        return out

    def tick(self) -> None:
        """要推的都推出去(要等网络:站点主程序放在自己那条道上跑)。

        A6 外审 F4:每一条、每一批**发之前按此刻**再核一遍(过期了没有、有人确认或解决了没有):前面的
        请求等网络可能等了好几秒。发了一部分以后有人确认了,后面的批次不再发(发出去的撤不回)。"""
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM push_outbox WHERE sent_ms IS NULL AND dropped='' AND next_ms<=? "
            "ORDER BY id LIMIT 50", (self._now(),))]
        for row in rows:
            why = self._skip(row, self._now())
            if why:
                self._drop(row, why)
                continue
            if not self.senders:
                continue                              # 没配:留着(配好了、一小时内的还能推)
            devices = self._devices()
            if not devices:
                self._drop(row, "没有登记的手机")
                continue
            title = f"{self.site_name} · P1" if self.site_name else "P1 告警"
            extras = {"alert_key": row["alert_key"], "tier": str(row["tier"])}
            done = [d for d in str(row["done"]).split(",") if d]
            sent_any, stopped, first, failed = bool(done), "", True, ""
            for name in PROVIDERS:                        # 一个通道没发出去,不耽误另一个
                reg = devices.get(name) or []
                if name in done or not reg or stopped:
                    continue
                sender = self.senders[name]
                step = int(getattr(sender, "batch", BATCH))
                try:
                    for i in range(0, len(reg), step):
                        if not first:
                            stopped = self._skip(row, self._now())
                            if stopped:
                                break
                        first = False
                        sender.send(reg[i:i + step], title, row["title"], extras)
                        sent_any = True
                except PushError as exc:
                    failed = str(exc)[:300]
                    log.warning("P1 推送(%s)没发出去:%s", name, exc)
                else:
                    if not stopped:
                        done.append(name)                 # 这个通道发完了:记下,重试不重发
                        with self.db.tx() as c:
                            c.execute("UPDATE push_outbox SET done=? WHERE id=?",
                                      (",".join(done), row["id"]))
                finally:
                    self._forget(getattr(sender, "take_dead", list)())
            if failed:
                self.last_error = failed
                wait = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** min(row["attempts"], 10))
                with self.db.tx() as c:
                    c.execute("UPDATE push_outbox SET attempts=attempts+1, next_ms=?, error=? "
                              "WHERE id=?", (self._now() + wait * 1000, self.last_error,
                                             row["id"]))
                log.warning("P1 推送 %s 秒后再试", wait)
                continue
            self.last_error = ""
            with self.db.tx() as c:                       # 记实际发完的时刻
                c.execute("UPDATE push_outbox SET sent_ms=?, error=? WHERE id=?",
                          (self._now(), f"发了一部分,{stopped},后面的没发" if stopped else "",
                           row["id"]))
            if stopped and not sent_any:
                self._drop(row, stopped)
        self._stuck(self._now())

    def _forget(self, reg_ids: list[str]) -> None:
        """通道说这几个推送号没了(App 卸了):从名单上删掉。"""
        if reg_ids:
            with self.db.tx() as c:
                c.executemany("DELETE FROM push_devices WHERE reg_id=?", [(r,) for r in reg_ids])

    def _drop(self, row: dict[str, Any], why: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE push_outbox SET dropped=? WHERE id=?", (why, row["id"]))

    def _skip(self, row: dict[str, Any], now: int) -> str:
        if now - row["created_ms"] > EXPIRE_MS:
            return "一个小时没发出去,作废"
        a = self.db.query("SELECT acked_ms, resolved_ms FROM alerts WHERE key=?",
                          (row["alert_key"],))
        if a and (a[0]["acked_ms"] is not None or a[0]["resolved_ms"] is not None):
            return "已经有人确认或解决了"
        return ""

    def _stuck(self, now: int) -> None:
        """发不出去连续 ``STUCK_MS``:报 P2;队里没有发不出去的了:解决。按当前的队列对账,每拍都来。"""
        if self.alerts is None:
            return
        oldest = self.db.query("SELECT MIN(created_ms) AS t FROM push_outbox WHERE "
                               "sent_ms IS NULL AND dropped='' AND attempts>0")[0]["t"]
        stuck = oldest is not None and now - oldest >= STUCK_MS
        open_ = self.alerts.has_open(SITE, "push_failed")
        if stuck and not open_:
            self.alerts.raise_alert(kind="push_failed", robot=SITE,
                                    title="P1 告警推送发不出去:手机收不到通知",
                                    detail=(self.last_error or "推送一直没发出去")[:250]
                                    + ";看站点能不能上外网、推送通道的密钥对不对")
        elif not stuck and oldest is None and open_:
            self.alerts.resolve_all(SITE, "push_failed", who="site:push_ok")
