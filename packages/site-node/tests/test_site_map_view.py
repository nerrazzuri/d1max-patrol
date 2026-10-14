"""地图显示改进(给客户看的图):建议的显示转角 ``rotation_deg``、平面图 ``plan.png``、预览里带防区
``areas``。只管显示 —— 导航栅格、狗用的坐标一样都不动。

转角的方向照三方约定:把预览图绕中心**在屏幕上顺时针**转 ``rotation_deg`` 度(屏幕 y 朝下;跟 CSS
``rotate(θdeg)`` 一个方向),主要的墙横平竖直。下面的假楼就是照这句话画的:楼在屏幕上本来横平竖直,
画到图上之前先按 CSS ``rotate(−θ)`` 转一下 —— 客户端再按 ``rotate(θ)`` 转回来就正了,所以站点该给 θ。
"""

from __future__ import annotations

import math
import os
import struct
import urllib.error
import urllib.request
import zlib

import numpy as np
import pytest
from test_site_api import MAP, PW, 站

from d1max_site import map_preview as mp

FREE, OCC, UNKNOWN = 1, 0, 2          # grid_ranks 的名次


# ------------------------------------------------------------ 小工具


def _css_rotate(x, y, deg):
    """CSS ``rotate(deg)``:屏幕坐标(y 朝下)里绕原点转,正的是顺时针。"""
    t = math.radians(deg)
    return x * math.cos(t) - y * math.sin(t), x * math.sin(t) + y * math.cos(t)


def _楼(theta: float, n: int = 360, a: float = 110, b: float = 70, *, 杂物: int = 0,
       seed: int = 1) -> np.ndarray:
    """名次栅格(行朝下):外面未知,一栋 2a × 2b 格的长方形楼,一格厚的外墙、两道内墙;
    楼在屏幕上按 CSS ``rotate(θ)`` 转过之后是横平竖直的。``杂物``:楼里随机撒的噪点数,另有
    ``杂物 // 40`` 个随便朝向的 8×4 格家具。"""
    g = np.full((n, n), UNKNOWN, np.uint8)
    rr, cc = np.mgrid[0:n, 0:n]
    ax, ay = _css_rotate(cc + 0.5 - n / 2, rr + 0.5 - n / 2, theta)
    inside = (np.abs(ax) <= a) & (np.abs(ay) <= b)
    g[inside] = FREE
    wall = inside & ((a - np.abs(ax) < 1) | (b - np.abs(ay) < 1))
    wall |= inside & (np.abs(ax - 20) < 0.5) & (ay < 0.4 * b)          # 竖的内墙,下面留门
    wall |= inside & (np.abs(ay) < 0.5) & (ax < -30)                   # 横的内墙
    g[wall] = OCC
    if 杂物:
        rng = np.random.default_rng(seed)
        idx = np.flatnonzero(inside.ravel())
        g.ravel()[rng.choice(idx, 杂物, replace=False)] = OCC
        for _ in range(杂物 // 40):
            cx, cy = rng.uniform(-a + 10, a - 10), rng.uniform(-b + 10, b - 10)
            t = rng.uniform(0, math.pi)
            u = (ax - cx) * math.cos(t) + (ay - cy) * math.sin(t)
            v = -(ax - cx) * math.sin(t) + (ay - cy) * math.cos(t)
            g[(np.abs(u) < 4) & (np.abs(v) < 2)] = OCC
    return g


def _差(got: float, want: float) -> float:
    """转角以 90° 为周期:差多少度。"""
    return abs((got - want + 45.0) % 90.0 - 45.0)


def _pgm(ranks: np.ndarray) -> bytes:
    """名次栅格 → map_server 的 PGM(占据 0、空闲 254、未知 205)。"""
    lut = np.array([0, 254, 205], np.uint8)
    h, w = ranks.shape
    return f"P5\n{w} {h}\n255\n".encode() + lut[ranks].tobytes()


def _png_ga(png: bytes) -> np.ndarray:
    """解 PNG(只认我们编的:8 位灰度 + 透明,不隔行;滤波 0/1/2)→ 数组 (高, 宽, 2)。"""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, kind
        if kind == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data)
            assert (depth, color, interlace) == (8, 4, 0), "要 8 位灰度 + 透明(色彩类型 4)"
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, 1 + 2 * w)
    out = np.zeros((h, 2 * w), np.uint8)
    prev = np.zeros(2 * w, np.uint8)
    for y in range(h):
        ftype, row = raw[y, 0], raw[y, 1:]
        if ftype == 0:
            cur = row.copy()
        elif ftype == 1:                       # Sub:每个字节加左边同一通道的
            cur = (np.cumsum(row.reshape(w, 2).astype(np.int64), axis=0) % 256).astype(
                np.uint8).reshape(-1)
        elif ftype == 2:                       # Up
            cur = row + prev
        else:
            raise AssertionError(f"滤波 {ftype} 测试里没实现")
        out[y] = cur
        prev = cur
    return out.reshape(h, w, 2)


# ------------------------------------------------------------ 转角


@pytest.mark.parametrize("theta", [20.0, -30.0, 44.0, -12.7, 5.0])
def test_转角_照契约的方向_一度以内(theta):
    got = mp.wall_rotation(_楼(theta))
    assert -45.0 <= got < 45.0
    assert _差(got, theta) <= 1.0, f"楼斜了 {theta}°(CSS 转 {theta}° 回正),给的是 {got}°"


def test_转角_顺逆不能反():
    """+20° 的楼和 −20° 的楼要给相反的数:符号反了客户端会越转越斜。"""
    assert mp.wall_rotation(_楼(20.0)) == pytest.approx(20.0, abs=1.0)
    assert mp.wall_rotation(_楼(-20.0)) == pytest.approx(-20.0, abs=1.0)


def test_转角_本来就正的给0():
    assert mp.wall_rotation(_楼(0.0)) == 0.0
    assert mp.wall_rotation(_楼(0.2)) == 0.0, "零点几度不转(转了反而糊)"
    assert mp.wall_rotation(_楼(0.0, 杂物=3000)) == 0.0


@pytest.mark.parametrize("theta", [20.0, -30.0, 44.0])
def test_转角_杂物噪点挪不动它(theta):
    clean = mp.wall_rotation(_楼(theta))
    messy = mp.wall_rotation(_楼(theta, 杂物=3000, seed=7))
    assert _差(messy, theta) <= 1.0, messy
    assert _差(messy, clean) <= 0.5, (clean, messy)


def test_转角_只有一道直墙也行():
    """所有点投到同一个值上(一道正的直墙,投到它的法向)不能出错。"""
    g = np.full((80, 120), FREE, np.uint8)
    g[40, 20:100] = OCC
    assert mp.wall_rotation(g) == 0.0
    rr, cc = np.mgrid[0:120, 0:120]
    ax, ay = _css_rotate(cc + 0.5 - 60, rr + 0.5 - 60, -25.0)
    g = np.full((120, 120), FREE, np.uint8)
    g[(np.abs(ay) < 0.5) & (np.abs(ax) < 50)] = OCC
    assert _差(mp.wall_rotation(g), -25.0) <= 1.0


def test_转角_看不出来给0():
    rng = np.random.default_rng(3)
    noise = np.full((300, 300), FREE, np.uint8)
    noise[rng.random((300, 300)) < 0.03] = OCC
    assert mp.wall_rotation(noise) == 0.0, "只有噪点"
    rr, cc = np.mgrid[0:300, 0:300]
    ring = np.full((300, 300), FREE, np.uint8)
    ring[np.abs(np.hypot(rr - 150, cc - 150) - 120) < 0.7] = OCC
    assert mp.wall_rotation(ring) == 0.0, "圆形场地没有主方向"
    few = np.full((50, 50), FREE, np.uint8)
    few[10, 10:20] = OCC
    assert mp.wall_rotation(few) == 0.0, "墙太少"
    assert mp.wall_rotation(np.full((20, 20), UNKNOWN, np.uint8)) == 0.0


# ------------------------------------------------------------ 平面图


def _plan(ranks):
    png, s = mp.render_plan(ranks)
    return _png_ga(png), s


def _小图():
    """60×40 格:左边 10 列未知,其余空闲;一道横墙(第 20 行,第 15–54 列)、一道竖墙(第 40 列,
    第 5–34 行)、一道 8 连通的 45° 斜墙。"""
    g = np.full((40, 60), FREE, np.uint8)
    g[:, :10] = UNKNOWN
    g[20, 15:55] = OCC
    g[5:35, 40] = OCC
    for i in range(12):
        g[3 + i, 14 + i] = OCC
    return g


def test_平面图_大小_未知透明_地板白_墙黑():
    g = _小图()
    px, s = _plan(g)
    assert 2 <= s <= 4 and s == mp.plan_scale(60, 40)
    assert px.shape == (40 * s, 60 * s, 2), "跟预览一样的范围,像素是预览的 s 倍"

    def at(r, c):                                # 原图格 (行, 列) 的中心落在平面图哪个像素
        return px[int((r + 0.5) * s), int((c + 0.5) * s)]
    assert at(30, 3)[1] == 0 and at(5, 2)[1] == 0 and at(38, 4)[1] == 0, "未知:全透明"
    assert tuple(at(30, 25)) == (255, 255), "空闲:不透明的白(地板)"
    assert tuple(at(10, 50)) == (255, 255)
    for r, c in [(20, 20), (20, 45), (10, 40), (30, 40), (8, 19)]:
        gray, alpha = at(r, c)
        assert alpha == 255 and gray <= 40, f"墙 ({r},{c}) 要黑:{gray},{alpha}"


def _连着(px, pts, s):
    """沿着一串原图格中心(行, 列)走,每一步附近(±1 个平面图像素)都有黑的,墙才算连着。"""
    dark = (px[..., 1].astype(int) * (255 - px[..., 0].astype(int))) >= 255 * 128
    for r, c in pts:
        y, x = int((r + 0.5) * s), int((c + 0.5) * s)
        if not dark[y - 1:y + 2, x - 1:x + 2].any():
            return (r, c)
    return None


def test_平面图_墙是连着的线_不是一格一格():
    g = _小图()
    px, s = _plan(g)
    # 一格一格地走(包括格与格之间),横墙、竖墙、斜墙都不断
    horiz = [(20, c / 4) for c in range(15 * 4, 54 * 4)]
    vert = [(r / 4, 40) for r in range(5 * 4, 34 * 4)]
    diag = [(3 + t / 4, 14 + t / 4) for t in range(0, 11 * 4)]
    for name, pts in (("横墙", horiz), ("竖墙", vert), ("斜墙", diag)):
        assert _连着(px, pts, s) is None, f"{name}在 {_连着(px, pts, s)} 断了"
    # 线是细的:横墙中间那一列,黑的(不透明、灰度 < 128)不超过 2.5 格高
    col = px[:, int(30.5 * s)]
    dark_rows = np.flatnonzero((col[:, 1] == 255) & (col[:, 0] < 128))
    assert 0 < len(dark_rows) <= 2.5 * s, len(dark_rows)
    # 抗锯齿:墙边上有介于黑白之间的灰
    edge = col[int(18 * s):int(23 * s), 0]
    assert ((edge > 20) & (edge < 235)).any(), "墙边要有过渡(抗锯齿),不是一刀切"


def test_平面图_单个噪点抹掉_小洞补上():
    g = np.full((40, 40), FREE, np.uint8)
    g[20, 20] = OCC                               # 孤零零一个占据格(噪点)
    g[10, 10] = UNKNOWN                           # 地板中间一个没扫到的格
    px, s = _plan(g)
    assert px[int(20.5 * s), int(20.5 * s)].tolist() == [255, 255]
    assert px[int(10.5 * s), int(10.5 * s)].tolist() == [255, 255]


def test_平面图_大图缩过的_范围跟预览一样():
    """原图 2500×1000 格:预览缩 3 倍(834×334 像素,比原图多盖 2 格宽、2 格高),平面图要跟预览
    一模一样的范围。一道一格宽的竖墙在原图第 1501 列 → 平面图第 (1501.5 / 3) × s 列。"""
    g = np.full((1000, 2500), FREE, np.uint8)
    g[:, 1501] = OCC
    g[700, :] = OCC
    png, info = mp.render(_pgm(g), mp.parse_map_yaml(
        "image: a.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n"))
    assert (info["width"], info["height"]) == (834, 334)
    px, s = _plan(g)
    assert s == mp.plan_scale(834, 334) and px.shape == (334 * s, 834 * s, 2)
    # 墙线的重心(按黑的程度加权,像素中心在 i + 0.5):要落在预览坐标 × s 上。范围要是按原图
    # (2500×1000 格)拉满,会偏 0.8–0.9 个像素。
    want = 1501.5 / 3 * s
    assert abs(_重心(px[int(100 * s), :], want) - want) <= 0.3
    want = 700.5 / 3 * s
    assert abs(_重心(px[:, int(200 * s)], want) - want) <= 0.3
    assert px[-1, :, 1].max() < 255, "预览多盖出来的那一点(原图外)不是不透明的地板"


def _重心(line, near):
    lo, hi = int(near) - 6, int(near) + 7
    d = line[lo:hi, 1].astype(float) * (255 - line[lo:hi, 0].astype(float))
    assert d.sum() > 0, "这里没有墙"
    return float(((np.arange(lo, hi) + 0.5) * d).sum() / d.sum())


def test_平面图_像素有上限():
    assert mp.plan_scale(1024, 1024) == 2
    assert mp.plan_scale(1024, 300) == 2
    assert mp.plan_scale(600, 400) == 3
    assert mp.plan_scale(331, 161) == 4
    for w, h in [(1024, 1024), (1, 1024), (700, 700), (5, 5)]:
        s = mp.plan_scale(w, h)
        assert 2 <= s <= 4 and max(w, h) * s <= mp.PLAN_MAX_SIDE
        assert w * h * s * s <= mp.PLAN_MAX_PIXELS


# ------------------------------------------------------------ 站点接口


_YAML = "image: yard.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\n"


@pytest.fixture
def 站点(tmp_path):
    from d1max_site.areas import AreaBook
    s = 站(tmp_path, maps={})
    s.accounts.add("gina", PW, role="guard")
    s.api.areas = AreaBook(s.db, now_ms=lambda: 1)
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _raw(s, path, token):
    r = urllib.request.Request(s.api.url + path)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def _导入(s, tmp_path, version, ranks):
    d = tmp_path / f"src-{version}"
    d.mkdir()
    (d / "yard.pgm").write_bytes(_pgm(ranks))
    (d / "yard.yaml").write_text(_YAML)
    s.maps.import_dir(d, map_id=MAP[0], version=version)


def test_预览带转角_平面图地址_防区(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    _导入(s, tmp_path, "30", _楼(20.0))
    s.api.areas.set(MAP[0], "30", "Front lawn", [[0, 0], [2, 0], [2, 1.5]], by="alice")
    s.api.areas.set(MAP[0], "7", "Other version", [[0, 0], [2, 0], [2, 1.5]], by="alice")
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/30/preview", token=gina)
    assert code == 200, d
    assert isinstance(d["rotation_deg"], float) and _差(d["rotation_deg"], 20.0) <= 1.0, d
    assert d["areas"] == [{"name": "Front lawn",
                           "points": [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 0.0},
                                      {"x": 2.0, "y": 1.5}]}], "只给这张图这一版上的,值班的也能看"
    assert d["plan"] == {"url": f"/api/maps/{MAP[0]}/30/plan.png",
                         "scale": mp.plan_scale(d["width"], d["height"])}
    for k in ("width", "height", "m_per_px", "left_x", "top_y", "standby", "homes", "zones"):
        assert k in d, f"老字段 {k} 还在"
    code, ctype, body = _raw(s, d["plan"]["url"], gina)
    assert code == 200 and ctype == "image/png"
    px = _png_ga(body)
    assert px.shape[:2] == (d["height"] * d["plan"]["scale"], d["width"] * d["plan"]["scale"])
    assert px[0, 0, 1] == 0, "角上是楼外(未知):透明"
    assert _raw(s, d["plan"]["url"], None)[0] == 401


def test_平面图接口_没有图404_没有栅格404_坏的500(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    assert _raw(s, f"/api/maps/{MAP[0]}/99/plan.png", gina)[0] == 404
    d = tmp_path / "src-31"
    d.mkdir()
    (d / "cloud.pcd").write_bytes(b"points")
    s.maps.import_dir(d, map_id=MAP[0], version="31")
    assert _raw(s, f"/api/maps/{MAP[0]}/31/plan.png", gina)[0] == 404
    d = tmp_path / "src-32"
    d.mkdir()
    (d / "yard.pgm").write_bytes(b"P5\n4 4\n255\n\x00")
    (d / "yard.yaml").write_text(_YAML)
    s.maps.import_dir(d, map_id=MAP[0], version="32")
    code, _, body = _raw(s, f"/api/maps/{MAP[0]}/32/plan.png", gina)
    assert code == 500 and b"yard.pgm" in body


def test_没开防区的站点_预览也有areas(站点, tmp_path):
    s = 站点
    s.api.areas = None
    _导入(s, tmp_path, "33", _小图())
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/33/preview", token=_登(s, "gina"))
    assert code == 200 and d["areas"] == [] and d["rotation_deg"] == 0.0, d


def _数着(monkeypatch, name):
    n = []
    real = getattr(mp, name)

    def counted(*a, **k):
        n.append(1)
        return real(*a, **k)
    monkeypatch.setattr(mp, name, counted)
    return n


def test_转角和平面图_一版只算一次_文件换了重算(站点, tmp_path, monkeypatch):
    s = 站点
    gina = _登(s, "gina")
    _导入(s, tmp_path, "34", _楼(20.0))
    rot = _数着(monkeypatch, "wall_rotation")
    plan = _数着(monkeypatch, "render_plan")
    for _ in range(3):
        assert s.req("GET", f"/api/maps/{MAP[0]}/34/preview", token=gina)[0] == 200
    assert _raw(s, f"/api/maps/{MAP[0]}/34/preview.png", gina)[0] == 200
    first = [_raw(s, f"/api/maps/{MAP[0]}/34/plan.png", gina)[2] for _ in range(3)]
    assert (len(rot), len(plan)) == (1, 1), "不是每次请求都重算"
    assert first[0] == first[1] == first[2]
    # 有人在盘上把栅格换了(同一版本号):大小、修改时刻变了,重算
    pgm = s.maps.root / MAP[0] / "34" / "yard.pgm"
    pgm.write_bytes(_pgm(_楼(-30.0)))
    st = pgm.stat()
    os.utime(pgm, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    d = s.req("GET", f"/api/maps/{MAP[0]}/34/preview", token=gina)[1]
    assert _差(d["rotation_deg"], -30.0) <= 1.0 and len(rot) == 2, d["rotation_deg"]
    again = _raw(s, f"/api/maps/{MAP[0]}/34/plan.png", gina)[2]
    assert len(plan) == 2 and again != first[0]
    assert _raw(s, f"/api/maps/{MAP[0]}/34/plan.png", gina)[2] == again and len(plan) == 2
