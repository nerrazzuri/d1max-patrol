"""W73 第一期:防区个数不设上下限;防区名可以是中文。

用户 11-04:「5 个防区不是上下限,只是一个例子」。
"""

# ruff: noqa: F811 —— 夹具「台」从别的测试文件导入,测试参数同名是 pytest 的用法
from __future__ import annotations

from test_site_intercept_reach import 假派遣
from test_site_modes import 台  # noqa: F401 —— 夹具

from d1max_site.db import SiteDB
from d1max_site.incidents import IncidentDesk
from d1max_site.modes import MAX_VISITOR_ZONES


def test_临时撤防一次可以撤几百个防区(台):
    assert MAX_VISITOR_ZONES >= 500
    zones = [f"区{i}" for i in range(300)]
    台.set_mode("visitor", by="olga", zones=zones, minutes=60)
    assert len(台.view()["visitor_zones"]) == 300


def test_外部设备的防区名可以是中文(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 1000)
    desk.set_intercept("gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0)
    desk.map_zone("东区", "gate")
    assert any(z["zone"] == "东区" for z in desk.intercepts()["zones"])
    db.close()
