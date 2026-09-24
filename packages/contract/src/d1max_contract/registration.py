"""设备注册(总设计 §3.6)。W00 只做数据结构与代理侧执行(决定 4):

站点签发一份注册:``site_id``、``robot_id``、凭证指纹、有效期。代理开机装载它,
凭它决定自己的主题命名空间(:class:`~d1max_contract.topics.Topics`)、拒绝不是发给
自己的命令(``matches``)。凭证本身(mTLS 证书)不在这个文件里,只有指纹。
broker 侧的 mTLS 与 ACL 落地归 W00c。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract import SCHEMA
from d1max_contract.errors import ContractError
from d1max_contract.topics import Topics
from d1max_contract.wire import as_int, as_str, check_schema


@dataclass(frozen=True)
class Registration:
    site_id: str
    robot_id: str
    credential_fingerprint: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        Topics(site_id=self.site_id, robot_id=self.robot_id)   # id 的规矩跟主题一致
        if not self.credential_fingerprint:
            raise ContractError("Registration: credential_fingerprint 不许为空")
        if self.expires_at <= self.issued_at:
            raise ContractError("Registration: expires_at 要晚于 issued_at")

    @property
    def topics(self) -> Topics:
        return Topics(site_id=self.site_id, robot_id=self.robot_id)

    def valid_at(self, now_ms: int) -> bool:
        return self.issued_at <= now_ms < self.expires_at

    def matches(self, *, site_id: str, robot_id: str) -> bool:
        return site_id == self.site_id and robot_id == self.robot_id

    def to_wire(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "site_id": self.site_id, "robot_id": self.robot_id,
                "credential_fingerprint": self.credential_fingerprint,
                "issued_at": self.issued_at, "expires_at": self.expires_at}

    @classmethod
    def from_wire(cls, d: Any) -> Registration:
        d = check_schema(d, "Registration")
        return cls(site_id=as_str(d, "site_id", "Registration", nonempty=True),
                   robot_id=as_str(d, "robot_id", "Registration", nonempty=True),
                   credential_fingerprint=as_str(d, "credential_fingerprint", "Registration",
                                                 nonempty=True),
                   issued_at=as_int(d, "issued_at", "Registration"),
                   expires_at=as_int(d, "expires_at", "Registration"))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_wire(), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Registration:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError(f"registration 文件读不了: {path}: {exc}") from exc
        return cls.from_wire(raw)
