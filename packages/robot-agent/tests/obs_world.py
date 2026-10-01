"""W11 测试用的栅格世界(W08 决定 10):真值墙 + 能动态加减的障碍(矩形),假感知按狗的真实位姿、视场、
距离算出狗身系局部栅格(跟感知节点同一个格式)、给仿真狗发净空许可;每拍查机身矩形压没压上真值障碍。"""

from __future__ import annotations

import math

import numpy as np

from d1max_contract.obsbridge import Grid, pack_bits

RES = 0.05
BODY_HL, BODY_HW = 0.465, 0.24


class 世界:
    def __init__(self, w_m=12.0, h_m=8.0, walls=()):
        self.h, self.w = int(h_m / RES), int(w_m / RES)
        self.occ = np.zeros((self.h, self.w), dtype=bool)
        self.occ[:2, :] = self.occ[-2:, :] = self.occ[:, :2] = self.occ[:, -2:] = True
        for x0, y0, x1, y1 in walls:
            self._fill(self.occ, x0, y0, x1, y1)
        #: 动态障碍(不在地图里):名字 → 矩形
        self.dyn: dict[str, tuple[float, float, float, float]] = {}

    def _fill(self, a, x0, y0, x1, y1, v=True):
        a[int(y0 / RES):int(math.ceil(y1 / RES)), int(x0 / RES):int(math.ceil(x1 / RES))] = v

    def 地图(self, d):
        """真值墙(不含动态障碍)写成 floor.pgm/.yaml。"""
        img = np.where(np.flipud(self.occ), 0, 254).astype(np.uint8)
        (d / "floor.pgm").write_bytes(b"P5\n%d %d\n255\n" % (self.w, self.h) + img.tobytes())
        (d / "floor.yaml").write_text(f"resolution: {RES}\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n")

    def 全部(self):
        a = self.occ.copy()
        for r in self.dyn.values():
            self._fill(a, *r)
        return a

    def 撞没撞(self, x, y, yaw):
        """机身矩形(不带余量)压没压上墙或动态障碍。"""
        a = self.全部()
        c, s = math.cos(yaw), math.sin(yaw)
        for u in np.arange(-BODY_HL, BODY_HL + 1e-9, RES / 2):
            for v in np.arange(-BODY_HW, BODY_HW + 1e-9, RES / 2):
                px, py = x + c * u - s * v, y + s * u + c * v
                r, k = int(py / RES), int(px / RES)
                if 0 <= r < self.h and 0 <= k < self.w and a[r, k]:
                    return True
        return False


class 假感知:
    """按狗的真实位姿算狗身系局部栅格:前半球(机身前沿往前)4 m 以内看得见;
    ``rear`` 开着后面也看得见。"""

    def __init__(self, world, *, size=80, res=0.1, rng=4.0, rear=False, rear_cal=None):
        self.world, self.size, self.res, self.rng, self.rear = world, size, res, rng, rear
        #: 后雷达外参标过(W09i;默认跟 ``rear`` 一样:看得见就当标过)。
        self.rear_cal = rear if rear_cal is None else rear_cal
        self.seq = 0
        self.check = "ok"
        n = size
        idx = np.arange(n)
        self.cx = (idx - n / 2 + 0.5) * res                        # 行 → x
        self.cy = (idx - n / 2 + 0.5) * res                        # 列 → y

    def grid(self, x, y, yaw, stamp_ns=0):
        n = self.size
        X, Y = np.meshgrid(self.cx, self.cy, indexing="ij")
        dist = np.hypot(X, Y)
        body = (np.abs(X) <= BODY_HL + 0.05) & (np.abs(Y) <= BODY_HW + 0.05)
        fov = (X > BODY_HL) | (self.rear & (X < -BODY_HL))
        known = fov & (dist <= self.rng) & ~body
        c, s = math.cos(yaw), math.sin(yaw)
        wx, wy = x + c * X - s * Y, y + s * X + c * Y
        a = self.world.全部()
        occ = np.zeros_like(known)
        # 一格(0.1 m)里查 4 个点
        for du in (-0.025, 0.025):
            for dv in (-0.025, 0.025):
                qx, qy = wx + c * du - s * dv, wy + s * du + c * dv
                r, k = (qy / RES).astype(int), (qx / RES).astype(int)
                inb = (r >= 0) & (r < a.shape[0]) & (k >= 0) & (k < a.shape[1])
                hit = np.zeros_like(known)
                hit[inb] = a[r[inb], k[inb]]
                occ |= hit
        occ &= known
        self.seq += 1
        return Grid(seq=self.seq, stamp_ns=stamp_ns, res=self.res, size=n,
                    occ=pack_bits(occ.ravel().tolist(), n),
                    known=pack_bits(known.ravel().tolist(), n), rear=self.rear,
                    rear_cal=self.rear_cal,
                    check=self.check, reason="" if self.check == "ok" else "外参歪了")

    def 净空(self, g: Grid, end: str = "head") -> float:
        """跟感知节点一样:机身前方(``end="tail"``:后方)走廊第一个挡 / 未知有多远。"""
        from d1max_localizer.obstacles import Config, clear_distance
        occ, known = g.bits()
        return clear_distance(occ, known, Config(), end=end)
