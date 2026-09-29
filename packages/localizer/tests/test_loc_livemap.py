"""建图时的实时预览(W09f):逐帧累加的俯视栅格、点云配位姿、写盘。合成数据:MOLA 系就是起点的
雷达系,雷达 X 朝上、Z 朝前(跟 RS-Airy 一样),雷达离地 0.5 m;平面系 x = 朝前(世界 Z)、
y = 上 × 前(世界 −Y)。"""

from __future__ import annotations

import base64
import json
import math
import struct
import types
import zlib

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer import livemap as L  # noqa: E402

H = 0.5                                    # 雷达离地
I3 = np.eye(3)


def _rot_up(yaw):
    """绕「上」(世界 X)转 ``yaw``:平面里朝前(世界 Z)转向平面 y(世界 −Y)。"""
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)      # 前(Z)→ c·Z − s·Y


def _world(px, py, h):
    """平面坐标 (px, py)、离地 h → MOLA 系(上 = X,平面 x = Z,平面 y = −Y)。"""
    return np.array([h - H, -py, px], float)


def _scene(origin_plane=(0.0, 0.0), *, wall_x=5.05, floor=True, wall=True, up_sign=1.0):
    """狗在 origin_plane:周围 4 m 见方的地面、平面 x = wall_x 处一堵 1.5 m 高的墙(y ∈ [−2, 2])。
    ``up_sign`` = −1:整个世界上下翻(「上」其实是 −X)。回 (雷达位置, 点)。"""
    ox, oy = origin_plane
    pts = []
    if floor:
        for x in np.arange(ox - 2, ox + 2, 0.05):
            for y in np.arange(oy - 2, oy + 2, 0.05):
                pts.append(_world(x, y, 0.0))
    if wall:
        for y in np.arange(-2, 2, 0.05):
            for h in np.arange(0.1, 1.5, 0.05):
                pts.append(_world(wall_x, y, h))
    o = _world(ox, oy, H)
    pts = np.array(pts)
    if up_sign < 0:
        flip = np.diag([-1.0, 1.0, 1.0])
        pts, o = pts @ flip, flip @ o
    return o, pts


def _cell(img, meta, px, py):
    res, (x0, y0) = meta["res"], meta["origin"]
    c = int(math.floor((px - x0) / res))
    r = meta["height"] - 1 - int(math.floor((py - y0) / res))
    if not (0 <= r < img.shape[0] and 0 <= c < img.shape[1]):
        return L.UNKNOWN                              # 图外:没画过
    return img[r, c]


def _feed(g, n, **kw):
    for _ in range(n):
        o, pts = _scene(**kw)
        g.add(o, I3, pts)


def test_地面白_墙黑_上的正负号按地面在雷达下面定():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES)
    img, meta = g.render()
    assert g.frames == L.UP_FRAMES                   # 定号之前的几帧也画进去了
    assert _cell(img, meta, 1.0, 0.5) == L.FREE
    assert _cell(img, meta, 5.05, 0.5) == L.OCC
    assert meta["res"] == pytest.approx(L.RES_M)
    assert np.allclose(g.up, [1, 0, 0])


def test_装法上的上给反了_按地面翻过来_墙还是墙():
    g = L.LiveGrid()
    for _ in range(L.UP_FRAMES):
        o, pts = _scene(up_sign=-1.0)
        g.add(o, I3, pts)
    img, meta = g.render()
    assert np.allclose(g.up, [-1, 0, 0])
    # 翻了之后平面 y 也跟着翻(上 × 前),墙还在前面 5 m
    assert _cell(img, meta, 5.05, 0.0) == L.OCC
    assert _cell(img, meta, 1.0, 0.0) == L.FREE


def test_定号之前不画_不出图():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES - 1)
    assert g.render() is None
    assert g.frames == 0


def test_障碍一格累计_3_个点才画_两个不够_地面一个就算():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES, wall=False)
    o, pts = _scene(wall=False)
    one = np.array([_world(3.05, 0.05, 1.0)])         # 地面外的一格:离地 1 m 的一个点
    for _ in range(L.OBST_HITS - 1):
        g.add(o, I3, np.vstack([pts, one]))
    img, meta = g.render()
    assert _cell(img, meta, 3.05, 0.05) == L.UNKNOWN
    assert _cell(img, meta, 1.05, 0.05) == L.FREE
    g.add(o, I3, np.vstack([pts, one]))
    img, meta = g.render()
    assert _cell(img, meta, 3.05, 0.05) == L.OCC


def test_太近太远的点不画_离地不在两段里的不画():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES, wall=False)
    o, _ = _scene()
    near = np.array([_world(0.3, 0.0, 1.0)] * 5)       # 0.3 m:狗自己
    far = np.array([_world(30.0, 0.0, 1.0)] * 5)       # 30 m
    mid = np.array([_world(3.0, 0.0, 0.3)] * 5)        # 离地 0.3 m:不是地面也不是障碍
    high = np.array([_world(3.0, 1.0, 2.5)] * 5)       # 2.5 m:树冠、天花板
    for _ in range(L.OBST_HITS):
        g.add(o, I3, np.vstack([near, far, mid, high]))
    img, meta = g.render()
    assert _cell(img, meta, 0.3, 0.0) != L.OCC
    assert _cell(img, meta, 3.0, 0.0) == L.UNKNOWN
    assert _cell(img, meta, 3.0, 1.0) == L.UNKNOWN
    assert meta["origin"][0] + meta["width"] * meta["res"] < 10    # 30 m 那个没把画幅撑大


def test_离地按这一帧雷达的高度算_上坡的地面不当障碍():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES, wall=False)
    # 狗爬上 1 m 高的坡顶:这一帧的地面在世界里高 1 m,按这帧雷达算还是离地 0
    o, pts = _scene((10.0, 0.0), wall=False)
    lift = np.array([1.0, 0.0, 0.0])
    for _ in range(L.OBST_HITS):
        g.add(o + lift, I3, pts + lift)
    img, meta = g.render()
    assert _cell(img, meta, 10.5, 0.5) == L.FREE


def test_画幅超了整体粗一倍_原来画上的还在():
    g = L.LiveGrid(max_side=64)                      # 0.1 m × 64 = 6.4 m
    _feed(g, L.UP_FRAMES)
    for k in range(1, 5):                             # 往前走 8 m:再粗一倍到 0.2,还不够再粗到 0.4
        o, pts = _scene((2.0 * k, 0.0), wall=False)
        g.add(o, I3, pts)
    img, meta = g.render()
    assert meta["res"] in (0.2, 0.4)
    assert max(img.shape) <= 64
    assert _cell(img, meta, 5.05, 0.5) == L.OCC
    assert _cell(img, meta, 9.0, 0.0) == L.FREE


def test_墙画上之后再粗化_障碍计数并过去_墙还在():
    g = L.LiveGrid(max_side=80)                      # 0.1 m × 80 = 8 m:起点那一片(7.1 m)装得下
    _feed(g, L.UP_FRAMES)
    img, meta = g.render()
    assert meta["res"] == pytest.approx(0.1) and _cell(img, meta, 5.05, 0.5) == L.OCC
    for k in range(1, 5):                             # 走到 8 m:12 m 装不下,粗到 0.2
        o, pts = _scene((2.0 * k, 0.0), wall=False)
        g.add(o, I3, pts)
    img, meta = g.render()
    assert meta["res"] == pytest.approx(0.2)
    assert _cell(img, meta, 5.05, 0.5) == L.OCC, "粗化时障碍计数要并过去"


def test_出图的坐标_左下角与_y_朝上():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES, wall=False)
    img, meta = g.render()
    res, (x0, y0) = meta["res"], meta["origin"]
    assert (meta["height"], meta["width"]) == img.shape
    # 地面是 [−2, 2) 见方:左下角 (−2, −2)、右上角在 (2, 2) 附近
    assert x0 == pytest.approx(-2.0, abs=res) and y0 == pytest.approx(-2.0, abs=res)
    assert meta["width"] * res == pytest.approx(4.0, abs=2 * res)
    # 只在 y > 1 的地方加一块地面:它在图的上半
    o, _ = _scene(wall=False)
    g.add(o, I3, np.array([_world(0.0, 1.5 + 0.01 * i, 0.0) for i in range(20)]))
    img, meta = g.render()
    top = img[: img.shape[0] // 2]
    assert (top == L.FREE).any()


def test_位姿与轨迹_0_3_m_一点_满了抽一半():
    g = L.LiveGrid(trail_max=8)
    _feed(g, L.UP_FRAMES, wall=False)
    for k in range(1, 30):
        o, pts = _scene((0.1 * k, 0.0), wall=False)
        g.add(o, I3, pts[::50])
    tr = g.trail
    steps = [math.dist(a, b) for a, b in zip(tr, tr[1:], strict=False)]
    assert len(tr) <= 8
    assert min(steps) >= 0.6 - 1e-6, "抽过之后新记的点也按翻倍的间距取"
    assert tr[-1][0] == pytest.approx(2.9, abs=0.65)
    # 狗朝平面 +y 转 90°
    o, pts = _scene((2.9, 0.0), wall=False)
    g.add(o, _rot_up(math.pi / 2), pts[::50])
    x, y, yaw = g.pose
    assert (x, y) == pytest.approx((2.9, 0.0), abs=1e-6)
    assert yaw == pytest.approx(math.pi / 2, abs=1e-6)


def _png_decode(b):
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", b[16:24])
    assert b[24:26] == b"\x08\x00"                    # 8 位灰度
    i, data = 8, b""
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]
        t = b[i + 4:i + 8]
        if t == b"IDAT":
            data += b[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(data)
    rows = [raw[r * (w + 1):(r + 1) * (w + 1)] for r in range(h)]
    assert all(r[0] == 0 for r in rows)
    return np.array([list(r[1:]) for r in rows], np.uint8)


def test_PNG_跟栅格一样():
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES)
    img, _ = g.render()
    assert (_png_decode(L.png(img)) == img).all()


def test_快照_有新帧才出_seq_只增_写盘是改名的(tmp_path, monkeypatch):
    g = L.LiveGrid()
    snap = L.Snapshots(tmp_path / "preview", run="bag-1", now=lambda: 1234.5)
    assert snap.write(g) is False                     # 还没图
    _feed(g, L.UP_FRAMES)
    assert snap.write(g) is True
    assert snap.write(g) is False                     # 没新帧不写
    d = json.loads((tmp_path / "preview" / L.SNAPSHOT).read_text())
    assert d["seq"] == 1 and d["run"] == "bag-1" and d["frames"] == L.UP_FRAMES
    assert d["written_at"] == 1234.5
    img, meta = g.render()
    assert (_png_decode(base64.b64decode(d["png"])) == img).all()
    for k in ("res", "origin", "width", "height", "pose", "trail", "dropped"):
        assert k in d
    _feed(g, 1)
    renamed = []
    real = L.os.replace
    monkeypatch.setattr(L.os, "replace", lambda a, b: (renamed.append((a, b)), real(a, b)))
    assert snap.write(g) is True
    assert renamed and str(renamed[0][1]).endswith(L.SNAPSHOT)
    assert json.loads((tmp_path / "preview" / L.SNAPSHOT).read_text())["seq"] == 2
    assert [p.name for p in (tmp_path / "preview").iterdir()] == [L.SNAPSHOT]


def test_写盘失败不炸_下次再写(tmp_path):
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES)
    blocker = tmp_path / "preview"
    blocker.write_text("不是目录")
    snap = L.Snapshots(blocker, run="r", now=lambda: 0.0)
    assert snap.write(g) is False
    blocker.unlink()
    assert snap.write(g) is True


# ------------------------------------------------------------------ 配对

class _Grid:
    def __init__(self):
        self.added = []

    def add(self, origin, R, pts):
        self.added.append((origin, R, pts))


def test_点云等位姿_位姿等点云_都配得上():
    g = _Grid()
    f = L.Feeder(g)
    assert f.want(10.0)
    f.on_scan(10.0, np.ones((3, 3)))
    assert not g.added
    f.on_pose(10.0, (1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0))
    assert len(g.added) == 1
    o, R, pts = g.added[0]
    assert np.allclose(o, [1, 2, 3]) and np.allclose(pts, np.ones((3, 3)) + [1, 2, 3])
    f.on_pose(11.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))   # 位姿先到
    assert f.want(11.0)
    f.on_scan(11.0, np.zeros((2, 3)))
    assert len(g.added) == 2


def test_位姿可以比点云晚_80_ms_以内_早_10_ms_以上不配():
    """MOLA 的位姿时刻可能是这一帧扫描的中间(离线轨迹比报文头晚 50 ms,10 Hz 下正好半帧)。"""
    g = _Grid()
    f = L.Feeder(g)
    f.on_scan(10.0, np.ones((3, 3)))
    f.on_pose(9.98, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))   # 早 20 ms
    f.on_pose(10.09, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))  # 晚 90 ms
    assert not g.added
    f.on_pose(10.05, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert len(g.added) == 1
    f.on_pose(20.08, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))  # 位姿先到,点云后到
    f.on_scan(20.0, np.ones((3, 3)))
    assert len(g.added) == 2


def test_两帧点云都在窗口里_配不晚于位姿的最近一帧():
    g = _Grid()
    f = L.Feeder(g)
    f.on_scan(10.0, np.full((1, 3), 1.0))
    f.on_scan(10.05, np.full((1, 3), 2.0))
    f.on_scan(10.1, np.full((1, 3), 3.0))
    f.on_pose(10.06, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert np.allclose(g.added[0][2], 2.0)
    assert [t for t, _ in f._scans] == [10.1]           # 更早的等不到位姿了


def test_点云转到世界系按位姿的转动():
    g = _Grid()
    f = L.Feeder(g)
    s = math.sqrt(0.5)
    f.on_scan(1.0, np.array([[1.0, 0.0, 0.0]]))
    f.on_pose(1.0, (0.0, 0.0, 0.0), (0.0, 0.0, s, s))  # 绕 z 转 90°
    assert np.allclose(g.added[0][2], [[0.0, 1.0, 0.0]])


def test_每秒最多_2_帧_多的不要():
    f = L.Feeder(_Grid())
    assert f.want(10.0)
    f.on_scan(10.0, np.ones((1, 3)))
    assert not f.want(10.1)
    assert not f.want(10.49)
    assert f.want(10.5)
    assert f.dropped == 2


def test_等位姿的点云最多留_10_帧_位姿也不无限攒():
    g = _Grid()
    f = L.Feeder(g, rate_hz=1000.0)
    for k in range(30):
        f.on_scan(float(k), np.ones((1, 3)))
    assert len(f._scans) == L.KEEP_SCANS
    f.on_pose(0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))  # 最早的已经丢了
    assert not g.added
    f.on_pose(29.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert len(g.added) == 1
    for k in range(100, 200):
        f.on_pose(float(k), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert len(f._poses) <= L.KEEP_POSES


def test_点云隔几个取一个():
    g = _Grid()
    f = L.Feeder(g)
    f.on_scan(1.0, np.ones((80, 3)))
    f.on_pose(1.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert len(g.added[0][2]) == 80 // L.POINT_STRIDE


def test_不反序列化就读得出时间戳_大小端都认():
    le = b"\x00\x01\x00\x00" + struct.pack("<iI", 1700000000, 250_000_000) + b"rest"
    be = b"\x00\x00\x00\x00" + struct.pack(">iI", 12, 500_000_000)
    assert L.cdr_stamp(le) == pytest.approx(1700000000.25)
    assert L.cdr_stamp(be) == pytest.approx(12.5)


# ------------------------------------------------------------------ 内审修复

def test_室内天花板比地面密_上照样按地面定():
    """内审应修 4:定号窗口放到 ±2 m 时,离地 2.4 m 的天花板(雷达上方 1.9 m)落进来又更密,「上」翻了、
    整张预览镜像。跟建图脚本一样只看 ±1.5 m。"""
    g = L.LiveGrid()
    floor = [_world(x, y, 0.0) for x in np.arange(-2, 2, 0.2) for y in np.arange(-2, 2, 0.2)]
    ceil = [_world(x, y, 2.4) for x in np.arange(-2, 2, 0.05) for y in np.arange(-2, 2, 0.05)]
    pts = np.array(floor + ceil)
    o = _world(0.0, 0.0, H)
    for _ in range(L.UP_FRAMES):
        g.add(o, I3, pts)
    assert np.allclose(g.up, [1, 0, 0])


def test_时间戳往回跳_从那一帧重新算_不冻住():
    f = L.Feeder(_Grid())
    assert [f.want(t) for t in (1000.0, 1000.5, 1001.0, 10.0, 10.5, 11.0, 10.7, 10.8)] == \
        [True, True, True, True, True, True, True, False]


class _M:
    """假的 ``nav_msgs/Odometry``。"""
    def __init__(self, t, p, q):
        ns = types.SimpleNamespace
        self.header = ns(stamp=ns(sec=int(t), nanosec=int(round((t % 1) * 1e9))))
        self.pose = ns(pose=ns(position=ns(x=p[0], y=p[1], z=p[2]),
                               orientation=ns(x=q[0], y=q[1], z=q[2], w=q[3])))


def _raw(t):
    return b"\x00\x01\x00\x00" + struct.pack("<iI", int(t), int(round((t % 1) * 1e9)))


def test_回调_坏消息丢掉计数_不往外抛_好的照收():
    g = _Grid()
    f = L.Feeder(g)

    def decode(raw):
        if raw.endswith(b"bad"):
            raise KeyError("x")
        return np.ones((8, 3))
    h = L.Handlers(f, decode_cloud=decode)
    h.on_scan(b"\x00\x01")                              # 太短
    h.on_scan(_raw(5.0) + b"bad")                       # 缺字段
    h.on_pose(_M(5.0, (float("nan"), 0, 0), (0, 0, 0, 1)))
    h.on_pose(_M(5.0, (0, 0, 0), (0, 0, 0, 0)))         # 全零四元数
    h.on_pose(object())                                 # 不像消息
    assert h.bad == 5 and not g.added
    h.on_scan(_raw(6.0))
    h.on_pose(_M(6.0, (1, 2, 3), (0, 0, 0, 1)))
    assert len(g.added) == 1 and np.allclose(g.added[0][0], [1, 2, 3])


def test_快照不写_NaN(tmp_path):
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES)
    g.pose = (float("nan"), 0.0, 0.0)
    snap = L.Snapshots(tmp_path, run="r", now=lambda: 0.0)
    assert snap.write(g) is False and snap.seq == 0
    assert not (tmp_path / L.SNAPSHOT).exists()


def test_PNG_太大先缩一半再发_障碍优先_左下角不动(tmp_path, monkeypatch):
    g = L.LiveGrid()
    _feed(g, L.UP_FRAMES)
    img, meta = g.render()
    monkeypatch.setattr(L, "PNG_MAX_BYTES", len(L.png(img)) - 1)
    snap = L.Snapshots(tmp_path, run="r", now=lambda: 0.0)
    assert snap.write(g) is True
    d = json.loads((tmp_path / L.SNAPSHOT).read_text())
    small = _png_decode(base64.b64decode(d["png"]))
    assert d["res"] == pytest.approx(2 * meta["res"]) and d["origin"] == meta["origin"]
    assert small.shape == (d["height"], d["width"]) == ((img.shape[0] + 1) // 2,
                                                       (img.shape[1] + 1) // 2)
    assert _cell(small, d, 5.05, 0.5) == L.OCC and _cell(small, d, 1.0, 0.5) == L.FREE


def test_缩一半_奇数行补在上面():
    img = np.array([[L.OCC, L.UNKNOWN, L.FREE],
                    [L.UNKNOWN, L.UNKNOWN, L.UNKNOWN],
                    [L.FREE, L.UNKNOWN, L.UNKNOWN]], np.uint8)
    out, meta = L.halve(img, {"res": 0.1, "origin": [0.0, 0.0], "width": 3, "height": 3})
    # 补一行在最上、一列在最右:下面两行并成第 1 行,第 0 行是最上那行
    assert out.tolist() == [[L.OCC, L.FREE], [L.FREE, L.UNKNOWN]]
    assert meta == {"res": 0.2, "origin": [0.0, 0.0], "width": 2, "height": 2}
