"""存好的地图读成占据栅格,以及 ``/api/maps/<id>/grid``。

重点在两件事上:

* **阈值和行序。** 这两个错了,画出来的图仍然"像一张图" —— 人不会怀疑它,
  直到照着它标的点全部偏掉。所以这里的断言全部落在具体某一格上。
* **取不到存好的图时的退路。** 建图还没存盘的时候,页面上要能看见正在长的
  那张;而两种来源必须能区分,不然人会以为草稿是成品。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from d1max_patrol.app.gridmap import (
    GridError,
    from_frame,
    load_saved,
    read_pgm,
)
from d1max_patrol.protocol.map_frames import MapFrame, decode_rle
from tests.app import conftest as C


def get_json(server, path: str, **kwargs):
    """URL 里的中文得先转义 —— urllib 的请求行是纯 ASCII 的。"""
    return C.get_json(server, quote(path, safe="/.%"), **kwargs)


def get_err(server, path: str, expect: int, **kwargs):
    return C.get_err(server, quote(path, safe="/.%"), expect, **kwargs)


#: 一张 2x2 的图,像素从左上角开始按行排:
#: 白(空地) 黑(障碍) / 灰(未知) 白(空地)
_PIXELS = bytes([255, 0, 128, 255])


def _write_map(maps_dir: Path, map_id: str = "map_test", *,
               pixels: bytes = _PIXELS, width: int = 2, height: int = 2,
               **extra) -> Path:
    """摆一对 ``map_saver_cli`` 那样的 pgm/yaml。"""
    maps_dir.mkdir(parents=True, exist_ok=True)
    pgm = maps_dir / f"{map_id}.pgm"
    pgm.write_bytes(f"P5\n{width} {height}\n255\n".encode("ascii") + pixels)
    meta = {"image": pgm.name, "resolution": 0.05,
            "origin": [-1.0, -2.0, 0.0], "negate": 0,
            "occupied_thresh": 0.65, "free_thresh": 0.196}
    meta.update(extra)
    (maps_dir / f"{map_id}.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
    return maps_dir / f"{map_id}.yaml"


@pytest.fixture
def maps_dir(tmp_path: Path) -> Path:
    _write_map(tmp_path / "maps")
    return tmp_path / "maps"


# ------------------------------------------------------------------ 读 pgm


def test_读得出宽高和像素(tmp_path):
    _write_map(tmp_path)
    assert read_pgm(tmp_path / "map_test.pgm") == (2, 2, _PIXELS)


def test_头里的注释不算数(tmp_path):
    """有些工具会在 P5 后面塞一行 ``# CREATOR: ...``。"""
    path = tmp_path / "有注释.pgm"
    path.write_bytes(b"P5\n# CREATOR: map_saver\n2 2\n255\n" + _PIXELS)
    assert read_pgm(path)[:2] == (2, 2)


def test_换行和空格等价(tmp_path):
    path = tmp_path / "一行头.pgm"
    path.write_bytes(b"P5 2 2 255 " + _PIXELS)
    assert read_pgm(path)[:2] == (2, 2)


def test_不是二进制pgm就直说(tmp_path):
    path = tmp_path / "文本.pgm"
    path.write_bytes(b"P2\n2 2\n255\n255 0 128 255\n")
    with pytest.raises(GridError, match="不是二进制"):
        read_pgm(path)


def test_灰度不是255就直说(tmp_path):
    path = tmp_path / "怪.pgm"
    path.write_bytes(b"P5\n2 2\n65535\n" + _PIXELS)
    with pytest.raises(GridError, match="255"):
        read_pgm(path)


def test_像素少了就直说不猜(tmp_path):
    """少了几个像素还照读,画出来的图会整体错位。"""
    path = tmp_path / "缺.pgm"
    path.write_bytes(b"P5\n2 2\n255\n" + bytes([255, 0]))
    with pytest.raises(GridError, match="实际只有"):
        read_pgm(path)


def test_文件不在就直说(tmp_path):
    with pytest.raises(GridError, match="读不了"):
        read_pgm(tmp_path / "没有这个.pgm")


# ------------------------------------------------------------ 读整张地图


def test_元数据原样带出来(maps_dir):
    grid = load_saved(maps_dir, "map_test")
    assert (grid.width, grid.height, grid.resolution) == (2, 2, 0.05)
    assert (grid.origin_x, grid.origin_y, grid.origin_yaw) == (-1.0, -2.0, 0.0)
    assert grid.source == "saved"


def test_行序翻过来了(maps_dir):
    """pgm 第一行在最上面,栅格第一行在最下面(y 最小)。

    图像是 白 黑 / 灰 白,翻过来第一行就是"灰 白"—— 未知在前。
    """
    assert load_saved(maps_dir, "map_test").cells == (-1, 0, 0, 100)


def test_中间那段是未知不是半占据(maps_dir):
    """未知画成 50,页面上看起来像整张图被墙围住。"""
    assert -1 in load_saved(maps_dir, "map_test").cells
    assert 50 not in load_saved(maps_dir, "map_test").cells


def test_阈值听yaml的(tmp_path):
    """把 free_thresh 抬到 0.6,原来的空地(shade=0)还是空地,
    但灰(shade≈0.498)从未知变成了空地 —— 证明读的确实是文件里那个数。"""
    _write_map(tmp_path / "m", free_thresh=0.6)
    assert load_saved(tmp_path / "m", "map_test").cells == (0, 0, 0, 100)


def test_negate反过来读(tmp_path):
    _write_map(tmp_path / "m", negate=1)
    #: 反过来之后白是障碍、黑是空地
    assert load_saved(tmp_path / "m", "map_test").cells == (-1, 100, 100, 0)


def test_图片路径是相对yaml的(tmp_path):
    """地图目录整个拷到别的机器上还得能读。"""
    _write_map(tmp_path / "m")
    assert load_saved(tmp_path / "m", "map_test").width == 2


def test_没有这张图就直说(tmp_path):
    with pytest.raises(GridError, match="没有存好的图"):
        load_saved(tmp_path, "不存在")


def test_yaml缺字段就说缺哪个(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "x.yaml").write_text("image: x.pgm\n", encoding="utf-8")
    with pytest.raises(GridError, match="resolution"):
        load_saved(d, "x")


def test_origin不成样子就直说(tmp_path):
    _write_map(tmp_path / "m", origin=[1.0])
    with pytest.raises(GridError, match="origin"):
        load_saved(tmp_path / "m", "map_test")


def test_编码之后解得回来(maps_dir):
    """页面上只有一份解码逻辑,它解的就是这个串。"""
    grid = load_saved(maps_dir, "map_test")
    wire = grid.to_wire()
    assert decode_rle(wire["rle"], expect=4) == grid.cells


# -------------------------------------------------------------- 桥上那一帧


def test_桥上那帧标成live():
    frame = MapFrame(width=1, height=2, resolution=0.1, origin_x=0.0,
                     origin_y=0.0, origin_yaw=0.0, data=(0, 100), ts_ms=0)
    grid = from_frame(frame)
    assert grid.source == "live"
    assert grid.cells == (0, 100)


# ------------------------------------------------------------------ 接口


def test_接口给得出栅格(server, ctx):
    _write_map(ctx.mapping.maps_dir)
    got = get_json(server, "/api/maps/map_test/grid")
    assert got["width"] == 2
    assert got["source"] == "saved"
    assert decode_rle(got["rle"], expect=4) == (-1, 0, 0, 100)


def test_没存盘就退回桥上正在长的那张(server, ctx):
    ctx.maps.latest = MapFrame(width=1, height=1, resolution=0.1, origin_x=0.0,
                               origin_y=0.0, origin_yaw=0.0, data=(100,),
                               ts_ms=0)
    got = get_json(server, "/api/maps/还没存的图/grid")
    assert got["source"] == "live", "页面得知道这张图还在长"


def test_两头都没有就是404(server):
    got = get_err(server, "/api/maps/没有这张/grid", 404)
    assert got["error"]


def test_图名里的斜杠进不来(server):
    """图名会被拼进路径,越界得在路由这一层就挡掉。"""
    assert get_err(server, "/api/maps/..%2F..%2Fetc/grid", 404)


def test_两条来源的线格式长得一模一样(maps_dir):
    """页面上只有一份解码器,盘上读的和桥上推的必须是同一种形状。

    多出或少一个字段,页面就得为其中一条来源开一个特例 —— 而那个特例只有
    在"建图刚跑完还没存盘"这个窄窗口里才走得到,平时测不着。
    """
    saved = load_saved(maps_dir, "map_test").to_wire()
    live = from_frame(MapFrame(width=2, height=2, resolution=0.05, origin_x=0.0,
                               origin_y=0.0, origin_yaw=0.0,
                               data=(-1, 0, 0, 100), ts_ms=0)).to_wire()
    assert saved.keys() == live.keys()
    assert saved["rle"] == live["rle"]        # 同样的格子编出同样的串
    assert saved["source"] != live["source"]  # 但来源必须分得开
