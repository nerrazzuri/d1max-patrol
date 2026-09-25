"""狗专用的接收口(W00c5d,决策 8)。

- **HTTPS + mTLS 必须**:跟 MQTT 同一张站点 CA、同一份吊销表;另外拿对端证书的指纹对登记表 ——
  **狗的身份就是它的证书**,不另发令牌,狗 A 写不进狗 B 的目录(路径里的狗号由站点按证书填,
  不看请求头)。
- 上行:``POST /api/intake/put``,形状由狗那头的 ``d1max_agent.engine.http_sink`` 定死:原始字节做
  body,``X-D1Max-Run/Rel/Offset/Total`` 在请求头里(URL 转义);回执 ``{ok, stored, sha256, message}``,
  收下一块**必须回 200**。
- 手机不走这个口;防火墙可以只放狗那一段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from d1max_contract.intake import (
    H_OFFSET,
    H_REL,
    H_RUN,
    H_TOTAL,
    MAX_CHUNK,
    REFUSED_STATUS,
    WIRE_PATH,
)
from d1max_site.evidence import EvidenceStore, PathRefused
from d1max_site.tlsserve import TlsHandlerMixin, TlsThreadingServer

log = logging.getLogger(__name__)

#: 站点狗专用口的默认端口。
DEFAULT_PORT = 8444
#: 一条请求最多等多久(秒)。
REQUEST_TIMEOUT_S = 60.0


def server_context(*, cert: Path, key: Path, ca: Path, crl: Path | None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca))
    if crl is not None and Path(crl).is_file():
        ctx.load_verify_locations(cafile=str(crl))
        ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return ctx


class IntakeServer:
    def __init__(self, *, host: str, port: int, ctx: ssl.SSLContext, db, store: EvidenceStore,
                 now_ms: Callable[[], int], maps: Any = None) -> None:
        self.db = db
        self.store = store
        #: W00c5d 第二部分:地图目录(收狗建的图、录包;给狗下载图)。
        self.maps = maps
        #: W00c5d 第三部分:发布目录(给狗下载发布包)。
        self.releases = None
        #: 永远不收的一块:``(狗, 一趟, 文件, 原因)``(主程序接到告警台)。
        self.on_refused: Callable[[str, str, str, str], None] | None = None
        self._now = now_ms
        intake = self

        class Handler(_Handler):
            site = intake
            timeout = REQUEST_TIMEOUT_S

        self.httpd = TlsThreadingServer((host, port), Handler, ctx=ctx, max_connections=64)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"https://{host}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="d1max-intake",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(10)

    def robot_for(self, der: bytes | None) -> str | None:
        """对端证书 → 登记表里的狗(指纹对得上、没吊销、没过期)。认不出来返回 None。"""
        if not der:
            return None
        fp = "sha256:" + hashlib.sha256(der).hexdigest()
        rows = self.db.query("SELECT robot_id, revoked, issued_at, expires_at FROM robots "
                             "WHERE fingerprint=?", (fp,))
        now = self._now()
        for r in rows:
            if not r["revoked"] and r["issued_at"] <= now < r["expires_at"]:
                return r["robot_id"]
        return None


class _Handler(TlsHandlerMixin):
    site: IntakeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("intake %s " + fmt, self.client_address[0], *args)

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int, why: str) -> None:
        # 结构完好、ok 为假:狗那头把这当成「站点明确说了不行」,从头再来、退避。
        self._reply(status, {"ok": False, "stored": 0, "sha256": "", "message": why})

    def _later(self, status: int, why: str) -> None:
        # 没有 ok 这个键:狗那头读成「站点没说清楚」,**只退避、不作废已传的进度**
        # (证书一时认不出、站点盘满这种,过一会儿就好,不该让狗从第 0 个字节重传)。
        self._reply(status, {"message": why})

    def do_GET(self) -> None:
        """狗下载站点下发的图:``/maps/<地图号>/<版本>/<文件名>``。只认登记过的狗。"""
        from d1max_site.maps import MapError
        parts = self.path.split("?", 1)[0].split("/")
        robot = self.site.robot_for(self.connection.getpeercert(binary_form=True))
        if robot is None:
            return self._later(403, "证书不认识或已吊销")
        if len(parts) == 3 and parts[1] == "releases" and self.site.releases is not None \
                and parts[2].endswith(".tar.gz"):
            from d1max_site.releases import ReleaseCatalogError
            try:
                path = self.site.releases.file_path(unquote(parts[2])[:-len(".tar.gz")])
            except ReleaseCatalogError as exc:
                return self._refuse(404, str(exc))
            return self._stream(path)
        if len(parts) != 5 or parts[1] != "maps" or self.site.maps is None:
            return self._refuse(404, "没有这个")
        try:
            path = self.site.maps.file_path(unquote(parts[2]), unquote(parts[3]),
                                            unquote(parts[4]))
        except MapError as exc:
            return self._refuse(404, str(exc))
        return self._stream(path)

    def _stream(self, path) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 16):
                self.wfile.write(chunk)

    def do_POST(self) -> None:
        kinds = {WIRE_PATH: "runs", "/maps" + WIRE_PATH: "maps", "/bags" + WIRE_PATH: "bags"}
        kind = kinds.get(self.path)
        if kind is None or (kind != "runs" and self.site.maps is None):
            self._drain()
            return self._refuse(404, "没有这个")
        robot = self.site.robot_for(self.connection.getpeercert(binary_form=True))
        if robot is None:
            self._drain()
            return self._later(403, "证书不认识或已吊销")
        try:
            n = int(self.headers.get("Content-Length", ""))
            offset = int(self.headers.get(H_OFFSET, ""))
            total = int(self.headers.get(H_TOTAL, ""))
        except ValueError:
            self.close_connection = True
            return self._refuse(400, "缺长度、偏移或总长")
        if n < 0 or n > MAX_CHUNK:
            self.close_connection = True
            return self._refuse(413, f"一块最多 {MAX_CHUNK} 字节")
        data = self.rfile.read(n)
        if len(data) != n:
            self.close_connection = True
            return self._refuse(400, "body 没读全")
        run = unquote(self.headers.get(H_RUN, ""))
        rel = unquote(self.headers.get(H_REL, ""))
        put = {"runs": self.site.store.put,
               "maps": getattr(self.site.maps, "put_map_chunk", None),
               "bags": getattr(self.site.maps, "put_bag_chunk", None)}[kind]
        try:
            got = put(robot, run, rel, offset=offset, data=data, total=total)
        except PathRefused as exc:
            # 永远不收:告诉狗隔离这个文件、别再重试;站点出一条告警让人看。
            log.warning("%s 传来的 %s/%s 永远不收: %s", robot, run, rel, exc)
            if self.site.on_refused is not None:
                try:
                    self.site.on_refused(robot, run, rel, str(exc))
                except Exception:
                    log.exception("报「不收」的告警失败")
            return self._reply(REFUSED_STATUS, {"ok": False, "stored": 0, "sha256": "",
                                                "refused": True, "message": str(exc)})
        except ValueError as exc:
            log.warning("%s 传来的 %s/%s 这一块不对: %s", robot, run, rel, exc)
            return self._refuse(400, str(exc))
        except OSError as exc:
            log.warning("证据库写不进去: %s", exc)
            return self._later(507, f"站点盘写不进去: {exc}")
        self._reply(200, {"ok": True, "stored": got.size, "sha256": got.sha256, "message": ""})

    def _drain(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if 0 < n <= MAX_CHUNK:
            self.rfile.read(n)
        else:
            self.close_connection = True
