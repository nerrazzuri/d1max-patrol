"""设备身份(W63,``docs/W63-设备身份-设计.md``):只有原厂上装能加入站点。

- **出厂申请**(上装板烧写时出):序列号、型号、设备公钥(私钥只在这块板上)、芯片编号 ``hw_id``
  (芯片唯一序号的 SHA-256,64 位十六进制)。
- **出厂证书**:公司**出厂钥匙**(Ed25519,跟发行、解绑令钥匙分开)签上面这些 + 签发时间、出厂钥匙编号。
- **答题**:站点出一道随机题,机器人用设备私钥签「用途 | 站点编号 | 题 | 序列号」;站点用证书里的设备公钥验。

证书是 JSON 对象,签的是固定次序拼起来的字节(不靠 JSON 怎么排版):见 :func:`_signed_bytes`。
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from d1max_contract import ed25519
from d1max_contract.binding import BindingError, check_serial

VERSION = 1
_CERT_DOMAIN = b"d1max-device-cert-v1\0"
_ATTEST_DOMAIN = b"d1max-attest-v1\0"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,32}")
_PURPOSE = re.compile(r"[a-z_]{1,16}")


class DeviceIdError(ValueError):
    """申请、证书、答题不对。"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: Any) -> bytes:
    if not isinstance(s, str):
        raise DeviceIdError("出厂证书已损坏")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as exc:
        raise DeviceIdError("出厂证书已损坏") from exc


def key_id(pub: bytes) -> str:
    """出厂公钥的编号 = 原始公钥字节 SHA-256 的前 16 位十六进制(作废名单里写它)。"""
    return hashlib.sha256(pub).hexdigest()[:16]


def _check(serial: Any, model: Any, hw_id: Any) -> tuple[str, str, str]:
    try:
        s = check_serial(serial)
    except BindingError as exc:
        raise DeviceIdError(str(exc)) from exc
    if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
        raise DeviceIdError("型号要是 1–32 位字母、数字、. _ -")
    if not isinstance(hw_id, str) or _HEX64.fullmatch(hw_id) is None:
        raise DeviceIdError("芯片编号要是 64 位小写十六进制")
    return s, model, hw_id


def make_request(*, serial: str, model: str, device_pub: bytes, hw_id: str) -> dict[str, Any]:
    """出厂申请(上装板烧写时出,拿到台式机上用出厂钥匙签)。"""
    s, m, h = _check(serial, model, hw_id)
    if not isinstance(device_pub, bytes) or len(device_pub) != 32:
        raise DeviceIdError("设备公钥要是 32 字节")
    return {"v": VERSION, "serial": s, "model": m, "device_pub": _b64(device_pub), "hw_id": h}


def _signed_bytes(c: dict[str, Any]) -> bytes:
    return _CERT_DOMAIN + "|".join(
        str(c[k]) for k in ("v", "serial", "model", "device_pub", "hw_id", "issued_ms", "key_id")
    ).encode("utf-8")


def sign_device(factory_seed: bytes, request: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """出厂钥匙签一张出厂证书。申请先核一遍(不成形的不签)。"""
    if not isinstance(request, dict):
        raise DeviceIdError("出厂申请要是 JSON 对象")
    s, m, h = _check(request.get("serial"), request.get("model"), request.get("hw_id"))
    pub = _unb64(request.get("device_pub"))
    if len(pub) != 32:
        raise DeviceIdError("出厂申请里的设备公钥不对")
    cert = {"v": VERSION, "serial": s, "model": m, "device_pub": _b64(pub), "hw_id": h,
            "issued_ms": int(now_ms), "key_id": key_id(ed25519.public_key(factory_seed))}
    cert["sig"] = _b64(ed25519.sign(factory_seed, _signed_bytes(cert)))
    return cert


@dataclass(frozen=True)
class DeviceCert:
    serial: str
    model: str
    device_pub: bytes
    hw_id: str
    issued_ms: int
    key_id: str


def verify_cert(cert: Any, factory_pubs: Iterable[bytes], *,
                revoked: Iterable[str] = ()) -> DeviceCert:
    """核出厂证书:成形、是认的出厂钥匙之一签的、那把没作废。对不上抛 :class:`DeviceIdError`。"""
    if not isinstance(cert, dict) or cert.get("v") != VERSION:
        raise DeviceIdError("出厂证书已损坏")
    try:
        s, m, h = _check(cert.get("serial"), cert.get("model"), cert.get("hw_id"))
        issued = cert.get("issued_ms")
        kid = cert.get("key_id")
        if not isinstance(issued, int) or isinstance(issued, bool) or not isinstance(kid, str):
            raise DeviceIdError("出厂证书已损坏")
        pub = _unb64(cert.get("device_pub"))
        sig = _unb64(cert.get("sig"))
    except DeviceIdError as exc:
        raise DeviceIdError("出厂证书已损坏") from exc
    if len(pub) != 32 or len(sig) != 64:
        raise DeviceIdError("出厂证书已损坏")
    bad = set(revoked)
    keys = [k for k in factory_pubs if key_id(k) == kid]
    if not keys:
        raise DeviceIdError("出厂证书不是认的出厂钥匙签的")
    if kid in bad:
        raise DeviceIdError("签这张出厂证书的出厂钥匙已作废")
    if not any(ed25519.verify(k, _signed_bytes(cert), sig) for k in keys):
        raise DeviceIdError("出厂证书签名不对")
    return DeviceCert(s, m, pub, h, issued, kid)


def attest_message(*, purpose: str, site_id: str, nonce: str, serial: str) -> bytes:
    """答题要签的字节:用途(``bind`` 挂申请 / ``claim`` 领证书)、站点编号、站点出的题、序列号。"""
    if _PURPOSE.fullmatch(purpose) is None:
        raise DeviceIdError("用途不对")
    for v in (site_id, nonce, serial):
        if not isinstance(v, str) or not v or "|" in v or len(v) > 200:
            raise DeviceIdError("答题内容不对")
    return _ATTEST_DOMAIN + f"{purpose}|{site_id}|{nonce}|{serial}".encode("utf-8")


def sign_attest(device_seed: bytes, msg: bytes) -> str:
    return _b64(ed25519.sign(device_seed, msg))


def verify_attest(cert: Any, msg: bytes, sig: Any) -> None:
    """答题签名要能用证书里的设备公钥验过(证书本身先用 :func:`verify_cert` 核)。"""
    pub = _unb64(cert.get("device_pub") if isinstance(cert, dict) else None)
    raw = _unb64(sig)
    if len(pub) != 32 or len(raw) != 64 or not ed25519.verify(pub, msg, raw):
        raise DeviceIdError("答题签名不对")
