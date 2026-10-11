"""手机扫码添加站点(App V2):配对码 = 站点名 + 地址 + 站点服务证书指纹,一行字,可以画成二维码。

- 形状:``D1MAXSITE1.<base64url(JSON)>``,JSON 是 ``{"n": 站点名, "u": 地址, "f": 指纹}``。
  跟狗的开通码(``D1MAX1.``,:mod:`d1max_site.provision`)前缀不同,扫错了当场认得出来。
- **里面没有任何秘密**:没有账号、口令、令牌。指纹是公开的(连上站点谁都看得到证书),配对码只是
  省得人手抄 64 位十六进制。手机扫了以后照样要输账号口令登录。
- 地址是**手机要连的地址**,站点自己不知道(它不知道自己在外面叫什么、过没过 VPN):网页上由值班台的地址
  带出来、可以改;命令行自己给。只收 ``https://主机[:端口]``,不带路径、账号、查询串。
- 信任从哪儿来:配对码要从登录后的值班台(或站点机器的命令行)拿。别人给的二维码能把手机指到别的站点去,
  所以手机上扫完先把站点名、地址、指纹摆出来让人核对,确认了才存。
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlsplit

PREFIX = "D1MAXSITE1."
MAX_NAME = 64
MAX_URL = 200
_FP = re.compile(r"^[0-9a-f]{64}$")


class PairingError(ValueError):
    """配对码拼不出来或读不出来(给人看的原因)。"""


def check_url(url: str) -> str:
    """手机要连的地址:只收 ``https://主机[:端口]``。回规范化后的(去掉末尾的斜杠)。"""
    url = url.strip()
    if not url or len(url) > MAX_URL:
        raise PairingError("地址要是 https://主机:端口")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError as exc:
        raise PairingError("地址要是 https://主机:端口") from exc
    if u.scheme != "https" or not u.hostname or u.username is not None or u.password is not None:
        raise PairingError("地址要是 https://主机:端口")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise PairingError("地址只写到端口为止,不带路径")
    if any(c.isspace() or ord(c) < 0x20 for c in url):
        raise PairingError("地址要是 https://主机:端口")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"https://{host}" + (f":{port}" if port is not None else "")


def encode(name: str, url: str, fingerprint: str) -> str:
    fp = fingerprint.strip().lower()
    if fp.startswith("sha256:"):
        fp = fp[7:]
    if _FP.match(fp) is None:
        raise PairingError("证书指纹要是 64 位十六进制")
    name = name.strip()
    if not name or len(name) > MAX_NAME:
        raise PairingError(f"站点名要有,最长 {MAX_NAME} 个字")
    raw = json.dumps({"n": name, "u": check_url(url), "f": fp}, separators=(",", ":"),
                     sort_keys=True, ensure_ascii=False).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(code: str) -> dict[str, str]:
    """读配对码(手机上是 Dart 那份;这份给测试和命令行核对用)。"""
    code = code.strip()
    if not code.startswith(PREFIX):
        raise PairingError(f"不是配对码(要以 {PREFIX} 开头)")
    body = code[len(PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise PairingError("配对码坏了") from exc
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("n", "u", "f")):
        raise PairingError("配对码坏了")
    if _FP.match(d["f"]) is None or not d["n"].strip() or len(d["n"]) > MAX_NAME:
        raise PairingError("配对码坏了")
    return {"name": d["n"], "url": check_url(d["u"]), "fingerprint": d["f"]}
