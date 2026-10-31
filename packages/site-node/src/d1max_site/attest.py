"""站点核设备身份(W63,``docs/W63-设备身份-设计.md`` 二、三):只有原厂上装能加入。

- 出题 :meth:`AttestDesk.challenge`:随机数带时间,站点自己的密钥(每次起来新生成,不落盘)签过;
  :data:`NONCE_TTL_MS` 内有效,答过一次就作废(记在内存里)。
- 核 :meth:`AttestDesk.verify`:出厂证书是认的出厂钥匙签的(没作废)、证书序列号就是来申请的、
  答题签名用证书里的设备公钥验得过、题是这个站点出的没过期没用过、现场芯片编号跟证书一致、
  这个序列号以前登记过的设备公钥(``device_ids``)没换。
- 核不过:抛 :class:`AttestRefused`,对外一律 :data:`NEUTRAL` 这一句;**不报警、管理员看不到**;
  在 ``company_reports`` 记一条(同一来源一分钟一条),公司后台(W43)做好后上传,在那之前工程师用
  ``d1max-site company-reports`` 导出。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from collections.abc import Callable, Iterable
from typing import Any

from d1max_contract import device_id as di

#: 核不过时对机器人、手机说的话(不说哪一项没过)。
NEUTRAL = "此设备无法加入站点,请联系供应商"
#: 题多久有效。
NONCE_TTL_MS = 5 * 60_000
#: 同一来源多久记一条给公司的记录。
REPORT_EVERY_MS = 60_000
_NONCE = re.compile(r"^(\d{1,15})\.([0-9a-f]{32})\.([0-9a-f]{64})$")


class AttestRefused(Exception):
    """核不过(对外的话固定是 :data:`NEUTRAL`;为什么只记给公司)。"""

    def __init__(self, why: str) -> None:
        super().__init__(NEUTRAL)
        self.why = why


class AttestDesk:
    def __init__(self, db: Any, *, site_id: str, now_ms: Callable[[], int],
                 factory_pubs: Callable[[], Iterable[bytes]],
                 revoked: Callable[[], Iterable[str]]) -> None:
        self.db = db
        self.site_id = site_id
        self._now = now_ms
        self._pubs = factory_pubs
        self._revoked = revoked
        self._key = secrets.token_bytes(32)
        self._used: dict[str, int] = {}
        self._reported: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 出题

    def _mac(self, ts: str, rnd: str) -> str:
        return hmac.new(self._key, f"{ts}.{rnd}".encode(), hashlib.sha256).hexdigest()

    def challenge(self) -> dict[str, Any]:
        ts, rnd = str(self._now()), secrets.token_hex(16)
        return {"nonce": f"{ts}.{rnd}.{self._mac(ts, rnd)}", "site_id": self.site_id,
                "expires_ms": int(ts) + NONCE_TTL_MS}

    def _take_nonce(self, nonce: Any) -> None:
        m = _NONCE.match(nonce) if isinstance(nonce, str) else None
        if m is None or not hmac.compare_digest(m.group(3), self._mac(m.group(1), m.group(2))):
            raise AttestRefused("题不是本站点出的")
        now = self._now()
        if not 0 <= now - int(m.group(1)) <= NONCE_TTL_MS:
            raise AttestRefused("题过期了")
        with self._lock:
            for k in [k for k, t in self._used.items() if now - t > NONCE_TTL_MS]:
                del self._used[k]
            if nonce in self._used:
                raise AttestRefused("题已经用过")
            self._used[nonce] = now

    # ------------------------------------------------------------ 核

    def verify(self, ans: Any, *, purpose: str, serial: str, source: str) -> di.DeviceCert:
        try:
            return self._verify(ans, purpose=purpose, serial=serial)
        except AttestRefused as exc:
            self._report(exc.why, ans, serial=serial, source=source, purpose=purpose)
            raise

    def _verify(self, ans: Any, *, purpose: str, serial: str) -> di.DeviceCert:
        if not isinstance(ans, dict):
            raise AttestRefused("没有设备身份(没有出厂证书)")
        try:
            cert = di.verify_cert(ans.get("cert"), list(self._pubs()), revoked=set(self._revoked()))
        except di.DeviceIdError as exc:
            raise AttestRefused(f"出厂证书:{exc}") from exc
        if cert.serial != serial:
            raise AttestRefused(f"出厂证书的序列号 {cert.serial} 不是来申请的 {serial}")
        nonce = ans.get("nonce")
        try:
            msg = di.attest_message(purpose=purpose, site_id=self.site_id, nonce=str(nonce or "-"),
                                    serial=serial)
            di.verify_attest(ans.get("cert"), msg, ans.get("sig"))
        except di.DeviceIdError as exc:
            raise AttestRefused(f"答题:{exc}") from exc
        self._take_nonce(nonce)
        if ans.get("hw_id") != cert.hw_id:
            raise AttestRefused("现场芯片编号跟出厂证书不一致(板子换过,或者整盘拷过去的)")
        pub = di._b64(cert.device_pub)
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT device_pub FROM device_ids WHERE serial=?", (serial,)).fetchone()
            if row is not None and row["device_pub"] != pub:
                raise AttestRefused("这个序列号登记过另一把设备钥匙(同一序列号两块板:仿冒)")
            if row is None:
                c.execute("INSERT INTO device_ids(serial, device_pub, hw_id, model, first_ms, "
                          "last_ms) VALUES (?,?,?,?,?,?)",
                          (serial, pub, cert.hw_id, cert.model, now, now))
            else:
                c.execute("UPDATE device_ids SET last_ms=? WHERE serial=?", (now, serial))
        return cert

    # ------------------------------------------------------------ 给公司的记录

    def _report(self, why: str, ans: Any, *, serial: str, source: str, purpose: str) -> None:
        now = self._now()
        with self._lock:
            last = self._reported.get(source)
            if last is not None and now - last < REPORT_EVERY_MS:
                return
            self._reported[source] = now
        cert = ans.get("cert") if isinstance(ans, dict) else None
        cert = cert if isinstance(cert, dict) else {}
        detail = {"why": why, "purpose": purpose,
                  "claimed": {k: str(cert.get(k, ""))[:100] for k in
                              ("serial", "model", "device_pub", "hw_id", "key_id")},
                  "hw_id_now": str(ans.get("hw_id", ""))[:100] if isinstance(ans, dict) else ""}
        with self.db.tx() as c:
            c.execute("INSERT INTO company_reports(at_ms, kind, source, serial, detail) "
                      "VALUES (?,?,?,?,?)", (now, "device_rejected", source[:64], serial[:64],
                                            json.dumps(detail, ensure_ascii=False)))
