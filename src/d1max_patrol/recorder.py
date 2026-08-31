"""导航链路原始帧录制。

**这个模块的产出是不可复现的。** 代码写错了明天还能改,真机上没录到的帧
是永远补不回来的 —— 客户现场那台机器只有一到两天。所以这里每一处取舍都
倒向"宁可慢、宁可占地方、宁可难看,也不能丢",而不是倒向性能或优雅。

三条由此推出的设计裁定,改之前先读明白为什么:

1. **同步写 + 每行 flush,不用异步队列。**
   导航链路的帧率是个位数 Hz(状态轮询周期 0.5s),同步写盘的开销在事件
   循环上根本量不出来。而"异步队列 + 写盘任务"会引入一整类关机时没排空
   缓冲区的故障 —— 进程在现场被 Ctrl+C 掉、被 OOM 掉、笔记本合盖睡死,
   缓冲区里那几十帧就没了。每行 flush 换来的是:**进程无论怎么死,已经
   录下的部分一定在盘上。**

2. **原样存 `raw` 字符串,不做二次序列化。**
   不要 `json.loads` 之后再 `json.dumps` 存回去 —— 那会抹掉厂商的键顺序、
   空格、数字格式(`1.0` vs `1`)、以及任何编码怪癖。而**这些恰恰是明天
   要拿来跟 61 条 golden fixture 逐字段对拍的东西**。这里存的是线缆上的
   字节,不是它的语义。

3. **录制失败绝不能弄断链路。**
   录制是旁路。磁盘满了、路径没权限、盘被拔了 —— 任何一种都不允许让
   一次导航请求失败。所有写入都吞异常,首次失败记一条 warning 并置
   `failed`,之后彻底闭嘴(否则一次磁盘满能刷几万条日志把真问题淹掉)。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def default_recording_path(runs_dir: str | Path = "runs", *, tag: str = "nav") -> Path:
    """按 UTC 时间戳生成一个不会撞名的录制文件路径。

    文件名用 UTC 而非本地时间: 现场那台笔记本的时区/夏令时是未知量,
    而录制文件要跟事后离线分析的时间轴对得上。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(runs_dir) / "frames" / f"{tag}-{stamp}.jsonl"


class FrameRecorder:
    """把一条链路上的每一帧原样落盘成 JSON Lines。

    用法::

        rec = FrameRecorder(default_recording_path())
        backend = VendorNavBackend(nav_config, recorder=rec)
        ...
        rec.close()

    每行一条记录,字段:

    ``t``    ISO-8601 UTC 墙上时间(带微秒),给人看、给跨设备对时用。
    ``mono`` 自录制开始的单调秒数。**算帧间隔只能用它** —— 墙上时钟会被
             NTP 拽、会跳时区,单调钟不会。
    ``dir``  ``tx``(我们发出) / ``rx``(设备发来) / ``note``(录制器自己
             打的标记,例如建链、断链、阶段分隔)。
    ``src``  链路标识,当前只有 ``nav``;第 3 卷接入 SDK 旁路后会有第二个值。
    ``raw``  线缆上的原始载荷,**逐字节原样**。文本帧存字符串;二进制帧
             无法按 UTF-8 解码时存 base64 并附 ``enc="base64"``。
    """

    def __init__(self, path: str | Path, *, src: str = "nav") -> None:
        self.path = Path(path)
        self.src = src
        #: 首次写入失败后置位。置位之后不再记日志,只是静默丢弃。
        self.failed = False
        #: 成功落盘的行数 —— 现场靠它一眼判断"到底录到东西没有"。
        self.count = 0
        self._t0 = time.monotonic()
        self._fh: Any = None

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" : 不让 Python 在 Windows 上把 \n 翻译成 \r\n。
            # jsonl 是行协议,多出来的 \r 会让 Linux 侧的离线分析脚本把它
            # 当成载荷的一部分。
            self._fh = self.path.open("a", encoding="utf-8", newline="")
        except OSError as exc:
            self.failed = True
            log.warning("无法打开录制文件 %s,本次不录制: %s", self.path, exc)
            return

        self.note("recording_started", path=str(self.path))

    # ----------------------------------------------------------- 写入

    def _write(self, record: dict[str, Any]) -> None:
        """落一行。任何异常都在这里终结,绝不外泄给调用方。"""
        if self.failed or self._fh is None:
            return
        try:
            record["t"] = datetime.now(timezone.utc).isoformat()
            record["mono"] = round(time.monotonic() - self._t0, 6)
            record["src"] = self.src
            # ensure_ascii=False: 厂商载荷里有中文(地图名、故障描述),
            # 存成 \uXXXX 之后现场用 grep / less 看就全是天书。
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            # 见模块文档裁定 1。**删掉这行 flush,进程被 Ctrl+C 掉就会丢帧,
            # 而且不会有任何报错。** 回归测试见 test_recorder.py::
            # test_每写一行都立刻落盘不留在缓冲区里。
            self._fh.flush()
            self.count += 1
        except Exception as exc:  # noqa: BLE001
            # 见模块文档裁定 3。这里绝不能往外抛 —— 抛出去会顺着
            # request() / _read_loop() 一路把链路弄断。
            self.failed = True
            log.warning("录制写入失败,后续帧将不再录制: %s", exc)

    @staticmethod
    def _payload(raw: Any) -> dict[str, Any]:
        """把线上载荷装进记录,尽最大努力保持原样。"""
        if isinstance(raw, (bytes, bytearray)):
            try:
                return {"raw": bytes(raw).decode("utf-8")}
            except UnicodeDecodeError:
                return {"raw": base64.b64encode(bytes(raw)).decode("ascii"),
                        "enc": "base64"}
        return {"raw": raw if isinstance(raw, str) else repr(raw)}

    def record_tx(self, raw: Any) -> None:
        """记一帧我们发出去的报文。"""
        self._write({"dir": "tx", **self._payload(raw)})

    def record_rx(self, raw: Any) -> None:
        """记一帧设备发来的报文。"""
        self._write({"dir": "rx", **self._payload(raw)})

    def note(self, event: str, **fields: Any) -> None:
        """打一条标记 —— 建链、断链、一致性巡检的阶段分隔等。

        标记跟真实帧混在同一个文件里、共用同一条 ``mono`` 时间轴,
        这样离线看的时候"这一串响应属于哪个阶段"是自明的。
        """
        self._write({"dir": "note", "event": event, **fields})

    def close(self) -> None:
        """收尾。重复调用无副作用 —— 现场的关机路径不止一条。"""
        if self._fh is None:
            return
        self.note("recording_stopped", frames=self.count)
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
