"""任务定义的加载、校验与往返。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.mission import (
    MIN_RETENTION_DAYS,
    MissionError,
    Policy,
    _parse_policy,
    dump_mission,
    load_mission,
    save_mission,
)

SAMPLE = """
mission: substation_night_patrol
map_id: map_20260901_1
route:
  source: vendor_path
  path_id: path_a
waypoints:
  - name: P1_transformer
    pose:
      position: {x: 1.2, y: 3.4, z: 0.0}
      orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}
    check: "配电柜门是否关闭"
    actions:
      - {type: dwell, seconds: 2}
      - {type: photo, camera: front}
policy:
  waypoint_timeout_s: 90
  battery_abort_pct: 12
"""

MINIMAL_WP = (
    "  - {name: a, pose: {position: {x: 0, y: 0, z: 0}, "
    "orientation: {x: 0, y: 0, z: 0, w: 1}}}\n"
)


def _write(tmp_path, text):
    p = tmp_path / "m.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_读得出主规范里那份样例(tmp_path):
    m = load_mission(_write(tmp_path, SAMPLE))
    assert m.mission == "substation_night_patrol"
    assert m.map_id == "map_20260901_1"
    assert m.route_source == "vendor_path"
    assert m.route_path_id == "path_a"
    assert len(m.waypoints) == 1
    wp = m.waypoints[0]
    assert wp.name == "P1_transformer"
    assert wp.pose.position.x == pytest.approx(1.2)
    assert wp.pose.position.y == pytest.approx(3.4)
    assert wp.check == "配电柜门是否关闭"
    assert [a.type for a in wp.actions] == ["dwell", "photo"]
    assert wp.actions[0].seconds == pytest.approx(2.0)
    assert wp.actions[1].camera == "front"


def test_没写的策略字段走默认值(tmp_path):
    m = load_mission(_write(tmp_path, SAMPLE))
    assert m.policy.waypoint_timeout_s == pytest.approx(90.0)   # 写了的用写的
    assert m.policy.battery_abort_pct == pytest.approx(12.0)
    assert m.policy.battery_return_pct == pytest.approx(25.0)   # 没写的用默认
    assert m.policy.loops == 1
    assert m.policy.on_waypoint_failed == "retry_then_skip"


def test_存回去再读一遍还是同一个(tmp_path):
    m = load_mission(_write(tmp_path, SAMPLE))
    out = tmp_path / "round.yaml"
    save_mission(m, out)
    assert load_mission(out) == m


def test_存任务不留临时文件(tmp_path):
    m = load_mission(_write(tmp_path, SAMPLE))
    out = tmp_path / "sub" / "round.yaml"
    save_mission(m, out)
    assert list(out.parent.iterdir()) == [out]


def test_check_是可选的_缺了就是空串(tmp_path):
    text = SAMPLE.replace('    check: "配电柜门是否关闭"\n', "")
    assert load_mission(_write(tmp_path, text)).waypoints[0].check == ""


def test_没有动作的点位也是合法的(tmp_path):
    """只是走过去看一眼,不拍照 —— 完全正常的用法。"""
    text = f"mission: x\nmap_id: y\nwaypoints:\n{MINIMAL_WP}"
    assert load_mission(_write(tmp_path, text)).waypoints[0].actions == ()


@pytest.mark.parametrize("bad, hint", [
    ("mission: x\nmap_id: y\nwaypoints: []\n", "至少要有一个点位"),
    (f"map_id: y\nwaypoints:\n{MINIMAL_WP}", "mission"),
    (f"mission: x\nwaypoints:\n{MINIMAL_WP}", "map_id"),
    ("mission: x\nmap_id: y\n", "waypoints"),
])
def test_缺了必填的东西要报出是哪一个(tmp_path, bad, hint):
    with pytest.raises(MissionError, match=hint):
        load_mission(_write(tmp_path, bad))


def test_不认识的动作类型当场拒绝(tmp_path):
    text = SAMPLE.replace("{type: dwell, seconds: 2}", "{type: teleport}")
    with pytest.raises(MissionError, match="teleport"):
        load_mission(_write(tmp_path, text))


def test_重名的点位被拒(tmp_path):
    """点位名是照片文件名的一部分,重名会让归档互相覆盖。"""
    text = SAMPLE + """  - name: P1_transformer
    pose:
      position: {x: 5.0, y: 6.0, z: 0.0}
      orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}
"""
    with pytest.raises(MissionError, match="P1_transformer"):
        load_mission(_write(tmp_path, text))


@pytest.mark.parametrize("evil", ["../etc/passwd", "a/b", "a\\b", "..", "x..y/z"])
def test_点位名不能带路径分隔符(tmp_path, evil):
    """照片文件名直接取点位名,带斜杠就写到别的目录去了。"""
    text = SAMPLE.replace("P1_transformer", evil)
    with pytest.raises(MissionError, match="点位名"):
        load_mission(_write(tmp_path, text))


def test_photo_必须说清楚哪个相机(tmp_path):
    text = SAMPLE.replace("{type: photo, camera: front}", "{type: photo, camera: side}")
    with pytest.raises(MissionError, match="side"):
        load_mission(_write(tmp_path, text))


def test_photo_漏了相机也被拒(tmp_path):
    text = SAMPLE.replace("{type: photo, camera: front}", "{type: photo}")
    with pytest.raises(MissionError, match="相机"):
        load_mission(_write(tmp_path, text))


def test_dwell_漏了时长被拒(tmp_path):
    text = SAMPLE.replace("{type: dwell, seconds: 2}", "{type: dwell}")
    with pytest.raises(MissionError, match="seconds"):
        load_mission(_write(tmp_path, text))


def test_布尔不会被当成时长(tmp_path):
    """YAML 里 ``seconds: true`` 会解成 Python 的 True,而 bool 是 int 的子类。"""
    text = SAMPLE.replace("{type: dwell, seconds: 2}", "{type: dwell, seconds: true}")
    with pytest.raises(MissionError, match="seconds"):
        load_mission(_write(tmp_path, text))


def test_中止电量高于返航电量被拒(tmp_path):
    """那样永远轮不到返航 —— 电量一掉就直接中止,返航策略成了摆设。"""
    text = SAMPLE.replace("battery_abort_pct: 12", "battery_abort_pct: 40")
    with pytest.raises(MissionError, match="返航"):
        load_mission(_write(tmp_path, text))


def test_不认识的失败处置被拒(tmp_path):
    text = SAMPLE + "  on_waypoint_failed: 装死\n"
    with pytest.raises(MissionError, match="on_waypoint_failed"):
        load_mission(_write(tmp_path, text))


def test_不是yaml的文件报得出是格式问题(tmp_path):
    with pytest.raises(MissionError, match="YAML"):
        load_mission(_write(tmp_path, "mission: [未闭合\n"))


def test_读不存在的文件报得出是哪个文件(tmp_path):
    with pytest.raises(MissionError, match="读不了"):
        load_mission(tmp_path / "没有这个文件.yaml")


def test_导出的是人能读也能改的_yaml(tmp_path):
    m = load_mission(_write(tmp_path, SAMPLE))
    text = dump_mission(m)
    assert "配电柜门是否关闭" in text, "中文不该被转义成 \\u"
    assert "P1_transformer" in text
    assert "\\u" not in text


def test_导出的动作只带这种动作用得上的字段(tmp_path):
    """全字段导出会让 YAML 变得没法读 —— 一个 photo 动作不需要 pitch。"""
    text = dump_mission(load_mission(_write(tmp_path, SAMPLE)))
    assert "pitch" not in text


@pytest.mark.parametrize("key", ["on_loc_lost", "on_control_lost"])
def test_不认识的丢失处置被拒(tmp_path, key):
    """词表要在读任务的时候就卡死,不能等出事那一刻才发现不认识。"""
    text = SAMPLE + f"  {key}: 装死\n"
    with pytest.raises(MissionError, match=key):
        load_mission(_write(tmp_path, text))


@pytest.mark.parametrize("key, value", [("on_loc_lost", "abort"),
                                        ("on_control_lost", "abort")])
def test_认识的丢失处置读得进来(tmp_path, key, value):
    text = SAMPLE + f"  {key}: {value}\n"
    assert getattr(load_mission(_write(tmp_path, text)).policy, key) == value


def test_中止线默认二十五不是十五():
    # 厂商的强制趴窝线是单块电池 10%(硬件手册 2.3.3)。默认 15 离它只剩 5 个点,
    # 而这 5 个点要覆盖:发现、告警、人走过去、把狗弄回来。不够。
    assert Policy().battery_abort_pct == 25.0


# ------------------------------------------------------------- 保留天数


def test_保留天数默认九十天():
    assert Policy().retention_days == 90


def test_保留天数写进线格式():
    assert Policy(retention_days=30).to_wire()["retention_days"] == 30


def test_保留天数能从任务文件里解出来():
    policy = _parse_policy({"retention_days": 365})
    assert policy.retention_days == 365


@pytest.mark.parametrize("bad", [0, -1, 3, 6, 1.5, "90", True, None])
def test_保留天数不合法要报错(bad):
    # 0 和负数当"永不删"是个陷阱:盘满那天水位删除只剩两条路 —— 违反承诺,
    # 或者让盘满到狗停机。小于 MIN_RETENTION_DAYS 则让删除预告失去意义。
    with pytest.raises(MissionError):
        _parse_policy({"retention_days": bad})


def test_保留天数下限正好是七天():
    assert MIN_RETENTION_DAYS == 7
    assert _parse_policy({"retention_days": MIN_RETENTION_DAYS}).retention_days == 7
