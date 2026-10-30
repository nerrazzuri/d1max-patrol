"""P1 告警推到手机(商业化 A6,决策 53:聚合推送用极光 JPush,只推标题)。

手机 App 不在前台、被系统清掉以后,站点直接连不到它;要走手机厂商的推送通道。极光把华为、小米、OPPO、
vivo、荣耀这些厂商的通道接好了,站点只调极光一个接口。

- **谁收**:手机登录站点(或值守令牌)以后,把自己的推送号(极光的 ``registration_id``)报给站点
  (``POST /api/push/devices``),记在 ``push_devices``;退出时注销(``POST /api/push/devices/remove``),
  停用了的账号不推。
- **推什么**:只推 **P1**(决策 53;W42 起另有「等你批准」两类只推给管理员、工程师,
  见 MANAGE_KINDS),只推**标题**(「<站点> · P1:<告警标题>」),不带照片、位置;点开
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
  通道都登记了、站点也都配了,只走一个(先 FCM),不推两遍。``push_outbox.done`` 记这一条在哪些
  通道名下的手机都发成功过,**只给人看**;发没发过只认逐台的账(见下;RC 加固复查 R1:原来拿它
  跳过整个通道,手机的 FCM 令牌失效、要改走它登记的极光时被跳过了)。
- **逐台记账、至少一次**(RC 加固外审 I1、I2):
  - **每发一次之前**(FCM 是每一台,极光是每一批)按此刻再核一遍有没有人确认、解决、过没过一小时;
    交给通道的有效期是此刻**还剩**多少,不是再给一小时。
  - 发成功的推送号记进 ``push_sent``(跟着库走,站点重启也在);没发成功的下一轮只补它们,发成功过的
    **不再发**。
  - **回执丢了**(超时、断线、5xx:手机可能已经收到,站点分不清)按没发成功算,下一轮再发 —— 所以是
    **至少一次**:这种手机可能收到两遍、响两次。
  - **推送去重**(通知栏里只剩一条):每条通知带一个稳定标记 :func:`collapse_id`(告警主键 + 档,跟
    时间、重试、进程无关),第二遍**盖掉**第一遍。FCM 用 ``android.notification.tag``;极光用
    ``apns_collapse_id``(iOS)和厂商通道的 ``notify_id``(华为、小米、OPPO、vivo、荣耀)。**还剩**:
    极光自己的通道(App 活着、走极光长连接)没有按标记盖掉的字段,那一路仍可能两条;盖掉**不保证
    不再响**;不同的告警、同一条升档,各是一条。要做到不再响得手机自己记着处理过哪一条,现在没做。
  - 一个通道连着 :data:`GIVE_UP_AFTER` 次失败(多半是整个不通)这一轮就先停,退避之后再来。
"""

from __future__ import annotations

import base64
import hashlib
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
#: 一个通道一轮里连着这么多次发送失败就先停(FCM 一台一个请求、一个最多等 10 秒:没外网时 500 台要等
#: 一个多小时,这期间别的告警都推不出去)。
GIVE_UP_AFTER = 3
#: 一条告警一轮里最多把名单重新算几遍(有推送号没了才重算;正常一遍)。
RECOUNT_MAX = 3
#: 极光的厂商通道里认 ``notify_id``(同一个号的通知后一条盖掉前一条)的那几家。依据:极光 REST v3
#: 推送接口公开文档 ``options.third_party_channel`` 的厂商属性表,``notify_id``(int)「厂商通知唯一
#: 标识 …… 对应 vivo、荣耀的 notifyId 字段;华为、小米、OPPO 的 notify_id 字段;不携带时,极光自动以
#: 极光产生的唯一消息 msgid 传递给厂商」。**没在真手机上验过**:各家收到同一个号是不是一定盖掉、
#: 没在极光控制台配那一家的应用带上它会不会被拒,都待真机、真账号验;出问题从这里删掉那一家即可。
JPUSH_NOTIFY_ID_VENDORS = ("huawei", "xiaomi", "oppo", "vivo", "honor")


def collapse_id(alert_key: str, tier: int | str) -> str:
    """一条告警的一档在手机通知栏里的标记(推送去重):同一条同一档推了几遍,通知栏里只剩一条。

    从告警在库里的主键(``alerts.key``,``robot/kind#seq``)和档算,跟时间、重试、推送号、进程都无关,
    站点重启也一样。取哈希不截断(原来截到 60 个字,机器人名长了会把末尾的序号截掉、两条告警撞成
    一条);``p1-`` 加 32 位十六进制,35 字节,在 APNs collapse id 的 64 字节以内。升档(``tier`` 变了)
    是另一条通知:升档就是要再响一次,不拿它去盖掉上一档。"""
    raw = f"{alert_key}#{int(tier)}".encode()
    return "p1-" + hashlib.sha256(raw).hexdigest()[:32]


def vendor_notify_id(alert_key: str, tier: int | str) -> int:
    """同一个标记给厂商通道用的整数号(``notify_id`` 是 int):1 到 2^31-1,同一条同一档永远一样。"""
    n = int(collapse_id(alert_key, tier)[3:11], 16) & 0x7FFFFFFF
    return n or 1


class PushError(Exception):
    """推送没发出去(网络、密钥、极光那边)。"""


class PushRetryNow(PushError):
    """这一次没发出去,但通道已经改好了(极光不认去重字段:之后不再带),**可以马上再发**。通道自己
    不重发:再发之前要过外层「发之前按此刻再核」(确认了、解决了、过期了就不发)并按此刻重算 TTL
    (外审 WRAP-I1)。"""


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
        #: 去重字段(``apns_collapse_id``、厂商 ``notify_id``)极光认不认没拿真账号验过。极光要是因为
        #: 参数回 400(400 = 这一条没发出去),去掉它们立刻重发一次,之后这台站点不再带:去重最多做不成
        #: (可能响两次),P1 不能因为它一条都推不出去。
        self.dedupe = True

    def send(self, reg_ids: list[str], title: str, body: str, extras: dict[str, str], *,
             ttl_s: int = EXPIRE_MS // 1000) -> str:
        """推给这几台。回极光的消息号。``ttl_s``:极光那头最多替离线的手机存多久。

        推送去重(同一条推了两遍,通知栏里只剩一条):iOS 带 ``options.apns_collapse_id``,厂商通道带
        ``options.third_party_channel.<厂商>.notify_id``(见 :data:`JPUSH_NOTIFY_ID_VENDORS`)。
        **极光自己的通道(App 活着时极光长连接直接送到)没有能按我们的标记盖掉的字段**:
        ``override_msg_id`` 要填上一条的极光消息号,回执丢了的时候站点恰恰没拿到它;所以这一路回执丢了
        的那台手机仍可能出现两条。"""
        tag = collapse_id(extras.get("alert_key", ""), extras.get("tier", "0") or "0")
        nid = vendor_notify_id(extras.get("alert_key", ""), extras.get("tier", "0") or "0")
        options: dict[str, Any] = {"time_to_live": max(1, int(ttl_s))}
        if self.dedupe:
            options |= {"apns_collapse_id": tag, "third_party_channel": {
                v: {"notify_id": nid} for v in JPUSH_NOTIFY_ID_VENDORS}}
        payload = {
            "platform": ["android", "ios"],
            "audience": {"registration_id": reg_ids},
            "notification": {
                "android": {"alert": body, "title": title, "priority": 2, "category": "alarm",
                            "extras": extras},
                "ios": {"alert": {"title": title, "body": body}, "sound": "default",
                        "extras": extras},
            },
            "options": options,
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
            if exc.code == 400 and self.dedupe:
                log.warning("极光回 400(%s):之后不再带推送去重字段,交回队列马上重发", detail)
                self.dedupe = False
                raise PushRetryNow(f"极光不认去重字段(400):{detail}") from exc
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
    - 通知带 ``android.notification.tag`` = :func:`collapse_id`(告警主键 + 档):同一条再发一遍,手机
      通知栏里**盖掉**原来那条,不叠成两条(firebase-messaging 24.1.0 的 ``CommonNotificationBuilder``
      拿 ``gcm.n.tag`` 当 ``NotificationManager.notify(tag, 0, …)`` 的 tag;没给 tag 时用
      ``"FCM-Notification:" + uptime``,每条都是新的)。**它不保证不再响一次**(RC 加固外审 I2:替换
      不是「处理过这一条」的去重)。不重发靠站点逐台记账(:class:`PushDesk`、``push_sent``)。
      App 在前台时 FCM 不替它弹通知(前台有 App 自己的告警页)。这里没有 APNs 段(FCM 只给安卓手机用;
      以后加 iOS 走 FCM 时带 ``apns.headers["apns-collapse-id"]`` = 同一个标记)。
    - 令牌失效了的(App 卸了、清了数据):记在 :meth:`take_dead` 里,站点把它从名单上删掉。
    """

    #: 站点一次交给它一台(RC 加固外审 I1、I2):每一台发之前站点都按此刻再核一遍,发成功一台记一台。
    batch = 1

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

    def send(self, reg_ids: list[str], title: str, body: str, extras: dict[str, str], *,
             ttl_s: int = EXPIRE_MS // 1000) -> str:
        tag = collapse_id(extras.get("alert_key", ""), extras.get("tier", "0") or "0")
        failed, last, why = 0, "", ""
        for reg in reg_ids:
            msg = {"message": {
                "token": reg,
                "notification": {"title": title, "body": body},
                "data": dict(extras),
                # 不带 android.collapse_key:FCM 每台手机同时只存 4 个不同折叠键的消息,超了随便丢哪个
                # (Firebase 文档「Collapsible and non-collapsible messages」)—— 离线时五条不同的 P1
                # 会丢掉一条。同一条的两遍离线时都存着也没关系,送到时 tag 一样、盖掉。
                "android": {"priority": "HIGH", "ttl": f"{max(1, int(ttl_s))}s",
                            "notification": {"tag": tag, "sound": "default"}},
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


#: 不是 P1、但要推给能批的人(管理员、工程师)的(W42 外审 I11):机器人等批准加入、机器人换 WiFi 等确认
#: (离线试新 WiFi 只等 30 分钟)。只推这两类人,标题不写「P1」。
MANAGE_KINDS = frozenset({"bind_request", "wifi_change"})
MANAGE_ROLES = ("admin", "engineer")
#: W53 / W52:别的不是 P1 也要推的 —— 种类 → (推给哪几种角色,标题)。角色写 ``None`` = 只推给告警
#: ``context.to`` 那个账号(临时巡逻排上了、起跑了、作废了:告诉派的人)。
PUSH_ROUTES: dict[str, tuple[tuple[str, ...] | None, str]] = {
    **{k: (MANAGE_ROLES, "等你批准") for k in MANAGE_KINDS},
    "account_request": (("admin",), "等你批准"),
    "adhoc_patrol": (("owner", "admin"), "临时巡逻"),
    "adhoc_queue": (None, "临时巡逻"),
}


def enqueue(tx: Any, alert: Any, now_ms: int) -> None:
    """告警写库的那个事务里调:没确认、没解决的 P1(和 :data:`MANAGE_KINDS`),这一条这一档还没排过
    就排上。"""
    if (getattr(alert.level, "name", str(alert.level)) != "P1"
            and getattr(alert, "kind", "") not in PUSH_ROUTES) \
            or alert.acked_ms is not None or alert.resolved_ms is not None:
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

    def _devices(self, roles: tuple[str, ...] | None = None, *,
                 account: str | None = None) -> dict[str, list[str]]:
        """推给谁,按通道分:名单里的、账号没停用的、站点配了那个通道的。退出登录、停用、改权限时名单里
        的行已经删了(A 阶段外审 I2);**登录闲置过期不删**:App 被系统清掉、半小时没请求,照样推。

        同一次登录(同一部手机)两个通道都登记了:只走 :data:`PROVIDERS` 里排前面的那个,不推两遍。"""
        rows = [r for r in self.db.query(
            "SELECT d.reg_id, d.provider, d.session, d.account, a.role FROM push_devices d "
            "JOIN accounts a ON a.name = d.account "
            "WHERE a.disabled = 0 AND d.session != '' ORDER BY d.reg_id")   # 没绑登录的老数据不推
            if (roles is None or r["role"] in roles)
            and (account is None or r["account"] == account)]
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
        请求等网络可能等了好几秒。发了一部分以后有人确认了,后面的批次不再发(发出去的撤不回)。
        RC 加固外审 I1、I2:FCM 一批就是一台;发成功的逐台记账,见模块说明。"""
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
            kind = self.db.query("SELECT kind, context FROM alerts WHERE key=?",
                                 (row["alert_key"],))
            route = PUSH_ROUTES.get(kind[0]["kind"]) if kind else None
            roles: tuple[str, ...] | None = None
            account: str | None = None
            if route is not None:
                roles, what = route
                title = f"{self.site_name} · {what}" if self.site_name else what
                if roles is None:
                    try:
                        account = str(json.loads(kind[0]["context"] or "{}").get("to") or "")
                    except (ValueError, AttributeError):
                        account = ""
            else:
                title = f"{self.site_name} · P1" if self.site_name else "P1 告警"
            extras = {"alert_key": row["alert_key"], "tier": str(row["tier"])}
            got = {r["reg_id"] for r in self.db.query(
                "SELECT reg_id FROM push_sent WHERE outbox_id=?", (row["id"],))}
            sent_any, stopped, first, failed = bool(got), "", True, ""
            done: list[str] = []
            tried: set[str] = set()                       # 这一轮发过没成的:重算名单时不再碰,等退避
            # 名单每一遍重新算(RC 加固复查 R1):这一遍里有推送号没了(FCM 令牌失效),那部手机登记的
            # 另一个通道这才轮得上 —— 原来名单只算一次、没有要发的通道也记成「发完了」,那部手机两头
            # 都没收到。**发没发过只看逐台的账**(``push_sent``),不看通道的「发完」标记。
            for _ in range(RECOUNT_MAX):
                devices = self._devices(roles, account=account)
                gone = 0
                done = []
                for name in PROVIDERS:                    # 一个通道没发出去,不耽误另一个
                    if name not in self.senders or stopped:
                        continue
                    mine = devices.get(name) or []
                    reg = [r for r in mine if r not in got and r not in tried]   # 发成功过的不再发
                    sender = self.senders[name]
                    step = int(getattr(sender, "batch", BATCH))
                    bad, streak = "", 0
                    for i in range(0, len(reg), step):
                        if not first:                     # 每发一次之前按此刻再核(外审 I1)
                            stopped = self._skip(row, self._now())
                            if stopped:
                                break
                        first = False
                        chunk = reg[i:i + step]
                        try:
                            self._send_once(sender, row, chunk, title, extras)
                        except PushError as exc:
                            # 这几台算没发成功(回执丢了的也在这里:至少一次);接着发后面的
                            bad, streak = str(exc)[:300], streak + 1
                            tried.update(chunk)
                            log.warning("P1 推送(%s)没发出去:%s", name, exc)
                            gone += self._forget(getattr(sender, "take_dead", list)())
                            if streak >= GIVE_UP_AFTER:
                                break                     # 多半整个通道不通:这一轮先停
                            continue
                        streak = 0
                        dead = set(getattr(sender, "take_dead", list)())
                        gone += self._forget(sorted(dead))
                        ok = [r for r in chunk if r not in dead]
                        if ok:
                            sent_any = True
                            got.update(ok)
                            with self.db.tx() as c:       # 发成功一次记一次(外审 I2)
                                c.executemany(
                                    "INSERT OR IGNORE INTO push_sent(outbox_id, reg_id, sent_ms) "
                                    "VALUES (?,?,?)", [(row["id"], r, self._now()) for r in ok])
                    if bad:
                        failed = bad
                    elif not stopped and mine and not tried.intersection(mine):
                        done.append(name)                 # 这一遍它名下的都发成功过(只给人看)
                if stopped or not gone:
                    break                                 # 没有推送号没了:名单没变,不用再算
            with self.db.tx() as c:
                c.execute("UPDATE push_outbox SET done=? WHERE id=?", (",".join(done), row["id"]))
            if not failed and not stopped and not sent_any:
                self._drop(row, "没有登记的手机")
                continue
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

    def _forget(self, reg_ids: list[str]) -> int:
        """通道说这几个推送号没了(App 卸了):从名单上删掉。回删了几个。"""
        if reg_ids:
            with self.db.tx() as c:
                c.executemany("DELETE FROM push_devices WHERE reg_id=?", [(r,) for r in reg_ids])
        return len(reg_ids)

    def _drop(self, row: dict[str, Any], why: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE push_outbox SET dropped=? WHERE id=?", (why, row["id"]))

    def _send_once(self, sender: Any, row: dict[str, Any], chunk: list[str], title: str,
                   extras: dict[str, str]) -> None:
        """发一批;通道说「改好了、马上再发」(:class:`PushRetryNow`)就**按此刻再核一遍**、重算 TTL
        再发一次(外审 WRAP-I1:原来极光在通道里自己重发,绕过了这道核对)。再核不过 → 报错,本批算没
        发成功。"""
        for again in (False, True):
            if again:
                why = self._skip(row, self._now())
                if why:
                    raise PushError(f"马上重发之前核对没过:{why}")
            left = (EXPIRE_MS - (self._now() - row["created_ms"])) // 1000
            try:
                sender.send(chunk, title, row["title"], extras, ttl_s=max(1, left))
                return
            except PushRetryNow:
                if again:
                    raise
                log.warning("P1 推送:通道改好了,再核一遍马上重发")

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
