"""这台狗有没有装上装。它决定升级怎么重启,而且它是会变的(§7.1)。"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.app.identity import (
    CONFIRM_PHRASE,
    Payload,
    read_payload,
    resolve,
    write_payload,
)

NOW = 1_700_000_000_000


def test_没记过的时候说没记过而不是说没有(tmp_path):
    """「没装上装」和「没人记过」是两回事。混成一个,升级就会照着猜的走。"""
    got = read_payload(tmp_path / "还没有这个文件.json")
    assert got == Payload()
    assert got.recorded is False and got.has is False


def test_记下之后读得回来(tmp_path):
    path = tmp_path / "payload.json"
    write_payload(has=True, by="装机-老王", now_ms=NOW, path=path,
                  confirm=CONFIRM_PHRASE)
    got = read_payload(path)
    assert got.has is True and got.recorded is True
    assert got.at_ms == NOW and got.by == "装机-老王"


def test_记下没装上装也算记过(tmp_path):
    """交付默认形态就是没上装。这一条必须能被显式记下来,否则装机永远缺一项。"""
    path = tmp_path / "payload.json"
    write_payload(has=False, by="装机-老王", now_ms=NOW, path=path,
                  confirm=CONFIRM_PHRASE)
    got = read_payload(path)
    assert got.has is False and got.recorded is True


def test_不带确认语就不许改(tmp_path):
    """这一改会静默地改掉升级的安全模型(§7.1 纪律 2)。"""
    path = tmp_path / "payload.json"
    with pytest.raises(ValueError, match="确认"):
        write_payload(has=True, by="谁", now_ms=NOW, path=path, confirm="嗯")
    assert not path.exists()


def test_确认语错一个字也不许改(tmp_path):
    path = tmp_path / "payload.json"
    with pytest.raises(ValueError):
        write_payload(has=True, by="谁", now_ms=NOW, path=path,
                      confirm=CONFIRM_PHRASE + "。")
    assert not path.exists()


def test_文件坏了当没记过(tmp_path):
    """读不动不等于「没装」 —— 那会让一台装了上装的机器走上只重启服务那条路。"""
    path = tmp_path / "payload.json"
    path.write_text("{半个", encoding="utf-8")
    assert read_payload(path) == Payload()


def test_改过一次还能再改回来(tmp_path):
    """客户拆掉上装的那天也要能记。单向的开关迟早对不上现实。"""
    path = tmp_path / "payload.json"
    write_payload(has=True, by="甲", now_ms=NOW, path=path, confirm=CONFIRM_PHRASE)
    write_payload(has=False, by="乙", now_ms=NOW + 9, path=path,
                  confirm=CONFIRM_PHRASE)
    got = read_payload(path)
    assert got.has is False and got.by == "乙" and got.at_ms == NOW + 9


def test_身份把上装属性带出去(tmp_path):
    path = tmp_path / "payload.json"
    write_payload(has=True, by="装机-老王", now_ms=NOW, path=path,
                  confirm=CONFIRM_PHRASE)
    ident = resolve(sn="D1M-0007", files=(), net_root=tmp_path / "没有网卡",
                    payload_file=path, host="h")
    wire = ident.to_wire()
    assert wire["has_payload"] is True
    assert wire["payload_recorded"] is True
    assert wire["payload_by"] == "装机-老王"


def test_没记过的机器身份里也说得清楚(tmp_path):
    ident = resolve(sn="D1M-0007", files=(), net_root=tmp_path / "没有网卡",
                    payload_file=tmp_path / "没有.json", host="h")
    wire = ident.to_wire()
    assert wire["has_payload"] is False
    assert wire["payload_recorded"] is False


def test_落盘的是合法json而且带时刻(tmp_path):
    path = tmp_path / "payload.json"
    write_payload(has=True, by="装机-老王", now_ms=NOW, path=path,
                  confirm=CONFIRM_PHRASE)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["has_payload"] is True and raw["at_ms"] == NOW
    assert raw["by"] == "装机-老王"


def test_从环境变量读路径(tmp_path, monkeypatch):
    """没有 override 时, payload_path() 从环境变量 D1MAX_PAYLOAD_FILE 读路径。"""
    from d1max_patrol.app.identity import PAYLOAD_ENV, payload_path

    env_path = tmp_path / "somewhere.json"
    monkeypatch.setenv(PAYLOAD_ENV, str(env_path))
    got = payload_path()
    assert got == env_path

    # override 仍然优先于环境变量
    override_path = tmp_path / "override.json"
    got_override = payload_path(override=override_path)
    assert got_override == override_path


def test_环境变量缺失时使用默认路径(tmp_path, monkeypatch):
    """没有 override 也没有环境变量时, payload_path() 返回默认的 PAYLOAD_FILE。"""
    from d1max_patrol.app.identity import PAYLOAD_ENV, PAYLOAD_FILE, payload_path

    monkeypatch.delenv(PAYLOAD_ENV, raising=False)
    got = payload_path()
    assert got == PAYLOAD_FILE
