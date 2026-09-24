"""设备注册的数据结构(W00 决定 4:只做数据与代理侧执行,broker 侧归 W00c)。"""

from __future__ import annotations

import json

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.registration import Registration

REG = Registration(site_id="penang-1", robot_id="D1MAX-C40011",
                   credential_fingerprint="sha256:abcd", issued_at=1_000, expires_at=2_000)


def test_往返文件(tmp_path):
    p = tmp_path / "registration.json"
    REG.save(p)
    assert Registration.load(p) == REG
    assert json.loads(p.read_text(encoding="utf-8"))["schema"] == "1.0"


def test_有效期判定():
    assert REG.valid_at(1_000) and REG.valid_at(1_999)
    assert not REG.valid_at(2_000) and not REG.valid_at(999)


def test_指纹不许为空_id走Topics的规矩():
    with pytest.raises(ContractError, match="credential_fingerprint"):
        Registration(site_id="s", robot_id="r", credential_fingerprint="",
                     issued_at=1, expires_at=2)
    with pytest.raises(ContractError):
        Registration(site_id="a/b", robot_id="r", credential_fingerprint="f",
                     issued_at=1, expires_at=2)


def test_匹配命令来源():
    assert REG.matches(site_id="penang-1", robot_id="D1MAX-C40011")
    assert not REG.matches(site_id="penang-2", robot_id="D1MAX-C40011")
    assert not REG.matches(site_id="penang-1", robot_id="D1MAX-OTHER")


def test_文件坏了要说清楚(tmp_path):
    p = tmp_path / "registration.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="registration"):
        Registration.load(p)
