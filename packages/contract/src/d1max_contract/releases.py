"""发布经站点的契约(W00c5d 第三部分,决策 8:版本也由站点管)。

- 站点上登记一版(发布包目录打成 tar.gz,带大小与 sha256)。
- 命令 ``release_install {name, sha256, size}``:狗从站点的狗专用口下载、核对、解开、核对包内指纹、
  落槽、建 venv —— **只落槽,不切**。做完发 ``release_installed`` / ``release_install_failed``。
- 命令 ``release_activate {name}``:空闲时才切(装单元 → 写在途标记 → 换链 → 重启代理);重启后
  连上站点才提交,起不来开机守卫数够次数就退回上一版。
- 命令 ``release_rollback {}``:退回上一版(要有在途的那一次升级)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 版本名:日期 + 一截内容哈希(跟狗上 ``engine.release`` 的规矩一样,也是防路径穿越的闸)。
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6,12}$")
RELEASE_KINDS = frozenset({"release_install", "release_activate", "release_rollback"})
#: 一个发布包最大多少字节。
MAX_PACKAGE_BYTES = 2 * 1024 ** 3
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def check_release_name(v: Any) -> str:
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise ContractError(f"版本名要形如 2026-09-20-77b2de:{v!r}")
    return v


@dataclass(frozen=True)
class ReleaseRef:
    name: str
    sha256: str
    size: int

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_wire(cls, d: Any) -> ReleaseRef:
        if not isinstance(d, dict):
            raise ContractError("release: 要是对象")
        size, sha = d.get("size"), d.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or \
                not 0 < size <= MAX_PACKAGE_BYTES:
            raise ContractError(f"release: size 要是 1–{MAX_PACKAGE_BYTES} 的整数")
        if not isinstance(sha, str) or not _HEX64.match(sha):
            raise ContractError("release: sha256 要是 64 位小写十六进制")
        return cls(name=check_release_name(d.get("name")), sha256=sha, size=size)
