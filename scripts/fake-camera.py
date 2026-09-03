#!/usr/bin/env python3
"""假相机 —— 冒充 ffmpeg,给彩排用。

现场之前要把"拍照 → 归档 → 判读 → 报告"整条路走一遍,而那条路的起点是
一次 ffmpeg 调用。这个脚本认得 ``app.video`` 发的那两种命令行:

* 带 ``-frames:v 1`` 的(``RtspStill``,拍照):吐一张 JPEG 就退。
* 不带的(``MjpegSource``,实时画面):按 ``-r`` 的帧率一直吐,直到被杀。

**吐出来的是真 JPEG**,不是几个占位字节 —— 报告要把它 base64 内嵌进 HTML,
浏览器解不开的话这一步就白测了。前后两路给的图不一样,页面上一眼能分清。

用法(跟仿真器一起):

    d1max-app --nav-url ws://127.0.0.1:10010 --ffmpeg scripts/fake-camera.py

要它可执行:``chmod +x scripts/fake-camera.py``。Windows 上直接
``--ffmpeg "python scripts/fake-camera.py"`` 是不行的,``--ffmpeg`` 只收一个
可执行文件名。
"""
from __future__ import annotations

import base64
import sys
import time

#: 前广角那一路。160x120 的深蓝底加一行字。
_FRONT = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBC"
    "YFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/2wBDARweHigjKE4r"
    "K06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSk"
    "pKSkpKT/wAARCAB4AKADASIAAhEBAxEB/8QAGQABAQEBAQEAAAAAAAAAAAAAAAMBAgQF"
    "/8QANBAAAgEDAQcCBQQBBAMAAAAAAAECAxESBCE0U3OTsdIxQRMyUWGRFCJxwaEjQlJi"
    "gdHw/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAL/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oA"
    "DAMBAAIRAxEAPwD5lKlTlSnUqTlFRko/tjle9/uvoMdLxq3SXkIbjV5kO0iJaFsdLxq3"
    "SXkMdLxq3SXkRAFsdLxq3SXkMdLxq3SXkRAFsdLxq3SXkMdLxq3SXkRAFsdLxq3SXkMd"
    "Lxq3SXkRAFsdLxq3SXkMdLxq3SXkRAFsdLxq3SXkMdLxq3SXkRAFsdLxq3SXkMdLxq3S"
    "XkRAFsdLxq3SXkMdLxq3SXkRAFsdLxq3SXkKtKnGlCpTnKSlJx/dHG1rfd/UiWnuNLmT"
    "7RAQ3GrzIdpES0Nxq8yHaREAAAAAAAAAAAAAAAAAAAAAAFp7jS5k+0SJae40uZPtEBDc"
    "avMh2kRLQ3GrzIdpEQAAAAAAAAAAAAAAAAAAAAAAWnuNLmT7RIlp7jS5k+0QENxq8yHa"
    "ROnDOpGF0smld+iKQ3GrzIdpEoNRkm4qST9H7gWnplFVGpSThG7jKFn6pf2UnocdRWp/"
    "E2U4OSlb5rL0/wAP8HEtSnSdKMJYuNllK7W1P6emz/J3PXZOT+HbJz9/ZqVvxkwJQowc"
    "YOpUwc/l2XX0u37bTFRvp3Vy2p/Lb22bfy0bGtDCCnTzcPl27PrZr32mx1MlBU9vw8HF"
    "xy2Nu+389gKT0OOorU/ibKcHJSt81l6f4f4Jw00qlCM4bZSlJJXS2JXZSeuycn8O2Tn7"
    "+zUrfjJkqeowpxjje2e2/wDyjYDFpqzxtFPJpJZL39DqOkqSni8VdSd8lbYrtHdPWKnK"
    "M/h3msU3lsai1b+PRE6WoUIRi4XSc77bXyil/QEQAAAAAAAAAALT3GlzJ9okS09xpcyf"
    "aICG41eZDtI4o0/i1qdO9s5KN/pdncNxq8yHaRKE5U5xnF2lF3T+4F1poOHxFVbpq93j"
    "t2W9Ff8A7I2rpoQp07SbnObV7bLWi13IwrVIRUYy2K+xpNbbX7I16iq2m57VLNOy2P8A"
    "+SApLS21FGkp7KtrO3ptt9fsb+lj8LNVVtTlFOyulf739mSeoqOpCpdKVP5bRSttv6fy"
    "Yq1SNPBS/b6eiv8AkD0fpIwacnknGpsatZxjf/0SjQi4QyqWlUV4rG99tv6Ylqq0mm5K"
    "6v8A7V7qz/m5kNRVpxShK1vR2V1/5ArLSRi25VWoKnnfG7+bG1rmy0KUpJVU1ByUm0lZ"
    "ppbLv7ohPUVJpptWccbKKWy9+5v6mrdtyvduTulZt2v2QHf6WKz/ANW+Moxjir3yTft/"
    "H3ONRR+DKKUslKN1st7tf0Z8erk5Z7XJS9PdencypVnVac2v2qyskrL19v5A4AAAAAAA"
    "ALT3GlzJ9okS09xpcyfaICG41eZDtIiWhuNXmQ7SIgAAAAAAAAAAAAAAAAAAAAAAtPca"
    "XMn2iRLT3GlzJ9ogIbjV5kO0iJaG41eZDtIiAAAAAAAAAAAAAAAAAAAAAAC09xpcyfaJ"
    "EtPcaXMn2iApVacaU6dSEpKUlL9ssbWv9n9RlpeDW6q8QAGWl4NbqrxGWl4NbqrxAAZa"
    "Xg1uqvEZaXg1uqvEABlpeDW6q8RlpeDW6q8QAGWl4NbqrxGWl4NbqrxAAZaXg1uqvEZa"
    "Xg1uqvEABlpeDW6q8RlpeDW6q8QAGWl4NbqrxGWl4NbqrxAAZaXg1uqvEZaXg1uqvEAB"
    "lpeDW6q8RVq05UoU6cJRUZOX7pZXvb7L6AAf/9k="
)

#: 后广角那一路。同样大小,暖色底 —— 跟前路必须一眼分得开。
_BACK = (
    "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"
)


def _pick(argv: list[str]) -> bytes:
    """从 ``-i rtsp://.../<name>`` 里认出这是哪一路。认不出就给前路。"""
    url = ""
    if "-i" in argv:
        index = argv.index("-i") + 1
        if index < len(argv):
            url = argv[index]
    return base64.b64decode(_BACK if url.rstrip("/").endswith("back") else _FRONT)


def _fps(argv: list[str]) -> float:
    if "-r" in argv:
        index = argv.index("-r") + 1
        if index < len(argv):
            try:
                return max(0.1, float(argv[index]))
            except ValueError:
                pass
    return 5.0


def main(argv: list[str]) -> int:
    jpeg = _pick(argv)
    out = sys.stdout.buffer
    # 一次性:``-frames:v 1`` 就是"拿到一帧就退"。取图那条路等的就是进程结束。
    if "-frames:v" in argv:
        out.write(jpeg)
        out.flush()
        return 0
    # 连续:image2pipe 的输出就是一串首尾相接的 JPEG,没有别的分隔。
    interval = 1.0 / _fps(argv)
    try:
        while True:
            out.write(jpeg)
            out.flush()
            time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        # 看的人关了标签页 —— 管道断了是正常收场,不是错。
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
