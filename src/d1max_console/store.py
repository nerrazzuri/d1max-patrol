"""服务器落盘。**§4.2 的服务器那一半在这儿:哈希是这儿算的。**

这个包是**服务器侧**,跟 ``d1max_patrol`` 平级。它只许从
``d1max_patrol.engine.*`` 里 import **纯数据函数**,
**不许 import ``d1max_patrol.app.*`` 或 ``d1max_patrol.backends.*``**,
后两者带着"能让狗动起来"的东西,而 spec §5.5 说服务器上不许有那个能力。
这条规矩由 ``tests/console/test_no_motion.py`` 盯着。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from d1max_patrol.engine.export import sha256_file

#: 路径段里一律不许出现的字符。反斜杠和冒号是 Windows 的两个后门
#: (``C:foo`` 盘符相对路径、``a\b`` 当分隔符),NUL 是 C 层截断,
#: 剩下六个(``* ? < > | "``)是 Windows 文件名语法本身不许出现的字符 ——
#: 它们在 Linux 上合法,狗侧 ``archive.py``/``mission.py`` 不会拦这些字符
#: (任务名来自手机上人手打的字符串,没有消毒),所以一个带 ``?`` 的任务名
#: 能在狗本地跑完一整趟,传到这儿才第一次被拒。不在这儿拒,就会在磁盘层
#: 炸成 ``OSError [Errno 22]``,跟"盘坏了"长得一模一样。
_坏字符 = frozenset("\\:\x00*?<>|\"")

#: NTFS 单个路径段的长度上限,单位是 **UTF-16 code unit**,不是 Python
#: 的字符数(code point)。NTFS 内部按 UTF-16 存文件名,非 BMP 字符
#: (比如大部分 emoji)在 UTF-16 里是一个代理对、占两个 code unit,但
#: Python 的 ``len()`` 只算一个 code point。``"😀" * 200`` 用 ``len()``
#: 量是 200,过得了这道闸,实际写到 NTFS 上是 400 个 code unit,照样炸
#: ``OSError``。这里量的是 code unit 数,不是把非 BMP 字符整类拒掉 ——
#: emoji 出现在人取的任务名里是正常事,不该因为"是 emoji"就被拒。
_MAX_SEG_LEN = 255


def _utf16_len(s: str) -> int:
    """按 UTF-16 code unit 数量计长,对齐 NTFS 的真实计量方式。"""
    return len(s.encode("utf-16-le")) // 2


#: Windows 保留设备名。**整段精确匹配,不区分大小写,不看有没有扩展名。**
#:
#: 真机验证过:只有整段就是 ``NUL``/``CON``/``COM1`` 这类词本身才是设备,``CON.txt``、
#: ``con.北区``、``nul.tar.gz`` 在真实 NTFS 上都是能正常建出来、正常读写
#: 的普通文件/目录 —— Win32 的设备别名只在"整段就是这几个词"时生效,
#: 不是"去掉扩展名之后是这几个词"就算。上一版按 ``seg.split(".", 1)[0]``
#: 取词根来判,比真实情况严太多:一个任务名叫 ``con.北区`` 的狗能在本地
#: 跑完一整趟,却会在上传时被这道闸误杀,而且是**静默的、永久的**丢
#: 数据 —— 400 之后狗不会重试。收窄成整段匹配是安全的:``ConsoleStore``
#: 落盘后会自己重算 sha256(见 ``put``),如果哪个名字真的撞上了设备,
#: 那会在哈希校验那一层大声炸出来,不会是这种悄无声息的数据丢失。
#: 两害相权,取其轻。
_保留设备名 = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


class PathRefused(ValueError):
    """路径不合规。**拒绝就是拒绝,不清洗后接着用。**

    清洗是在猜对方想干什么。一个正常的狗不会发 ``..``,发了就是有人在试,
    那一刻要做的是留下拒绝的记录,不是帮他把路径修好。
    """


def safe_join(root: Path, *parts: str) -> Path:
    """把网络上来的路径片段接到 ``root`` 底下。**两道闸,都要过。**

    第一道按**段**查:``..``、绝对路径、空段、控制字符、超长段、Windows
    保留设备名、**尾随的空格和点**。最后这一条是补的 —— Win32 在 syscall
    时会把最后一段的尾随点和空格剥掉(``"a."`` 和 ``"a"`` 落到同一个文件),
    这道剥离发生在 ``resolve()`` 和这道段级查之后、真正 ``open()`` 之前,
    两道闸都看不见,只能在这儿把带尾随点/空格的段整段拒掉,不是拒
    "剥完之后变空的" 那一小撮。
    第二道按**结果**查:接完之后 ``resolve()`` 一次,确认它确实还在 ``root``
    底下,Windows 上 ``C:foo`` 这种盘符相对路径按段查看不出来。
    """
    root = root.resolve()
    cur = root
    for part in parts:
        for seg in part.split("/"):
            if not seg or not seg.strip():
                raise PathRefused(f"空路径段:{part!r}")
            if seg in {".", ".."}:
                raise PathRefused(f"路径里有 {seg!r}:{part!r}")
            seg_len = _utf16_len(seg)
            if seg_len > _MAX_SEG_LEN:
                raise PathRefused(f"路径段过长({seg_len} 个 UTF-16 code unit):{part!r}")
            if seg != seg.rstrip(" ."):
                raise PathRefused(
                    f"路径段有尾随空格或点,Win32 打开文件时会把它们剥掉,"
                    f"造成两个不同的名字指向同一个文件:{seg!r}"
                )
            if seg.upper() in _保留设备名:
                raise PathRefused(f"路径段是 Windows 保留设备名:{seg!r}")
            if _坏字符 & set(seg) or any(ord(c) < 32 for c in seg):
                raise PathRefused(f"路径段里有不许出现的字符:{seg!r}")
            cur = cur / seg
    out = cur.resolve()
    # **第二道。** 前面按段查过一遍了,这儿查的是结果,两道判据不同,
    # 谁也不能替谁。
    if out != root and root not in out.parents:
        raise PathRefused(f"接出来的路径跑到 {root} 外面去了:{out}")
    return out


@dataclass(frozen=True)
class Stored:
    """落盘之后的实况。``sha256`` 是**从盘上读出来重算的**,不是回显。"""

    size: int
    sha256: str


class ConsoleStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_of(self, sn: str, run: str, rel: str) -> Path:
        return safe_join(self.root, sn, run, rel)

    def size_of(self, sn: str, run: str, rel: str) -> int:
        p = self.path_of(sn, run, rel)
        return p.stat().st_size if p.exists() else 0

    def runs_of(self, sn: str) -> list[str]:
        """这只狗盘上有哪几趟。``<mission>/<stamp>`` 两级,排好序。"""
        base = safe_join(self.root, sn)
        if not base.is_dir():
            return []
        out = []
        for mission in sorted(p for p in base.iterdir() if p.is_dir()):
            for stamp in sorted(p for p in mission.iterdir() if p.is_dir()):
                out.append(f"{mission.name}/{stamp.name}")
        return out

    def put(self, sn: str, run: str, rel: str, *,
            offset: int, data: bytes) -> Stored:
        """收一块,落盘,**然后自己从盘上读出来算哈希**。

        三种 offset:

        - 正好接上,追加
        - 比盘上小,这是重传,**截断到那个位置再写**
        - 比盘上大,**中间缺了一段。不写,把盘上真实的大小报回去**,
          狗看见 ``stored`` 比自己以为的小就会 rewind(见 ``engine/uploader.py``)。
          补零或者照写都会造出一个哈希永远对不上的文件,那种文件只会让
          狗无限重传,而没有任何一次能成功。

        ``offset`` 本身也是狗发来的,不可信:负数会一路走到 ``fh.seek()``
        抛出 ``OSError``,在调用方眼里跟"盘坏了"长得一模一样,所以在这里
        就地拦下,翻成语义清楚的 ``ValueError``。
        """
        if offset < 0:
            raise ValueError(f"offset 不能是负数:{offset}")
        path = self.path_of(sn, run, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        have = path.stat().st_size if path.exists() else 0
        if offset > have:
            return Stored(size=have, sha256=self._hash(path))
        with open(path, "r+b" if path.exists() else "w+b") as fh:
            fh.seek(offset)
            fh.write(data)
            fh.truncate(offset + len(data))
            fh.flush()
            os.fsync(fh.fileno())
        return Stored(size=path.stat().st_size, sha256=self._hash(path))

    @staticmethod
    def _hash(path: Path) -> str:
        """整个文件重算一遍。**已知代价:每收一块就整文件重算,对大文件是 O(n²)。**

        真机上没量过(挂账 98)。这一版就这么写,先正确再快。照片几百 KB、
        ``events.jsonl`` 几十 KB,今天这个量级付得起。**别为了快把它改成
        增量哈希然后信任狗发来的起点**,§4.2 要的正是"服务器自己从盘上
        读出来算的那个数"。
        """
        if not path.exists():
            return hashlib.sha256(b"").hexdigest()
        return sha256_file(path)
