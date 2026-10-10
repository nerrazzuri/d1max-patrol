"""告警搁置(B1,ISA-18.2 shelving):只有 P2;要人、要原因;最多 12 小时;到点自动回来;写库、重启还在。"""

from __future__ import annotations

import pytest
from test_site_api import 站

from d1max_site.alert_store import AlertDesk
from d1max_site.alerts import MAX_SHELVE_MS
from d1max_site.db import SiteDB

NOW = 1_800_000_000_000


def _台(tmp_path, clock):
    return AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: clock[0])


def test_只有P2能搁置_要原因_有上限_到点自动回来_重启还在(tmp_path):
    clock = [NOW]
    desk = _台(tmp_path, clock)
    p1 = desk.raise_alert(kind="estop_pressed", robot="A", title="急停")
    p2 = desk.raise_alert(kind="cctv_offline", robot="cctv:west", title="掉线")
    with pytest.raises(ValueError, match="只有 P2"):
        desk.shelve(p1.key, who="wang", until_ms=NOW + 60_000, reason="修")
    with pytest.raises(ValueError, match="原因"):
        desk.shelve(p2.key, who="wang", until_ms=NOW + 60_000, reason="  ")
    with pytest.raises(ValueError, match="小时以内"):
        desk.shelve(p2.key, who="wang", until_ms=NOW + MAX_SHELVE_MS + 1, reason="修")
    with pytest.raises(ValueError, match="小时以内"):
        desk.shelve(p2.key, who="wang", until_ms=NOW, reason="修")
    a = desk.shelve(p2.key, who="wang", until_ms=NOW + 3600_000, reason="摄像头在修")
    assert a.shelved_at(NOW) and a.shelved_by == "wang" and a.shelved_reason == "摄像头在修"
    again = desk.raise_alert(kind="cctv_offline", robot="cctv:west", title="掉线")
    assert again.key == p2.key and again.count == 2 and again.shelved_at(NOW), "再触发不打断搁置"
    clock[0] = NOW + 3600_000
    assert not again.shelved_at(clock[0]), "到点自动回来"
    desk2 = _台(tmp_path, clock)                         # 重启:搁置记录在库里
    got = {x["key"]: x for x in desk2.open()}
    assert got[p2.key]["shelved_until_ms"] == NOW + 3600_000
    assert got[p2.key]["shelved_reason"] == "摄像头在修"
    u = desk2.unshelve(p2.key)
    assert not u.shelved_at(clock[0])
    desk2.resolve(p2.key, who="wang")
    with pytest.raises(ValueError, match="解决"):
        desk2.shelve(p2.key, who="wang", until_ms=clock[0] + 60_000, reason="x")


def test_接口_搁置要权限_坏参数400_P1拒(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        tok = s.login()
        p2 = s.desk.raise_alert(kind="cctv_offline", robot="cctv:west", title="掉线")
        p1 = s.desk.raise_alert(kind="estop_pressed", robot="A", title="急停")
        from urllib.parse import quote
        k2, k1 = quote(p2.key, safe=""), quote(p1.key, safe="")
        now = s.desk._now()
        code, d = s.req("POST", f"/api/alerts/{k2}/shelve",
                        {"until_ms": now + 3600_000, "reason": "在修"}, token=tok)
        assert code == 200 and d["alert"]["shelved_reason"] == "在修"
        assert s.req("POST", f"/api/alerts/{k2}/shelve", {"reason": "x"}, token=tok)[0] == 400
        code, d = s.req("POST", f"/api/alerts/{k1}/shelve",
                        {"until_ms": now + 60_000, "reason": "x"}, token=tok)
        assert code == 400 and "P1" in d["error"]
        assert s.req("POST", f"/api/alerts/{k2}/unshelve", {}, token=tok)[0] == 200
        assert s.req("POST", f"/api/alerts/{k2}/shelve", {"until_ms": now + 1, "reason": "x"})[0] \
            == 401
    finally:
        s.close()
