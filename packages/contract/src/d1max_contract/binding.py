"""机器人绑定(W42,``docs/W42-机器人绑定-设计.md``):站点、机器人、手机三边共用的格式。

- **绑定码**:每台随机 16 位(Crockford Base32,80 位随机),印在上装贴纸上。机器人只存它的 SHA-256。
  手输时大小写、``-``、空格都不计较;``I``/``L`` 当 ``1``、``O`` 当 ``0``。
- **贴纸二维码**:``D1MAXBOT1.<base64url(JSON)>``,``s`` 序列号、``c`` 绑定码、``w`` 热点名、
  ``p`` 热点密码、``f`` 配网服务证书指纹(DER 的 SHA-256 前 32 位十六进制)。
  跟站点配对码 ``D1MAXSITE1.``、开通码 ``D1MAX1.`` 前缀不同,扫错当场认得出。
- **核对码**:6 位数,从机器人那次申请的随机数算出来。手机和站点上各显示一次,两边对得上再批。
- **解绑令**:``D1MAXUNBIND1.<base64url(JSON)>.<base64url(签名)>``,
  JSON 是 ``{"s": 序列号, "exp": 到期毫秒,
  "n": 随机号}``,公司解绑钥匙(Ed25519,跟发行钥匙分开)签名。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any

from d1max_contract import ed25519

STICKER_PREFIX = "D1MAXBOT1."
UNBIND_PREFIX = "D1MAXUNBIND1."
_UNBIND_DOMAIN = b"d1max-unbind-v1\n"

#: Crockford Base32(没有 I L O U)。
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 16
#: 序列号也是站点上的机器人编号:跟站点 ``SAFE_ID`` 一样的字。
SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FP32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{16,64}$")
#: 解绑令最长管多久(签的时候拦一道;机器人那头也按它拦)。
UNBIND_MAX_MS = 7 * 24 * 3600_000


class BindingError(ValueError):
    """格式不对(给人看的原因)。"""


# ------------------------------------------------------------ 绑定码


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize_code(text: str) -> str:
    """手输的、扫来的绑定码 → 16 位大写。对不上格式抛 :class:`BindingError`。"""
    if not isinstance(text, str):
        raise BindingError("绑定码要是 16 位字母数字")
    s = "".join(ch for ch in text.upper() if ch not in "- \t")
    s = s.replace("I", "1").replace("L", "1").replace("O", "0")
    if len(s) != CODE_LEN or any(ch not in ALPHABET for ch in s):
        raise BindingError("绑定码要是 16 位字母数字")
    return s


def code_hash(code: str) -> str:
    return hashlib.sha256(("d1max-bind-code:" + normalize_code(code)).encode()).hexdigest()


def pretty_code(code: str) -> str:
    """印在贴纸上的样子:``7KQ2-M9XD-4HTP-R8WC``。"""
    c = normalize_code(code)
    return "-".join(c[i:i + 4] for i in range(0, CODE_LEN, 4))


def check_digits(nonce: str) -> str:
    """核对码:6 位数。"""
    h = hashlib.sha256(("d1max-bind-check:" + nonce).encode()).digest()
    return f"{int.from_bytes(h[:8], 'big') % 1_000_000:06d}"


def check_serial(serial: Any) -> str:
    if not isinstance(serial, str) or SERIAL_RE.match(serial) is None:
        raise BindingError("序列号只收字母、数字、点、下划线、减号,最多 64 个")
    return serial


def check_nonce(nonce: Any) -> str:
    if not isinstance(nonce, str) or _NONCE.match(nonce) is None:
        raise BindingError("随机数要是 16–64 位小写十六进制")
    return nonce


# ------------------------------------------------------------ WiFi


def check_wifi(ssid: Any, psk: Any) -> tuple[str, str]:
    """庄园 WiFi 名和密码。名字 1–32 字节;密码空(不加密)、8–63 个可见 ASCII,或者 64 位十六进制。"""
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 \
            or any(ord(ch) < 0x20 for ch in ssid):
        raise BindingError("WiFi 名要有,最长 32 字节")
    if not isinstance(psk, str):
        raise BindingError("WiFi 密码不对")
    if psk and not (_HEX64.match(psk) or (8 <= len(psk) <= 63
                                          and all(0x20 <= ord(ch) < 0x7f for ch in psk))):
        raise BindingError("WiFi 密码要 8–63 位(字母、数字、符号),不加密的留空")
    return ssid, psk


# ------------------------------------------------------------ 贴纸二维码


@dataclass(frozen=True)
class Sticker:
    serial: str
    code: str
    hotspot_ssid: str
    hotspot_psk: str
    fingerprint: str          # 配网服务证书指纹前 32 位十六进制

    def encode(self) -> str:
        d = {"s": self.serial, "c": normalize_code(self.code), "w": self.hotspot_ssid,
             "p": self.hotspot_psk, "f": self.fingerprint}
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()
        return STICKER_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def short_fp(der: bytes) -> str:
    """配网服务证书(DER)→ 贴纸上的 32 位指纹。"""
    return hashlib.sha256(der).hexdigest()[:32]


def decode_sticker(text: str) -> Sticker:
    text = text.strip() if isinstance(text, str) else ""
    if not text.startswith(STICKER_PREFIX):
        raise BindingError(f"不是机器人贴纸(要以 {STICKER_PREFIX} 开头)")
    body = text[len(STICKER_PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise BindingError("贴纸二维码坏了") from exc
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in "scwpf"):
        raise BindingError("贴纸二维码坏了")
    if _FP32.match(d["f"]) is None:
        raise BindingError("贴纸二维码坏了")
    check_wifi(d["w"], d["p"])
    return Sticker(check_serial(d["s"]), normalize_code(d["c"]), d["w"], d["p"], d["f"])


# ------------------------------------------------------------ 解绑令


@dataclass(frozen=True)
class UnbindToken:
    serial: str
    expires_ms: int
    nonce: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_unbind(seed: bytes, serial: str, *, now_ms: int, hours: float) -> str:
    check_serial(serial)
    ttl = int(hours * 3600_000)
    if not 0 < ttl <= UNBIND_MAX_MS:
        raise BindingError("解绑令有效期要在 0–168 小时之间")
    body = json.dumps({"s": serial, "exp": now_ms + ttl, "n": secrets.token_hex(16)},
                      separators=(",", ":"), sort_keys=True).encode()
    return UNBIND_PREFIX + _b64(body) + "." + _b64(ed25519.sign(seed, _UNBIND_DOMAIN + body))


def verify_unbind(text: str, pub: bytes, *, serial: str, now_ms: int) -> UnbindToken:
    """验解绑令:签名、序列号、没过期。用过没有由机器人自己记(这里不管)。"""
    text = text.strip() if isinstance(text, str) else ""
    if not text.startswith(UNBIND_PREFIX) or text.count(".") != 2:
        raise BindingError("不是解绑令")
    body_s, sig_s = text[len(UNBIND_PREFIX):].split(".")
    try:
        body, sig = _unb64(body_s), _unb64(sig_s)
        d = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise BindingError("解绑令坏了") from exc
    if not ed25519.verify(pub, _UNBIND_DOMAIN + body, sig):
        raise BindingError("解绑令签名不对")
    if not isinstance(d, dict) or d.get("s") != serial:
        raise BindingError("这张解绑令不是给这台机器人的")
    exp, nonce = d.get("exp"), d.get("n")
    if not isinstance(exp, int) or isinstance(exp, bool) or not isinstance(nonce, str) \
            or _NONCE.match(nonce) is None:
        raise BindingError("解绑令坏了")
    if now_ms > exp:
        raise BindingError("解绑令过期了:请公司重新签一张")
    if exp - now_ms > UNBIND_MAX_MS:
        raise BindingError("解绑令的有效期不对(机器人的钟可能不准)")
    return UnbindToken(serial, exp, nonce)


# ------------------------------------------------------------ WiFi 状态(遥测 ``net.wifi``)

#: 机器人现在走哪条线。
LINKS = ("wifi", "4g", "none")
#: 挂着的 WiFi 改动:``online`` 机器人连得上站点、等确认才换;``trial`` 连不上站点、正在试新 WiFi。
CHANGE_MODES = ("online", "trial")
_CHANGE_ID = re.compile(r"^[0-9a-f]{8,32}$")


def wifi_view(net: Any) -> dict[str, Any] | None:
    """遥测 ``net.wifi`` → 站点、网页、手机看的形状;形状不对的字段丢掉,整块不对回 ``None``。

    ``{"ssid": 现在连的 WiFi, "link": "wifi"/"4g"/"none",
    "change": {"id", "ssid", "mode", "expires_ms"} 或 None}``"""
    w = net.get("wifi") if isinstance(net, dict) else None
    if not isinstance(w, dict):
        return None
    out: dict[str, Any] = {"ssid": w["ssid"][:64] if isinstance(w.get("ssid"), str) else "",
                           "link": w["link"] if w.get("link") in LINKS else "none",
                           "change": None}
    c = w.get("change")
    if isinstance(c, dict) and isinstance(c.get("id"), str) and _CHANGE_ID.match(c["id"]) \
            and c.get("mode") in CHANGE_MODES and isinstance(c.get("ssid"), str):
        exp = c.get("expires_ms")
        out["change"] = {"id": c["id"], "ssid": c["ssid"][:64], "mode": c["mode"],
                         "expires_ms": exp if isinstance(exp, int) and not isinstance(exp, bool)
                         else None}
    return out
