"""CCTV 接入(W19,决策 33、34):固定摄像头**自带的**人形、区域入侵检测 → W16 的入侵派遣。

摄像头还没定牌子(决策 33),所以走通用标准 **ONVIF**(几乎所有网络摄像头、NVR 都支持):站点向每台摄像头
订阅事件(``CreatePullPointSubscription``,然后一直 ``PullMessages``、定时 ``Renew``)。摄像头报
「区域入侵」「越线」「有人」这类规则触发,就按这台摄像头登记的防区,当成一条入侵交给 ``IncidentDesk``
(来源 ``cctv-<名字>``,照样限流、合并、选狗、响铃)。

- **只认由「没有」变「有」的那一下**(``PropertyOperation`` 是 ``Changed``、值变成真);
  订阅一开始摄像头
  报的现状(``Initialized``)不算 —— 不然站点一重启,正好有东西在画面里的那几台全报一遍入侵。
- **普通「画面动了」默认不算**(树影、车灯、雨,太容易误报);按摄像头打开(``motion``)。
- 同一台摄像头 30 s 内只报一条(W16 那头同防区 60 s 内还会合并)。
- **连不上要让人知道**:断了超过 ``OFFLINE_ALERT_S`` 报一次 P2
  ``cctv_offline``(这一路的入侵就收不到了)。
- 鉴权:WS-Security 的 UsernameToken(口令摘要),再加 HTTP Digest(有的摄像头两样都要)。口令存在站点库里
  (库只许站点用户读,见 ``db``),手机上看不到、改不了(命令行管)。
- 实时画面(``CctvView``):站点拉 RTSP、转 MJPEG,多个观众共用一条,没人看了收掉。

线程:每台摄像头一条订阅线程;报入侵跳到站点的事件循环里(``submit``)。``CctvManager.sync`` 定时从库里
读摄像头(命令行加、删之后不用重启站点)。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import IO, Any

from d1max_site.ca import SAFE_ID
from d1max_site.db import SiteDB

log = logging.getLogger(__name__)

#: 断了多久报 ``cctv_offline``(秒)。
OFFLINE_ALERT_S = 300
#: 同一台摄像头两条入侵最少隔多久(秒)。
DEBOUNCE_S = 30.0
#: 连不上之后隔多久重连(秒,逐次翻倍到上限)。
RETRY_S, RETRY_MAX_S = 5.0, 60.0
#: 订阅的有效期、续订提前量(秒)。
SUB_TERM_S, RENEW_EVERY_S = 600, 300
#: 一次 PullMessages 最多等多久(秒)。
PULL_WAIT_S = 20

#: 算「入侵」的规则(主题里含这些词,不分大小写)。各家叫法不一:海康 FieldDetector/LineDetector,大华
#: CrossRegion/CrossLine,ONVIF 分析规则 ObjectsInside、人形 People/Human/Person。
INTRUSION_TOPICS = ("fielddetector", "linedetector", "crossregion", "crossline", "intrusion",
                    "objectsinside", "linecross", "people", "human", "person", "tripwire")
MOTION_TOPICS = ("motion",)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
_PW_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
              "#PasswordDigest")
_NONCE_ENC = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0"
              "#Base64Binary")


class OnvifError(RuntimeError):
    pass


class OnvifAuthError(OnvifError):
    """口令不对(摄像头回 401 或 SOAP 的 NotAuthorized)。"""


# ------------------------------------------------------------ ONVIF 客户端


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, *names: str) -> ET.Element | None:
    """按本地名一层层找(不管命名空间:各家用的前缀、版本不一)。"""
    cur: ET.Element | None = el
    for n in names:
        if cur is None:
            return None
        cur = next((c for c in cur.iter() if _local(c.tag) == n and c is not cur), None)
    return cur


def _security(user: str, password: str, now: datetime) -> str:
    nonce = os.urandom(16)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode())
                              .digest()).decode()
    return (f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{NS["wsse"]}" '
            f'xmlns:wsu="{NS["wsu"]}"><wsse:UsernameToken>'
            f"<wsse:Username>{_esc(user)}</wsse:Username>"
            f'<wsse:Password Type="{_PW_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_NONCE_ENC}">{base64.b64encode(nonce).decode()}'
            f"</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken>"
            "</wsse:Security>")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class OnvifClient:
    """够用就好的 ONVIF 客户端:设备能力里取事件服务的地址、订阅、拉消息、续订、退订。"""

    def __init__(self, url: str, user: str, password: str, *, timeout_s: float = 10.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        if not re.match(r"^https?://", url):
            url = "http://" + url
        if not re.search(r"https?://[^/]+/.+", url):
            url = url.rstrip("/") + "/onvif/device_service"
        self.device_url = url
        self.user, self.password = user, password
        self.timeout_s = timeout_s
        self._now = now
        pw = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pw.add_password(None, url.split("/", 3)[0] + "//" + url.split("/", 3)[2], user, password)
        self._opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pw))

    def call(self, url: str, body: str, *, action: str = "", to: bool = False,
             timeout_s: float | None = None) -> ET.Element:
        head = _security(self.user, self.password, self._now()) if self.user else ""
        if to:
            head += f'<wsa:To xmlns:wsa="{NS["wsa"]}">{_esc(url)}</wsa:To>'
            if action:
                head += f'<wsa:Action xmlns:wsa="{NS["wsa"]}">{_esc(action)}</wsa:Action>'
        env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS["s"]}" '
               f'xmlns:tds="{NS["tds"]}" xmlns:tev="{NS["tev"]}" xmlns:wsnt="{NS["wsnt"]}">'
               f"<s:Header>{head}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
        ctype = "application/soap+xml; charset=utf-8" + (f'; action="{action}"' if action else "")
        req = urllib.request.Request(url, data=env.encode(), method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with self._opener.open(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            if exc.code in (401, 403):
                raise OnvifAuthError(f"{url} 回 {exc.code}:口令不对?") from exc
            if b"NotAuthorized" in raw:
                raise OnvifAuthError(f"{url}:NotAuthorized") from exc
            raise OnvifError(f"{url} 回 {exc.code}:{_fault(raw)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise OnvifError(f"{url} 连不上:{getattr(exc, 'reason', exc)}") from exc
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OnvifError(f"{url} 回的不是 XML") from exc
        if _find(root, "Body", "Fault") is not None:
            if b"NotAuthorized" in raw:
                raise OnvifAuthError(f"{url}:NotAuthorized")
            raise OnvifError(f"{url}:{_fault(raw)}")
        return root

    def events_url(self) -> str:
        root = self.call(self.device_url, "<tds:GetCapabilities><tds:Category>Events"
                         "</tds:Category></tds:GetCapabilities>")
        x = _find(root, "Events", "XAddr")
        if x is None or not (x.text or "").strip():
            raise OnvifError("这台摄像头不报事件服务(不支持 ONVIF 事件?)")
        return x.text.strip()

    def subscribe(self, events_url: str) -> str:
        root = self.call(events_url, "<tev:CreatePullPointSubscription>"
                         f"<tev:InitialTerminationTime>PT{SUB_TERM_S}S"
                         "</tev:InitialTerminationTime></tev:CreatePullPointSubscription>",
                         action=f"{NS['tev']}/EventPortType/CreatePullPointSubscriptionRequest")
        a = _find(root, "SubscriptionReference", "Address")
        if a is None or not (a.text or "").strip():
            raise OnvifError("订阅没回地址")
        return a.text.strip()

    def pull(self, sub_url: str) -> list[ET.Element]:
        root = self.call(sub_url, f"<tev:PullMessages><tev:Timeout>PT{PULL_WAIT_S}S</tev:Timeout>"
                         "<tev:MessageLimit>20</tev:MessageLimit></tev:PullMessages>",
                         action=f"{NS['tev']}/PullPointSubscription/PullMessagesRequest",
                         to=True, timeout_s=PULL_WAIT_S + 10)
        return [m for m in root.iter() if _local(m.tag) == "NotificationMessage"]

    def renew(self, sub_url: str) -> None:
        self.call(sub_url, f"<wsnt:Renew><wsnt:TerminationTime>PT{SUB_TERM_S}S"
                  "</wsnt:TerminationTime></wsnt:Renew>",
                  action=f"{NS['wsnt']}/SubscriptionManager/RenewRequest", to=True)

    def unsubscribe(self, sub_url: str) -> None:
        with contextlib.suppress(OnvifError):
            self.call(sub_url, "<wsnt:Unsubscribe/>",
                      action=f"{NS['wsnt']}/SubscriptionManager/UnsubscribeRequest", to=True,
                      timeout_s=3)


def _fault(raw: bytes) -> str:
    m = re.search(rb"<[^>]*Text[^>]*>([^<]{1,200})<", raw)
    return m.group(1).decode("utf-8", "replace") if m else raw[:120].decode("utf-8", "replace")


@dataclass(frozen=True)
class OnvifEvent:
    """一条通知里判断要用的几样。"""

    topic: str
    operation: str                      # Initialized / Changed / Deleted / ""(有的不带)
    source: dict[str, str]
    data: dict[str, str]

    @property
    def active(self) -> bool:
        """数据里有一项是「真」(IsInside、IsMotion、State、Triggered……各家名字不一)。"""
        return any(v.strip().lower() in ("true", "1") for v in self.data.values())

    @property
    def key(self) -> str:
        """同一条规则同一个来源算一路(「由无到有」按这一路记)。"""
        return self.topic + "|" + "|".join(f"{k}={v}" for k, v in sorted(self.source.items()))


def parse_event(m: ET.Element) -> OnvifEvent:
    t = _find(m, "Topic")
    topic = (t.text or "").strip() if t is not None else ""
    msg = next((c for c in m.iter() if _local(c.tag) == "Message" and c.get("UtcTime")
                or _local(c.tag) == "Message" and c.get("PropertyOperation")), None)
    op = msg.get("PropertyOperation", "") if msg is not None else ""

    def items(name: str) -> dict[str, str]:
        el = _find(msg, name) if msg is not None else None
        if el is None:
            return {}
        return {si.get("Name", ""): si.get("Value", "") for si in el.iter()
                if _local(si.tag) == "SimpleItem"}
    return OnvifEvent(topic=topic, operation=op, source=items("Source"), data=items("Data"))


# ------------------------------------------------------------ 每台摄像头一条订阅


@dataclass
class Camera:
    name: str
    onvif_url: str
    username: str
    password: str
    zone: str
    rtsp_url: str = ""
    motion: bool = False

    def wants(self, topic: str) -> bool:
        t = topic.lower()
        if any(w in t for w in INTRUSION_TOPICS):
            return True
        return self.motion and any(w in t for w in MOTION_TOPICS)


@dataclass
class CamStatus:
    connected: bool = False
    since_ms: int = 0
    last_event_ms: int | None = None
    last_topic: str = ""
    error: str = ""
    states: dict[str, bool] = field(default_factory=dict)
    last_fire: float = -1e9


class CameraWatch:
    """一台摄像头:连上、订阅、一直拉;断了退避重连。``on_intrusion(camera, topic)`` 在本线程里调。"""

    def __init__(self, cam: Camera, *, on_intrusion: Callable[[Camera, OnvifEvent], None],
                 now_ms: Callable[[], int], client: OnvifClient | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.cam = cam
        self.status = CamStatus(since_ms=now_ms())
        self._on = on_intrusion
        self._now = now_ms
        self._mono = monotonic
        self.client = client or OnvifClient(cam.onvif_url, cam.username, cam.password)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"cctv-{cam.name}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = PULL_WAIT_S + 15) -> bool:
        """等订阅线程退出。它可能正卡在一次 PullMessages 里(最长 ``PULL_WAIT_S`` 加网络超时)。
        回真的退了没有(超时就是没退,别当成退了)。"""
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _set(self, connected: bool, error: str = "") -> None:
        st = self.status
        if st.connected != connected:
            st.since_ms = self._now()
        st.connected = connected
        st.error = error

    def handle(self, ev: OnvifEvent) -> bool:
        """一条通知。回报没报入侵。只认「由无到有」的那一下;订阅一开始报的现状只记下来。
        **停了就不报**(W19 外审:删掉、改了防区之后,旧线程从卡着的 PullMessages 回来还会按旧的
        派)。"""
        if self._stop.is_set():
            return False
        st = self.status
        if not self.cam.wants(ev.topic):
            return False
        was = st.states.get(ev.key, False)
        st.states[ev.key] = ev.active
        if ev.operation == "Initialized" or not ev.active or was:
            return False
        if self._mono() - st.last_fire < DEBOUNCE_S:
            return False
        st.last_fire = self._mono()
        st.last_event_ms = self._now()
        st.last_topic = ev.topic
        try:
            self._on(self.cam, ev)
        except Exception:
            log.exception("摄像头 %s 的入侵交不出去", self.cam.name)
        return True

    def _run(self) -> None:
        wait = RETRY_S
        while not self._stop.is_set():
            sub = ""
            try:
                sub = self.client.subscribe(self.client.events_url())
                self._set(True)
                wait = RETRY_S
                renew_at = self._mono() + RENEW_EVERY_S
                while not self._stop.is_set():
                    for m in self.client.pull(sub):
                        if self._stop.is_set():          # 拉回来的时候已经叫停了:一条都不处理
                            break
                        self.handle(parse_event(m))
                    if self._mono() >= renew_at:
                        self.client.renew(sub)
                        renew_at = self._mono() + RENEW_EVERY_S
            except OnvifError as exc:
                if self.status.connected or self.status.error != str(exc):
                    log.warning("摄像头 %s:%s", self.cam.name, exc)
                self._set(False, str(exc)[:300])
            except Exception as exc:                     # 不许一台摄像头的线程悄悄死掉
                log.exception("摄像头 %s 的订阅线程出错", self.cam.name)
                self._set(False, f"{type(exc).__name__}: {exc}"[:300])
            if sub:
                self.client.unsubscribe(sub)
            if self._stop.wait(wait):
                break
            wait = min(wait * 2, RETRY_MAX_S)


# ------------------------------------------------------------ 管:从库里读、状态、告警


def load_cameras(db: SiteDB) -> list[Camera]:
    """口令在库里是加密的(W30),读出来解开。"""
    from d1max_site.sealbox import open_value

    return [Camera(name=r["name"], onvif_url=r["onvif_url"], username=r["username"],
                   password=open_value(db, r["password"]), zone=r["zone"], rtsp_url=r["rtsp_url"],
                   motion=bool(r["motion"]))
            for r in db.query("SELECT * FROM cameras ORDER BY name")]


def add_camera(db: SiteDB, cam: Camera, *, now_ms: int) -> None:
    if not SAFE_ID.match(cam.name):
        raise ValueError(f"摄像头名字只许字母、数字、. _ -:{cam.name!r}")
    if not cam.zone:
        raise ValueError("要防区(--zone):这台摄像头报的入侵算哪个防区")
    from d1max_site.sealbox import seal_value
    sealed = seal_value(db, cam.password)             # 口令加密落库(W30,决策 43)
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO cameras(name, onvif_url, username, password, zone, "
                  "rtsp_url, motion, added_ms) VALUES (?,?,?,?,?,?,?,?)",
                  (cam.name, cam.onvif_url, cam.username, sealed, cam.zone, cam.rtsp_url,
                   int(cam.motion), now_ms))


def remove_camera(db: SiteDB, name: str) -> bool:
    with db.tx() as c:
        return c.execute("DELETE FROM cameras WHERE name=?", (name,)).rowcount > 0


def alert_robot(name: str) -> str:
    """一台摄像头的告警记在谁名下:每台一个(两台同时断不合成一条,恢复了各自解决)。"""
    return f"cctv:{name}"


class CctvManager:
    """站点里的摄像头:按库起停订阅、出状态、断太久报告警、恢复了自动解决。``report`` 把入侵交给
    事件派遣(跳到事件循环)。

    ``alerts``(``LoopAlerts``,同步、出错抛)管告警:``raise_alert``、``resolve_all``、
    ``resolve_except``、``has_open``。**告警簿(落库的)才是真理源**(W19 复查):站点重启之后内存是
    空的,但断着时报的那条还在库里 —— 摄像头连着就去解决它,不看内存里记没记过;报之前先看库里是不是
    已经挂着。报、解决**成了**才在内存里记一笔(省得每拍都去问),失败下一拍重试。

    ``on_changed(名字)``:这台摄像头删了、改了配置(站点主程序接到 API:当场关掉正在看的旧画面)。"""

    def __init__(self, db: SiteDB, *, report: Callable[[Camera, OnvifEvent], None],
                 now_ms: Callable[[], int], alerts: Any = None,
                 watch_factory: Callable[..., CameraWatch] = CameraWatch) -> None:
        self.db = db
        self._report = report
        self._now = now_ms
        self._alerts = alerts
        self._factory = watch_factory
        self._lock = threading.Lock()
        self.watches: dict[str, CameraWatch] = {}
        #: 这一回连上之后,库里的「连不上」确认解决了的(省得每拍去问);断了就划掉。
        self._clean: set[str] = set()
        #: 这一回断了之后,库里确认挂着「连不上」的(报成了、或者本来就挂着);连上了就划掉。
        self._told: set[str] = set()
        self.on_changed: Callable[[str], None] | None = None

    def _stop_join(self, gone: list[CameraWatch]) -> None:
        """锁外等旧订阅退出(W19 外审:不等的话,删掉、改了防区之后它从卡着的 PullMessages 回来还会
        按旧的派一次狗;新旧订阅也会重叠)。等不到(超时)记一条错:那条线程还在,只是 ``handle`` 叫停
        之后不再派。"""
        for w in gone:
            if not w.join():
                log.error("摄像头 %s 的旧订阅线程没在时限内退出(叫停了、不会再派)", w.cam.name)

    def _changed(self, name: str) -> None:
        if self.on_changed is not None:
            try:
                self.on_changed(name)
            except Exception:
                log.exception("通知「摄像头 %s 删了 / 改了」失败", name)

    def _resolve(self, name: str) -> None:
        if name in self._clean or self._alerts is None:
            return
        try:
            self._alerts.resolve_all(alert_robot(name), "cctv_offline",
                                     who="站点:摄像头连上了")
        except Exception:
            log.exception("解决「摄像头 %s 连不上」失败,下一拍再试", name)
            return
        self._clean.add(name)

    def _raise(self, w: CameraWatch) -> None:
        name, st = w.cam.name, w.status
        if name in self._told or self._alerts is None:
            return
        robot = alert_robot(name)
        try:
            if not self._alerts.has_open(robot, "cctv_offline"):
                self._alerts.raise_alert(
                    kind="cctv_offline", robot=robot,
                    title=f"摄像头 {name} 连不上(防区 {w.cam.zone})",
                    detail=f"{scrub(st.error) or '连不上'};这一路的入侵现在收不到")
        except Exception:
            log.exception("报「摄像头 %s 连不上」失败,下一拍再试", name)
            return
        self._told.add(name)

    def sync(self) -> None:
        """照库里的摄像头起停订阅(命令行加、删、改了之后,这一拍就跟上)。断太久的报告警,连上了的、库里已经
        没有了的,把告警簿里的「连不上」解决掉。"""
        want = {c.name: c for c in load_cameras(self.db)}
        gone: list[CameraWatch] = []
        with self._lock:
            for name in list(self.watches):
                w = self.watches[name]
                if name not in want or want[name] != w.cam:
                    w.stop()
                    gone.append(self.watches.pop(name))
        for w in gone:
            self._changed(w.cam.name)                    # 正在看的旧画面当场关(不等下一次打开)
        self._stop_join(gone)                            # 旧的退干净了才起新的:不重叠
        for w in gone:
            if w.cam.name not in want:
                self._clean.discard(w.cam.name)
                self._told.discard(w.cam.name)
        if self._alerts is not None:
            try:                                         # 库里没有了的(含站点停机期间删的)
                self._alerts.resolve_except("cctv_offline", "cctv:", set(want),
                                            who="站点:摄像头已删除")
            except Exception:
                log.exception("解决已删除摄像头的「连不上」失败,下一拍再试")
        with self._lock:
            for name, cam in want.items():
                if name not in self.watches:
                    w = self._factory(cam, on_intrusion=self._report, now_ms=self._now)
                    self.watches[name] = w
                    w.start()
            watches = list(self.watches.values())
        now = self._now()
        for w in watches:
            st, name = w.status, w.cam.name
            if st.connected:
                self._told.discard(name)
                self._resolve(name)
            else:
                self._clean.discard(name)
                if now - st.since_ms >= OFFLINE_ALERT_S * 1000:
                    self._raise(w)

    def view(self) -> list[dict[str, Any]]:
        """给手机看的(不带口令、不带地址里的口令)。"""
        with self._lock:
            watches = list(self.watches.values())
        return [{"name": w.cam.name, "zone": w.cam.zone, "motion": w.cam.motion,
                 "live": bool(w.cam.rtsp_url), "connected": w.status.connected,
                 "since_ms": w.status.since_ms, "last_event_ms": w.status.last_event_ms,
                 "last_topic": w.status.last_topic, "error": scrub(w.status.error)}
                for w in sorted(watches, key=lambda w: w.cam.name)]

    def camera(self, name: str) -> Camera | None:
        with self._lock:
            w = self.watches.get(name)
        return w.cam if w is not None else None

    def close(self) -> None:
        """叫停全部订阅并等它们退出(站点收尾:不许退出之后还往收掉的事件循环里交入侵)。"""
        with self._lock:
            gone = list(self.watches.values())
            for w in gone:
                w.stop()
            self.watches.clear()
        self._stop_join(gone)


def incident_body(cam: Camera, ev: OnvifEvent, now_ms: int) -> dict[str, Any]:
    """交给 ``IncidentDesk.handle`` 的那一条(来源 ``cctv-<名字>``)。"""
    return {"event_id": f"{cam.name}-{now_ms}", "type": "intrusion", "zone": cam.zone,
            "occurred_at": now_ms, "detail": {"camera": cam.name, "topic": ev.topic[:200]}}


def incident_reporter(incidents: Any, submit: Callable[[Callable[[], Any]], Any],
                      now_ms: Callable[[], int]) -> Callable[[Camera, OnvifEvent], None]:
    """订阅线程里调:跳到站点的事件循环(``submit``)交给事件派遣,来源 ``cctv-<名字>``,照样限流。"""
    def report(cam: Camera, ev: OnvifEvent) -> None:
        source = f"cctv-{cam.name}"

        async def go() -> None:
            if incidents.allow(source):                  # 摄像头抽风刷屏:照样限流(W16)
                await incidents.handle(source, incident_body(cam, ev, now_ms()))
        submit(go)
    return report


# ------------------------------------------------------------ 实时画面


_CRED_RE = re.compile(r"(\w+://)[^/@\s'\"]+@")


def scrub(text: str) -> str:
    """把地址里的账号口令抹掉(``rtsp://user:pw@host`` → ``rtsp://***@host``):日志、错误、接口一律过它。"""
    return _CRED_RE.sub(r"\1***@", text or "")


def _rtsp_with_auth(url: str, user: str, password: str) -> str:
    """地址里没带账号就把这台摄像头的账号塞进去(``rtsp://user:pw@host/...``)。**只写进 0600
    的临时文件
    交给 ffmpeg,不进命令行**(见 ``CctvView``)。"""
    if not user or "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return url
    from urllib.parse import quote
    scheme, rest = url.split("//", 1)
    return f"{scheme}//{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


_FFMPEG_MAJOR: dict[str, int] = {}


def ffmpeg_major(ffmpeg: str) -> int:
    """ffmpeg 的大版本(问一次记住)。5 起 concat 文件里能给每个输入带选项(``option``)。"""
    if ffmpeg not in _FFMPEG_MAJOR:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True,
                                 timeout=10).stdout
            m = re.search(r"version n?(\d+)\.", out)
            _FFMPEG_MAJOR[ffmpeg] = int(m.group(1)) if m else 0
        except (OSError, subprocess.TimeoutExpired):
            _FFMPEG_MAJOR[ffmpeg] = 0
    return _FFMPEG_MAJOR[ffmpeg]


class CctvView:
    """一台摄像头的实时画面:一条 ffmpeg 拉 RTSP、出 MJPEG,观众共用;最后一个观众走了 ``idle_s``
    收掉。

    - **口令不进命令行**(W19 外审:同机别的用户 ``ps``、``/proc/<pid>/cmdline`` 看得到)。
      带账号的地址写进
      一个只许站点用户读(0600)的 concat 清单,ffmpeg 从清单里打开;画面出来就删清单。ffmpeg 5 起清单里
      再带 ``rtsp_transport tcp``;4.x(Ubuntu 22.04)不认这一句,用 ffmpeg 默认的(先 UDP,收不到转 TCP)
      。
    - **一代一条进程**:每起一条新的就换一代,清掉上一代的画面(摄像头已经断了的话,新观众不许先看到
      上一轮缓存的那张,以为画面还活着);旧一代的读线程改不了新一代的状态。
    - **收尾都走一处**(``_reap``):ffmpeg 自己退了、没人看了、改了配置、站点收尾 —— 杀进程、回收、
      关管道、
      关错误输出的临时文件、删清单。``close`` 幂等。
    """

    def __init__(self, cam: Camera, *, ffmpeg: str = "ffmpeg", idle_s: float = 10.0,
                 popen: Callable[..., Any] = subprocess.Popen,
                 major: Callable[[str], int] = ffmpeg_major) -> None:
        self.cam = cam
        self._ffmpeg = ffmpeg
        self._idle_s = idle_s
        self._popen = popen
        self._major = major
        self._cond = threading.Condition()
        self._gen = 0
        self._proc: Any = None
        self._err: IO[bytes] | None = None
        self._list: str | None = None
        self._frame: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._closed = False
        self.error = ""

    def _write_list(self) -> str:
        url = _rtsp_with_auth(self.cam.rtsp_url, self.cam.username, self.cam.password)
        fd, path = tempfile.mkstemp(prefix="d1max-cam-", suffix=".ffconcat")   # 0600
        lines = ["ffconcat version 1.0", "file '" + url.replace("'", "'\\''") + "'"]
        if self._major(self._ffmpeg) >= 5:
            lines.append("option rtsp_transport tcp")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def _argv(self, listing: str) -> list[str]:
        return [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-protocol_whitelist", "file,rtsp,rtsps,rtp,srtp,udp,tcp,tls,http,https",
                "-i", listing, "-an", "-vf", "scale=-2:'min(720,ih)'",
                "-r", "8", "-q:v", "6", "-f", "mjpeg", "pipe:1"]

    def _start_locked(self) -> None:
        from d1max_site.video import _frames
        self._gen += 1
        gen = self._gen
        self._frame = None                               # 上一代的画面不给新观众
        self.error = ""
        self._list = self._write_list()
        self._err = tempfile.TemporaryFile()
        try:
            self._proc = self._popen(self._argv(self._list), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=self._err)
        except OSError as exc:
            self.error = f"起不来 ffmpeg:{exc}"
            self._reap_locked()
            return
        proc = self._proc

        def pump() -> None:
            first = True
            for f in _frames(proc.stdout):
                with self._cond:
                    if self._gen != gen:
                        return
                    self._frame, self._seq = f, self._seq + 1
                    if first:                            # 画面出来了:清单(带口令)不用了
                        first = False
                        self._drop_list_locked()
                    self._cond.notify_all()
            with self._cond:
                if self._gen != gen:
                    return
                self.error = "画面断了"
                tail = self._tail_locked()
                if tail:
                    log.warning("摄像头 %s 的画面断了:%s", self.cam.name, scrub(tail))
                self._reap_locked()
                self._cond.notify_all()
        threading.Thread(target=pump, daemon=True, name=f"cctv-view-{self.cam.name}").start()

    def _tail_locked(self) -> str:
        if self._err is None:
            return ""
        with contextlib.suppress(OSError, ValueError):
            self._err.seek(0)
            return self._err.read()[-300:].decode("utf-8", "replace").strip()
        return ""

    def _drop_list_locked(self) -> None:
        if self._list is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._list)
            self._list = None

    def _reap_locked(self) -> None:
        """这一代收干净:杀、回收、关管道、关错误输出、删清单。(锁里调;杀进程不会卡太久。)"""
        from d1max_site.video import _kill
        p, self._proc = self._proc, None
        self._gen += 1                                   # 读线程看见换代就不再动状态
        if p is not None:
            _kill(p)
            with contextlib.suppress(Exception):
                p.wait(5)
            if getattr(p, "stdout", None) is not None:
                with contextlib.suppress(OSError):
                    p.stdout.close()
        if self._err is not None:
            with contextlib.suppress(OSError):
                self._err.close()
            self._err = None
        self._drop_list_locked()
        self._frame = None

    def close(self) -> None:
        """站点收尾、换了配置:收掉,之后不再起。幂等。"""
        with self._cond:
            self._closed = True
            self._reap_locked()
            self._cond.notify_all()

    def frames(self, *, first_timeout_s: float = 15.0) -> Iterator[bytes]:
        with self._cond:
            if self._closed:
                raise TimeoutError("这台摄像头的画面已经关了")
            self._viewers += 1
            if self._proc is None:
                self._start_locked()
            seen = 0                                     # 这一代的最新那张马上给(换代时已清空)
            gen = self._gen
        try:
            deadline = time.monotonic() + first_timeout_s
            got_any = False
            while True:
                with self._cond:
                    while self._seq == seen and self._proc is not None and self._gen == gen:
                        left = deadline - time.monotonic() if not got_any else 5.0
                        if left <= 0 or not self._cond.wait(left):
                            if not got_any:
                                raise TimeoutError("摄像头没出画面")
                            break
                    if self._gen != gen or self._proc is None:
                        return                           # 这一代没了(断了、关了)
                    if self._seq == seen:
                        continue
                    seen, frame = self._seq, self._frame
                if frame is not None:
                    got_any = True
                    yield frame
        finally:
            with self._cond:
                self._viewers -= 1
                idle = self._viewers == 0
                idle_gen = self._gen
            if idle:
                def later() -> None:
                    time.sleep(self._idle_s)
                    with self._cond:
                        if self._viewers or self._gen != idle_gen or self._proc is None:
                            return
                        self._reap_locked()
                threading.Thread(target=later, daemon=True).start()
