"""判读与复核的 HTTP 接口。

判读本身归 ``tests/inspect/test_judge.py`` 管,这里只测这一层:路由通不通、
错误怎么变成状态码、复核写进去之后 ``/api/runs/<id>`` 读不读得回来,以及
**报告会不会跟着失效** —— 结论改了但报告还是旧的,是这一层最容易漏的坑。

一个真 API 都不打:没配密钥时判读会把每张照片标成 ``pending``,接口的形状
在这条路径上完整走得完。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.app import conftest as C

PHOTO = "P1_transformer__front__20260901T101500Z.jpg"
_JPG = b"\xff\xd8fake\xff\xd9"


def _q(path: str) -> str:
    """URL 里的中文得先转义 —— urllib 的请求行是纯 ASCII 的。"""
    return quote(path, safe="/.%")


def request(server, path: str, **kwargs):
    return C.request(server, _q(path), **kwargs)


def status(server, path: str, **kwargs) -> int:
    return request(server, path, **kwargs)[0]


def get_json(server, path: str, **kwargs):
    return C.get_json(server, _q(path), **kwargs)


def get_err(server, path: str, expect: int, **kwargs):
    return C.get_err(server, _q(path), expect, **kwargs)


@pytest.fixture(autouse=True)
def 没有密钥(monkeypatch):
    """开发机上真配了密钥的那台,不该在跑测试时打真 API。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def run_id(ctx) -> str:
    """一趟跑完的归档,两张照片。返回它在 URL 里的 id。"""
    run = Path(ctx.runs_root) / "巡检一号" / "20260902T101500Z"
    (run / "photos").mkdir(parents=True)
    for name in (PHOTO, "P2_gauge__front__20260901T101600Z.jpg"):
        (run / "photos" / name).write_bytes(_JPG)
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": "巡检一号", "map_id": "map_test",
                    "waypoints": [{"name": "P1_transformer", "pose": {},
                                   "check": "配电柜门是否关闭"}]},
        "started_at": "20260902T101500Z", "fingerprint": {},
        "summary": {"state": "COMPLETED", "results": []},
    }, ensure_ascii=False), encoding="utf-8")
    return "巡检一号__20260902T101500Z"


# --------------------------------------------------------------------- 判读


def test_判读一趟给回每张照片的结论(server, run_id):
    got = get_json(server, f"/api/runs/{run_id}/judge", method="POST")
    assert len(got["findings"]) == 2


def test_没配密钥时判读不报错只是标待判读(server, run_id):
    """现场没网、密钥没配是常态,页面该照常列得出照片。"""
    got = get_json(server, f"/api/runs/{run_id}/judge", method="POST")
    assert all(f["verdict"] == "pending" for f in got["findings"])


def test_判读结果落盘之后读详情读得回来(server, run_id):
    request(server, f"/api/runs/{run_id}/judge", method="POST")
    got = get_json(server, f"/api/runs/{run_id}")
    assert [f["photo"] for f in got["findings"]][0] == PHOTO


def test_判读一趟不存在的运行是404(server, run_id):
    assert status(server, "/api/runs/没有这趟__x/judge", method="POST") == 404


def test_判读会让旧报告失效(server, run_id):
    """结论变了报告还是旧的,比没有报告更坑人。"""
    request(server, f"/api/runs/{run_id}/report.md")
    request(server, f"/api/runs/{run_id}/judge", method="POST")
    run = Path(server._ctx.runs_root) / "巡检一号" / "20260902T101500Z"
    assert not (run / "report.md").exists()


# --------------------------------------------------------------------- 复核


def test_复核记下来了(server, run_id):
    got = get_json(server, f"/api/runs/{run_id}/review/{PHOTO}", method="POST",
                   payload={"verdict": "normal", "note": "看过了"})
    assert got["reviews"][PHOTO] == {"verdict": "normal", "note": "看过了"}


def test_复核之后读详情读得回来(server, run_id):
    request(server, f"/api/runs/{run_id}/review/{PHOTO}", method="POST",
            payload={"verdict": "abnormal", "note": "有油渍"})
    got = get_json(server, f"/api/runs/{run_id}")
    assert got["reviews"][PHOTO]["verdict"] == "abnormal"


def test_复核不认识的结论是400(server, run_id):
    got = get_err(server, f"/api/runs/{run_id}/review/{PHOTO}", 400,
                  method="POST", payload={"verdict": "大概吧", "note": ""})
    assert got["error"]


def test_复核一张不存在的照片是400(server, run_id):
    assert status(server, f"/api/runs/{run_id}/review/没有这张.jpg",
                  method="POST",
                  payload={"verdict": "normal", "note": ""}) == 400


def test_复核不覆盖模型的结论(server, run_id):
    request(server, f"/api/runs/{run_id}/judge", method="POST")
    run = Path(server._ctx.runs_root) / "巡检一号" / "20260902T101500Z"
    before = (run / "findings.json").read_text(encoding="utf-8")
    request(server, f"/api/runs/{run_id}/review/{PHOTO}", method="POST",
            payload={"verdict": "normal", "note": "看过了"})
    assert (run / "findings.json").read_text(encoding="utf-8") == before


def test_复核会让旧报告失效(server, run_id):
    request(server, f"/api/runs/{run_id}/report.md")
    request(server, f"/api/runs/{run_id}/review/{PHOTO}", method="POST",
            payload={"verdict": "normal", "note": "看过了"})
    run = Path(server._ctx.runs_root) / "巡检一号" / "20260902T101500Z"
    assert not (run / "report.md").exists()


def test_复核一趟不存在的运行是404(server, run_id):
    assert status(server, f"/api/runs/没有这趟__x/review/{PHOTO}",
                  method="POST",
                  payload={"verdict": "normal", "note": ""}) == 404


def test_报告里最终以人的结论为准(server, run_id):
    """两层结论的意义就在这儿:模型判待判读,人改成异常,报告写异常。"""
    request(server, f"/api/runs/{run_id}/judge", method="POST")
    request(server, f"/api/runs/{run_id}/review/{PHOTO}", method="POST",
            payload={"verdict": "abnormal", "note": "门开着"})
    _, body, _ = request(server, f"/api/runs/{run_id}/report.md")
    assert "异常" in body.decode("utf-8")
