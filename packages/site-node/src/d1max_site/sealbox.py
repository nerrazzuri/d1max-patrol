"""站点上的加密(W30,决策 43):库里的口令、备份。**调系统 ``openssl`` 命令行**,不引 Python 依赖
(同站点自建 CA,W00c 设计决定三 A)。

- 密钥:32 字节随机数,存在一个文件里(``/etc/d1max-site/secrets.key``、``backup.key``,安装脚本生成,
  只有站点服务用户能读;**不跟库放一起、不进备份**)。
- 加密:AES-256-CTR(``openssl enc``,每次随机盐派生这一次的密钥和 IV;口令从环境变量给子进程,
  不上命令行)+ HMAC-SHA256(先加密后算 MAC,整段都算)。加密密钥、MAC 密钥都由主密钥按用途派生。
- 小的值(口令):``enc1:`` + base64。文件(备份):``D1SEAL1\\n`` 开头、密文、最后 32 字节是 MAC。
- 解不开(密钥不对、被改过):抛 :class:`SealError`,绝不回半截明文。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path

PREFIX = "enc1:"
FILE_MAGIC = b"D1SEAL1\n"
KEY_BYTES = 32
_TAG = 32
#: 加密文件比原文多这么多字节:我们的头 + openssl 带口令时的 ``Salted__`` 头和 8 字节盐 + MAC
#: (CTR 不补齐)。备份增量按它换算原文长度(系统审查 S08)。
_OPENSSL_SALT_HEADER = 16
FILE_OVERHEAD = len(FILE_MAGIC) + _OPENSSL_SALT_HEADER + _TAG
_CHUNK = 1 << 20


class SealError(RuntimeError):
    """加解密失败(openssl 不在、密钥不对、内容被改过)。"""


def load_or_create_key(path: Path) -> bytes:
    """读密钥文件;没有就生成一把(0600)。长度不对就报错 —— 绝不悄悄换一把(换了旧的就解不开)。"""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(KEY_BYTES))
    raw = path.read_bytes()
    if len(raw) != KEY_BYTES:
        raise SealError(f"密钥文件 {path} 不是 {KEY_BYTES} 字节")
    return raw


class SealBox:
    def __init__(self, key: bytes, *, openssl: str | None = None) -> None:
        if len(key) != KEY_BYTES:
            raise SealError(f"密钥要 {KEY_BYTES} 字节")
        self._enc = hmac.new(key, b"d1max-seal-enc", hashlib.sha256).hexdigest()
        self._mac = hmac.new(key, b"d1max-seal-mac", hashlib.sha256).digest()
        self._openssl = openssl or shutil.which("openssl") or "openssl"

    # ------------------------------------------------------------ openssl

    def _run(self, args: list[str], data: bytes | None = None, *, src: Path | None = None,
             dst: Path | None = None) -> bytes:
        cmd = [self._openssl, "enc", "-aes-256-ctr", "-pbkdf2", "-iter", "1", "-md", "sha256",
               "-pass", "env:D1MAX_SEAL", *args]
        if src is not None:
            cmd += ["-in", str(src)]
        if dst is not None:
            cmd += ["-out", str(dst)]
        try:
            got = subprocess.run(cmd, input=data, capture_output=True, timeout=600,
                                 env={"D1MAX_SEAL": self._enc, "PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SealError(f"openssl 跑不起来: {exc}") from exc
        if got.returncode != 0:
            raise SealError(f"openssl 失败: {got.stderr.decode(errors='replace')[:200]}")
        return got.stdout

    # ------------------------------------------------------------ 小的值

    def seal(self, text: str) -> str:
        ct = self._run(["-e"], text.encode("utf-8"))
        tag = hmac.new(self._mac, ct, hashlib.sha256).digest()
        return PREFIX + base64.b64encode(ct + tag).decode("ascii")

    def open(self, value: str) -> str:
        if not value.startswith(PREFIX):
            raise SealError("不是加密过的值")
        try:
            raw = base64.b64decode(value[len(PREFIX):], validate=True)
        except ValueError as exc:
            raise SealError("加密值不是 base64") from exc
        ct, tag = raw[:-_TAG], raw[-_TAG:]
        if len(raw) <= _TAG or not hmac.compare_digest(
                tag, hmac.new(self._mac, ct, hashlib.sha256).digest()):
            raise SealError("加密值对不上(密钥不对,或者被改过)")
        return self._run(["-d"], ct).decode("utf-8")

    @staticmethod
    def sealed(value: str) -> bool:
        return value.startswith(PREFIX)

    # ------------------------------------------------------------ 文件

    def seal_file(self, src: Path, dst: Path) -> None:
        """``src`` 加密写到 ``dst``(先写 ``dst.part`` 再换名)。"""
        body = dst.with_name(dst.name + ".part")
        try:
            self._run(["-e"], src=src, dst=body)
            mac = hmac.new(self._mac, FILE_MAGIC, hashlib.sha256)
            with open(body, "rb") as f:
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    mac.update(chunk)
            tmp = dst.with_name(dst.name + ".tmp")
            with open(tmp, "wb") as out, open(body, "rb") as f:
                out.write(FILE_MAGIC)
                shutil.copyfileobj(f, out, _CHUNK)
                out.write(mac.digest())
            os.replace(tmp, dst)
        finally:
            body.unlink(missing_ok=True)

    def verify_file(self, src: Path) -> None:
        """只核 MAC、不解密、不落盘(备份校验用,A2)。对不上抛 :class:`SealError`。"""
        size = src.stat().st_size
        if size < len(FILE_MAGIC) + _TAG:
            raise SealError(f"{src.name} 太短,不是加密备份")
        mac = hmac.new(self._mac, FILE_MAGIC, hashlib.sha256)
        with open(src, "rb") as f:
            if f.read(len(FILE_MAGIC)) != FILE_MAGIC:
                raise SealError(f"{src.name} 不是加密备份")
            left = size - len(FILE_MAGIC) - _TAG
            while left:
                chunk = f.read(min(_CHUNK, left))
                if not chunk:
                    raise SealError(f"{src.name} 读不全")
                mac.update(chunk)
                left -= len(chunk)
            tag = f.read(_TAG)
        if not hmac.compare_digest(tag, mac.digest()):
            raise SealError(f"{src.name} 对不上(密钥不对,或者被改过)")

    def open_file(self, src: Path, dst: Path) -> None:
        """解密 ``src`` 写到 ``dst``。先核 MAC,对不上一个字节都不写。"""
        size = src.stat().st_size
        if size < len(FILE_MAGIC) + _TAG:
            raise SealError(f"{src.name} 太短,不是加密备份")
        mac = hmac.new(self._mac, FILE_MAGIC, hashlib.sha256)
        body = dst.with_name(dst.name + ".part")
        try:
            with open(src, "rb") as f, open(body, "wb") as out:
                if f.read(len(FILE_MAGIC)) != FILE_MAGIC:
                    raise SealError(f"{src.name} 不是加密备份")
                left = size - len(FILE_MAGIC) - _TAG
                while left:
                    chunk = f.read(min(_CHUNK, left))
                    if not chunk:
                        raise SealError(f"{src.name} 读不全")
                    mac.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
                tag = f.read(_TAG)
            if not hmac.compare_digest(tag, mac.digest()):
                raise SealError(f"{src.name} 对不上(密钥不对,或者被改过)")
            self._run(["-d"], src=body, dst=dst)
        finally:
            body.unlink(missing_ok=True)


# ------------------------------------------------------------ 站点库里的口令(W30)

#: 安装脚本放密钥的地方(站点服务对 /etc 只读:只读不建)。``site.json`` 的 ``secrets_key``、
#: ``backup_key`` 可以改。
DEFAULT_KEYS = {"secrets": Path("/etc/d1max-site/secrets.key"),
                "backup": Path("/etc/d1max-site/backup.key")}


def site_box(cfg: dict, which: str) -> SealBox | None:
    """站点配的那把密钥(``secrets`` / ``backup``);没有就是 ``None``(不加密,调用方记日志)。"""
    import logging
    path = Path(cfg.get(f"{which}_key") or DEFAULT_KEYS[which])
    if not path.is_file():
        logging.getLogger(__name__).warning("没有 %s 密钥 %s:%s不加密(装机脚本会生成)",
                                            which, path, "口令" if which == "secrets" else "备份")
        return None
    return SealBox(load_or_create_key(path))


_opened: dict[str, str] = {}


def seal_value(db, value: str) -> str:
    """要落库的口令:库配了密钥(``db.sealbox``)就加密;空串、已经加密过的原样。"""
    box = getattr(db, "sealbox", None)
    if box is None or not value or value.startswith(PREFIX):
        return value
    return box.seal(value)


def open_value(db, value: str) -> str:
    """库里读出来的口令:加密过的解开(记在内存里,同一个值不再起 openssl);老的明文原样。"""
    if not value.startswith(PREFIX):
        return value
    if value in _opened:
        return _opened[value]
    box = getattr(db, "sealbox", None)
    if box is None:
        raise SealError("库里的口令加密过,站点没配密钥(secrets_key)")
    try:
        plain = box.open(value)
    except SealError as exc:
        raise SealError(f"{exc}:/etc/d1max-site/secrets.key 不是加密时的那一把"
                        "(换新主机恢复时要把原来的 secrets.key 放回去)") from exc
    if len(_opened) > 1000:
        _opened.clear()
    _opened[value] = plain
    return plain


def migrate_plaintext(db) -> int:
    """老库里的明文口令(摄像头口令、事件源密钥)就地加密。站点起来时调。回改了几条。"""
    if getattr(db, "sealbox", None) is None:
        return 0
    n = 0
    with db.tx() as c:
        for table, key, col in (("cameras", "name", "password"),
                                ("incident_sources", "name", "secret")):
            for r in c.execute(f"SELECT {key} AS k, {col} AS v FROM {table}").fetchall():
                if r["v"] and not r["v"].startswith(PREFIX):
                    c.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?",
                              (seal_value(db, r["v"]), r["k"]))
                    n += 1
    return n
