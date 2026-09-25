"""狗往站点搬文件的契约(W00c5d,决策 8)。

狗专用的 HTTPS 口(mTLS,狗的身份就是它的证书),上行沿用现成的分块续传:
``POST /api/intake/put``,原始字节做 body,元数据走请求头(见 ``d1max_agent.engine.http_sink``);
站点**按自己存下的字节**重算 sha256 回执,狗对上了才算这一块传到。

站点只收**狗能产生的**那几种文件:一趟的清单、事件流、抽样遥测、照片。判读结论、人工复核、报告
都是站点自己生成的,狗传上来的一律不收(免得覆盖站点那一份)。
"""

from __future__ import annotations

import re

WIRE_PATH = "/api/intake/put"
#: 站点**永远不收**这一块(名字不合规、不是狗能产生的文件、跟收齐的内容冲突):回这个状态码,
#: 狗把这个文件隔离起来、不再重试(不然每 5 分钟重传 1 MiB,一天几百 MB 的 4G),这一趟留在狗上等人看。
REFUSED_STATUS = 422
#: 请求头(值用 URL 转义,见 http_sink)。
H_RUN, H_REL, H_OFFSET, H_TOTAL = "X-D1Max-Run", "X-D1Max-Rel", "X-D1Max-Offset", "X-D1Max-Total"
#: 一块最多多大(狗那头一块 1 MiB)。
MAX_CHUNK = 2 * 1024 * 1024

#: 一趟的目录名:``<任务>/<UTC 时刻>``,时刻形如 ``20260925T010000Z``。
#: 可以带数字后缀:同秒第二趟 ``-2``;发件箱模式每趟一个随机后缀(见 ``engine.archive``)。
STAMP_RE = re.compile(r"^\d{8}T\d{6}Z(-\d{1,8})?$")
#: 狗能上传的文件(相对这一趟的目录)。照片另算:``photos/<名字>.jpg``。
DOG_FILES = frozenset({"manifest.json", "events.jsonl", "telemetry.jsonl"})
_PHOTO_RE = re.compile(r"^photos/[^/]{1,250}\.(jpg|jpeg|png)$", re.IGNORECASE)


def dog_may_upload(rel: str) -> bool:
    return rel in DOG_FILES or bool(_PHOTO_RE.match(rel))


def split_run(run: str) -> tuple[str, str] | None:
    """``<任务>/<时刻>`` → (任务, 时刻);形状不对返回 None。"""
    parts = run.split("/")
    if len(parts) != 2 or not parts[0] or not STAMP_RE.match(parts[1]):
        return None
    return parts[0], parts[1]
