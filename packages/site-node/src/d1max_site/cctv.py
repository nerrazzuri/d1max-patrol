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
    offline_told: bool = False
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

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    def _set(self, connected: bool, error: str = "") -> None:
        st = self.status
        if st.connected != connected:
            st.since_ms = self._now()
        st.connected = connected
        st.error = error
        if connected:
            st.offline_told = False

    def handle(self, ev: OnvifEvent) -> bool:
        """一条通知。回报没报入侵。只认「由无到有」的那一下;订阅一开始报的现状只记下来。"""
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
    return [Camera(name=r["name"], onvif_url=r["onvif_url"], username=r["username"],
                   password=r["password"], zone=r["zone"], rtsp_url=r["rtsp_url"],
                   motion=bool(r["motion"]))
            for r in db.query("SELECT * FROM cameras ORDER BY name")]


def add_camera(db: SiteDB, cam: Camera, *, now_ms: int) -> None:
    if not SAFE_ID.match(cam.name):
        raise ValueError(f"摄像头名字只许字母、数字、. _ -:{cam.name!r}")
    if not cam.zone:
        raise ValueError("要防区(--zone):这台摄像头报的入侵算哪个防区")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO cameras(name, onvif_url, username, password, zone, "
                  "rtsp_url, motion, added_ms) VALUES (?,?,?,?,?,?,?,?)",
                  (cam.name, cam.onvif_url, cam.username, cam.password, cam.zone, cam.rtsp_url,
                   int(cam.motion), now_ms))


def remove_camera(db: SiteDB, name: str) -> bool:
    with db.tx() as c:
        return c.execute("DELETE FROM cameras WHERE name=?", (name,)).rowcount > 0


class CctvManager:
    """站点里的摄像头:按库起停订阅、出状态、断太久报告警。``report`` 把入侵交给事件派遣
    (跳到事件循环)。"""

    def __init__(self, db: SiteDB, *, report: Callable[[Camera, OnvifEvent], None],
                 now_ms: Callable[[], int], alert: Callable[..., Any] | None = None,
                 watch_factory: Callable[..., CameraWatch] = CameraWatch) -> None:
        self.db = db
        self._report = report
        self._now = now_ms
        self._alert = alert
        self._factory = watch_factory
        self._lock = threading.Lock()
        self.watches: dict[str, CameraWatch] = {}

    def sync(self) -> None:
        """照库里的摄像头起停订阅(命令行加、删、改了之后,这一拍就跟上)。顺手看断太久的报告警。"""
        want = {c.name: c for c in load_cameras(self.db)}
        with self._lock:
            for name in list(self.watches):
                w = self.watches[name]
                if name not in want or want[name] != w.cam:
                    w.stop()
                    del self.watches[name]
            for name, cam in want.items():
                if name not in self.watches:
                    w = self._factory(cam, on_intrusion=self._report, now_ms=self._now)
                    self.watches[name] = w
                    w.start()
            watches = list(self.watches.values())
        now = self._now()
        for w in watches:
            st = w.status
            if not st.connected and not st.offline_told \
                    and now - st.since_ms >= OFFLINE_ALERT_S * 1000:
                st.offline_told = True
                if self._alert is not None:
                    try:
                        self._alert(kind="cctv_offline", robot="site",
                                    title=f"摄像头 {w.cam.name} 连不上(防区 {w.cam.zone})",
                                    detail=f"{st.error or '连不上'};这一路的入侵现在收不到")
                    except Exception:
                        log.exception("报「摄像头连不上」失败")

    def view(self) -> list[dict[str, Any]]:
        """给手机看的(不带口令、不带地址里的口令)。"""
        with self._lock:
            watches = list(self.watches.values())
        return [{"name": w.cam.name, "zone": w.cam.zone, "motion": w.cam.motion,
                 "live": bool(w.cam.rtsp_url), "connected": w.status.connected,
                 "since_ms": w.status.since_ms, "last_event_ms": w.status.last_event_ms,
                 "last_topic": w.status.last_topic, "error": w.status.error}
                for w in sorted(watches, key=lambda w: w.cam.name)]

    def camera(self, name: str) -> Camera | None:
        with self._lock:
            w = self.watches.get(name)
        return w.cam if w is not None else None

    def close(self) -> None:
        with self._lock:
            for w in self.watches.values():
                w.stop()
            self.watches.clear()


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


def _rtsp_with_auth(url: str, user: str, password: str) -> str:
    """地址里没带账号就把这台摄像头的账号塞进去(``rtsp://user:pw@host/...``)。"""
    if not user or "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return url
    from urllib.parse import quote
    scheme, rest = url.split("//", 1)
    return f"{scheme}//{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


class CctvView:
    """一台摄像头的实时画面:一条 ffmpeg 拉 RTSP、出 MJPEG,观众共用;最后一个观众走了 ``idle_s``
    收掉。"""

    def __init__(self, cam: Camera, *, ffmpeg: str = "ffmpeg", idle_s: float = 10.0,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.cam = cam
        self._ffmpeg = ffmpeg
        self._idle_s = idle_s
        self._popen = popen
        self._cond = threading.Condition()
        self._proc: Any = None
        self._frame: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._err: IO[bytes] | None = None
        self.error = ""

    def _argv(self) -> list[str]:
        url = _rtsp_with_auth(self.cam.rtsp_url, self.cam.username, self.cam.password)
        return [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", url, "-an", "-vf", "scale=-2:'min(720,ih)'",
                "-r", "8", "-q:v", "6", "-f", "mjpeg", "pipe:1"]

    def _start_locked(self) -> None:
        from d1max_site.video import _frames
        self._err = tempfile.TemporaryFile()
        self._proc = self._popen(self._argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=self._err)
        proc = self._proc

        def pump() -> None:
            for f in _frames(proc.stdout):
                with self._cond:
                    if self._proc is not proc:
                        return
                    self._frame, self._seq = f, self._seq + 1
                    self._cond.notify_all()
            with self._cond:
                if self._proc is proc:
                    self.error = "画面断了"
                    self._proc = None
                    self._cond.notify_all()
        threading.Thread(target=pump, daemon=True, name=f"cctv-view-{self.cam.name}").start()

    def frames(self, *, first_timeout_s: float = 15.0) -> Iterator[bytes]:
        from d1max_site.video import _kill
        with self._cond:
            self._viewers += 1
            if self._proc is None:
                self.error = ""
                self._start_locked()
        seen = 0
        try:
            deadline = time.monotonic() + first_timeout_s
            while True:
                with self._cond:
                    while self._seq == seen and self._proc is not None:
                        left = deadline - time.monotonic() if seen == 0 else 5.0
                        if left <= 0 or not self._cond.wait(left):
                            if seen == 0:
                                raise TimeoutError("摄像头没出画面")
                            break
                    if self._proc is None and self._seq == seen:
                        return
                    seen, frame = self._seq, self._frame
                if frame is not None:
                    yield frame
        finally:
            with self._cond:
                self._viewers -= 1
                idle = self._viewers == 0
            if idle:
                def later() -> None:
                    time.sleep(self._idle_s)
                    with self._cond:
                        if self._viewers or self._proc is None:
                            return
                        p, self._proc = self._proc, None
                    _kill(p)
                threading.Thread(target=later, daemon=True).start()
