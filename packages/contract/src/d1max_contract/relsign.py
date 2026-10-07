"""发布包签名(W30,决策 43):发行方私钥签、狗上固化公钥验。**调系统 ``openssl``**(Ed25519,
``pkeyutl -rawin``,OpenSSL ≥ 1.1.1),不引 Python 依赖。

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
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

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


def _openssl(args: list[str], *, openssl: str | None = None) -> subprocess.CompletedProcess:
    exe = openssl or shutil.which("openssl") or "openssl"
    try:
        return subprocess.run([exe, *args], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SignError(f"openssl 跑不起来: {exc}") from exc


def keygen(private: Path, public: Path) -> None:
    """生成一对 Ed25519 密钥(私钥 0600)。私钥已经在就拒(不许盖掉)。"""
    if private.exists():
        raise SignError(f"{private} 已经有了,不盖")
    private.parent.mkdir(parents=True, exist_ok=True)
    got = _openssl(["genpkey", "-algorithm", "ed25519", "-out", str(private)])
    if got.returncode != 0:
        raise SignError(f"生成私钥失败: {got.stderr.decode(errors='replace')[:200]}")
    private.chmod(0o600)
    got = _openssl(["pkey", "-in", str(private), "-pubout", "-out", str(public)])
    if got.returncode != 0:
        raise SignError(f"导出公钥失败: {got.stderr.decode(errors='replace')[:200]}")


def sign(manifest: dict[str, Any], private: Path) -> str:
    with tempfile.TemporaryDirectory() as d:
        msg = Path(d) / "msg"
        msg.write_bytes(message(manifest))
        out = Path(d) / "sig"
        got = _openssl(["pkeyutl", "-sign", "-rawin", "-inkey", str(private), "-in", str(msg),
                        "-out", str(out)])
        if got.returncode != 0:
            raise SignError(f"签名失败: {got.stderr.decode(errors='replace')[:200]}")
        return base64.b64encode(out.read_bytes()).decode("ascii")


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
    with tempfile.TemporaryDirectory() as d:
        msg, sf = Path(d) / "msg", Path(d) / "sig"
        msg.write_bytes(message(manifest))
        sf.write_bytes(raw)
        got = _openssl(["pkeyutl", "-verify", "-pubin", "-inkey", str(public), "-rawin",
                        "-in", str(msg), "-sigfile", str(sf)])
    if got.returncode != 0:
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
