#!/usr/bin/env python3
"""把占据栅格地图拟合成干净的长方形房间图（cv2.minAreaRect 拟合四面墙）。

用法: fit_rect.py <in_stem> <out_stem>
读 <in_stem>.pgm/.yaml → 写 <out_stem>.pgm/.yaml/.png（同坐标系，可给定位/看板用）。
另出 <out_stem>_overlay.png：红框叠在真实墙点上，用来验证拟合贴不贴。
"""
import shutil
import sys

import cv2
import numpy as np


def read_pgm(path):
    d = open(path, "rb").read()
    i = 2
    v = []
    while len(v) < 3:
        while d[i] in b" \t\n\r":
            i += 1
        s = i
        while d[i] not in b" \t\n\r":
            i += 1
        v.append(int(d[s:i]))
    w, h, _ = v
    i += 1
    return np.frombuffer(d[i:i + w * h], np.uint8).reshape(h, w), w, h


def main():
    ins, outs = sys.argv[1], sys.argv[2]
    a, w, h = read_pgm(ins + ".pgm")
    ys, xs = np.where(a < 65)                       # 墙点（占据）
    if len(xs) < 8:
        print("墙点太少，拟合不了")
        return
    rect = cv2.minAreaRect(np.column_stack([xs, ys]).astype(np.int32))
    (cx, cy), (rw, rh), ang = rect
    print(f"拟合矩形 {rw*0.05:.1f}m × {rh*0.05:.1f}m  角度{ang:.1f}°  (墙点{len(xs)})")
    box = cv2.boxPoints(rect).astype(np.int32)

    out = np.full((h, w), 205, np.uint8)            # 外=未知
    cv2.fillPoly(out, [box], 254)                   # 内=空闲
    cv2.polylines(out, [box], True, 0, 2)           # 边=墙
    open(outs + ".pgm", "wb").write(f"P5\n{w} {h}\n255\n".encode() + out.tobytes())
    shutil.copy(ins + ".yaml", outs + ".yaml")
    import os
    y = open(outs + ".yaml").read().replace(os.path.basename(ins) + ".pgm",
                                            os.path.basename(outs) + ".pgm")
    open(outs + ".yaml", "w").write(y)
    cv2.imwrite(outs + ".png", cv2.resize(out, (w * 4, h * 4), interpolation=cv2.INTER_NEAREST))

    rgb = np.stack([a, a, a], -1).copy()
    cv2.polylines(rgb, [box], True, (0, 0, 255), 1)
    cv2.imwrite(outs + "_overlay.png",
                cv2.resize(rgb, (w * 5, h * 5), interpolation=cv2.INTER_NEAREST))
    print(f"写出 {outs}.pgm/.yaml/.png + {outs}_overlay.png")


if __name__ == "__main__":
    main()
