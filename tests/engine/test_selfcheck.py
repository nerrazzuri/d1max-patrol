"""升级前那一遍。重到显得过分为止 —— 因为它同时是回滚的判据(§7.2)。"""

from __future__ import annotations

import json
from dataclasses import replace

from d1max_agent.engine.selfcheck import (
    BACKUP_NAG_DAYS,
    MIN_UPGRADE_BATTERY_PCT,
    UPGRADE_FREE_MARGIN_MB,
    PrecheckInputs,
    mission_schema_floor,
    precheck,
)


def _好的输入(**kw) -> PrecheckInputs:
    base = PrecheckInputs(
        package_ok=True,
        package_detail="哈希对得上",
        requires_mission_schema=1,
        mission_schema_floor=1,
        free_mb=8192.0,
        need_mb=512.0,
        battery_pct=88.0,
        engine_running=False,
        lease_active=False,
        payload_recorded=True,
        has_payload=False,
        auto=False,
        backup_age_days=1.0,
    )
    return replace(base, **kw)


def _项(report, name):
    return next(c for c in report.checks if c.name == name)


def test_一切正常时七项全绿(tmp_path):
    report = precheck(_好的输入())
    assert report.ok is True
    assert len(report.checks) == 7
    assert [c.name for c in report.checks] == [
        "package", "schema", "disk", "battery", "busy", "payload", "backup"]


def test_包坏了就拒绝(tmp_path):
    report = precheck(_好的输入(package_ok=False, package_detail="哈希对不上"))
    assert report.ok is False
    assert _项(report, "package").ok is False
    assert "哈希对不上" in _项(report, "package").detail


def test_新版要的schema比盘上的包高就拒绝(tmp_path):
    report = precheck(_好的输入(requires_mission_schema=3, mission_schema_floor=1))
    assert _项(report, "schema").ok is False
    assert report.ok is False


def test_盘上一个任务包都没有时schema这项放行(tmp_path):
    """空目录不该拦升级 —— 没有包就没有读不懂的包。"""
    report = precheck(_好的输入(requires_mission_schema=3, mission_schema_floor=0))
    assert _项(report, "schema").ok is True


def test_装得下新版但保不住回滚位就拒绝(tmp_path):
    """规格的原话:装不下新版 + 保不住回滚位。两份都要算进去。"""
    report = precheck(_好的输入(free_mb=600.0, need_mb=512.0))
    assert _项(report, "disk").ok is False


def test_空闲恰好等于新版加余量就放行(tmp_path):
    """``>=`` 的边界:差一个字节都不该算过,卡在线上算过。"""
    need_mb = 512.0
    report = precheck(_好的输入(free_mb=need_mb + UPGRADE_FREE_MARGIN_MB,
                              need_mb=need_mb))
    assert _项(report, "disk").ok is True


def test_电量不到一半就拒绝(tmp_path):
    report = precheck(_好的输入(battery_pct=MIN_UPGRADE_BATTERY_PCT - 0.1))
    assert _项(report, "battery").ok is False
    assert precheck(_好的输入(battery_pct=MIN_UPGRADE_BATTERY_PCT)).ok is True


def test_有任务在跑就拒绝(tmp_path):
    assert _项(precheck(_好的输入(engine_running=True)), "busy").ok is False


def test_有活跃租约就拒绝(tmp_path):
    assert _项(precheck(_好的输入(lease_active=True)), "busy").ok is False


def test_上装属性没记过就拒绝(tmp_path):
    """选不了重启粒度就不许升。理由见本卷「设计定夺」第七条。"""
    report = precheck(_好的输入(payload_recorded=False))
    assert _项(report, "payload").ok is False
    assert "没记过" in _项(report, "payload").detail


def test_有上装的机器不许自动升(tmp_path):
    """凌晨三点自动升一次、没抢回 SDK 会话,狗就死到天亮(§7.1)。"""
    报 = precheck(_好的输入(has_payload=True, auto=True))
    assert _项(报, "payload").ok is False
    # 同一台机器,人点的就放行。
    assert _项(precheck(_好的输入(has_payload=True, auto=False)), "payload").ok is True


def test_很久没备份只提示不阻断(tmp_path):
    """§7.5 说得很清楚:备份这一项是提示,不是闸。"""
    report = precheck(_好的输入(backup_age_days=99.0))
    assert _项(report, "backup").ok is False
    assert report.ok is True          # 整体照样放行
    assert "backup" not in [c.name for c in report.blocking]


def test_从来没备份过也只提示(tmp_path):
    report = precheck(_好的输入(backup_age_days=None))
    assert _项(report, "backup").ok is False
    assert report.ok is True


def test_备份年龄恰好等于门槛就不提示(tmp_path):
    """``<=`` 的边界:卡在 14 天整算过,不该被当成「很久没备份」。"""
    report = precheck(_好的输入(backup_age_days=BACKUP_NAG_DAYS))
    assert _项(report, "backup").ok is True


def test_盘上任务包的schema取最低的那个(tmp_path):
    d = tmp_path / "missions"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"schema": 3}), encoding="utf-8")
    (d / "b.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
    assert mission_schema_floor(d) == 1


def test_没写schema的任务包当1(tmp_path):
    """第 5 卷才会真加这个字段。在那之前盘上的包全是 1。"""
    d = tmp_path / "missions"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"name": "甲"}), encoding="utf-8")
    assert mission_schema_floor(d) == 1


def test_目录空或不存在时回0(tmp_path):
    assert mission_schema_floor(tmp_path / "没有这个目录") == 0
    (tmp_path / "空").mkdir()
    assert mission_schema_floor(tmp_path / "空") == 0


def test_上线格式全过时blocking是空的(tmp_path):
    wire = precheck(_好的输入()).to_wire()
    assert wire["ok"] is True
    assert wire["blocking"] == []
    assert len(wire["checks"]) == 7
    assert wire["checks"][0] == {"name": "package", "ok": True, "detail": "哈希对得上"}


def test_上线格式列出真正拦住的那几项(tmp_path):
    """``backup`` 不过不该出现在 ``blocking`` 里 —— 它只是提示。"""
    report = precheck(_好的输入(battery_pct=1.0, backup_age_days=99.0))
    wire = report.to_wire()
    assert wire["ok"] is False
    assert wire["blocking"] == ["battery"]
    assert any(c["name"] == "backup" and c["ok"] is False for c in wire["checks"])
