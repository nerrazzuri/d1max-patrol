#!/usr/bin/env python3
"""Erase the false arc wall from maps/newdog2_floor (glass-reflection / ray-leak ghost,
hand-marked by the operator as "not a real wall"). Headless equivalent of map_editor's
Free pen; writes maps/newdog2_floor_edited.{pgm,yaml}, source map untouched.

Run from the repo root:  python3 tools/lio/mapedit/erase_arc_newdog2.py
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_editor import load_map, save_map, OCC, FREE, UNKNOWN
import numpy as np
from scipy import ndimage

SRC, OUT = 'maps/newdog2_floor.pgm', 'maps/newdog2_floor_edited.pgm'
a, meta, _ = load_map(SRC); H, W = a.shape
res = meta['resolution']; ox, oy = meta['origin'][:2]
rr, cc = np.mgrid[0:H, 0:W]
X = ox + (cc + .5) * res; Y = oy + (H - rr - .5) * res          # pixel centres, world

# rough centreline read off the map (world m)
P = np.array([(-1.2,-10.0),(0,-9.4),(1,-8.85),(2,-8.2),(3,-7.4),(3.5,-6.9),(4,-6.35),(4.4,-5.8)])
def dist_polyline(X, Y, P):
    d = np.full(X.shape, np.inf)
    for (x0,y0),(x1,y1) in zip(P[:-1], P[1:]):
        vx, vy = x1-x0, y1-y0
        t = np.clip(((X-x0)*vx + (Y-y0)*vy)/(vx*vx+vy*vy), 0, 1)
        d = np.minimum(d, np.hypot(X-(x0+t*vx), Y-(y0+t*vy)))
    return d
occ = a == OCC
seed = occ & (dist_polyline(X, Y, P) < 0.30)

# circle fit (algebraic, then trim outliers twice) on seed pixels
xs, ys = X[seed], Y[seed]
for _ in range(3):
    A = np.c_[2*xs, 2*ys, np.ones_like(xs)]
    sol, *_ = np.linalg.lstsq(A, xs**2 + ys**2, rcond=None)
    cx, cy = sol[0], sol[1]; R = np.sqrt(sol[2] + cx*cx + cy*cy)
    e = np.abs(np.hypot(xs-cx, ys-cy) - R)
    keep = e < max(0.10, 2*np.median(e)); xs, ys = xs[keep], ys[keep]
print(f'circle: centre=({cx:.2f},{cy:.2f}) R={R:.2f} m, inlier px={len(xs)}, median resid={np.median(e):.3f} m')

# erase band: on the circle, inside the arc's angular span, stopping short of both real walls
ring = np.abs(np.hypot(X-cx, Y-cy) - R) < 0.17
span = (X > -1.12) & (X < 4.42) & (Y > -10.2) & (Y < -5.75)
mask = occ & ring & span & (dist_polyline(X, Y, P) < 0.45)
print('erase px:', mask.sum())
out = a.copy(); out[mask] = FREE

# pass 2: leftovers. The arc is a bit thicker than the ring on its outer edge, leaving
# a trail of specks. Remove only SMALL occ/unknown components hugging the circle, so
# anything attached to real structure (furniture, walls) is untouched by construction.
d = np.hypot(X-cx, Y-cy) - R
near = (np.abs(d) < 0.35) & span
for val in (OCC, UNKNOWN):
    lbl, n = ndimage.label(out == val, structure=np.ones((3,3)))
    if n:
        sizes = ndimage.sum(np.ones_like(lbl), lbl, index=np.arange(1, n+1))
        inside = ndimage.minimum(near, lbl, index=np.arange(1, n+1)) > 0   # component fully inside band
        kill = np.isin(lbl, np.nonzero((sizes < 40) & inside)[0] + 1)
        out[kill] = FREE; mask |= kill
# left stub welded to the wall corner: free space all around, so a slightly wider ring is safe here
stub = (out != FREE) & (d > -0.2) & (d < 0.32) & (X > -1.2) & (X < -0.4) & (Y < -9.5) & (Y > -10.2)
out[stub] = FREE; mask |= stub
print('total erased px:', mask.sum())
save_map(out, meta, OUT); print('wrote', OUT)
