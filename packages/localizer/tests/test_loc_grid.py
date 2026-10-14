"""二维规划栅格(W09c1):按 frames.json 的平面画。合成数据:一间 10 × 6 m 的屋子,MOLA 系是 X 朝上的
雷达系(跟 RS-Airy 一样),地图平面系里的墙、狗走过的路已知。"""

from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer import grid as G  # noqa: E402
from d1max_localizer.frames import Frames  # noqa: E402

# 雷达 X 朝上(MOLA 系里的「上」略歪)、Z 朝前
UP = (0.998, -0.027, 0.053)


def _frames():
    n = math.sqrt(sum(c * c for c in UP))
    up = tuple(c / n for c in UP)
    return Frames(up=up, sensor_up=(1.0, 0.0, 0.0), sensor_forward=(0.0, 0.0, 1.0),
                  sensor_height=0.6)


def _room(frames, *, ghost=True):
    """地图平面系里造点(x, y, 高度),再转回 MOLA 系。屋子 x∈[0,10]、y∈[0,6];狗沿 y=3 从 x=1
    走到 9。"""
    rng = np.random.default_rng(0)
    pts = []
    for h in np.linspace(0.0, 2.4, 13):                  # 四面墙
        xs = np.arange(0, 10, 0.02)
        ys = np.arange(0, 6, 0.02)
        pts += [np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]]
    fl = rng.uniform([0, 0], [10, 6], (40000, 2))        # 地面
    pts.append(np.c_[fl, np.zeros(len(fl))])
    # 狗走过的地方站了个人
    pts.append(np.c_[np.full(200, 4.0), np.full(200, 3.0), np.linspace(0.8, 1.6, 200)])
    if ghost:                                            # 远处玻璃的反射
        pts.append(np.c_[np.full(300, 40.0), np.linspace(0, 6, 300), np.full(300, 1.2)])
    P = np.vstack(pts)
    traj = np.c_[np.linspace(1, 9, 80), np.full(80, 3.0), np.full(80, 0.6)]
    L = np.asarray(frames.level_matrix())
    return P @ L, traj @ L                               # 地图平面系 → MOLA 系(L 是正交阵)


def test_墙是占据_屋里走过的地方是可通行_远处的鬼影不进来():
    f = _frames()
    P, T = _room(f)
    g = G.render(P, T, f)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x, y in ((5.0, 0.0), (5.0, 6.0), (0.0, 3.0), (10.0, 3.0)):
        assert at(x, y) == G.OCCUPIED, (x, y)
    for x, y in ((2.0, 3.0), (5.0, 2.0), (8.0, 4.5), (4.0, 3.0)):
        assert at(x, y) == G.FREE, (x, y)                 # 那个人:射线穿过得多,判可通行
    assert g.cell_of(40.0, 3.0) is None, "离路线 12 m 以外的鬼影不在图上"


def test_栅格的坐标跟定位的平面同一个系():
    """路线上每一点(就是定位器会报的位置)都落在可通行的格子上。"""
    f = _frames()
    P, T = _room(f, ghost=False)
    g = G.render(P, T, f)
    q = G.level(T, f)
    for x, y, _ in q[::7]:
        assert g.image[g.cell_of(x, y)] == G.FREE


def test_写出来是_map_server_的格式(tmp_path):
    f = _frames()
    P, T = _room(f, ghost=False)
    g = G.render(P, T, f)
    pgm, yml = G.write(g, tmp_path / "floor")
    raw = pgm.read_bytes()
    header, body = raw.split(b"\n255\n", 1)
    w, h = (int(v) for v in header.split(b"\n")[1].split())
    assert header.startswith(b"P5") and len(body) == w * h == g.image.size
    y = yml.read_text()
    assert "image: floor.pgm" in y and "resolution: 0.05" in y and "mode: trinary" in y
    ox, oy = (float(v) for v in y.split("origin: [")[1].split("]")[0].split(",")[:2])
    assert (round(ox, 3), round(oy, 3)) == (round(g.origin[0], 3), round(g.origin[1], 3))
    assert -0.5 < ox < 0.0 and -0.5 < oy < 0.0, "屋角在原点附近"


def _walls_and_person(frames, floor_x=(0.0, 10.0)):
    """屋子的墙 + 一个跟着狗走的人(狗后面 0.5 m、右边 0.3 m,离地 0.8–1.6 m)。回:合起来的点云、
    雷达走过的位置、逐帧扫描 [(雷达位置, 这一帧的点)](都在 MOLA 系)、狗身中心走过的路(地图平面)。"""
    L = np.asarray(frames.level_matrix())
    walls = []
    for h in np.linspace(0.8, 1.6, 9):
        xs, ys = np.arange(0, 10, 0.02), np.arange(0, 6, 0.02)
        walls += [np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                  np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                  np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                  np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]]
    walls = np.vstack(walls)
    rng = np.random.default_rng(3)
    floor = np.c_[rng.uniform([floor_x[0], 0], [floor_x[1], 6], (20000, 2)), np.zeros(20000)]
    a = np.linspace(0, 2 * np.pi, 48, endpoint=False)
    pillar = np.vstack([np.c_[5.0 + 0.15 * np.cos(a), 1.2 + 0.15 * np.sin(a), np.full(48, h)]
                        for h in np.linspace(0.8, 1.6, 9)])   # 路和墙之间一根柱子(一直在)
    xs = np.linspace(1, 9, 80)
    sensor = np.c_[xs, np.full(80, 3.0), np.full(80, 0.6)]
    scans, people = [], []
    for x in xs:
        a = np.linspace(0, 2 * np.pi, 24, endpoint=False)
        person = np.c_[x - 0.5 + 0.15 * np.cos(a), 2.7 + 0.15 * np.sin(a), np.full(24, 1.2)]
        person = np.vstack([person + [0, 0, dz] for dz in (-0.4, 0.0, 0.4)])
        people.append(person)
        scans.append((np.array([x, 3.0, 0.6]) @ L, np.vstack([walls, floor, pillar, person]) @ L))
    P = np.vstack([walls, floor, pillar, *people])
    return P @ L, sensor @ L, scans, np.c_[xs - 0.4, np.full(80, 3.0)]


def test_跟着狗走的人_真射线清得掉_走过的路和右边都是可通行_墙还在():
    """W09c1 真数据(newdog2)上踩到的:人跟着狗走,他站过的地方连成一道「墙」,模拟射线碰到就停、永远
    清不掉,走过的路被判成墙、右半边全成了「没扫到」。逐帧扫描的真射线:那一刻人不在的地方射线穿过去。"""
    f = _frames()
    P, T, scans, body = _walls_and_person(f)
    g = G.render(P, T, f, scans=scans, body_path=body)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x in (2.0, 4.5, 7.0):
        assert at(x - 0.5, 2.7) == G.FREE, "人站过的地方"
        assert at(x, 1.5) == G.FREE, "人右边那半间屋"
        assert at(x - 0.4, 3.0) == G.FREE, "狗身中心走过的路"
    for x, y in ((5.0, 0.0), (5.0, 6.0), (0.0, 3.0), (10.0, 3.0), (5.0, 1.05)):
        assert at(x, y) == G.OCCUPIED, (x, y)            # (5, 1.05):柱子朝路的那一面
    # 柱子后面的墙点跟柱子在同一个方位:每个方位只打最近的那个,不然射线穿过柱子把它清掉;
    # 地面的点不打射线(它只说明地面上空,说明不了障碍层那个高度是空的)
    old = G.render(P, T, f)                               # 模拟射线:人那道「墙」还在
    assert old.image[old.cell_of(4.0, 2.7)] != G.FREE


def test_狗身子占过的格子一律可通行_身宽两边各_0_25_m():
    """没有一条射线、测的地方也没看到地面的时候,可通行只来自狗身子走过的路;路上的障碍点(比如当时开着的
    门)也不算。地面只铺在屋子最里头(x 8.5–10)。"""
    f = _frames()
    P, T, _, body = _walls_and_person(f, floor_x=(8.5, 10.0))
    L = np.asarray(f.level_matrix())
    door = np.c_[np.full(50, 4.6), np.full(50, 3.0), np.linspace(0.8, 1.6, 50)] @ L
    g = G.render(np.vstack([P, door]), T, f, scans=[], body_path=body)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x in (1.0, 4.6, 7.0):
        assert at(x, 3.0) == G.FREE, x
    r, c = g.cell_of(4.6, 3.0)
    assert (g.image[r - 1:r + 2, c - 1:c + 2] == G.FREE).all(), "门那几格(换算后可能差一格)"
    assert at(4.0, 3.2) == G.FREE, "中心线旁边 0.2 m"
    assert at(4.0, 3.4) != G.FREE, "0.4 m 以外不是身子"
    assert g.cell_of(4.0, 1.5) is None or at(4.0, 1.5) != G.FREE, "没有射线:别处都不知道"


def _field(frames, slope=0.0, wall_x=None):
    """开阔地:24 × 16 m 的地面(没有齐腰高的东西),狗沿 y=0 从 x=0 走到 10;地面按 ``slope``
    往 +x 升。"""
    L = np.asarray(frames.level_matrix())
    rng = np.random.default_rng(5)
    g = rng.uniform([-6, -8], [18, 8], (400000, 2))
    pts = [np.c_[g, slope * g[:, 0]]]
    if wall_x is not None:                               # 一堵墙(离当地地面 0.8–1.6 m)
        ys = np.arange(-4, 4, 0.02)
        pts += [np.c_[np.full_like(ys, wall_x), ys, np.full_like(ys, slope * wall_x + h)]
                for h in np.linspace(0.8, 1.6, 9)]
    xs = np.linspace(0, 10, 60)
    traj = np.c_[xs, np.zeros(60), slope * xs + 0.6]
    return np.vstack(pts) @ L, traj @ L


def test_开阔地_看到过地面又没有障碍就是可通行_不只走过的那条线():
    """内审阻断 1:庄园的草坪、广场没有齐腰高的东西,射线没处打,原来只有走过的那一条线是可通行的,站点
    把路边的点全拒了。"""
    f = _frames()
    P, T = _field(f)
    for scans in (None, []):
        g = G.render(P, T, f, scans=scans)
        for x, y in ((5.0, 3.0), (5.0, -4.0), (12.0, 0.0), (-3.0, 2.0)):
            assert g.cell_of(x, y) is not None and g.image[g.cell_of(x, y)] == G.FREE, (x, y)


def test_坡地_地面按走过的地方逐段算_上坡的地面不当障碍_墙照样是墙():
    """内审再议 14:整张图一个地面高度,坡上的地面落进障碍层。"""
    f = _frames()
    P, T = _field(f, slope=0.1, wall_x=14.0)              # 10 m 升 1 m
    g = G.render(P, T, f, scans=[])
    for x, y in ((8.0, 2.0), (9.5, -3.0), (3.0, 0.0)):
        assert g.image[g.cell_of(x, y)] == G.FREE, (x, y)
    assert g.image[g.cell_of(14.0, 0.0)] == G.OCCUPIED


def test_栅格太大就放粗_粗到_0_2_m_还装不下就报错(monkeypatch):
    """内审小 9:轨迹发散到几公里就申请巨大的数组(Orin 上 OOM 会连累定位器、代理);边长超过站点预览的
    上限(2500 万格)站点也预览不了。"""
    f = _frames()
    P, T = _room(f, ghost=False)
    monkeypatch.setattr(G, "MAX_CELLS", 20_000)
    g = G.render(P, T, f)
    assert g.res == 0.1 and g.image.size <= 20_000
    monkeypatch.setattr(G, "MAX_CELLS", 500)
    with pytest.raises(G.GridTooLarge, match="轨迹"):
        G.render(P, T, f)


def test_离地_0_4_到_0_7_m_的矮东西也是障碍():
    """修阻断 1 时顺带改的:障碍层原来从 0.7 m 起,newdog2 上 0.75 m 高的展台当地地面高一点就看不见;
    长椅、花坛这种 0.5 m 的更是从来看不见。"""
    f = _frames()
    P, T = _field(f)
    L = np.asarray(f.level_matrix())
    a = np.linspace(0, 2 * np.pi, 60, endpoint=False)
    bench = np.vstack([np.c_[5.0 + 0.3 * np.cos(a), 3.0 + 0.3 * np.sin(a), np.full(60, h)]
                       for h in np.linspace(0.45, 0.6, 6)]) @ L
    g = G.render(np.vstack([P, bench]), T, f, scans=[])
    assert g.image[g.cell_of(5.0, 2.7)] == G.OCCUPIED


def _bench(frames, x, y, r=0.2):
    """0.45–0.52 m 高的凳子(刚过障碍层的下沿:地面估高 0.1 m 它就没了)。"""
    a = np.linspace(0, 2 * np.pi, 60, endpoint=False)
    return np.vstack([np.c_[x + r * np.cos(a), y + r * np.sin(a), np.full(60, h)]
                      for h in np.linspace(0.45, 0.52, 6)]) @ np.asarray(frames.level_matrix())


def test_轨迹高度漂了_走过的格子按点云修地面_路边的矮凳还是障碍():
    """MOLA 的高度会漂(newdog2 上一处漂了 0.16 m):光按轨迹往下挪,地面就高了,矮东西掉出障碍层。"""
    f = _frames()
    P, T = _field(f)
    L = np.asarray(f.level_matrix())
    t = T @ L.T                                           # 回到地图平面系(L 是正交阵)
    t[(t[:, 0] > 3) & (t[:, 0] < 7), 2] += 0.2            # 这一段轨迹高了 0.2 m
    g = G.render(np.vstack([P, _bench(f, 5.0, 0.35)]), t @ L, f, scans=[])
    assert g.image[g.cell_of(5.0, 0.15)] == G.OCCUPIED


def test_墙根一堆侧面点不把地面抬高_旁边的矮凳还是障碍():
    """宽窗里取平均的话,墙从地面起那一截的点把地面往上拉,旁边 0.5 m 高的凳子就掉出障碍层;
    取最密的那一层(地面是一大片水平的点)。"""
    f = _frames()
    P, T = _field(f)
    L = np.asarray(f.level_matrix())
    zs, ys = np.arange(0.0, 1.2, 0.01), np.arange(-2.5, -1.5, 0.01)
    wall = np.c_[np.full(len(zs) * len(ys), 14.0), np.repeat(ys, len(zs)),
                 np.tile(zs, len(ys))] @ L                # 1 m 长、从地面起的墙,点很密
    g = G.render(np.vstack([P, wall, _bench(f, 14.5, -2.0)]), T, f, scans=[])
    assert g.image[g.cell_of(14.5, -2.2)] == G.OCCUPIED


def _table_room(frames, every=4):
    """屋子(墙上 0.45、0.5、1.6 m 三圈)+ 一张桌子(x 4.5–5.5、y 4.0–5.0,桌面 0.7–0.8 m,没有腿)。狗沿
    y = 3 走,雷达 0.6 m 高。薄桌面只在每 ``every`` 帧里打到一次(真雷达线束稀,远一点就漏);别的帧
    那个方向最近的是桌子后面墙的低处 —— 射线从桌面底下钻过去。"""
    L = np.asarray(frames.level_matrix())
    xs, ys = np.arange(0, 10, 0.02), np.arange(0, 6, 0.02)
    walls = np.vstack([np.vstack([np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                                  np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                                  np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                                  np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]])
                       for h in (0.45, 0.5, 1.6)])          # 打不着桌面的那几束:从底下、上面过
    tx, ty = np.meshgrid(np.arange(4.5, 5.5, 0.015), np.arange(4.0, 5.0, 0.015))
    table = np.vstack([np.c_[tx.ravel(), ty.ravel(), np.full(tx.size, h)] for h in (0.7, 0.8)])
    rng = np.random.default_rng(5)
    floor = np.c_[rng.uniform([0, 0], [10, 6], (20000, 2)), np.zeros(20000)]
    sx = np.linspace(1, 9, 80)
    scans = []
    for i, x in enumerate(sx):
        pts = [walls, floor] + ([table] if i % every == 0 else [])
        scans.append((np.array([x, 3.0, 0.6]) @ L, np.vstack(pts) @ L))
    P = np.vstack([walls, floor, table])
    sensor = np.c_[sx, np.full(80, 3.0), np.full(80, 0.6)]
    return P @ L, sensor @ L, scans


def test_W34_射线从桌面底下钻过去不算穿过_桌子留着_没东西的地方照样清():
    """2026-10-08 C40221 双雷达地图:桌子、椅子大多没了(占用 14.0 → 7.7 m²)。桌子四周都看过、是
    空地:墙只画看得见的那一面(2026-10-14)以后桌面里头是「不知道」,只有一圈边是占据 —— 规划两样
    都当挡,所以这里验的是「一格都没被清成可通行」,外加边是占据。"""
    f = _frames()
    P, T, scans = _table_room(f)
    g = G.render(P, T, f, scans=scans)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    blocked = sum(at(x, y) != G.FREE for x in np.arange(4.6, 5.45, 0.1)
                  for y in np.arange(4.1, 4.95, 0.1))
    assert blocked >= 79, f"桌子那 81 格只剩 {blocked} 格是挡"
    for x in np.arange(4.55, 5.5, 0.1):                   # 朝狗的那条边
        assert G.OCCUPIED in _column(g, x, 3.95, 4.15), round(x, 2)
    for x, y in ((2.0, 4.5), (7.5, 4.5), (5.0, 3.6)):
        assert at(x, y) == G.FREE, ("桌子旁边、前面照样清", x, y)
    assert at(5.0, 5.98) == G.OCCUPIED, "后面的墙"


# ------------------------------------------------------------ 墙画薄(2026-10-14)

DOOR = (2.4, 3.8)                                          # 隔墙上的门(y,1.4 m 宽)


def _scans(frames, walls, floor, origins, rng, sigma, jitter=0.0):
    """每个雷达位置一帧:墙点沿射线方向带 ``sigma`` 的测距噪声(每帧各抖各的)+ 地面点;``jitter``:
    每帧的位姿误差(平面上两个方向各这么大的标准差,整帧连雷达一起挪 —— 拼出来的点图里同一堵墙就
    厚了)。回(逐帧扫描(MOLA 系), MOLA 点图里的墙点(每三帧取一帧、每帧隔一个点 —— 点图就是这些帧
    拼起来的))。"""
    L = np.asarray(frames.level_matrix())
    scans, mapped = [], []
    for i, o in enumerate(origins):
        d = walls - o
        r = np.hypot(d[:, 0], d[:, 1])[:, None]
        noisy = o + d * (r + rng.normal(0.0, sigma, (len(walls), 1))) / r
        e = np.r_[rng.normal(0.0, jitter, 2), 0.0]
        scans.append(((o + e) @ L, np.vstack([noisy + e, floor]) @ L))
        if i % 3 == 0:
            mapped.append(noisy[::2] + e)
    return scans, np.vstack(mapped)


def _two_rooms(frames, *, walk_x=(1.0, 9.0), sigma=0.02, jitter=0.03):
    """两间屋:A x∈[0,5]、C x∈[5.1,10]、y∈[0,4],中间 10 cm 的隔墙(x 5.0–5.1)上开一扇门
    (:data:`DOOR`)。墙点带 ``sigma`` 的测距噪声、每帧 ``jitter`` 的位姿误差(:func:`_scans`)。狗
    (雷达 0.6 m 高)沿 y = 3.1 从 ``walk_x[0]`` 走到 ``walk_x[1]``(门在 y 2.4–3.8)。回:点图、雷达
    走过的位置、逐帧扫描(都在 MOLA 系)、狗身中心走过的路(地图平面)。"""
    L = np.asarray(frames.level_matrix())
    rng = np.random.default_rng(7)
    xs, ys = np.arange(0.0, 10.0, 0.02), np.arange(0.0, 4.0, 0.02)
    py = ys[(ys < DOOR[0]) | (ys >= DOOR[1])]
    walls = np.vstack([np.vstack([np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                                  np.c_[xs, np.full_like(xs, 4.0), np.full_like(xs, h)],
                                  np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                                  np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)],
                                  np.c_[np.full_like(py, 5.0), py, np.full_like(py, h)],
                                  np.c_[np.full_like(py, 5.1), py, np.full_like(py, h)]])
                       for h in np.linspace(0.8, 1.6, 5)])
    floor = np.c_[rng.uniform([0, 0], [10, 4], (30000, 2)), np.zeros(30000)]
    sx = np.linspace(walk_x[0], walk_x[1], 60)
    sensor = np.c_[sx, np.full(len(sx), 3.1), np.full(len(sx), 0.6)]
    scans, mapped = _scans(frames, walls, floor, sensor, rng, sigma, jitter)
    P = np.vstack([mapped, floor])
    return P @ L, sensor @ L, scans, np.c_[sx - 0.4, np.full(len(sx), 3.1)]


def _column(g, x, y0, y1):
    """``x`` 处、y 从 ``y0`` 往上到 ``y1`` 那一列格子的值(从下往上;出了图的不要)。"""
    r, c = g.cell_of(x, y0)
    n = int(round((y1 - y0) / g.res))
    return g.image[[r - k for k in range(n + 1) if r - k >= 0], c]


def test_墙有测距噪声_只画朝空地的那一面_一格厚_一格不缺():
    """2026-10-14 办公室图墙厚中位数 0.20 m、p90 0.55 m(一格 0.05 m):射线停在打到的点前 0.1 m,
    墙面前那两格的噪点从来没有射线穿过;墙背后抖出来的点打不到射线,比例永远是 1。雷达只看得到墙朝着
    它的那一面:只留一层(用户 2026-10-14 嫌两层还是厚)。直墙带 2 cm 测距噪声、3 cm 位姿误差,每一列
    都还有墙、墙面没挪、最厚一格。"""
    f = _frames()
    P, T, scans, body = _two_rooms(f)
    g = G.render(P, T, f, scans=scans, body_path=body)
    thick = []
    for x in np.arange(0.3, 9.75, 0.05):
        if 4.8 < x < 5.3:
            continue                                      # 隔墙那几列
        col = _column(g, x, 3.6, 4.4)
        occ = col == G.OCCUPIED
        assert occ.any(), ("北墙这一列没了", round(x, 2))
        thick.append(int(occ.sum()))
        first = int(np.argmax(occ))                       # 从屋里往外第一个占据格
        assert (col[:first] == G.FREE).all(), ("墙前面不是可通行", round(x, 2))
        assert abs(3.6 + first * g.res - 4.0) <= 0.1, ("墙面挪了", round(x, 2), first)
    assert max(thick) <= 1, f"北墙最厚 {max(thick)} 格(各厚度的列数 {np.bincount(thick)})"


SPECKS = ((2.6, 5.89), (3.0, 5.89), (3.4, 5.89))           # 墙面前 11 cm 的噪点(测距拖尾、多次反射)


def test_墙面前的零星噪点清掉():
    """6 × 6 m 的屋子,狗朝北墙走过去(x = 3,y 0.5 → 4.0,一直正对着它)。点图里北墙面前 11 cm 有几团
    零星的点(每团 8 个,扫描里没有):原来射线停在打到的点前 0.125–0.15 m,正对着墙的射线到不了那一格,
    点再少也判占据,墙往屋里胖两格;现在停在 0.075–0.1 m,那一格被穿过几十次,清掉。屋子中间悬着的
    一团本来就清得掉。"""
    f = _frames()
    L = np.asarray(f.level_matrix())
    rng = np.random.default_rng(11)
    xs = np.arange(0.0, 6.0, 0.02)
    walls = np.vstack([np.vstack([np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                                  np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                                  np.c_[np.zeros_like(xs), xs, np.full_like(xs, h)],
                                  np.c_[np.full_like(xs, 6.0), xs, np.full_like(xs, h)]])
                       for h in np.linspace(0.8, 1.6, 5)])
    floor = np.c_[rng.uniform([0, 0], [6, 6], (20000, 2)), np.zeros(20000)]
    blobs = [np.c_[x + rng.uniform(-0.01, 0.01, 8), y + rng.uniform(-0.01, 0.01, 8),
                   rng.uniform(0.9, 1.4, 8)] for x, y in (*SPECKS, (4.5, 3.0))]
    sy = np.linspace(0.5, 4.0, 60)
    sensor = np.c_[np.full(len(sy), 3.0), sy, np.full(len(sy), 0.6)]
    scans, mapped = _scans(f, walls, floor, sensor, rng, 0.02)
    P = np.vstack([mapped, floor, *blobs]) @ L
    g = G.render(P, sensor @ L, f, scans=scans, body_path=np.c_[np.full(len(sy), 3.0), sy - 0.4])
    for x, y in (*SPECKS, (4.5, 3.0)):
        assert g.image[g.cell_of(x, y)] == G.FREE, (x, y)
    for x in (2.6, 3.0, 3.4):
        assert G.OCCUPIED in _column(g, x, 5.9, 6.1), ("北墙还在", x)


def _plan(g, tmp_path, a, b):
    """拿狗上同一份规划(代价图 + A*)在这张栅格上从 ``a`` 规划到 ``b``,回路过的格子心
    [(x, y)](连线加密过)。"""
    from d1max_contract.planning import astar
    from d1max_contract.planning import costmap as cm
    G.write(g, tmp_path / "floor")
    c = cm.from_map_dir(tmp_path, robot_radius_m=cm.ROBOT_RADIUS_M + cm.INFLATE_MARGIN_M)
    h, w = c.shape
    cells = astar.plan(c.cost.tobytes(), c.hard.astype(np.uint8).tobytes(), w, h,
                       c.cell_of(*a), c.cell_of(*b))
    dense = [rc for p, q in zip(cells[:-1], cells[1:], strict=True)
             for rc in astar.line_cells(p, q)]
    return [c.center(r, cc) for r, cc in dense]


def test_狗能过的门照样过得去_隔墙照样关着_规划只从门过(tmp_path):
    """墙画薄了不许漏:起终点隔着隔墙、直线穿墙最近,规划出来的路要绕到门那儿过(用跟狗同一份的规划:
    膨胀到外接圆半径 + 余量)。"""
    f = _frames()
    P, T, scans, body = _two_rooms(f)
    g = G.render(P, T, f, scans=scans, body_path=body)
    path = _plan(g, tmp_path, (2.5, 1.2), (7.5, 1.2))
    cross = [y for x, y in path if 4.95 <= x <= 5.15]
    assert cross, "路没过隔墙那条线"
    assert all(DOOR[0] <= y <= DOOR[1] for y in cross), f"从隔墙上穿过去了:{min(cross):.2f}"
    wall = [y for y in np.arange(0.1, 3.95, 0.05)         # 隔墙画成占据的那些行
            if G.OCCUPIED in [g.image[g.cell_of(x, y)] for x in np.arange(4.95, 5.2, 0.05)]]
    below, above = [y for y in wall if y < 3.0], [y for y in wall if y > 3.0]
    assert len(below) == len(np.arange(0.1, max(below) + 0.01, 0.05)), "门下面那段隔墙有缺口"
    assert max(below) >= DOOR[0] - 0.1 and min(above) <= DOOR[1] + 0.1, \
        f"门框被射线削掉了:门画成 {max(below):.2f}–{min(above):.2f}(真的 {DOOR})"


def test_从门里看到的后墙照样关着_墙外没有可通行(tmp_path):
    """狗只在 A 屋里走,C 屋只从门里看过去:看到的后墙(x = 10)也不许打穿,墙外一格可通行都不许有。"""
    f = _frames()
    P, T, scans, body = _two_rooms(f, walk_x=(1.0, 4.0))
    g = G.render(P, T, f, scans=scans, body_path=body)
    h, w = g.image.shape
    xs = g.origin[0] + (np.arange(w) + 0.5) * g.res
    ys = g.origin[1] + (h - 1 - np.arange(h) + 0.5) * g.res
    X, Y = np.meshgrid(xs, ys)
    outside = (X > 10.12) | (X < -0.12) | (Y > 4.12) | (Y < -0.12)
    assert not (g.image[outside] == G.FREE).any(), "墙外有可通行的格"
    seen = [y for y in np.arange(0.2, 3.9, 0.05) if g.cell_of(9.5, y) is not None
            and g.image[g.cell_of(9.5, y)] == G.FREE]
    assert len(seen) > 5, "从门里看过去 C 屋应该看到一片"
    for y in seen:                                        # 看得到的那一段后墙是挡
        row = [g.image[g.cell_of(x, y)] for x in np.arange(9.85, 10.2, 0.05)
               if g.cell_of(x, y) is not None]
        assert G.OCCUPIED in row, round(y, 2)


def test_只画一面不改哪里能走(monkeypatch):
    """墙只画看得见的那一面只把占据改成「不知道」:可通行的格一格不变(规划、拦截点把两样都当挡)。"""
    f = _frames()
    P, T, scans, body = _two_rooms(f)
    g = G.render(P, T, f, scans=scans, body_path=body)
    monkeypatch.setattr(G, "SURFACE_LAYERS", 10 ** 6)
    full = G.render(P, T, f, scans=scans, body_path=body)
    assert g.image.shape == full.image.shape and g.origin == full.origin
    assert ((g.image == G.FREE) == (full.image == G.FREE)).all()
    assert not ((g.image == G.OCCUPIED) & (full.image != G.OCCUPIED)).any()
    assert (full.image == G.OCCUPIED).sum() > (g.image == G.OCCUPIED).sum()


def test_只画一面_墙后面的去掉_隔着不知道看得到的墙不剥_另一堵墙不动():
    """直接在掩码上验(:func:`_surface`):

    - 贴着可通行区的 8 格厚墙只留一层(:data:`SURFACE_LAYERS`);
    - 跟可通行区隔着 6 格「不知道」、只有墙头贴着可通行区的走廊墙(擦着看过去的那种):只走占据格得从
      墙头沿着墙身一路数下去,哪儿都「深」,可它朝走廊那一层隔着「不知道」就看得到可通行区,整段留着,
      只剥它背面 —— 2026-10-14 只留贴着可通行区那一层的试法,在办公室图上丢过一整段这样的墙;
    - 一堵两格厚的墙后面隔一道缝的另一堵墙(玻璃后面的墙、重影):只走占据格够不着,不动;两格厚的那堵
      自己只留朝空地那一层。"""
    H, W = 40, 80
    free = np.zeros((H, W), bool)
    occ = np.zeros((H, W), bool)
    free[0:10, :] = True                                  # 可通行区
    occ[10:18, 0:30] = True                               # 贴着它的 8 格厚墙
    occ[16:20, 30:70] = True                              # 走廊墙:跟可通行区隔 6 格「不知道」
    free[10:30, 72:80] = True                             # 墙头那边也是可通行区
    occ[16:20, 70:72] = True
    drop = G._surface(occ, free, 0.05)
    assert not (drop & ~occ).any(), "只动占据格"
    kept = occ & ~drop
    assert kept[10, 0:30].all() and not kept[11:18, 2:28].any(), "厚墙留一层"
    assert kept[16, 30:72].all(), "走廊墙朝走廊的那一层整段留着"
    assert not kept[17:20, 34:56].any(), "走廊墙的背面去掉(墙头那一截看得到墙头外面,留着)"
    free = np.zeros((H, W), bool)
    occ = np.zeros((H, W), bool)
    free[0:10, :] = True
    occ[10:12, 0:50] = True                               # 两格厚的墙
    occ[15:18, 0:50] = True                               # 后面隔 3 格「不知道」的另一堵
    occ[10:18, 60:80] = True                              # 旁边一堵 8 格厚的
    kept = occ & ~G._surface(occ, free, 0.05)
    assert kept[15:18, 0:50].all(), "隔着缝的另一堵墙不动"
    assert kept[10, 0:50].all() and not kept[11, 2:48].any(), "两格厚的那堵只留朝空地那一层"
    assert kept[10, 60:80].all() and not kept[11:18, 62:78].any(), "厚的那堵照样留一层"


def _one_ray(res, x0, hit_x):
    """一帧、一条射线:雷达在 (``x0``, 0.02) 离地 0.6 m,打到 (``hit_x``, 0.02) 离地 1.0 m 的一个点;
    栅格左下角 (−1, −1)。射线每 0.025 m 一个采样点,``x0`` 取 0.0125 让采样点都落在格子的四分之一处
    (不落在格子边上)。回(那一行每格被穿过的次数, 某个 x 所在那一格的次数)。"""
    f = _frames()
    L = np.asarray(f.level_matrix())
    g = np.mgrid[-1:4:0.05, -1:1:0.05].reshape(2, -1).T
    floor = np.c_[g, np.zeros(len(g))]
    sensor = np.array([[x0, 0.02, 0.6], [x0 + 0.5, 0.02, 0.6]])
    ground = G._Ground(sensor, 0.0, 12.0, floor)
    lo = np.array([-1.0, -1.0])
    shape = (int(2 / res) + 1, int(5 / res) + 1)
    scan = (sensor[0] @ L, np.array([[hit_x, 0.02, 1.0]]) @ L)
    passes, used = G._raytrace_scans([scan], f, lo, shape, res, (0.4, 1.8), 12.0, ground)
    assert used == 1
    row = passes[int((0.02 - lo[1]) / res)]
    return row, lambda x: row[int((x - lo[0]) / res)]


def test_真射线停在打到的点前_0_075_到_0_1_m_打到的那一格不算穿过():
    """:data:`RAY_STOP_SHORT_M`:原来写 0.1、采样又少算一步,实际停在打到的点前 0.125–0.15 m(墙面前
    两格从来没有射线正着穿过);现在最后一个采样点离打到的点 0.075–0.1 m。打到的点在 2.9925:采样点
    2.9125 落在 [2.90, 2.95) 里,这一格(打到的点前 4–9 cm)有射线穿过;原来最后一个是 2.8875。"""
    row, at = _one_ray(0.05, 0.0125, 2.9925)
    assert at(2.5) == 1 and at(2.87) == 1
    assert at(2.92) == 1, "打到的点前 4–9 cm 那一格要被穿过"
    assert at(2.97) == 0, "打到的那一格"
    assert row[int((2.9925 + 1) / 0.05):].sum() == 0, "打到的点后面没有"


def test_栅格放粗了_打到的那一格也不算穿过():
    """栅格放粗到 0.2 m 一格(:data:`MAX_CELLS`)时停 0.075 m 不够一格:打到的点在 [3.0, 3.2) 深处,
    最后一个采样点 3.1125 还在这一格里 —— 这一格要单独排除,不然墙格被自己的射线「穿过」。"""
    row, at = _one_ray(0.2, 0.0125, 3.19)
    assert at(3.19) == 0, "打到的那一格"
    assert at(2.9) == 1
