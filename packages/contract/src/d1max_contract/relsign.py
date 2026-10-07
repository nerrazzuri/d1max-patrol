"""发布包签名(W30,决策 43):发行方私钥签、狗上固化公钥验。Ed25519,**纯 Python**
(:mod:`d1max_contract.ed25519`,只用标准库;W30 外审 1:狗上 OpenSSL 1.1.1 的 ``pkeyutl`` 不支持
Ed25519,原来调 ``openssl`` 的做法在狗上合法的包也验不过)。密钥文件是跟 OpenSSL 一样的 PEM。

签的是 ``release.json`` 里这几项的规范 JSON:``name``、``version``、``content_sha256``
(整棵包的指纹,不含 ``release.json`` 本身)、``requires_mission_schema``。签名(base64)写回
``release.json`` 的 ``signature`` —— ``release.json`` 不算进包的指纹,写回去不改指纹;
包里任何一个字节、或者这几项任何一项变了,签名就对不上。

- 私钥:``release keygen`` 生成,**只在发行方手里、离线保管,不进仓库、不上狗、不上站点**。
- 公钥:``/etc/d1max/release-pub.pem``(装机脚本放),狗上验;站点配了也先验一遍(登记时就拒)。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from d1max_contract import ed25519

SIGNED_FIELDS = ("name", "version", "content_sha256", "requires_mission_schema")
DEFAULT_PUBKEY = Path("/etc/d1max/release-pub.pem")


class SignError(RuntimeError):
    """签不了、验不过(没有签名、签名不对、openssl 不在)。"""


def message(manifest: dict[str, Any]) -> bytes:
    """要签的那段字节:签名覆盖的几项的规范 JSON(键排序、没有空白)。"""
    body = {k: manifest.get(k) for k in SIGNED_FIELDS}
    body["requires_mission_schema"] = body["requires_mission_schema"] or 1
    return b"d1max-release\n" + json.dumps(body, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8")


def keygen(private: Path, public: Path) -> None:
    """生成一对 Ed25519 密钥(私钥 0600,PEM 跟 OpenSSL 一样)。私钥已经在就拒(不许盖掉)。"""
    import os
    if private.exists():
        raise SignError(f"{private} 已经有了,不盖")
    private.parent.mkdir(parents=True, exist_ok=True)
    seed = ed25519.new_seed()
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(ed25519.private_pem(seed))
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(ed25519.public_pem(ed25519.public_key(seed)))


def sign(manifest: dict[str, Any], private: Path) -> str:
    try:
        seed = ed25519.seed_from_pem(Path(private).read_bytes())
    except (OSError, ValueError) as exc:
        raise SignError(f"读不了私钥 {private}: {exc}") from exc
    return base64.b64encode(ed25519.sign(seed, message(manifest))).decode("ascii")


def verify(manifest: dict[str, Any], public: Path) -> None:
    """验 ``manifest["signature"]``。对就返回,不对抛 :class:`SignError`。"""
    sig = manifest.get("signature")
    if not isinstance(sig, str) or not sig:
        raise SignError("包没有签名")
    try:
        raw = base64.b64decode(sig, validate=True)
    except ValueError as exc:
        raise SignError("签名不是 base64") from exc
    if not Path(public).is_file():
        raise SignError(f"没有发行公钥 {public}")
    try:
        pub = ed25519.pub_from_pem(Path(public).read_bytes())
    except (OSError, ValueError) as exc:
        raise SignError(f"发行公钥读不了: {exc}") from exc
    if not ed25519.verify(pub, message(manifest), raw):
        raise SignError("签名对不上(不是我们发行的,或者包被改过)")


def check_package(pkg: Path, public: Path | None, *, allow_unsigned: bool = False) -> str:
    """装之前验一个包目录(``release.json`` 在里面):空串 = 可以装,否则是为什么不能装。
    ``public`` 不在:``allow_unsigned`` 才放行(开发、仿真);在了就一定要验过。"""
    try:
        manifest = json.loads((Path(pkg) / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"读不了 release.json: {exc}"
    if public is None or not Path(public).is_file():
        return "" if allow_unsigned else f"狗上没装发行公钥({public or DEFAULT_PUBKEY}),不装"
    try:
        verify(manifest, public)
    except SignError as exc:
        return str(exc)
    return ""
