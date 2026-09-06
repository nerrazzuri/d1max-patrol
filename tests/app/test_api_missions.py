"""任务、运行、地图、归档这四组接口。

**这里起的是真服务、打的是真 HTTP 请求**,理由同 ``tests/app/conftest.py``:
这一层真正会出问题的地方(id 怎么解码、越界怎么挡、错误怎么变成响应体)全在
HTTP 那一侧,直接调处理函数一个都测不到。

后端是假的,归档是手搓的目录 —— 一条 ROS 命令都不会真起。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import quote

import pytest

from d1max_patrol.engine.machine import RunState
from tests.app import conftest as C

# --------------------------------------------------------------------- 小工具


def _q(path: str) -> str:
    """URL 里的中文得先转义 —— urllib 的请求行是纯 ASCII 的,不转义在客户端
    就抛了,根本到不了服务。``%`` 留在白名单里,免得把已经转义好的再转一遍。
    """
    return quote(path, safe="/.%")


def request(server, path: str, **kwargs):
    return C.request(server, _q(path), **kwargs)


def status(server, path: str, **kwargs) -> int:
    return request(server, path, **kwargs)[0]


def get_json(server, path: str, **kwargs):
    return C.get_json(server, _q(path), **kwargs)


def get_err(server, path: str, expect: int, **kwargs):
    return C.get_err(server, _q(path), expect, **kwargs)


def _mission(**overrides) -> dict:
    """一份能过 ``parse_mission`` 的最小任务定义。"""
    base = {
        "mission": "巡检一号",
        "map_id": "map_test",
        "waypoints": [{
            "name": "P1_transformer",
            "pose": {"position": {"x": 1.0, "y": 0.0, "z": 0.0},
                     "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
            "check": "配电柜门是否关闭",
            "actions": [{"type": "dwell", "seconds": 0.1}],
        }],
    }
    base.update(overrides)
    return base


def _put(server, path: str, payload):
    code, body, _ = request(server, path, method="PUT", payload=payload)
    return code, json.loads(body)


def _post(server, path: str, payload=None):
    code, body, _ = request(server, path, method="POST", payload=payload or {})
    return code, json.loads(body)


def _post_status(server, path: str, payload=None) -> int:
    return request(server, path, method="POST", payload=payload or {})[0]


def _make_run(root: Path, mission: str, stamp: str,
              photos: tuple[str, ...] = ("P1__front__20260901-100005.jpg",),
              *, with_report: bool = False) -> Path:
    """手搓一个归档目录。

    不走 ``RunArchive``:那样得把整趟运行跑一遍,而这些测试要的只是"目录长这
    个样子的时候,接口给什么"。
    """
    run = root / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": mission, "map_id": "map_test"},
        "started_at": stamp,
        "fingerprint": {"sdk": "0.1.1"},
        "summary": {
            "state": "DONE", "reason": "", "succeeded": 1, "failed": 0,
            "total": 1,
            "results": [{"name": "P1", "ok": True, "arrived_ms": 0,
                         "elapsed_s": 3.5, "photos": list(photos), "note": ""}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"kind": "nav", "waypoint": "P1"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    for name in photos:
        # 一个最小的合法 JPEG 头尾。内容不重要,能读回来就行。
        (run / "photos" / name).write_bytes(b"\xff\xd8\xff\xd9")
    if with_report:
        (run / "report.md").write_text("# 老报告\n", encoding="utf-8")
    return run


@pytest.fixture
def saved(server):
    """已经存了一份任务的服务。返回 (server, 任务 id)。"""
    code, _ = _put(server, "/api/missions/巡检一号", _mission())
    assert code == 200
    return server, "巡检一号"


# ------------------------------------------------------------------- 任务往返


def test_任务列表一开始是空的(server):
    assert get_json(server, "/api/missions") == {"missions": []}


def test_存一份任务再读回来是同一份(server):
    code, saved_wire = _put(server, "/api/missions/巡检一号", _mission())
    assert code == 200
    got = get_json(server, "/api/missions/" + quote("巡检一号"))
    assert got == saved_wire
    assert got["mission"] == "巡检一号"
    assert got["map_id"] == "map_test"


def test_存过之后会出现在列表里(saved):
    server, mid = saved
    assert get_json(server, "/api/missions")["missions"] == [mid]


def test_保存了不合法的任务给400并说清哪儿不对(server):
    """错在哪儿必须写在 ``error`` 里 —— 页面上只显示这一行。"""
    bad = _mission()
    del bad["map_id"]
    err = get_err(server, "/api/missions/巡检一号", 400,
                  method="PUT", payload=bad)
    assert "map_id" in err["error"]


def test_任务定义存不进去就一个字节都不落盘(server, ctx):
    bad = _mission()
    del bad["map_id"]
    request(server, "/api/missions/巡检一号", method="PUT", payload=bad)
    assert not (ctx.missions_dir / "巡检一号.yaml").exists()


def test_任务名和文件名对不上直接拒(server):
    """悄悄以哪一个为准都会让人找不到自己刚存的东西。"""
    err = get_err(server, "/api/missions/甲", 400, method="PUT",
                  payload=_mission(mission="乙"))
    assert "对不上" in err["error"]


def test_body里不写任务名就按文件名补上(server):
    body = _mission()
    del body["mission"]
    code, wire = _put(server, "/api/missions/丙", body)
    assert code == 200
    assert wire["mission"] == "丙"


def test_任务定义不是对象也给400(server):
    code, _body, _h = request(server, "/api/missions/甲", method="PUT",
                              raw=b"[1, 2, 3]")
    assert code == 400


def test_读一个不存在的任务给404(server):
    assert status(server, "/api/missions/没存过") == 404


def test_任务名带两点的直接拒(server):
    """``..`` 每个字符都在白名单里,拼出来却是往上一层走。"""
    assert status(server, "/api/missions/..") == 400
    assert status(server, "/api/missions/a..b") == 400


def test_任务名带斜杠的根本匹配不上路由(server):
    """占位符不跨斜杠 —— 解码之后带斜杠的 id 连路由都进不来。"""
    assert status(server, "/api/missions/" + quote("../secret", safe="")) == 404


def test_存下来的是人手能改的YAML(saved, ctx):
    """不引数据库的全部理由就是"手写和页面编的是同一份东西"。"""
    text = (ctx.missions_dir / "巡检一号.yaml").read_text(encoding="utf-8")
    assert "配电柜门是否关闭" in text, "中文被转义成 \\uXXXX 就没法手改了"


def test_任务接口只认GET和PUT(server):
    assert status(server, "/api/missions/甲", method="DELETE") == 405


# --------------------------------------------------------------- 起飞检查与起任务


def test_起飞检查过了任务就真的跑起来了(saved, ctx):
    server, mid = saved
    code, body = _post(server, f"/api/missions/{quote(mid)}/run")
    assert code == 200
    assert ctx.engine.running
    assert "state" in body["run"]
    assert [c["name"] for c in body["checks"]] == [
        "nav_ready", "device_ready", "localized", "home", "battery", "storage",
        "removable"]


def test_起飞检查没过时run返回409并列出没过的项(saved, ctx):
    from d1max_patrol.protocol.nav_types import NavStatus
    ctx.nav.nav = NavStatus.ACTIVE
    server, mid = saved
    err = get_err(server, f"/api/missions/{quote(mid)}/run", 409, method="POST",
                  payload={})
    assert "nav_ready" in json.dumps(err, ensure_ascii=False)


def test_起飞检查没过就一点都没起(saved, ctx):
    """没过还起了一半,是这条路上最贵的一种错。"""
    ctx.device.batt = 5.0
    server, mid = saved
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 409
    assert not ctx.engine.running


def test_每一项检查的理由都带回来(saved, ctx):
    ctx.device.control = False
    server, mid = saved
    err = get_err(server, f"/api/missions/{quote(mid)}/run", 409, method="POST")
    bad = [c for c in err["checks"] if not c["ok"]]
    assert bad and all(c["detail"] for c in bad), "没过的项必须说清当时是什么状态"


def test_起一个不存在的任务给404(server):
    assert _post_status(server, "/api/missions/没存过/run") == 404


def test_任务在跑的时候再起一次给409(saved, ctx):
    server, mid = saved
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 200
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 409


# ------------------------------------------------------------------- 运行控制


def test_没任务在跑时暂停给409(server):
    """点了"暂停"却什么都没发生,人会以为已经停了。"""
    assert _post_status(server, "/api/run/pause") == 409
    assert _post_status(server, "/api/run/resume") == 409
    assert _post_status(server, "/api/run/abort") == 409


def test_暂停和继续走得通(saved, ctx):
    server, mid = saved
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    assert _post_status(server, "/api/run/pause") == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.PAUSED))
    assert _post_status(server, "/api/run/resume") == 200


def test_中止把任务停下来(saved, ctx):
    server, mid = saved
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    assert _post_status(server, "/api/run/abort", {"reason": "人喊停"}) == 200
    assert ctx.bridge.call(
        lambda: ctx.engine.wait_done(timeout_s=5.0)) is RunState.ABORTED
    assert ctx.engine.snapshot.reason == "人喊停"


def test_不写理由的中止也有个说得出口的理由(saved, ctx):
    server, mid = saved
    assert _post_status(server, f"/api/missions/{quote(mid)}/run") == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    assert _post_status(server, "/api/run/abort") == 200
    ctx.bridge.call(lambda: ctx.engine.wait_done(timeout_s=5.0))
    assert ctx.engine.snapshot.reason, "归档里留一条空理由,事后没人查得出为什么停"


# ----------------------------------------------------------------------- 地图


def test_地图列表带着当前后端的能力集(server):
    """按钮该不该能点由后端说了算,不该让页面自己猜。"""
    d = get_json(server, "/api/maps")
    assert d["maps"] == ["map_test", "map_b"]
    assert set(d["caps"]) <= {"mapping", "map_admin", "reloc"}


def test_自建路线的地图列表能力集是空的(server_local_backend):
    assert get_json(server_local_backend, "/api/maps")["caps"] == []


def test_切图切得动(server, ctx):
    assert _post_status(server, "/api/maps/load", {"map_id": "map_b"}) == 200
    assert ctx.nav.current_map == "map_b"


def test_切一张不存在的图给409(server):
    """后端明确回了拒绝 —— 那是 409,不是 500。"""
    assert _post_status(server, "/api/maps/load", {"map_id": "没这张"}) == 409


def test_切图不给图名给400(server):
    assert _post_status(server, "/api/maps/load", {}) == 400


def test_图名带两点的直接拒(server):
    assert _post_status(server, "/api/maps/load", {"map_id": "../etc"}) == 400


# ------------------------------------------------------------------- 初始位姿


def test_初始位姿发的是一条initialpose并且走实时域(ctx):
    """直接看生成的命令。文档 §4 那条 ``ros2 topic pub`` 就是这个样子。"""
    spec = ctx.mapping.spec_for_initial_pose(1.5, -2.0, math.pi / 2)
    assert spec.argv[:6] == ("ros2", "topic", "pub", "--once", "/initialpose",
                             "geometry_msgs/msg/PoseWithCovarianceStamped")
    assert spec.env["ROS_DOMAIN_ID"] == "24"
    assert spec.env["RMW_IMPLEMENTATION"] == "rmw_zenoh_cpp"


def test_初始位姿的朝向写成四元数(ctx):
    """``w`` 不写出来默认是 0,而那是一个非法姿态。"""
    msg = json.loads(ctx.mapping.spec_for_initial_pose(0.0, 0.0, math.pi).argv[-1])
    assert msg["header"]["frame_id"] == "loc_map"
    assert msg["pose"]["pose"]["position"]["x"] == 0.0
    assert msg["pose"]["pose"]["orientation"]["z"] == pytest.approx(1.0)
    assert msg["pose"]["pose"]["orientation"]["w"] == pytest.approx(0.0, abs=1e-9)


def test_发初始位姿的接口把三个数原样传下去(server, ctx):
    seen = []

    async def fake(x, y, yaw=0.0):
        seen.append((x, y, yaw))

    ctx.mapping.publish_initial_pose = fake
    assert _post_status(server, "/api/pose/initial",
                        {"x": 1.0, "y": 2.0, "yaw": 0.5}) == 200
    assert seen == [(1.0, 2.0, 0.5)]


def test_初始位姿不是数给400(server):
    assert _post_status(server, "/api/pose/initial", {"x": "往前一点"}) == 400


def test_录包期间不许发初始位姿(server, ctx):
    """那一刻链路上正在录,发一条进去只会把这段包搅乱。"""
    ctx.mapping._phase = "recording"
    assert _post_status(server, "/api/pose/initial", {"x": 0.0, "y": 0.0}) == 409


def test_自建路线上调重定位接口被明确拒绝(server_local_backend):
    """能力集里没有不等于可以静默无操作 —— 得说清楚这条路线该点哪个。"""
    err = get_err(server_local_backend, "/api/pose/reset", 409, method="POST")
    assert "/api/pose/initial" in json.dumps(err, ensure_ascii=False)


def test_厂商路线上重定位调得动(server, ctx):
    assert _post_status(server, "/api/pose/reset") == 200
    assert ctx.nav.reloc_calls == 1


# ----------------------------------------------------------------------- 归档


def test_没跑过的时候历史是空的(server):
    assert get_json(server, "/api/runs") == {"runs": []}


def test_历史运行按时间倒序(server, ctx):
    """人打开这一页是来看**刚才那趟**的,不是来翻上个月的。"""
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    _make_run(ctx.runs_root, "巡检一号", "20260903-090000")
    _make_run(ctx.runs_root, "巡检二号", "20260902-080000")
    ids = [r["id"] for r in get_json(server, "/api/runs")["runs"]]
    assert ids == ["巡检一号__20260903-090000",
                   "巡检二号__20260902-080000",
                   "巡检一号__20260901-100000"]


def test_历史列表带着结果汇总(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    row = get_json(server, "/api/runs")["runs"][0]
    assert row["mission"] == "巡检一号"
    assert (row["state"], row["succeeded"], row["failed"]) == ("DONE", 1, 0)


def test_运行详情带着事件和照片(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    d = get_json(server, "/api/runs/" + quote("巡检一号__20260901-100000"))
    assert d["photos"] == ["P1__front__20260901-100005.jpg"]
    assert d["events"] == [{"kind": "nav", "waypoint": "P1"}]
    assert d["manifest"]["fingerprint"] == {"sdk": "0.1.1"}


def test_取一个不存在的运行给404(server):
    assert status(server, "/api/runs/没跑过__20260101-000000") == 404


def test_取照片取得到(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    code, body, headers = request(
        server, "/api/runs/" + quote("巡检一号__20260901-100000")
        + "/photos/P1__front__20260901-100005.jpg")
    assert code == 200
    assert body.startswith(b"\xff\xd8")
    assert headers["Content-Type"] == "image/jpeg"


def test_取照片取不到别的目录的文件(server, ctx):
    """归档目录里 manifest.json 就在照片目录隔壁。"""
    run_id = quote("巡检一号__20260901-100000")
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    assert status(server, f"/api/runs/{run_id}/photos/..") == 400
    assert status(server, f"/api/runs/{run_id}/photos/manifest.json") == 404
    assert status(server, f"/api/runs/{run_id}/photos/"
                  + quote("../manifest.json", safe="")) == 404


def test_取一张不存在的照片给404(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    assert status(server, "/api/runs/" + quote("巡检一号__20260901-100000")
                  + "/photos/没拍过.jpg") == 404


def test_报告没生成时会当场生成一份(server, ctx):
    """跑到一半被强杀、目录从别的机器拷过来 —— 人来翻报告多半就是这两种。"""
    run = _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    assert not (run / "report.md").exists()
    code, body, headers = request(
        server, "/api/runs/" + quote("巡检一号__20260901-100000") + "/report.md")
    assert code == 200
    assert "巡检报告" in body.decode("utf-8")
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert (run / "report.md").exists(), "生成完要落盘,不能每次都重算"


def test_已经有报告就不重新生成(server, ctx):
    run = _make_run(ctx.runs_root, "巡检一号", "20260901-100000",
                    with_report=True)
    code, body, _ = request(
        server, "/api/runs/" + quote("巡检一号__20260901-100000") + "/report.md")
    assert code == 200
    assert body.decode("utf-8").strip() == "# 老报告"
    assert (run / "report.md").read_text(encoding="utf-8").strip() == "# 老报告"


def test_报告也出得了HTML(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    code, body, headers = request(
        server, "/api/runs/" + quote("巡检一号__20260901-100000") + "/report.html")
    assert code == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<html" in body.lower()


def test_报告只认md和html(server, ctx):
    _make_run(ctx.runs_root, "巡检一号", "20260901-100000")
    assert status(server, "/api/runs/" + quote("巡检一号__20260901-100000")
                  + "/report.pdf") == 404
