"""Ed25519(RFC 8032)纯 Python 实现:生成、签名、验签。**只用标准库**(W30 外审 1:狗上是
Ubuntu 20.04 的 OpenSSL 1.1.1,它的 ``pkeyutl`` 不支持 Ed25519;狗上又是离线装包,不加 Python 依赖)。

照 RFC 8032 第 5.1 节和第 6 节的参考算法写,用 RFC 的测试向量和 OpenSSL 3 互相核对(见测试)。
只用在发布包签名上:验签一次几十毫秒,签名在发行方自己的电脑上(不在乎侧信道)。

密钥文件跟 OpenSSL 一样的 PEM:私钥 PKCS#8(``openssl genpkey -algorithm ed25519`` 生成的也认),
公钥 SubjectPublicKeyInfo。
"""

from __future__ import annotations

import base64
import hashlib
import os

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_PRIV_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
_PUB_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(a: tuple, b: tuple) -> tuple:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, pt: tuple) -> tuple:
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(a: tuple, b: tuple) -> bool:
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
assert _GX is not None
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt: tuple) -> bytes:
    zinv = _inv(pt[2])
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes) -> tuple | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _h(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _h(prefix + msg) % _Q
    rs = _compress(_mul(r, _G))
    h = _h(rs + pub + msg) % _Q
    s = (r + h * a) % _Q
    return rs + int.to_bytes(s, 32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    a = _decompress(pub)
    if a is None:
        return False
    rs = sig[:32]
    r = _decompress(rs)
    if r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    h = _h(rs + pub + msg) % _Q
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# ------------------------------------------------------------ PEM(跟 OpenSSL 一样)


def _pem(label: str, der: bytes) -> bytes:
    b = base64.b64encode(der).decode("ascii")
    return (f"-----BEGIN {label}-----\n{b}\n-----END {label}-----\n").encode("ascii")


def _unpem(data: bytes, label: str) -> bytes:
    text = data.decode("ascii", "replace")
    head, tail = f"-----BEGIN {label}-----", f"-----END {label}-----"
    if head not in text or tail not in text:
        raise ValueError(f"不是 {label} 的 PEM")
    body = text.split(head, 1)[1].split(tail, 1)[0]
    return base64.b64decode("".join(body.split()), validate=True)


def new_seed() -> bytes:
    return os.urandom(32)


def private_pem(seed: bytes) -> bytes:
    return _pem("PRIVATE KEY", _PRIV_PREFIX + seed)


def public_pem(pub: bytes) -> bytes:
    return _pem("PUBLIC KEY", _PUB_PREFIX + pub)


def seed_from_pem(data: bytes) -> bytes:
    der = _unpem(data, "PRIVATE KEY")
    if len(der) != 48 or not der.startswith(_PRIV_PREFIX):
        raise ValueError("不是 Ed25519 私钥")
    return der[len(_PRIV_PREFIX):]


def pub_from_pem(data: bytes) -> bytes:
    der = _unpem(data, "PUBLIC KEY")
    if len(der) != 44 or not der.startswith(_PUB_PREFIX):
        raise ValueError("不是 Ed25519 公钥")
    return der[len(_PUB_PREFIX):]
