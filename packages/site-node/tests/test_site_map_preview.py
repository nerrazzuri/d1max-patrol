"""建好的图的预览(W00c6h):站点把登记了的栅格图(``.pgm`` + ``.yaml``)渲染成 PNG 给手机看,连同这张图上
登记的待命点。站点主机上没有 PIL:纯 Python 解 PGM、编 PNG。"""

from __future__ import annotations

import json
import struct
import urllib.error
import urllib.request
import zlib

import pytest
from test_site_api import MAP, PW, 站

from d1max_site.map_preview import (
    MAX_SIDE,
    NoRaster,
    PreviewError,
    parse_map_yaml,
    parse_pgm,
    render,
)


def _pgm(w, h, pixels: bytes, *, magic=b"P5", comment=True) -> bytes:
    head = magic + b"\n" + (b"# CREATOR: map_saver\n" if comment else b"") + \
        f"{w} {h}\n255\n".encode()
    return head + pixels


def _png_rows(png: bytes) -> tuple[int, int, list[bytes]]:
    """解我们自己编的 PNG(灰度 8 位、不滤波):宽、高、每行像素。"""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, kind
        if kind == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", data[:10])
            assert (depth, color) == (8, 0)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[i * (w + 1):(i + 1) * (w + 1)] for i in range(h)]
    assert all(r[0] == 0 for r in rows)
    return w, h, [r[1:] for r in rows]


_YAML = """image: yard.pgm
resolution: 0.050000
origin: [-10.0, -5.0, 0.0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""


def test_三种格子_颜色_坐标换算():
    # 4×2:占据(0)、空闲(254)、未知(205)、再一个占据;第二行全空闲。
    img = _pgm(4, 2, bytes([0, 254, 205, 0]) + bytes([254] * 4))
    png, meta = render(img, parse_map_yaml(_YAML))
    w, h, rows = _png_rows(png)
    assert (w, h) == (4, 2)
    assert rows[0] == bytes([0, 254, 205, 0]) and rows[1] == bytes([254] * 4)
    assert meta == {"width": 4, "height": 2, "m_per_px": 0.05, "left_x": -10.0,
                    "top_y": -5.0 + 2 * 0.05}


def test_反色_ascii的pgm_注释():
    y = parse_map_yaml(_YAML.replace("negate: 0", "negate: 1"))
    img = _pgm(2, 1, b"0 255\n", magic=b"P2")
    png, _ = render(img, y)
    assert _png_rows(png)[2][0] == bytes([254, 0]), "negate:0 是空闲、255 是占据"


def test_大图缩到最大边_细墙不丢():
    w, h = 3000, 1200
    rows = [bytearray([254] * w) for _ in range(h)]
    for r in rows:
        r[1501] = 0                                            # 一像素宽的竖墙
    rows[601] = bytearray([0] * w)                             # 一像素宽的横墙(不在每格的第一行)
    png, meta = render(_pgm(w, h, b"".join(rows)), parse_map_yaml(_YAML))
    pw, ph, out = _png_rows(png)
    assert max(pw, ph) <= MAX_SIDE and pw == -(-w // 3) and ph == h // 3
    assert all(0 in r for r in out), "缩小之后竖墙还在(一格里有占据就算占据)"
    assert out[200] == bytes(pw), "横墙也在(一格的几行合起来看)"
    assert meta["m_per_px"] == pytest.approx(0.15)
    assert meta["top_y"] == pytest.approx(-5.0 + h * 0.05)


def test_坏的pgm_没有栅格():
    y = parse_map_yaml(_YAML)
    with pytest.raises(PreviewError, match="灰度"):
        render(b"P6\n2 2\n255\n" + bytes(12), y)              # 彩色的不认
    with pytest.raises(PreviewError):
        render(_pgm(4, 4, bytes(3)), y)                       # 短了
    with pytest.raises(PreviewError, match="太大"):
        render(b"P5\n99999 99999\n255\n", y)                  # 太大:先看头,不去读
    with pytest.raises(PreviewError):
        parse_map_yaml("image: a.pgm\n")                     # 没分辨率
    with pytest.raises(PreviewError):
        parse_map_yaml(_YAML.replace("resolution: 0.050000", "resolution: -1"))
    assert issubclass(NoRaster, PreviewError)
    got = parse_pgm(_pgm(2, 2, bytes(4), comment=False))
    assert got[:2] == (2, 2)


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
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def _导入(s, tmp_path, version, files):
    d = tmp_path / f"src-{version}"
    d.mkdir()
    for n, b in files.items():
        (d / n).write_bytes(b)
    s.maps.import_dir(d, map_id=MAP[0], version=version)


def test_看图_谁都能看_待命点一起给(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    _导入(s, tmp_path, "8", {"yard.pgm": _pgm(4, 2, bytes([0, 254, 205, 0] + [254] * 4)),
                             "yard.yaml": _YAML.encode()})
    with s.db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A', 'dock', ?, '8', 1.5, 2.0, 0.5, 1)", (MAP[0],))
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A', 'old', ?, '7', 9, 9, 0, 0)", (MAP[0],))
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/8/preview", token=gina)
    assert code == 200 and (d["width"], d["height"]) == (4, 2), d
    assert d["standby"] == [{"robot_id": "A", "name": "dock", "x": 1.5, "y": 2.0, "yaw": 0.5,
                             "default": True}], "只给这张图这一版上的"
    code, ctype, body = _raw(s, f"/api/maps/{MAP[0]}/8/preview.png", gina)
    assert code == 200 and ctype == "image/png"
    assert _png_rows(body)[:2] == (4, 2)
    assert _raw(s, f"/api/maps/{MAP[0]}/8/preview.png", None)[0] == 401


def test_没有这张图_404_没有栅格_404_坏的_500(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    assert s.req("GET", f"/api/maps/{MAP[0]}/99/preview", token=gina)[0] == 404
    _导入(s, tmp_path, "9", {"cloud.pcd": b"points"})
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/9/preview", token=gina)
    assert code == 404 and "栅格" in d["error"], d
    _导入(s, tmp_path, "10", {"yard.pgm": b"P5\n4 4\n255\n\x00", "yard.yaml": _YAML.encode()})
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/10/preview", token=gina)
    assert code == 500 and "yard.pgm" in d["error"], d
    code, _, body = _raw(s, f"/api/maps/{MAP[0]}/10/preview.png", gina)
    assert code == 500 and "yard.pgm" in json.loads(body)["error"]


def test_同一版只渲染一次(站点, tmp_path, monkeypatch):
    import d1max_site.map_preview as mp
    s = 站点
    gina = _登(s, "gina")
    _导入(s, tmp_path, "11", {"yard.pgm": _pgm(2, 1, bytes([0, 254])),
                              "yard.yaml": _YAML.encode()})
    n = []
    real = mp.render

    def 数着(*a, **k):
        n.append(1)
        return real(*a, **k)
    monkeypatch.setattr(mp, "render", 数着)
    for _ in range(3):
        assert s.req("GET", f"/api/maps/{MAP[0]}/11/preview", token=gina)[0] == 200
    assert _raw(s, f"/api/maps/{MAP[0]}/11/preview.png", gina)[0] == 200
    assert len(n) == 1, "图一版登记了就不变:渲染一次缓存着"
