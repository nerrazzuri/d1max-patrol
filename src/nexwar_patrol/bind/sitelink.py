"""配网服务跟站点的往来(W42 §4、§5):一律 HTTPS、**钉站点证书指纹**(跟 ``provision`` 一样:对不上就断,
请求一个字节都不发)。"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
from typing import Any
from urllib.parse import quote, urlparse


class LinkError(RuntimeError):
    """连不上、指纹不对、站点回了错:``status`` 是 HTTP 状态(连不上是 0)。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class SiteLink:
    def __init__(self, *, timeout_s: float = 8.0, attest: Any = None) -> None:
        self.timeout_s = timeout_s
        #: W63:``attest(purpose, site_id, nonce)`` → 答题(``devid.attester``);没有 = 不答
        self.attest = attest

    def _call(self, site: dict[str, Any], method: str, path: str,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        u = urlparse(str(site.get("url") or ""))
        if u.scheme != "https" or not u.hostname:
            raise LinkError(f"站点地址错误:{site.get('url')}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE                 # 不靠 CA 链:靠下面钉住的指纹
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout_s,
                                           context=ctx)
        try:
            try:
                conn.connect()
            except OSError as exc:
                raise LinkError(f"无法连接站点 {u.hostname}:{u.port or 443}:{exc}") from exc
            der = conn.sock.getpeercert(binary_form=True) or b""
            want = str(site.get("fp") or "").lower().removeprefix("sha256:")
            if hashlib.sha256(der).hexdigest() != want:
                raise LinkError("站点证书指纹不匹配:不是手机上配对的那个站点")
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(256 * 1024)
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, LinkError):
                raise
            raise LinkError(f"跟站点说话已断开:{exc}") from exc
        finally:
            conn.close()
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = {}
        if resp.status != 200:
            raise LinkError(str(d.get("error") or f"站点回 {resp.status}"), resp.status)
        return d if isinstance(d, dict) else {}

    def challenge(self, site: dict[str, Any]) -> dict[str, Any] | None:
        """W63:站点出的题;老站点没有出题的口(404)回 ``None``(它也不核)。"""
        try:
            return self._call(site, "GET", "/api/attest/challenge")
        except LinkError as exc:
            if exc.status == 404:
                return None
            raise

    def request(self, site: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        if self.attest is not None:                    # W63:先要题、用设备钥匙答,一起交
            ch = self.challenge(site)
            if ch is not None:
                ans = self.attest("bind", str(ch.get("site_id") or ""), str(ch.get("nonce") or ""))
                if ans is not None:
                    body = {**body, "attest": ans}
        return self._call(site, "POST", "/api/bind/request", body)

    def poll(self, site: dict[str, Any], rid: str, secret: str) -> dict[str, Any]:
        return self._call(site, "GET", f"/api/bind/request/{quote(rid)}?secret={quote(secret)}")

    def cert_state(self, site: dict[str, Any], serial: str, cert: str) -> str:
        d = self._call(site, "GET", f"/api/bind/state?serial={quote(serial)}&cert={quote(cert)}")
        s = d.get("state")
        return s if s in ("active", "revoked", "unknown") else "unknown"
