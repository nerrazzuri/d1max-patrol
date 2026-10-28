"""W40/W41 外审(2026-10-28)站点这一侧:R5 定位丢了 / 掉线期间,禁行区 P1 的「离开计时」要清零;
R8 站点重启后,库里还挂着的禁行区 P1 照样自动解决。复现照外审报告。"""

from __future__ import annotations

import pytest
from test_site_alerts import _status

from d1max_contract.messages import Event, MapPose, Telemetry
from d1max_contract.zones import ZoneSet
from d1max_site.alert_sources import NOGO_CLEAR_S, SiteAlertSources
from d1max_site.alert_store import AlertDesk
from d1max_site.db import SiteDB


def _台(tmp_path):
    now = [1_000_000]
    db = SiteDB(tmp_path / "site.db")
    desk = AlertDesk(db, now_ms=lambda: now[0])
    zs = ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 1, "zones": [
        {"id": "pond", "kind": "nogo", "polygon": [[0, 0], [2, 0], [2, 2], [0, 2]]}]})
    src = SiteAlertSources(desk, now_ms=lambda: now[0])
    src.nav_zones = lambda *a: zs
    src.on_status("A", _status())
    src.on_event("A", Event(event_id="inside-1", seq=1, boot_id="b1", stamp=now[0],
                            kind="inside_nogo", data={"zone": "pond", "x": 1.0, "y": 1.0}))
    return now, db, desk, src, zs


def _远(src, now, pose=True):
    src.on_telemetry("A", Telemetry(
        stamp=now[0], battery_pct=80.0, task_state=None, loc_quality=0.9,
        pose=MapPose(map_id="m", map_version="1", frame_id="map", x=5.0, y=1.0, yaw=0.0)
        if pose else None))


def _开着(desk):
    return [a for a in desk.open() if a["kind"] == "nogo"]


@pytest.mark.parametrize("断", ["定位不可信", "掉线"])
def test_外审R5_中间定位丢了或掉线_恢复后要重新连着离开5秒才解决(tmp_path, 断):
    now, db, desk, src, _ = _台(tmp_path)
    _远(src, now)
    now[0] += 1_000
    src.on_status("A", _status(loc_ok=False) if 断 == "定位不可信" else _status(online=False))
    for _ in range(8):
        now[0] += 1_000
        _远(src, now, pose=False)
    src.on_status("A", _status())
    _远(src, now)
    assert _开着(desk), "恢复后第一个位置就解决了:没重新数"
    now[0] += int(NOGO_CLEAR_S * 1000) + 100
    _远(src, now)
    assert not _开着(desk), "重新连着离开够久:解决"
    db.close()


def test_外审R8_站点重启后_库里挂着的禁行区P1照样自动解决(tmp_path):
    now, db, desk, _, zs = _台(tmp_path)
    assert _开着(desk)
    again = SiteAlertSources(desk, now_ms=lambda: now[0])
    again.nav_zones = lambda *a: zs
    again.seed("A", _status())
    for _ in range(12):
        now[0] += 1_000
        _远(again, now)
    assert not _开着(desk)
    db.close()


@pytest.mark.parametrize("res, 当噪点", [(0.2, True), (0.25, False), (0.5, False)])
def test_外审R7_粗分辨率的图_一格就超过005平方米的不当噪点(res, 当噪点):
    """原来 ``max(1, int(0.05 / res²))`` 至少放一格:0.5 m 一格(0.25 m²)的孤立障碍也被当噪点清掉。"""
    from d1max_site.map_denoise import find_specks
    from d1max_site.map_preview import parse_map_yaml
    px = bytearray([254]) * 400
    px[10 * 20 + 10] = 0
    pgm = b"P5\n20 20\n255\n" + bytes(px)
    meta = parse_map_yaml(f"image: floor.pgm\nresolution: {res}\norigin: [0,0,0]\nnegate: 0\n"
                          "occupied_thresh: 0.65\nfree_thresh: 0.196\n")
    assert (len(find_specks(pgm, meta)) == 1) is 当噪点
