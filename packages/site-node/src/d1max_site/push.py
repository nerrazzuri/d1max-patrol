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
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_site.alert_sources import SITE

log = logging.getLogger(__name__)

JPUSH_URL = "https://api.jpush.cn/v3/push"
DEFAULT_SECRET = Path("/etc/d1max-site/jpush.secret")
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


def load_sender(cfg: dict[str, Any]) -> tuple[Any, str]:
    """按 ``site.json`` 造推送通道。回 (通道或 ``None``, 没配 / 配不好的原因)。"""
    p = cfg.get("push") or {}
    if not p:
        return None, "没配推送(site.json 的 push)"
    if p.get("provider", "jpush") != "jpush":
        return None, f"不认识的推送通道 {p.get('provider')!r}(现在只有 jpush)"
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
                 site_name: str = "", why_off: str = "") -> None:
        self.db = db
        self._now = now_ms
        self.sender = sender
        self.site_name = site_name
        #: 没配推送的原因(值守汇总里明说)。
        self.why_off = why_off if sender is None else ""
        #: 告警台(站点主程序接上):发不出去报 P2。
        self.alerts: Any = None
        self.last_error = ""

    # ------------------------------------------------------------ 手机登记

    def register(self, account: str, reg_id: str, platform: str) -> None:
        reg_id = str(reg_id).strip()
        if not reg_id or len(reg_id) > 128 or not reg_id.isascii():
            raise ValueError("registration_id 不对")
        if platform not in ("android", "ios", "harmony"):
            raise ValueError("platform 要是 android / ios / harmony")
        with self.db.tx() as c:
            c.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms) "
                      "VALUES (?,?,?,?) ON CONFLICT(reg_id) DO UPDATE SET "
                      "account=excluded.account, platform=excluded.platform, "
                      "updated_ms=excluded.updated_ms", (reg_id, account, platform, self._now()))

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
        return {"configured": self.sender is not None, "why_off": self.why_off, "devices": n,
                "waiting": waiting, "error": self.last_error}

    # ------------------------------------------------------------ 发

    def tick(self) -> None:
        """要推的都推出去(要等网络:站点主程序放在自己那条道上跑)。"""
        now = self._now()
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM push_outbox WHERE sent_ms IS NULL AND dropped='' AND next_ms<=? "
            "ORDER BY id LIMIT 50", (now,))]
        for row in rows:
            why = self._skip(row, now)
            if why:
                with self.db.tx() as c:
                    c.execute("UPDATE push_outbox SET dropped=? WHERE id=?", (why, row["id"]))
                continue
            if self.sender is None:
                continue                              # 没配:留着(配好了、一小时内的还能推)
            # 停用了的账号的手机不推(停用就该收不到了)
            reg = [r["reg_id"] for r in self.db.query(
                "SELECT d.reg_id FROM push_devices d JOIN accounts a ON a.name = d.account "
                "WHERE a.disabled = 0")]
            if not reg:
                with self.db.tx() as c:
                    c.execute("UPDATE push_outbox SET dropped='没有登记的手机' WHERE id=?",
                              (row["id"],))
                continue
            title = f"{self.site_name} · P1" if self.site_name else "P1 告警"
            try:
                for i in range(0, len(reg), BATCH):
                    self.sender.send(reg[i:i + BATCH], title, row["title"],
                                     {"alert_key": row["alert_key"]})
            except PushError as exc:
                self.last_error = str(exc)[:300]
                wait = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** min(row["attempts"], 10))
                with self.db.tx() as c:
                    c.execute("UPDATE push_outbox SET attempts=attempts+1, next_ms=?, error=? "
                              "WHERE id=?", (now + wait * 1000, self.last_error, row["id"]))
                log.warning("P1 推送没发出去(%s 秒后再试):%s", wait, exc)
                continue
            self.last_error = ""
            with self.db.tx() as c:
                c.execute("UPDATE push_outbox SET sent_ms=?, error='' WHERE id=?",
                          (now, row["id"]))
        self._stuck(now)

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
                                    + ";看站点能不能上外网、极光的 AppKey 和 Master Secret 对不对")
        elif not stuck and oldest is None and open_:
            self.alerts.resolve_all(SITE, "push_failed", who="site:push_ok")
