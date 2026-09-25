"""代理判一帧遥控能不能执行(W00c5c;决策 7:规矩由代理执行,站点出错最坏只会让狗停下)。

三条,一条不过就丢(不回执、只计数):

1. **租约代次**等于当前这一次遥控的代次 —— 别的代次(旧连接残留、被接管的那一位)的帧不执行。
2. **序号严格增**:重复的、回退的都不执行(补投、乱序)。被判「在途积压」丢掉的帧序号照样往前走:
   憋着的旧帧之后才到,也不许再执行。
3. **在途时间不超标**:狗与站点的钟不必对准(Orin 的钟现场就错过)。记「到达(狗钟)− 发出(站点钟)」
   的滑动最小值当基线(= 钟差 + 最小在途);一帧比基线多出 ``max_transit_ms`` 就是在网络里憋久了,
   它说的摇杆位置已经不是操作员此刻的意思。

帧本身还带 ``ttl_ms``(默认 300):执行的速度命令就用这个有效期,帧不来了,HAL 与旁路进程两层到期自停。
"""

from __future__ import annotations

from collections import deque

from d1max_contract.teleop import TeleopFrame

#: 在途时间比基线多出多少就算积压(毫秒)。
MAX_TRANSIT_MS = 250
#: 基线取最近多少帧的最小值。10 Hz 下约 5 s:钟慢慢漂也跟得上。
BASELINE_WINDOW = 50


class FrameGate:
    def __init__(self, *, lease_epoch: int, max_transit_ms: int = MAX_TRANSIT_MS,
                 window: int = BASELINE_WINDOW) -> None:
        self.lease_epoch = lease_epoch
        self._max = max_transit_ms
        self._offsets: deque[int] = deque(maxlen=window)
        self._last_seq = 0
        #: 丢了多少帧、为什么(看得见才查得到)。
        self.dropped: dict[str, int] = {"epoch": 0, "seq": 0, "late": 0}

    def accept(self, frame: TeleopFrame, *, rx_ms: int) -> str:
        """收下返回空串;否则返回丢掉的原因(``epoch``/``seq``/``late``)。"""
        if frame.lease_epoch != self.lease_epoch:
            return self._drop("epoch")
        if frame.seq <= self._last_seq:
            return self._drop("seq")
        self._last_seq = frame.seq
        offset = rx_ms - frame.sent_at
        self._offsets.append(offset)
        if offset - min(self._offsets) > self._max:
            return self._drop("late")
        return ""

    def _drop(self, why: str) -> str:
        self.dropped[why] += 1
        return why
