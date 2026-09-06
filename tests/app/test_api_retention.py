"""盘况、预告、导出、清盘的 HTTP 接口。

这一层只测接口的形状:路由通不通、参数不对怎么变成状态码、engine 算出来的
东西有没有原样送出去。真正的判断逻辑归 ``tests/engine/test_retention*.py``
和 ``tests/engine/test_export.py`` 管 —— 那边能穷举,这边不能。

**最要紧的是最后那两条:**「没预告满 7 天的一趟都删不掉」和「导出确认之后
那一趟才真的删得掉」。合起来就是 spec §4.6 第 2 条:自动删除必须有导出通道
兜底,否则系统是在单方面销毁客户的资产。

时间戳一律从"此刻"往回推,不写死。写死的日期今天能过,一年之后会因为
"那趟归档过期了"而莫名其妙地红。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from d1max_patrol.engine.archive import STAMP_FMT
from d1max_patrol.engine.retention import NOTICE_REL, UPLOADED_REL, read_notice
from tests.app import conftest as C

#: 让每趟归档有点实际大小,好让 ``size_bytes`` 不是 0。
_JPG = b"\xff\xd8fake\xff\xd9" * 64


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).strftime(STAMP_FMT)


def _date(days_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d")


#: 三趟归档的时间戳。**在模块导入时算一次**,不在每个断言里重算 ——
#: 重算的话跨过一秒就成了另一个名字,而那种红是查一下午才查得出来的。
_OLD1 = _stamp(400)
_OLD2 = _stamp(300)
_NEW = _stamp(1)


def _q(path: str) -> str:
    """URL 里的中文得先转义 —— urllib 的请求行是纯 ASCII 的。"""
    return quote(path, safe="/.%")


def _write_run(ctx, mission: str, stamp: str, *, retention_days: int = 90,
               photos: int = 2) -> Path:
    """摆一趟"跑完了"的归档。

    **manifest 里必须有 summary** —— ``scan_runs`` 靠它判"没人在写这一趟了"。
    没有 summary 的目录要等满 24 小时安定期才算数,而测试等不起。
    """
    run = Path(ctx.runs_root) / mission / stamp
    (run / "photos").mkdir(parents=True)
    for n in range(photos):
        (run / "photos" / f"P{n}__front__{stamp}.jpg").write_bytes(_JPG)
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": mission, "map_id": "map_test", "waypoints": [],
                    "policy": {"retention_days": retention_days}},
        "started_at": stamp, "fingerprint": {},
        "summary": {"state": "COMPLETED", "total": photos},
    }, ensure_ascii=False), encoding="utf-8")
    return run


@pytest.fixture
def 三趟(ctx):
    """两趟早过期了,一趟是昨天的。"""
    _write_run(ctx, "巡检一号", _OLD1)
    _write_run(ctx, "巡检一号", _OLD2)
    _write_run(ctx, "巡检二号", _NEW)
    return ctx


# ------------------------------------------------------------------ 盘况

def test_盘况给得出用量和预告(server, 三趟):
    got = C.get_json(server, "/api/storage")
    assert got["total_bytes"] > 0
    assert 0.0 <= got["used_ratio"] <= 1.0
    assert got["runs_total"] == 3
    assert got["runs_bytes"] > 0
    assert got["baselines_bytes"] == 0
    assert got["exports_bytes"] == 0
    assert "detail" in got["forecast"]


def test_盘况把过期的那两趟列进预告(server, 三趟):
    # §4.4:过期不等于立刻删,所以"已过期还在盘上"是常态,必须进名单。
    fc = C.get_json(server, "/api/storage")["forecast"]
    assert len(fc["runs"]) == 2
    assert all(r["mission"] == "巡检一号" for r in fc["runs"])
    assert fc["bytes_at_risk"] > 0


def test_看过盘况就等于预告过了(server, 三趟):
    C.get_json(server, "/api/storage")
    assert (Path(三趟.runs_root) / NOTICE_REL).is_file()
    assert len(read_notice(三趟.runs_root)) == 2


def test_再看一次预告的时间不会被刷新(server, 三趟):
    """钟要能填满。每看一次就把它拨回去的话,7 天永远数不到。"""
    C.get_json(server, "/api/storage")
    先 = read_notice(三趟.runs_root)
    C.get_json(server, "/api/storage")
    assert read_notice(三趟.runs_root) == 先


def test_一趟都没跑过也答得出盘况(server):
    got = C.get_json(server, "/api/storage")
    assert got["runs_total"] == 0
    assert got["forecast"]["runs"] == []


# ------------------------------------------------------------------ 清盘

def _sweep(server, payload):
    code, body, _ = C.request(server, "/api/storage/sweep", method="POST",
                              payload=payload)
    return code, json.loads(body)


def test_清盘默认只给方案不动盘(server, 三趟):
    """删之前先看名单。这是它跟一个"清理"按钮的全部区别。"""
    code, got = _sweep(server, {"free_bytes": 10**9})
    assert code == 200
    assert got["applied"] is False
    assert got["deleted"] == []
    assert len(list(Path(三趟.runs_root).glob("*/*"))) == 3


def test_单机时没预告满七天的一趟也删不掉(server, 三趟):
    """这一卷最要紧的一条。预告刚写下去,钟一天都没走,所以谁也删不了。"""
    C.get_json(server, "/api/storage")            # 先预告
    code, got = _sweep(server, {"free_bytes": 10**9, "apply": True})
    assert code == 200
    assert got["deleted"] == []
    assert got["sweep"]["delete"] == []
    assert got["sweep"]["short_bytes"] > 0
    assert "导出并释放" in got["sweep"]["detail"]
    assert len(list(Path(三趟.runs_root).glob("*/*"))) == 3


def test_盘在水位线下就一个都不删(server, 三趟):
    code, got = _sweep(server, {"free_bytes": 0, "apply": True})
    assert code == 200
    assert got["sweep"]["delete"] == []
    assert "水位线下" in got["sweep"]["detail"]


@pytest.mark.parametrize("bad", ["2G", 1.5, True, [], {}])
def test_要腾的字节数不是整数是400(server, 三趟, bad):
    assert _sweep(server, {"free_bytes": bad})[0] == 400


def test_清盘的请求体不是对象是400(server, 三趟):
    code, _, _ = C.request(server, "/api/storage/sweep", method="POST",
                           payload=[1, 2, 3])
    assert code == 400


# ------------------------------------------------------------------ 导出

def _export(server, payload):
    code, body, _ = C.request(server, "/api/exports", method="POST",
                              payload=payload)
    return code, json.loads(body)


@pytest.fixture
def 包(server, 三趟):
    """把那两趟老的打成一个包。返回包的线格式。"""
    code, got = _export(server, {"start": _date(500), "end": _date(100)})
    assert code == 200, got
    return got


def test_导出一个区间打出一个包(包):
    assert 包["name"].startswith("export-")
    assert 包["name"].endswith(".zip")
    assert len(包["sha256"]) == 64
    assert 包["size_bytes"] > 0
    assert len(包["runs"]) == 2
    assert 包["confirmed"] is False


def test_区间里一趟都没有是400(server, 三趟):
    """空包也能算出哈希、也能被确认 —— 于是"导出并释放"会在什么都没导出的
    情况下走完,然后把原件删掉。那是整条链最坏的失败方式。"""
    code, got = _export(server, {"start": _date(250), "end": _date(200)})
    assert code == 400
    # detail 里是 ``ExportError`` 原话:"这个区间里一趟都没有 —— 不打空包……"
    assert "不打空包" in got["detail"]


@pytest.mark.parametrize("bad", ["2026/08/01", "20260801", "八月", "", 20260801,
                                 None, "2026-13-01"])
def test_日期不是那个形状是400(server, 三趟, bad):
    assert _export(server, {"start": bad, "end": _date(100)})[0] == 400


def test_起止反了是400(server, 三趟):
    assert _export(server, {"start": _date(100), "end": _date(500)})[0] == 400


def test_起止一样也是400(server, 三趟):
    """左闭右开:同一天到同一天是空区间,不是"这一天"。"""
    assert _export(server, {"start": _date(300), "end": _date(300)})[0] == 400


def test_打好的包列得出来(server, 包):
    got = C.get_json(server, "/api/exports")
    assert [b["name"] for b in got["exports"]] == [包["name"]]
    assert got["exports"][0]["sha256"] == 包["sha256"]


def _download(server, name: str) -> tuple[int, bytes, dict]:
    return C.request(server, f"/api/exports/{name}")


def test_下载回来的字节跟包里记的哈希对得上(server, 包):
    code, blob, headers = _download(server, 包["name"])
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert hashlib.sha256(blob).hexdigest() == 包["sha256"]
    assert len(blob) == 包["size_bytes"]


def test_下载一个不存在的包是404(server, 三趟):
    assert _download(server, "export-20260101T000000Z.zip")[0] == 404


@pytest.mark.parametrize("bad", ["manifest.json", "export-x.txt", "runs.zip",
                                 "export-.zip", "export-x.zip.json"])
def test_包名不合法是400(server, 三趟, bad):
    """名字直接从 URL 上来。挡穿越的规矩只有 ``export.safe_name`` 那一份。

    带斜杠的穿越根本到不了这个处理函数:``_compile`` 的占位符不跨斜杠,
    ``/api/exports/..%2Fmanifest.json`` 解码之后匹配不上任何路由,是 404。
    这里挑的都是**能到**处理函数的坏名字 —— 包括侧写文件本身:
    ``export-x.zip.json`` 里记着哈希,让人下得走它没有任何好处。
    """
    assert _download(server, bad)[0] == 400


def _confirm(server, name: str, sha: str):
    code, body, _ = C.request(server, f"/api/exports/{name}/confirm",
                              method="POST", payload={"sha256": sha})
    return code, json.loads(body)


def test_哈希对不上不放行(server, 三趟, 包):
    """路上掉了字节。这时候删掉原件就是真的没了。"""
    code, got = _confirm(server, 包["name"], "0" * 64)
    assert code == 409
    assert "对不上" in got["error"] + got["detail"]
    # 没确认,也没给任何一趟打标记 —— 两样都不能漏。
    assert C.get_json(server, "/api/exports")["exports"][0]["confirmed"] is False
    assert not list(Path(三趟.runs_root).glob(f"*/*/{UPLOADED_REL}"))


def test_没给哈希也不放行(server, 三趟, 包):
    code, _, _ = C.request(server, f"/api/exports/{包['name']}/confirm",
                           method="POST", payload={})
    assert code == 409


def test_哈希对上了才给那几趟打上别处还有一份的标记(server, 三趟, 包):
    code, got = _confirm(server, 包["name"], 包["sha256"])
    assert code == 200
    assert got["confirmed"] is True
    marked = sorted(p.parent.name
                    for p in Path(三趟.runs_root).glob(f"*/*/{UPLOADED_REL}"))
    assert len(marked) == 2


def test_确认可以重复一次不出错(server, 三趟, 包):
    """手机上网络一抖就会重发。重发不能变成一次失败。"""
    assert _confirm(server, 包["name"], 包["sha256"])[0] == 200
    assert _confirm(server, 包["name"], 包["sha256"])[0] == 200


def test_确认过之后清盘才真的删得掉(server, 三趟, 包):
    """spec §4.6 第 2 条整条链的出口。

    单机档、没预告满 7 天,本来一趟都删不动(见上面那条)。走完"导出 →
    对上哈希 → 确认"之后,那两趟的别处已经有一份了,水位删除这才动得了它们。
    """
    assert _confirm(server, 包["name"], 包["sha256"])[0] == 200
    code, got = _sweep(server, {"free_bytes": 10**9, "apply": True})
    assert code == 200
    assert len(got["deleted"]) == 2
    剩下 = sorted(p.name for p in Path(三趟.runs_root).glob("*/*")
                  if p.is_dir())
    assert 剩下 == [_NEW]


def test_清盘删得掉归档也碰不到基线集(server, 三趟, 包):
    """spec §4.5 的正面断言:**基线集永不参与水位删除。**

    "它在 runs_root 外面所以扫不到"是推理,这条是证据 —— 而这一条要是错了,
    判读会从此静默地每张都变成"无基准",没有任何人会发现。
    """
    ctx = 三趟
    ctx.baselines.mkdir(parents=True, exist_ok=True)
    基线 = ctx.baselines / "P0__front.jpg"
    基线.write_bytes(_JPG)
    assert _confirm(server, 包["name"], 包["sha256"])[0] == 200
    assert len(_sweep(server, {"free_bytes": 10**9, "apply": True})[1]
               ["deleted"]) == 2
    assert 基线.read_bytes() == _JPG
    assert C.get_json(server, "/api/storage")["baselines_bytes"] == len(_JPG)


# ------------------------------------------------------- 基线目录与判读接线

def test_基线和导出都在runs旁边不在里面(ctx, tmp_path):
    """在里面就活在被清扫器一遍遍扫过的目录树里。今天扫不到它们是巧合。"""
    assert ctx.baselines == tmp_path / "baselines"
    assert ctx.exports == tmp_path / "exports"
    assert not ctx.baselines.is_relative_to(ctx.runs_root)
    assert not ctx.exports.is_relative_to(ctx.runs_root)


def test_判读接口把基线目录交下去了(server, 三趟, monkeypatch):
    """基线本身的来回归 ``tests/inspect/test_judge_baselines.py`` 管 ——
    没配密钥时每张照片都判 ``pending``,而 ``pending`` 不回写基线,
    在这一层看不出区别。这里测的是这条线接上了、而且没把判读弄坏。
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run_id = f"巡检二号__{_NEW}"
    got = C.get_json(server, _q(f"/api/runs/{run_id}/judge"), method="POST")
    assert len(got["findings"]) == 2
