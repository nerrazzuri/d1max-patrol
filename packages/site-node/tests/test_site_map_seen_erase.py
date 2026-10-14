"""地图台账 M9、44、M7(用户 2026-10-14):站点画图时

- **「看到过、狗没走过」**(``seen.pbm``,建图时只透过玻璃、门口看到过、被定成「不知道」的格):平面图上
  画成淡淡的地面(白、半透明),栅格预览上是介于地面和没建图之间的灰;预览 JSON 带 ``seen: true``
  (客户端据此在图例里加一行)。规划、狗不读它。
- **抹掉区**(区域种类 ``erase``):那一块在预览、平面图上都抹成没建图(墙也不画),所有客户端不用自己管。
- **有内容的范围** ``content_bbox``(预览像素 ``[x0, y0, x1, y1]``):地面 + 墙 + 「看到过」,去掉
  抹掉的;客户端「全图」按它缩放,不把四周的空白算进去。
"""

from __future__ import annotations

import urllib.error
import urllib.request

import numpy as np
import pytest
from test_site_api import MAP, PW, 站
from test_site_map_view import _pgm, _png_ga

from d1max_site import map_preview as mp

FREE, OCC, UNKNOWN = 1, 0, 2
_YAML = "image: yard.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\n"
META = mp.parse_map_yaml(_YAML)


def _pbm(mask: np.ndarray) -> bytes:
    h, w = mask.shape
    return f"P4\n{w} {h}\n".encode() + np.packbits(mask.astype(bool), axis=1).tobytes()


def _gray_rows(png: bytes) -> np.ndarray:
    """解栅格预览(8 位灰度、不滤波)→ 数组 (高, 宽)。"""
    import struct
    import zlib
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, w + 1)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:]


def _屋():
    """40 × 60 格(行朝下):左上一间屋(行 5–24、列 5–29)四面墙、里面是地面;屋外右边一片「看到过」
    (行 8–19、列 31–49,透过玻璃看到的);其余没建图。"""
    g = np.full((40, 60), UNKNOWN, np.uint8)
    g[5:25, 5:30] = FREE
    g[5, 5:30] = g[24, 5:30] = OCC
    g[5:25, 5] = g[5:25, 29] = OCC
    seen = np.zeros(g.shape, bool)
    seen[8:20, 31:50] = True
    return g, seen


# ------------------------------------------------------------ 读掩码


def test_掩码_P4_大小要跟栅格一样_坏的不认():
    _, seen = _屋()
    got = mp.parse_seen(_pbm(seen), 60, 40)
    assert got.shape == (40, 60) and (got == seen).all()
    for bad in (_pbm(seen)[:-1], _pbm(seen) + b"\x00" * 64, b"P5\n60 40\n" + bytes(300),
                b"P4\n60\n", b""):
        with pytest.raises(mp.PreviewError):
            mp.parse_seen(bad, 60, 40)
    with pytest.raises(mp.PreviewError, match="不一样大"):
        mp.parse_seen(_pbm(seen), 61, 40)
    assert mp.seen_limit(60, 40) == len(b"P4\n60 40\n") + 8 * 40 + 16


# ------------------------------------------------------------ 栅格预览、范围


def test_预览_看到过的是浅灰_抹掉的是没建图_范围按有内容的算():
    g, seen = _屋()
    png, info = mp.render(_pgm(g), META)
    assert info["content_bbox"] == [5.0, 5.0, 30.0, 25.0], "没有掩码:只算地面和墙"
    assert "seen" not in info
    png, info = mp.render(_pgm(g), META, seen=seen)
    px = _gray_rows(png)
    assert px[10, 40] == mp.SEEN_GRAY and px[10, 10] == 254 and px[5, 10] == 0
    assert px[30, 40] == 205 and 205 < mp.SEEN_GRAY < 254
    assert info["seen"] is True and info["content_bbox"] == [5.0, 5.0, 50.0, 25.0]
    erase = np.zeros(g.shape, bool)
    erase[:, 25:] = True                                 # 屋子右边一截(含右墙)+ 外面那一片全抹掉
    png, info = mp.render(_pgm(g), META, seen=seen, erase=erase)
    px = _gray_rows(png)
    assert px[10, 27] == 205 and px[10, 29] == 205 and px[10, 40] == 205, "抹掉的:没建图,墙也不画"
    assert px[10, 10] == 254 and px[5, 10] == 0
    assert "seen" not in info, "看到过的全被抹掉了:不报"
    assert info["content_bbox"] == [5.0, 5.0, 25.0, 25.0]
    png, info = mp.render(_pgm(np.full((5, 5), UNKNOWN, np.uint8)), META)
    assert "content_bbox" not in info, "什么都没有:不给范围"


def test_范围_大图缩过的按预览像素算():
    g = np.full((1000, 2500), UNKNOWN, np.uint8)
    g[100:200, 1500:1800] = FREE
    seen = np.zeros(g.shape, bool)
    seen[300:301, 1200:1201] = True
    _, info = mp.render(_pgm(g), META, seen=seen)
    assert (info["width"], info["height"]) == (834, 334)
    assert info["content_bbox"] == pytest.approx([1200 / 3, 100 / 3, 1800 / 3, 301 / 3], abs=1e-3)


# ------------------------------------------------------------ 平面图


def test_平面图_看到过的是淡淡的地面_抹掉的透明_墙也不画():
    g, seen = _屋()
    px, s = _plan(g)
    assert px[_at(s, 12, 40)][1] == 0, "没有掩码:屋外透明"
    px, s = _plan(g, seen=seen)
    gray, alpha = px[_at(s, 12, 40)]
    assert gray == 255 and abs(int(alpha) - round(mp.PLAN_SEEN_ALPHA * 255)) <= 2, (gray, alpha)
    assert tuple(px[_at(s, 15, 15)]) == (255, 255), "地面照旧不透明的白"
    assert px[_at(s, 32, 40)][1] == 0, "没建图的照旧透明"
    gray, alpha = px[_at(s, 5, 15)]
    assert alpha == 255 and gray <= 40, "墙照旧黑"
    erase = np.zeros(g.shape, bool)
    erase[:, 25:] = True
    px, s = _plan(g, seen=seen, erase=erase)
    for r, c in ((12, 40), (15, 27), (15, 29), (5, 27)):
        assert px[_at(s, r, c)][1] == 0, ("抹掉的地方透明", r, c)
    assert tuple(px[_at(s, 15, 15)]) == (255, 255)


def _plan(g, **kw):
    png, s = mp.render_plan(g, **kw)
    return _png_ga(png), s


def _at(s, r, c):
    return int((r + 0.5) * s), int((c + 0.5) * s)


def test_抹掉区_多边形压到的格_格心在里面才算():
    """地图坐标的多边形 → 栅格上要抹的格(行朝下);格心在多边形里才算(只管显示,不往外扩)。"""
    m = mp.erase_mask((((-9.0, 0.0), (-8.0, 0.0), (-8.0, -4.0), (-9.0, -4.0)),), 60, 40, META)
    # 列:x = -10 + (c + 0.5)·0.05 ∈ (-9, -8) → c 20..39;
    # 行:y = -5 + (39.5 − r)·0.05 ∈ (-4, 0) → r 0..19(最上一行 y = −3.025 < 0;y > −4 → r ≤ 19)
    rows, cols = np.nonzero(m)
    assert (cols.min(), cols.max()) == (20, 39) and (rows.min(), rows.max()) == (0, 19)
    assert m.sum() == 20 * 20
    assert not mp.erase_mask((), 60, 40, META).any()


# ------------------------------------------------------------ 站点接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, maps={})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _raw(s, path, token):
    r = urllib.request.Request(s.api.url + path)
    r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _导入(s, tmp_path, version, g, seen=None, seen_bytes=None):
    d = tmp_path / f"src-{version}"
    d.mkdir()
    (d / "yard.pgm").write_bytes(_pgm(g))
    (d / "yard.yaml").write_text(_YAML)
    if seen is not None or seen_bytes is not None:
        (d / "seen.pbm").write_bytes(seen_bytes if seen_bytes is not None else _pbm(seen))
    s.maps.import_dir(d, map_id=MAP[0], version=version)


#: 抹掉区:屋子右边那一截和外面(地图坐标:x ≥ −8.75 即列 ≥ 25)
JUNK = {"id": "junk", "kind": "erase", "label": "抹掉",
        "polygon": [[-8.75, -6.0], [-6.0, -6.0], [-6.0, 0.0], [-8.75, 0.0]]}


def test_接口_预览带看到过和范围_抹掉区抹掉预览和平面图_平面图地址跟着变(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    g, seen = _屋()
    _导入(s, tmp_path, "40", g, seen)
    base = f"/api/maps/{MAP[0]}/40"
    code, d = s.req("GET", base + "/preview", token=gina)
    assert code == 200 and d["seen"] is True and d["content_bbox"] == [5.0, 5.0, 50.0, 25.0], d
    assert d["plan"]["url"] == base + "/plan.png", "没有抹掉区:地址跟原来一样"
    code, body = _raw(s, d["plan"]["url"], gina)
    px, sc = _png_ga(body), d["plan"]["scale"]
    assert code == 200 and 0 < px[_at(sc, 12, 40)][1] < 255, "看到过的:淡淡的"
    # 管理员画一块抹掉区(收紧,当场生效)
    s.disp.zones.put(MAP[0], "40", [JUNK], base_revision=0, by="alice")
    code, d2 = s.req("GET", base + "/preview", token=gina)
    assert code == 200 and "seen" not in d2 and d2["content_bbox"] == [5.0, 5.0, 25.0, 25.0], d2
    assert d2["plan"]["url"].startswith(base + "/plan.png?e="), "抹掉区变了:地址变(客户端重新取图)"
    assert [z["kind"] for z in d2["zones"]] == ["erase"]
    code, body = _raw(s, d2["plan"]["url"], gina)
    px = _png_ga(body)
    assert code == 200 and px[_at(sc, 12, 40)][1] == 0 and px[_at(sc, 15, 29)][1] == 0
    assert tuple(px[_at(sc, 15, 15)]) == (255, 255)
    code, body = _raw(s, base + "/preview.png", gina)
    assert code == 200 and _gray_rows(body)[12, 40] == 205 and _gray_rows(body)[15, 29] == 205
    # 删掉抹掉区:回到原样
    s.disp.zones.put(MAP[0], "40", [], base_revision=1, by="alice")
    code, d3 = s.req("GET", base + "/preview", token=gina)
    assert d3["plan"]["url"] == base + "/plan.png" and d3["content_bbox"] == d["content_bbox"]


def test_接口_没有掩码的老版本照旧_掩码坏了不认也不挡(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    g, seen = _屋()
    _导入(s, tmp_path, "41", g)
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/41/preview", token=gina)
    assert code == 200 and "seen" not in d and d["content_bbox"] == [5.0, 5.0, 30.0, 25.0]
    _导入(s, tmp_path, "42", g, seen_bytes=_pbm(seen[:, :59]))      # 大小跟栅格对不上
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/42/preview", token=gina)
    assert code == 200 and "seen" not in d, d
    assert _raw(s, f"/api/maps/{MAP[0]}/42/plan.png", gina)[0] == 200
    _导入(s, tmp_path, "43", g, seen_bytes=_pbm(seen) + bytes(10_000))   # 比该有的大:不读
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/43/preview", token=gina)
    assert code == 200 and "seen" not in d


def test_接口_同一份抹掉区只渲染一次_改了重渲(站点, tmp_path, monkeypatch):
    s = 站点
    gina = _登(s, "gina")
    g, seen = _屋()
    _导入(s, tmp_path, "44", g, seen)
    n = []
    real = mp.render_plan

    def counted(*a, **k):
        n.append(1)
        return real(*a, **k)
    monkeypatch.setattr(mp, "render_plan", counted)
    url = s.req("GET", f"/api/maps/{MAP[0]}/44/preview", token=gina)[1]["plan"]["url"]
    for _ in range(3):
        assert _raw(s, url, gina)[0] == 200
    assert len(n) == 1
    s.disp.zones.put(MAP[0], "44", [JUNK], base_revision=0, by="alice")
    url2 = s.req("GET", f"/api/maps/{MAP[0]}/44/preview", token=gina)[1]["plan"]["url"]
    for _ in range(2):
        assert _raw(s, url2, gina)[0] == 200
    assert len(n) == 2 and url2 != url
