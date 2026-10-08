"""狗上的证据加密(W30b,决策 51):照片、截图、录像落到狗的发件箱之前用**站点的公钥**封上,
狗自己解不开(狗被偷了,盘上的图像也看不了);站点收齐之后用私钥解开。

- 密钥:X25519(RFC 7748,这里纯 Python 实现:狗上 OpenSSL 1.1.1、不加 Python 依赖,同 W30 的签名)。
  站点私钥 32 字节存 ``/etc/d1max-site/evidence.key``(0600,不进备份);公钥十六进制文本,
  给狗放 ``/etc/d1max/evidence-pub.key``。
- 每个文件一对临时密钥:``共享 = X25519(临时私钥, 站点公钥)``,``HKDF-SHA256`` 派生加密口令和 MAC 密钥
  (盐是两把公钥)。
- 加密同站点 :mod:`d1max_site.sealbox`:``openssl enc`` AES-256-CTR(口令从环境变量给,不上命令行)
  + HMAC-SHA256(先加密后算 MAC,整段都算)。
- 文件格式:``MAGIC`` + 临时公钥(32)+ openssl 密文 + MAC(32)。文件名在原名后面加 :data:`SUFFIX`。
- 解不开(不是这把私钥、被改过):抛 :class:`SealError`,一个字节的明文都不写。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path

MAGIC = b"D1EV1\n"
SUFFIX = ".d1e"
KEY_BYTES = 32
_TAG = 32
_CHUNK = 1 << 20
_P = 2**255 - 19
_A24 = 121665


class SealError(RuntimeError):
    """封、拆失败(openssl 不在、不是这把私钥、内容被改过)。"""


# ------------------------------------------------------------ X25519(RFC 7748)


def _clamp(k: bytes) -> int:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def x25519(k: bytes, u: bytes) -> bytes:
    """标量乘(蒙哥马利梯子,RFC 7748 §5)。``k``、``u`` 都是 32 字节小端。"""
    if len(k) != KEY_BYTES or len(u) != KEY_BYTES:
        raise SealError("X25519 的标量、点都要 32 字节")
    n = _clamp(k)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (n >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE = (9).to_bytes(32, "little")


def public_key(priv: bytes) -> bytes:
    return x25519(priv, _BASE)


def keypair() -> tuple[bytes, bytes]:
    priv = os.urandom(KEY_BYTES)
    return priv, public_key(priv)


def _hkdf(ikm: bytes, salt: bytes, info: bytes, n: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


def _keys(shared: bytes, eph_pub: bytes, site_pub: bytes) -> tuple[str, bytes]:
    if shared == bytes(32):
        raise SealError("公钥不对(共享密钥是零)")
    okm = _hkdf(shared, eph_pub + site_pub, b"d1max-evidence-v1", 64)
    return okm[:32].hex(), okm[32:]


# ------------------------------------------------------------ 公钥文件


def load_public(path: Path | str) -> bytes:
    """十六进制文本的公钥(64 个字符,允许前后空白)。"""
    try:
        raw = bytes.fromhex(Path(path).read_text("ascii").strip())
    except (OSError, ValueError) as exc:
        raise SealError(f"证据公钥 {path} 读不了: {exc}") from exc
    if len(raw) != KEY_BYTES:
        raise SealError(f"证据公钥 {path} 不是 {KEY_BYTES} 字节")
    return raw


def load_private(path: Path | str) -> bytes:
    raw = Path(path).read_bytes()
    if len(raw) != KEY_BYTES:
        raise SealError(f"证据私钥 {path} 不是 {KEY_BYTES} 字节")
    return raw


def write_keypair(priv_path: Path | str, pub_path: Path | str) -> bytes:
    """生成一对,私钥 0600(已经有就不动、报错:换了旧证据就解不开)。回公钥。"""
    priv_path, pub_path = Path(priv_path), Path(pub_path)
    if priv_path.exists():
        raise SealError(f"{priv_path} 已经有了:不换(换了以前封的证据就解不开)")
    priv, pub = keypair()
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_text(pub.hex() + "\n", encoding="ascii")
    return pub


# ------------------------------------------------------------ openssl


def _openssl(args: list[str], *, secret: str, data: bytes | None = None,
             src: Path | None = None, dst: Path | None = None,
             openssl: str | None = None) -> bytes:
    cmd = [openssl or shutil.which("openssl") or "openssl", "enc", "-aes-256-ctr", "-pbkdf2",
           "-iter", "1", "-md", "sha256", "-pass", "env:D1MAX_EVSEAL", *args]
    if src is not None:
        cmd += ["-in", str(src)]
    if dst is not None:
        cmd += ["-out", str(dst)]
    try:
        got = subprocess.run(cmd, input=data, capture_output=True, timeout=600,
                             env={"D1MAX_EVSEAL": secret, "PATH": os.environ.get("PATH", "")})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SealError(f"openssl 跑不起来: {exc}") from exc
    if got.returncode != 0:
        raise SealError(f"openssl 失败: {got.stderr.decode(errors='replace')[:200]}")
    return got.stdout


def _hidden(dst: Path, ext: str) -> Path:
    """封到一半的临时文件:点开头(狗的发件箱扫盘跳过点开头的,不会把半截的排进上传队列)。"""
    return dst.with_name(f".{dst.name}{ext}")


class Sealer:
    """狗上:用站点公钥封文件。"""

    def __init__(self, site_pub: bytes, *, openssl: str | None = None) -> None:
        if len(site_pub) != KEY_BYTES:
            raise SealError("站点公钥要 32 字节")
        self.site_pub = site_pub
        self._openssl = openssl

    def _begin(self) -> tuple[bytes, str, bytes]:
        eph, eph_pub = keypair()
        secret, mac_key = _keys(x25519(eph, self.site_pub), eph_pub, self.site_pub)
        return eph_pub, secret, mac_key

    def _write(self, dst: Path, eph_pub: bytes, mac_key: bytes, body: Path) -> None:
        mac = hmac.new(mac_key, MAGIC + eph_pub, hashlib.sha256)
        with open(body, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                mac.update(chunk)
        tmp = _hidden(dst, ".tmp")
        with open(tmp, "wb") as out, open(body, "rb") as f:
            out.write(MAGIC + eph_pub)
            shutil.copyfileobj(f, out, _CHUNK)
            out.write(mac.digest())
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)

    def seal_bytes(self, data: bytes, dst: Path) -> None:
        """内存里的 ``data`` 封进 ``dst``(先写 ``.part``、``.tmp``,换名才算数;明文不落盘)。"""
        dst = Path(dst)
        eph_pub, secret, mac_key = self._begin()
        body = _hidden(dst, ".part")
        try:
            _openssl(["-e"], secret=secret, data=data, dst=body, openssl=self._openssl)
            self._write(dst, eph_pub, mac_key, body)
        finally:
            body.unlink(missing_ok=True)

    def seal_file(self, src: Path, dst: Path) -> None:
        """``src`` 封进 ``dst``(``src`` 不动,调用方确认 ``dst`` 写成了再删)。"""
        dst = Path(dst)
        eph_pub, secret, mac_key = self._begin()
        body = _hidden(dst, ".part")
        try:
            _openssl(["-e"], secret=secret, src=Path(src), dst=body, openssl=self._openssl)
            self._write(dst, eph_pub, mac_key, body)
        finally:
            body.unlink(missing_ok=True)


def open_file(src: Path, dst: Path, site_priv: bytes, *, openssl: str | None = None) -> None:
    """站点上:拆开 ``src`` 写到 ``dst``。先核 MAC,对不上一个字节都不写。"""
    src, dst = Path(src), Path(dst)
    size = src.stat().st_size
    head = len(MAGIC) + KEY_BYTES
    if size < head + _TAG:
        raise SealError(f"{src.name} 太短,不是封好的证据")
    with open(src, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise SealError(f"{src.name} 不是封好的证据")
        eph_pub = f.read(KEY_BYTES)
    site_pub = public_key(site_priv)
    secret, mac_key = _keys(x25519(site_priv, eph_pub), eph_pub, site_pub)
    mac = hmac.new(mac_key, MAGIC + eph_pub, hashlib.sha256)
    body = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as f, open(body, "wb") as out:
            f.seek(head)
            left = size - head - _TAG
            while left:
                chunk = f.read(min(_CHUNK, left))
                if not chunk:
                    raise SealError(f"{src.name} 读不全")
                mac.update(chunk)
                out.write(chunk)
                left -= len(chunk)
            tag = f.read(_TAG)
        if not hmac.compare_digest(tag, mac.digest()):
            raise SealError(f"{src.name} 对不上(不是这把私钥,或者被改过)")
        tmp = dst.with_name(dst.name + ".tmp")
        _openssl(["-d"], secret=secret, src=body, dst=tmp, openssl=openssl)
        os.replace(tmp, dst)
    finally:
        body.unlink(missing_ok=True)
        dst.with_name(dst.name + ".tmp").unlink(missing_ok=True)


def sealed_name(name: str) -> str:
    return name + SUFFIX


def plain_name(name: str) -> str | None:
    """``x.jpg.d1e`` → ``x.jpg``;不是封好的名字回 ``None``。"""
    return name[: -len(SUFFIX)] if name.endswith(SUFFIX) and len(name) > len(SUFFIX) else None
