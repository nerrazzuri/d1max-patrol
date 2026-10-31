"""App 安装包放在站点上分发(W55,用户 2026-10-31 定):自助客户没有别的渠道拿新版 App。

管理员 / 工程师在网页上传公司签名的安装包(带一份签名清单);App 登录后问站点有没有比自己新的,
有就提示、下载、安装。清单跟机器人升级包同一套签名(``d1max_contract.relsign``):

``{"name": <文件名>, "version": "1.2.0+12", "content_sha256": <文件的 sha256>,
"requires_mission_schema": 1, "signature": ...}``。``+`` 后面的数是构建号,比新旧只看它。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PLATFORMS = ("android", "linux", "windows", "macos")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(apk|tar\.gz|zip|dmg|msix|exe)$")
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}\+(\d{1,9})$")
MAX_BYTES = 300 * 1024 * 1024


class AppDistError(ValueError):
    pass


def build_of(version: str) -> int:
    m = _VERSION.fullmatch(version or "")
    if m is None:
        raise AppDistError("版本要写成 主.次.修+构建号(例如 1.2.0+12)")
    return int(m.group(1))


class AppStore:
    def __init__(self, root: Path | str, *, pubkey: Path | None) -> None:
        """``pubkey`` 为 ``None`` = 不验签(只给开发环境,同站点 ``release_unsigned``)。"""
        self.root = Path(root)
        self.pubkey = pubkey

    def _meta(self, platform: str) -> Path:
        return self.root / platform / "latest.json"

    def latest(self, platform: str) -> dict[str, Any] | None:
        if platform not in PLATFORMS:
            raise AppDistError(f"平台要是 {'/'.join(PLATFORMS)}")
        try:
            m = json.loads(self._meta(platform).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return m if isinstance(m, dict) else None

    def check(self, platform: str, build: int) -> dict[str, Any]:
        m = self.latest(platform)
        if m is None or m["build"] <= build:
            return {"available": False}
        return {"available": True, "version": m["version"], "build": m["build"],
                "size": m["size"], "name": m["name"], "uploaded_ms": m.get("uploaded_ms")}

    def file(self, platform: str) -> tuple[Path, dict[str, Any]]:
        m = self.latest(platform)
        if m is None:
            raise AppDistError("站点上没有这个平台的安装包")
        return self.root / platform / m["name"], m

    def add(self, platform: str, data: bytes, manifest: Any, *, now_ms: int) -> dict[str, Any]:
        if platform not in PLATFORMS:
            raise AppDistError(f"平台要是 {'/'.join(PLATFORMS)}")
        if not isinstance(manifest, dict):
            raise AppDistError("要带签名清单(JSON)")
        name = manifest.get("name")
        if not isinstance(name, str) or not _FILE.fullmatch(name):
            raise AppDistError("清单里的文件名错误")
        version = str(manifest.get("version") or "")
        build = build_of(version)
        if len(data) > MAX_BYTES or not data:
            raise AppDistError("安装包为空或过大")
        if hashlib.sha256(data).hexdigest() != manifest.get("content_sha256"):
            raise AppDistError("安装包与签名清单不匹配(文件被改过或传错了)")
        if self.pubkey is not None:
            from d1max_contract import relsign
            try:
                relsign.verify(manifest, self.pubkey)
            except relsign.SignError as exc:
                raise AppDistError(f"签名不通过,不是公司发布的:{exc}") from exc
        cur = self.latest(platform)
        if cur is not None and cur["build"] >= build:
            raise AppDistError(f"站点上已有构建号 {cur['build']} 的安装包,只收更新的")
        d = self.root / platform
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.part"
        tmp.write_bytes(data)
        os.replace(tmp, d / name)
        meta = {"name": name, "version": version, "build": build, "size": len(data),
                "sha256": manifest["content_sha256"], "uploaded_ms": now_ms}
        tmpm = self._meta(platform).with_suffix(".part")
        tmpm.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmpm, self._meta(platform))
        for old in d.iterdir():                  # 只留最新那一个
            if old.is_file() and old.name not in (name, "latest.json"):
                old.unlink(missing_ok=True)
        return meta
