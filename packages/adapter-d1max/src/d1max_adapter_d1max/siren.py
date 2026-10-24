"""生成喇叭警笛声(W40,决策 70:上装 V1.2 没有独立警笛,用喇叭循环放这一段)。

    python3 -m d1max_adapter_d1max.siren /var/lib/d1max/clips/siren.wav

一段 2 秒的上下扫频(600–1400 Hz,美式 wail 那种),16 kHz 单声道 16 位,代理循环放。只用标准库,
狗上的系统 Python 就能跑。音量先给 0.8,真机上嫌刺耳再调 ``--gain``(功放增益另说)。
"""

from __future__ import annotations

import argparse
import math
import struct
import wave
from pathlib import Path


def siren(seconds: float = 2.0, rate: int = 16000, low: float = 600.0, high: float = 1400.0,
          gain: float = 0.8) -> bytes:
    n = int(seconds * rate)
    phase, out = 0.0, bytearray()
    for i in range(n):
        x = i / n                                     # 前一半往上扫、后一半往下扫,首尾接得上
        f = low + (high - low) * (1 - abs(2 * x - 1))
        phase += 2 * math.pi * f / rate
        out += struct.pack("<h", int(32767 * gain * math.sin(phase)))
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--gain", type=float, default=0.8, help="0–1")
    a = ap.parse_args()
    if not 0 < a.gain <= 1 or not 0.5 <= a.seconds <= 10:
        raise SystemExit("--gain 要在 0–1,--seconds 要在 0.5–10")
    with wave.open(str(a.out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(siren(a.seconds, gain=a.gain))
    print(f"写好了 {a.out}")


if __name__ == "__main__":
    main()
