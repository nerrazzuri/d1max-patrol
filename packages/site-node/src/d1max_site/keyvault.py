"""站点密钥的离线托管与自检(商业化 A3)。

站点上有几把**不在备份里**的密钥(:data:`KEYS`):口令密钥、备份密钥、证据私钥,配了推送的
还有极光的 Master Secret。站点主机坏了,没有它们:备份解不开、库里的口令解不开、狗封着传上来
还没解开的证据再也解不开。以前只能叫人把几个二进制文件一个个拷走保管,容易丢、容易拿混。

- **托管包**(:func:`export`,``d1max-site keys-export``):几把密钥打成**一个文件**,用**口令**
  加密(scrypt 派生 + 站点同一套 AES-256-CTR + HMAC)。存 U 盘、交给客户 IT 保管都行;口令另外记。
- **导回**(:func:`import_`,``d1max-site keys-import``,root):恢复到新主机时一次放回
  ``/etc/d1max-site/``,权限 ``root:d1max-site 0440``。已经有了、而且不一样的,不盖(除非
  ``--force``):盖错了旧备份就解不开。
- **自检**(:func:`status`,``d1max-site keys-status``;体检里也有一项):在不在、长度对不对、
  权限是不是太宽,每把一个短指纹(SHA-256 前 16 位),对照离线那份用。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from d1max_site.sealbox import SealBox, SealError

MAGIC = b"D1KEYS1\n"
#: scrypt 的参数(2^15、8、1:一次派生约 0.1 秒、32 MB 内存)。
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
#: 口令至少这么长。
MIN_PASSPHRASE = 12
#: 站点密钥:名字 → (``site.json`` 里改位置的键, 缺省位置, 定长字节数;``None`` = 不定长的文本)。
KEYS: dict[str, tuple[str, str, int | None]] = {
    "secrets": ("secrets_key", "/etc/d1max-site/secrets.key", 32),
    "backup": ("backup_key", "/etc/d1max-site/backup.key", 32),
    "evidence": ("evidence_key", "/etc/d1max-site/evidence.key", 32),
    "jpush": ("", "/etc/d1max-site/jpush.secret", None),
}
#: 没配就可以没有的(A 阶段外审 M1:配了推送,即 ``site.json`` 的 ``push`` 有 ``app_key``,就必须有)。
OPTIONAL = frozenset({"jpush"})


def _optional(name: str, cfg: dict[str, Any]) -> bool:
    if name == "jpush":
        return not (cfg.get("push") or {}).get("app_key")
    return name in OPTIONAL


class KeyError_(RuntimeError):
    """托管包、密钥文件不对(口令不对、包坏了、要盖掉不一样的密钥)。"""


def paths(cfg: dict[str, Any], *, root: Path | None = None) -> dict[str, Path]:
    """每把密钥在哪儿。``root``:换个目录(测试、导回到别处),只保留文件名。"""
    out = {}
    for name, (cfg_key, default, _n) in KEYS.items():
        p = Path((cfg.get(cfg_key) if cfg_key else None) or default)
        if name == "jpush":
            p = Path((cfg.get("push") or {}).get("secret_file") or default)
        out[name] = (root / p.name) if root is not None else p
    return out


def _fp(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def status(cfg: dict[str, Any], *, root: Path | None = None) -> list[dict[str, Any]]:
    """每把密钥:在不在、长度、权限、短指纹、毛病(空串 = 没毛病)。不读出密钥内容以外的东西。"""
    out = []
    for name, p in paths(cfg, root=root).items():
        row: dict[str, Any] = {"name": name, "path": str(p), "present": p.is_file(),
                               "fingerprint": "", "mode": "", "problem": ""}
        if not p.is_file():
            row["problem"] = "" if _optional(name, cfg) else (
                "没有这把密钥" + (":配了推送,缺它推送恢复不了" if name == "jpush" else ""))
            out.append(row)
            continue
        try:
            raw = p.read_bytes()
            st = p.stat()
        except OSError as exc:
            row["problem"] = f"读不了:{exc}"
            out.append(row)
            continue
        row["fingerprint"] = _fp(raw)
        row["mode"] = oct(stat.S_IMODE(st.st_mode))
        want = KEYS[name][2]
        if want is not None and len(raw) != want:
            row["problem"] = f"长度不对({len(raw)} 字节,要 {want})"
        elif want is None and not raw.strip():
            row["problem"] = "是空的"
        elif st.st_mode & 0o007:
            row["problem"] = f"权限太宽({row['mode']}):别的用户也读得到"
        out.append(row)
    return out


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                          p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)


def export(cfg: dict[str, Any], out: Path, passphrase: str, *,
           root: Path | None = None) -> list[str]:
    """把站点密钥打成一个口令加密的托管包 ``out``(0600)。回打进去的密钥名。缺了必需的就不打。"""
    if len(passphrase) < MIN_PASSPHRASE:
        raise KeyError_(f"口令至少 {MIN_PASSPHRASE} 个字符")
    keys: dict[str, str] = {}
    for row in status(cfg, root=root):
        if row["problem"]:
            raise KeyError_(f"{row['name']}({row['path']}):{row['problem']},先修好再打包")
        if row["present"]:
            keys[row["name"]] = Path(row["path"]).read_bytes().hex()
    salt = os.urandom(16)
    box = SealBox(_derive(passphrase, salt))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="d1max-keys-") as tmp:
            plain, sealed = Path(tmp) / "keys.json", Path(tmp) / "keys.sealed"
            plain.write_text(json.dumps({"v": 1, "keys": keys}), encoding="utf-8")
            box.seal_file(plain, sealed)
            body = sealed.read_bytes()
        tmp_out = out.with_name(out.name + ".part")
        tmp_out.write_bytes(MAGIC + salt + body)
        tmp_out.chmod(0o600)
        tmp_out.replace(out)
    finally:
        os.umask(old)
    return sorted(keys)


def read_bundle(src: Path, passphrase: str) -> dict[str, bytes]:
    """解开托管包:名字 → 密钥字节。口令不对、包坏了抛 :class:`KeyError_`。"""
    raw = Path(src).read_bytes()
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 16:
        raise KeyError_(f"{src} 不是站点密钥托管包")
    salt, body = raw[len(MAGIC):len(MAGIC) + 16], raw[len(MAGIC) + 16:]
    box = SealBox(_derive(passphrase, salt))
    old = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="d1max-keys-") as tmp:
            sealed, plain = Path(tmp) / "keys.sealed", Path(tmp) / "keys.json"
            sealed.write_bytes(body)
            try:
                box.open_file(sealed, plain)
            except SealError as exc:
                raise KeyError_("口令不对,或者托管包被改过") from exc
            d = json.loads(plain.read_text(encoding="utf-8"))
    finally:
        os.umask(old)
    return {k: bytes.fromhex(v) for k, v in d["keys"].items() if k in KEYS}


def import_(cfg: dict[str, Any], src: Path, passphrase: str, *, root: Path | None = None,
            force: bool = False, group: str = "d1max-site") -> list[str]:
    """把托管包里的密钥放回去。已经有、而且不一样的不盖(``force`` 才盖)。回放了哪几把。

    权限 0440;以 root 跑、有 ``group`` 这个组的话属主改成 ``root:<group>``(跟装机脚本一样)。"""
    keys = read_bundle(src, passphrase)
    where = paths(cfg, root=root)
    clash = [n for n, raw in keys.items()
             if where[n].is_file() and where[n].read_bytes() != raw]
    if clash and not force:
        raise KeyError_("这几把已经有了、而且跟托管包里的不一样,不盖(盖错了旧备份就解不开):"
                        + "、".join(clash) + ";确定要盖加 --force")
    done = []
    for name, raw in keys.items():
        p = where[name]
        if p.is_file() and p.read_bytes() == raw:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.part")
        old = os.umask(0o077)
        try:
            tmp.write_bytes(raw)
        finally:
            os.umask(old)
        tmp.chmod(0o440)
        if os.geteuid() == 0:
            try:
                import grp
                os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
            except KeyError:
                pass
        tmp.replace(p)
        done.append(name)
    return done
