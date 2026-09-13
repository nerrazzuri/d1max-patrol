"""服务器落盘。**§4.2 的服务器那一半在这儿:哈希是这儿算的。**"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from d1max_console.store import ConsoleStore, PathRefused, safe_join


def test_收一整块_回执里的哈希是自己算的(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "巡检一/20260911T101500Z", "events.jsonl",
                offset=0, data=b'{"kind": "started"}\n')
    assert got.size == 20
    assert got.sha256 == hashlib.sha256(b'{"kind": "started"}\n').hexdigest()


def test_续传_第二块接在第一块后面(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"aaa")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=3, data=b"bbb")
    assert got.size == 6
    assert got.sha256 == hashlib.sha256(b"aaabbb").hexdigest()


def test_offset跳号_拒绝写_并且报真实大小(tmp_path: Path) -> None:
    """中间缺一段。**不许补零,不许照写** —— 那会造出一个哈希永远对不上的文件。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"aaa")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=99, data=b"bbb")
    assert got.size == 3, "报的是盘上真实的大小,狗看见这个数会 rewind"
    assert got.sha256 == hashlib.sha256(b"aaa").hexdigest()


def test_offset往回_是重传_截断后重写(tmp_path: Path) -> None:
    """新块必须比旧块**短**,这样才能验到 ``truncate()`` 真的在干活 ——
    等长覆盖测不出少了 truncate 有什么区别,盘上会不会留下陈旧的尾巴。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"AAAABBBB")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=4, data=b"C")
    assert got.size == 5, "旧的 BBBB 必须被截掉,不能留在 C 后面"
    assert got.sha256 == hashlib.sha256(b"AAAAC").hexdigest()


@pytest.mark.parametrize("坏", [
    "../etc/passwd",
    "a/../../b",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "a//b",
    "",
    "   ",
    "a/\x00b",
    "a/\nb",
    # 反斜杠族。单独一段里带反斜杠,pathlib 在 Windows 上会把它当分隔符
    # 拆成两层子目录,**始终还在 root 底下**,所以这条只有字符闸能拦得住,
    # 结果闸对它完全无感(复评 M3:删掉反斜杠字符闸,这条从绿转红)。
    "a\\b",
    "..\\..\\x",
    # UNC 与盘符相对路径的另外两种写法,C:/Windows/win.ini 之外的形状。
    "\\\\host\\share",
    "C:foo",
    # 冒号但不是"单字母盘符"形状,pathlib 不会把它当换盘符处理,
    # 结果还是落在 root 底下,同样只有字符闸拦得住(复评 M5 的对偶用例)。
    "photos:secret",
    # 尾随空格/点。Win32 在真正 open() 的时候会把最后一段的尾随点和空格
    # 剥掉,resolve() 和按段查都看不出来,只能整段拒掉(复评应修一)。
    ".. ",
    "...",
    ". ",
    "a.",
    "a..",
    # 超长段,不拒会在磁盘层炸成 OSError,跟"盘坏了"长得一样。
    "L" * 260,
    # Windows 保留设备名。**只放整段精确等于这几个词的用例** ——
    # ``NUL.txt`` 这种带扩展名的形状真机上是普通文件,已经挪到
    # test_保留设备名收窄成整段匹配_带扩展名的不再被误杀 里当正例(复评应修 C)。
    "NUL",
    "CON",
    "COM1",
    "LPT1",
    # Windows 文件名语法本身不许出现的 6 个字符(复评应修 A)。狗侧
    # archive.py/mission.py 不消毒,人手打的任务名/点位名能一路带着
    # 这些字符跑完一整趟,传到这儿第一次被拒。
    "a*b",
    "a?b",
    "a<b",
    "a>b",
    "a|b",
    'a"b',
])
def test_路径穿越一律拒(tmp_path: Path, 坏: str) -> None:
    """**这是这一卷唯一一道拦路径穿越的闸。** 拒绝就是拒绝,不清洗后接着用。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", 坏, offset=0, data=b"x")


def test_尾随点造成的路径别名_直接拒绝(tmp_path: Path) -> None:
    """``events.jsonl.`` 和 ``events.jsonl`` 会被 Win32 当成同一个文件 ——
    这不是"清洗一下接着用"能解决的,必须整段拒掉,已经落盘的东西不能被
    这种别名覆盖(复评应修二的原样复现)。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"original")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", "events.jsonl.", offset=0, data=b"ZZZZZZZZ")
    # 原文件必须原封不动,不能被这个"别名" rel 覆盖掉。
    assert s.path_of("D1MAX-01", "r/t", "events.jsonl").read_bytes() == b"original"


def test_safe_join拦住junction逃逸(tmp_path: Path) -> None:
    """**这是第二道闸唯一拦得住、第一道闸完全无感的攻击形状。**

    ``root/SN/`` 底下建一个指向 root 外面的目录联接(junction),每一段
    单看都是合法名字,第一道按段查毫无办法;只有落盘之后 ``resolve()``
    一次、确认结果真的还在 root 底下的第二道闸能拦住它。这条测试删掉
    第二道闸会转红(复评 M4),留着它才对得上"两道都要,谁也不能替谁"
    这句话。

    本机若不能建 junction(比如权限被组策略收紧),就 ``skip`` 而不是
    假装测过 —— 复评在这台机器上实测能建,若环境不同这条会如实报告。
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    sn_dir = root / "D1MAX-01"
    sn_dir.mkdir()
    junction = sn_dir / "escape"
    # mklink 的控制台输出走系统 ANSI 代码页(中文 Windows 上是 GBK),
    # 不是 UTF-8,这里按字节收,自己挑编码解,免得 text=True 用 UTF-8
    # 硬解出 UnicodeDecodeError 把测试输出弄脏。
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, check=False,
    )
    if made.returncode != 0:
        err = made.stderr.decode("gbk", errors="replace").strip()
        pytest.skip(f"本机建不出 junction,跳过:{err}")
    with pytest.raises(PathRefused):
        safe_join(root, "D1MAX-01", "escape", "secret.txt")
    s = ConsoleStore(root)
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "escape", "PWNED.txt", offset=0, data=b"x")


def test_sn和run也要过闸(tmp_path: Path) -> None:
    """三个字段全是狗发来的,三个都不可信。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("../../root", "r/t", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "../..", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        # 尾随空格会被 Win32 剥掉,"D1MAX-02 " 会落进 "D1MAX-02" 的目录树,
        # 等于一只狗能踩进另一只狗的地盘(复评应修二点名的第二个后果)。
        s.put("D1MAX-02 ", "r/t", "a.jpg", offset=0, data=b"x")


def test_sn和run里的win32非法字符也拒(tmp_path: Path) -> None:
    """应修 A 补的六个字符不止拦 rel,sn/run 三个字段都要过闸。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("D1MAX?01", "r/t", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t<1", "a.jpg", offset=0, data=b"x")


def test_emoji超过UTF16上限才拒(tmp_path: Path) -> None:
    """**这条是应修 B 的回归用例。** 128 个 emoji 用 Python ``len()`` 数是
    128,远没到 255,旧代码会放行;但每个 emoji 是非 BMP 字符,在 NTFS
    实际存成 UTF-16 时是一个代理对、占 2 个 code unit,128 个就是 256
    个 code unit,超过 NTFS 的 255 上限,真机上会在磁盘层炸成 ``OSError``。
    按 code unit 计长之后,这条必须在 ``ConsoleStore`` 这一层就被拒。"""
    s = ConsoleStore(tmp_path)
    段 = "😀" * 128
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", f"{段}.jpg", offset=0, data=b"x")


def test_emoji没超UTF16上限时正常收(tmp_path: Path) -> None:
    """**正例,防止把应修 B 修成"非 BMP 字符整类拒绝"。** 120 个 emoji 是
    240 个 UTF-16 code unit,没超 255,是一个合法的人取的名字,该正常收。"""
    s = ConsoleStore(tmp_path)
    段 = "😀" * 120
    got = s.put("D1MAX-01", "r/t", f"{段}.jpg", offset=0, data=b"x")
    assert got.size == 1


def test_中文段落在UTF16上限之内照收(tmp_path: Path) -> None:
    """中文字符在 BMP 里,一个字符正好是一个 UTF-16 code unit,
    250 个字符 = 250 个 code unit,没超 255,照收(应修 B 的另一个正例)。"""
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "r/t", "中" * 250 + ".jpg", offset=0, data=b"x")
    assert got.size == 1


def test_保留设备名收窄成整段匹配_带扩展名的不再被误杀(tmp_path: Path) -> None:
    """**应修 C,这一轮第一优先级的丢数据修复。**

    真机验证过:``CON.txt``、``con.北区``、``NUL.tar.gz`` 在真实 NTFS 上
    都是能正常创建、正常读写的普通文件——Win32 的设备别名只在"整段就是
    这几个词"时生效,不是"去掉扩展名之后是这几个词"就算。上一版按
    ``seg.split(".", 1)[0]`` 取词根来判,比真实情况严太多:一个任务名
    叫 ``con.北区`` 的狗能在本地跑完一整趟,却会在上传时被误杀,而且是
    静默的、永久的丢数据(400 之后狗不会重试)。这条测试钉住收窄之后的
    正确行为——这些名字必须能正常收下,不能再被拒。
    """
    s = ConsoleStore(tmp_path)
    for rel in ("CON.txt", "con.北区.jpg", "NUL.tar.gz", "PRN.jpg", "aux.log"):
        got = s.put("D1MAX-01", "r/t", rel, offset=0, data=b"x")
        assert got.size == 1, f"{rel!r} 应该被正常收下,不该被当成保留设备名拒掉"


def test_保留设备名裸词依然精确拒绝(tmp_path: Path) -> None:
    """收窄之后,整段精确等于保留名(不区分大小写)依然要拒 ——
    这条防止把闸收得太松,把裸的 NUL/CON 也放行了。"""
    s = ConsoleStore(tmp_path)
    for rel in ("NUL", "nul", "CON", "Con", "COM1", "lpt1"):
        with pytest.raises(PathRefused):
            s.put("D1MAX-01", "r/t", rel, offset=0, data=b"x")


def test_sn大小写在两种文件系统上都是缺口_留给上层(tmp_path: Path) -> None:
    """**应修 D:钉住现状,不是宣称这里安全。**

    ``ConsoleStore`` 把 ``sn`` 当大小写敏感的字符串用,而文件系统未必
    这么看。两种文件系统上都是缺口,只是形状不同 ——

    * **大小写不敏感**(开发机的 NTFS、macOS 默认的 APFS):
      ``D1MAX-02`` 和 ``d1max-02`` 落在同一个目录,第二次写**盖掉**
      第一次的文件。一只狗的数据被另一个大小写的"同一只狗"覆盖。
    * **大小写敏感**(服务器跑的 Linux,ext4/xfs):落成两棵独立的树,
      同一只狗被记成两只,配额、清理、名录全部各算各的。

    两种都不对,而且**都不是路径穿越**,所以不归这道闸管。这是身份
    归一化问题("D1MAX-02" 和 "d1max-02" 算不算同一台设备),真正的
    修法是在拿 ``sn`` 当隔离键**之前**,由花名册先核对、换成规范形式
    —— 那是 Task 8/9 的范围,不是这一层的。

    这条测试**不是在证明这里安全**,是防止未来有人扫一眼代码就以为
    这条路已经被挡住了。
    """
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-02", "r/t", "a.jpg", offset=0, data=b"x")
    s.put("d1max-02", "r/t", "a.jpg", offset=0, data=b"yy")

    读回 = s.path_of("D1MAX-02", "r/t", "a.jpg").read_bytes()
    同一个目录 = 读回 == b"yy"
    落盘的 = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    if 同一个目录:
        # 大小写不敏感:两次 put 落在同一棵树上,盘上只有一个目录,
        # 后写的把先写的盖掉了 —— 数据真的没了。
        # 断言的是**目录数**,不是分支条件本身,不然这一支就是同义反复。
        assert len(落盘的) == 1, f"大小写不敏感的盘上该只有一棵树,实得 {落盘的}"
    else:
        # 大小写敏感:各写各的,一只狗被记成两只。
        assert 读回 == b"x"
        assert s.path_of("d1max-02", "r/t", "a.jpg").read_bytes() == b"yy"
        assert 落盘的 == ["D1MAX-02", "d1max-02"], f"该是两棵独立的树,实得 {落盘的}"

    # 无论哪种文件系统,runs_of 都答得出来 —— 它按字符串问,
    # 从来没有把这两个 sn 当成同一只狗来核对过。
    assert s.runs_of("D1MAX-02") == ["r/t"]
    assert s.runs_of("d1max-02") == ["r/t"]


def test_孤立代理字符在闸上拒_不许炸成UnicodeEncodeError(tmp_path: Path) -> None:
    """**这条是修 B 自己带出来的缺陷的回归用例。**

    按 UTF-16 计长用的 ``encode("utf-16-le")`` 默认是 ``strict``,
    碰上孤立代理字符会抛 ``UnicodeEncodeError``。而计长这一步排在
    字符闸**前面**,一炸就是所有闸都没机会拒 —— 本该 400 的请求
    变成 500,Task 8 分不出是畸形输入还是盘坏了,狗会照着 5xx
    无限重试,同时值守屏上多一条假的磁盘告警。

    这个输入够得着真实流量:狗跑 Linux,``os.listdir()`` 对非 UTF-8
    的字节名用 ``surrogateescape`` 解出来的就是孤立代理,
    ``json.dumps`` 默认能把它编出去。

    要的是 ``PathRefused``,不是 ``UnicodeEncodeError``,
    更不是在 ``put()`` 里 catch 了再翻译。
    """
    孤 = "a" + chr(0xD800) + "b"
    s = ConsoleStore(tmp_path)
    for sn, run, rel in (
        (f"D1MAX{孤}", "r/t", "a.jpg"),
        ("D1MAX-01", f"r/{孤}", "a.jpg"),
        ("D1MAX-01", "r/t", f"{孤}.jpg"),
    ):
        with pytest.raises(PathRefused):
            s.put(sn, run, rel, offset=0, data=b"x")


def test_成对代理的emoji不受影响(tmp_path: Path) -> None:
    """正例:合法的非 BMP 字符在内存里不是孤立代理,照收。
    防止把上一条修成"凡是代理区就拒",那会把所有 emoji 一起误杀。"""
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "r/t", "任务😀.jpg", offset=0, data=b"x")
    assert got.size == 1


def test_中文路径照收(tmp_path: Path) -> None:
    """run 目录名是中文的(巡检一),照收不误,闸拦的是穿越,不是非 ASCII。"""
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "变电站一号/20260911T101500Z",
                "photos/P1__front__1757000000.jpg", offset=0, data=b"\xff\xd8")
    assert got.size == 2
    assert s.path_of("D1MAX-01", "变电站一号/20260911T101500Z",
                     "photos/P1__front__1757000000.jpg").exists()


def test_safe_join结果必须落在root底下(tmp_path: Path) -> None:
    with pytest.raises(PathRefused):
        safe_join(tmp_path, "..")
    assert safe_join(tmp_path, "a", "b") == (tmp_path / "a" / "b").resolve()


def test_数得出一只狗有哪几趟(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "巡检一/20260911T101500Z", "a.jpg", offset=0, data=b"x")
    s.put("D1MAX-01", "巡检一/20260911T120000Z", "a.jpg", offset=0, data=b"x")
    s.put("D1MAX-02", "巡检一/20260911T101500Z", "a.jpg", offset=0, data=b"x")
    assert s.runs_of("D1MAX-01") == ["巡检一/20260911T101500Z",
                                     "巡检一/20260911T120000Z"]
    assert s.runs_of("D1MAX-99") == []


def test_负数offset直接拒(tmp_path: Path) -> None:
    """``offset`` 也是狗发来的。不拦的话它会掉进 ``seek()``,
    冒出来一个跟"盘坏了"长得一模一样的 ``OSError``。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(ValueError, match="offset"):
        s.put("D1MAX-01", "r/t", "a.jpg", offset=-1, data=b"x")
