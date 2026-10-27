"""W44 第二期:站点用哪个时区、值班设置(交班时间、多久没巡过)。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_site_api import PW, 站

from d1max_site.db import SiteDB
from d1max_site.site_settings import SettingsError, SiteSettings
from d1max_site.sitetime import day_text, local_day_start, site_tz

KL = ZoneInfo("Asia/Kuala_Lumpur")


def _设时区(db, tz):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO route_store(id, timezone) VALUES (1, ?)", (tz,))


def test_站点时区_路线库设了用它(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    _设时区(db, "Asia/Kuala_Lumpur")
    assert site_tz(db) == KL


def test_站点时区_没设用本地(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    local = datetime.now().astimezone().tzinfo
    assert site_tz(db).utcoffset(datetime.now()) == local.utcoffset(datetime.now())
    _设时区(db, None)
    assert site_tz(db).utcoffset(datetime.now()) == local.utcoffset(datetime.now())


def test_站点时区_认不出来用本地(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    _设时区(db, "Mars/Base")
    local = datetime.now().astimezone().tzinfo
    assert site_tz(db).utcoffset(datetime.now()) == local.utcoffset(datetime.now())


def test_一天的开始():
    t = int(datetime(2026, 10, 27, 3, 0, tzinfo=KL).timestamp() * 1000)
    assert local_day_start(t, KL) == int(datetime(2026, 10, 27, tzinfo=KL).timestamp() * 1000)
    assert day_text(t, KL) == "2026-10-27"
    # 吉隆坡 2026-10-27 03:00 在 UTC 还是 26 号
    assert day_text(t, ZoneInfo("UTC")) == "2026-10-26"


def test_值班设置_默认(tmp_path):
    assert SiteSettings(SiteDB(tmp_path / "s.db")).duty() == {"shift_hours": [7, 19],
                                                              "stale_hours": 2}


def test_值班设置_存了读回来(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    SiteSettings(db).set_duty({"shift_hours": [20, 8], "stale_hours": 3}, by="alice")
    assert SiteSettings(db).duty() == {"shift_hours": [8, 20], "stale_hours": 3}
    # 只给一样,另一样不动
    SiteSettings(db).set_duty({"stale_hours": 4}, by="alice")
    assert SiteSettings(db).duty() == {"shift_hours": [8, 20], "stale_hours": 4}


@pytest.mark.parametrize("bad", [{"shift_hours": [7, 7]}, {"shift_hours": [7, 24]},
                                 {"shift_hours": [7]}, {"shift_hours": "7,19"},
                                 {"shift_hours": [7.5, 19]}, {"stale_hours": 0},
                                 {"stale_hours": 49}, {"stale_hours": True}])
def test_值班设置_不合规矩(tmp_path, bad):
    with pytest.raises(SettingsError):
        SiteSettings(SiteDB(tmp_path / "s.db")).set_duty(bad, by="alice")


def test_值班设置_接口权限(tmp_path):
    s = 站(tmp_path)
    try:
        s.accounts.add("gina", PW, role="guard")
        gina = s.req("POST", "/api/login", {"name": "gina", "password": PW})[1]["token"]
        alice = s.login()
        assert s.req("GET", "/api/duty-settings", token=gina) == (
            200, {"shift_hours": [7, 19], "stale_hours": 2})
        assert s.req("POST", "/api/duty-settings", {"stale_hours": 3}, token=gina)[0] == 403
        code, d = s.req("POST", "/api/duty-settings", {"shift_hours": [8, 20]}, token=alice)
        assert code == 200 and d == {"shift_hours": [8, 20], "stale_hours": 2}
        assert s.req("POST", "/api/duty-settings", {"stale_hours": 0}, token=alice)[0] == 400
        audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
        assert any(r["actor"] == "alice" and "/api/duty-settings" in r["action"]
                   and r["status"] == 200 for r in audit)
    finally:
        s.close()
