"""交付件的**内容**护栏:骨架与篇幅、厂商私有协议特征、PIN 位数的耦合。

**跟 ``test_deploy_files.py`` 的分工**:那一份守的是每一份交付件**自己**的
约定(单元里那几行、脚本的七步、清单里那几条验收);这一份守的是**整份名单
上都得成立**的三件事。名单直接 import 那边的 ``交付文件()``,不另抄一份 ——
抄一份的话,加第七份交付件那天两份名单会各走各的,而这正是护栏最容易悄悄
失效的方式。

三件事各自防的是什么:

* **骨架与篇幅**:六份交付件里有两份(``docs/任务包格式.md``、
  ``docs/值守与告警.md``)长期只被遍历名单的泄密护栏扫过,而泄密护栏查的都是
  「不许出现某某」—— **空文件全部通过**。把这两份清空成 0 字节,原来那一整
  批测试一条都不会红。这里把「每一份都得有骨架和字数下界」做成**遍历名单的
  一条**,于是加第七份交付件时它自动被要求补一行(见
  ``test_每一份交付件都在骨架表上``)。
* **厂商私有协议特征**:交付件红线里「不许出现厂商私有协议细节」这一条,
  原来只有一个 ``VENDOR_WS_PORT`` 钉住 10010 那一个数,函数名和报文形状一个
  都不查。
* **PIN 位数**:``install.sh`` 里的 ``10**6`` 和泄密护栏那条 ``\\d{6}`` 互不
  知情。谁把生成器改成八位,两份交付件里「六位数字」当场变成假话,而那条
  六位数护栏会**静默失效**(八位数不会被 ``(?!\\d)`` 收下),一条测试都不红。

**这份文件里写厂商协议特征时,一律只写前缀、形状、正则,不抄任何一段真的
报文样例。** 仓是私有的,``refs/`` 底下是厂商的私有协议文档;测试文件虽然
不是交付件,也不该成为第七个泄露面。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from d1max_agent.engine.release import _NAME_RE
from tests.test_deploy_coexist import 有bash
from tests.test_deploy_files import DEPLOY, ROOT, 交付文件, 六位数

# --------------------------------------------------------------- 一、没被掏空

#: 每一份交付件的「没被掏空」判据:一组必须出现的**骨架标记**,外加一个
#: **字数下界**。
#:
#: 骨架取的是各自**最稳的那一层** —— 编号小节的前缀、单元文件的段名、脚本
#: 的步号 —— 而不是完整标题:标题的措辞随时会被改得更好,那不该让护栏变红,
#: 而「第三节整节没了」必须变红。
#:
#: 字数下界按各自**现状**留足余量(现状写在每一行的注释里),普遍留了三成
#: 以上。它不是在管字数,是在拦「内容被掏空,只剩下够骗过关键词检查的骨架」;
#: 定得贴着现状的话,别人正常改文档就会被它撞红,那种护栏活不过一个星期。
骨架与下界: dict[Path, tuple[tuple[str, ...], int]] = {
    # 现状约 9100 字。骨架是屏幕上那七步 —— 它们同时是 docs/装机清单.md 第二节
    # 的对应物,少一步就是现场照着清单跑会跑空的那种错。
    DEPLOY / "install.sh": (
        ("1/7", "2/7", "3/7", "4/7", "5/7", "6/7", "7/7"), 6000),
    # 现状约 1900 字。systemd 单元的三个段,少一个这份单元就不是单元了。
    DEPLOY / "d1max-agent.service": (
        ("[Unit]", "[Service]", "[Install]"), 1200),
    # 现状约 1600 字。代理的启动参数都在这儿(W00c5d 第三部分内部评审 B1)。
    DEPLOY / "d1max-agent-start": (
        ("exec", "--transport", "--tls-key", "--hal", "--outbox", "D1MAX_AGENT_ARGS"), 1000),
    # 实测 8700 上下 —— 采集要覆盖 snapshot、diff 两条路和端口那一段,
    # 少于下界说明有人把兜底或者某一类采集删了
    DEPLOY / "footprint.sh": (
        ("snapshot", "diff", "FILE|", "PORT|", "maxdepth", "timeout"), 6000),
    # 实测 14000 上下 —— 五个步骤加上对账声明块;**下界卡得住**
    # "有人把 patrol_agent 那一段删掉" 这种改法
    DEPLOY / "uninstall.sh": (
        ("1/5", "2/5", "3/5", "4/5", "5/5", "--dry-run", "--keep-data"), 9000),
    # 现状约 12000 字(W00c5e 重写)。六个编号小节。
    ROOT / "docs" / "装机清单.md": (
        ("## 一、", "## 二、", "## 三、", "## 四、", "## 五、", "## 六、"), 6000),
    # 现状约 5000 字(W00c5e 删了狗上落包那两节)。这一份的小节没有编号,骨架就是标题本身。
    ROOT / "docs" / "任务包格式.md": (
        ("## bundle.yaml", "## schedule.yaml", "## missions/",
         "## 哪些东西不进包", "## 狗上没有任务包", "## 接口"), 3500),
    # 现状约 3400 字(W00c5e 按站点那一套重写)。九个编号小节 —— 跟 test_鉴权文档没有被掏空
    # 是同一份判据,这里重复一遍是因为这条是**遍历名单**的:那一份哪天被挪走或改名,这一份
    # 对鉴权文档的覆盖不该跟着一起没。
    ROOT / "docs" / "鉴权与控制权.md": (
        ("## 零、", "## 一、", "## 二、", "## 三、", "## 四、",
         "## 五、", "## 六、", "## 七、", "## 八、"), 3000),
    # 现状约 7200 字。七个编号小节。**不钉那一节「附:……」** —— 它是给维护者
    # 看的、随时可能被整节移出交付面,钉住它等于拦着别人做对的事。
    ROOT / "docs" / "值守与告警.md": (
        ("## 一、", "## 二、", "## 三、", "## 四、",
         "## 五、", "## 六、", "## 七、"), 5000),
}


def test_每一份交付件都在骨架表上():
    """**这一条是上面那张表的活口。**

    没有它,加第七份交付件时会发生的事是:名单上多了一份,泄密护栏自动盖过去
    了(那几条都遍历 ``交付文件()``),内容护栏却因为表里没有这一行而**默默
    跳过**,没有任何信号。所以两个方向都要对得上:名单里的每一份都得在表上,
    表上也不许留着已经不在名单里的死行。
    """
    名单 = 交付文件()
    缺 = [文件.name for 文件 in 名单 if 文件 not in 骨架与下界]
    assert not 缺, f"这几份交付件还没有内容护栏,去 骨架与下界 里各补一行:{缺}"
    多 = [文件.name for 文件 in 骨架与下界 if 文件 not in set(名单)]
    assert not 多, f"骨架与下界 里这几行已经不在交付名单上了,是死行:{多}"


def test_交付件没有被掏空():
    """泄密护栏查的都是「不许出现某某」,而**空文件全都符合**。

    把 ``docs/任务包格式.md`` 清空成 0 字节,``read_text`` 照常返回空串,五条
    泄密护栏一条不落地通过 —— 那份文档在名单上、在盘上、是 0 字节,而测试全
    绿。这条查的是反过来的一面:骨架还在不在,篇幅还够不够。
    """
    for 文件 in 交付文件():
        骨架, 下界 = 骨架与下界[文件]
        assert 文件.exists(), f"交付件 {文件.name} 不在盘上"
        text = 文件.read_text(encoding="utf-8")
        for 记 in 骨架:
            assert 记 in text, f"{文件.name} 少了骨架标记 {记!r},像是被掏空或者整节被删了"
        assert len(text) >= 下界, (
            f"{文件.name} 只剩 {len(text)} 字(下界 {下界}),像是被掏空了")


# ------------------------------------------------- 二、厂商私有协议不许进交付面

#: 厂商导航 WebSocket 协议的特征。**全部是前缀、形状、正则,没有一段是可用的
#: 报文样例** —— 仓是私有的,``refs/`` 底下那份协议文档不该借这份测试再漏一次。
#:
#: 为什么钉前缀不钉清单:厂商加一个新接口,只要它还姓这几个姓,这里照样拦得
#: 住;而抄一份完整的函数名清单,既会过期,本身也更像是把人家的接口目录誊了
#: 一遍。
#:
#: 为什么这条红线要有护栏:交付文档会被复制到每一个站点、贴进每一张工单。
#: 厂商协议细节一旦跟着文档散出去,是撤不回来的 —— 跟热点口令、设备 PIN 同
#: 一类问题,只是它不像口令那样一眼就认得出来,所以更需要机器来盯。
厂商协议特征 = (
    # 报文信封的几个结构键。我们自己的 HTTP 接口没有任何一个字段叫这些名字,
    # 出现即意味着有人照抄了一段厂商报文进来。
    re.compile(r"\breq_func\b"),
    re.compile(r"\breq_result\b"),
    re.compile(r"\bframe_count\b"),
    re.compile(r"\btime_stamp\b"),
    # head 里那两个枚举的形状:消息类型一律 app_ 开头,来源是厂商的节点名。
    re.compile(r"\bapp_(?:req|resp|sub_topic|topic_req|topic_resp)\b"),
    re.compile(r"\b(?:alg_control_node|robot_roamerx)\b"),
    # 响应外壳那个类名的形状(厂商把 Response 拼成了 Reponse,两种拼法都认)。
    re.compile(r"\bApp(?:Res|Rep)[a-z]*Object[A-Za-z]*"),
    # 导航一族方法名的前缀。
    re.compile(r"\b(?:start|stop|pause|continue|get)_nav(?![a-z])"),
    re.compile(r"\b(?:get|set)_navigation_speed\b"),
    # 建图与地图管理一族。
    re.compile(r"\b(?:start|stop|get)_mapping(?![a-z])"),
    re.compile(r"\bnotify_[a-z_]+_status\b"),
    re.compile(r"\bget_(?:all_)?(?:pgm_map|paths_by_mapid)(?![a-z])"),
    re.compile(r"\b(?:add|remove|rename)_(?:nav_path|map)(?![a-z])"),
    # 定位一族。
    re.compile(r"\b(?:loc_load_map|get_loc_status|reset_loc)\b"),
    # 回充(ARC)一族。
    re.compile(r"\b(?:get|set|start|stop|exit)_[a-z_]*(?:arc|charging)(?![a-z])"),
)


def test_交付件里不许出现厂商私有协议特征():
    """红线清单里「厂商私有协议细节」这一条,原来一条护栏都没有。

    原来只有 ``VENDOR_WS_PORT`` 钉着 10010 那**一个数**,函数名、报文形状一个
    都不查 —— 也就是说这条红线纯靠人守,而六份交付件会被复制到每一个站点。
    这里遍历名单查特征,加第七份交付件时它自动被盖上。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        for 模式 in 厂商协议特征:
            assert 模式.search(text) is None, (
                f"{文件.name} 里出现了厂商私有协议特征:{模式.pattern}")


def test_厂商协议特征本身不是一段可用的样例():
    """**这一条守的是这份测试文件自己。**

    仓是私有的,``refs/`` 底下是厂商的私有协议文档。上面那组特征要是哪天被人
    「补全」成一份真报文 —— 带上花括号、带上键值对 —— 这份测试就成了第七个
    泄露面,而且比交付件更不容易被想起来检查。所以在这儿钉死:每一条都只能是
    前缀 / 形状 / 正则,不许出现 JSON 标点,也不许长得离谱。
    """
    for 模式 in 厂商协议特征:
        assert not set(模式.pattern) & set("\"'"), (
            f"这条特征带上了引号,像是把真报文的键值对抄进来了:{模式.pattern}")
        assert len(模式.pattern) <= 80, (
            f"这条特征太长了,像是誊了一整段报文:{模式.pattern}")


# ------------------------------------------------------------- 三、PIN 位数

#: 设备 PIN 是几位数。**这个数在三处互不知情的地方各被假设了一遍**:
#:
#: 1. ``deploy/install.sh`` 里生成 PIN 的 ``randbelow(10**6)`` 与 ``:06d``;
#: 2. ``test_deploy_files.py`` 里那条泄密护栏的 ``六位数`` 正则;
#: 3. 两份交付文档里「六位数字」「一百万种」这类白纸黑字的说法。
#:
#: 谁把 1 改成八位,2 会**静默失效**(八位数不会被 ``(?!\d)`` 收下,于是一条
#: 抄进文档的真 PIN 再也不会被拦下),3 当场变成假话,而**一条测试都不会红**。
#: 这个常量就是把 1 和 2 拴在一起的那根绳子。第 3 处没有拴进来,理由见
#: ``test_PIN位数没有第三个说法``。
PIN_位数 = 6


def test_泄密护栏认的就是这么多位():
    """那条 ``六位数`` 正则必须跟上面同一个常量走。

    **不查它的 pattern 字面量,查它的行为** —— 换个等价写法(比如
    ``\\d{5}\\d``)不该让护栏变红,而位数真的变了必须变红。所以三件事一起查:
    正好这么多位要收,多一位、少一位都不许收。多一位那一头是关键:八位 PIN
    抄进文档而 ``(?!\\d)`` 把它放过去,正是这条护栏静默失效的样子。
    """
    正好 = "1" * PIN_位数
    assert 六位数.fullmatch(正好) is not None, (
        f"六位数 那条正则收不下 {PIN_位数} 位,它跟 install.sh 已经对不上了")
    assert 六位数.search("1" * (PIN_位数 + 1)) is None, (
        f"六位数 那条正则把 {PIN_位数 + 1} 位也收了,前后那两个断言失效了")
    assert 六位数.search("1" * (PIN_位数 - 1)) is None, (
        f"六位数 那条正则把 {PIN_位数 - 1} 位也收了,会误伤一批正当的数字")


# ------------------------------------------------- 四、shell 标识符一律 ASCII
#
# **这一节存在的全部理由,是下面这件真事。**
#
# ``deploy/install.sh`` 的变量名和函数名原来是中文(``根=/opt/d1max``、
# ``说() {...}``)。bash 的变量名**只认 ASCII**(``legal_identifier()`` 是逐
# 字节判的),``根=/opt/d1max`` 因此不是赋值,而是「执行一条叫
# ``根=/opt/d1max`` 的命令」;配上第 4 行的 ``set -euo pipefail``,脚本在
# **第一行有效语句上**就退 127 —— 也就是说,这份交付件一行都跑不起来,而它
# 明天要在 Orin NX 上跑。跟 locale 无关,``bash -n`` 也查不出来:语法是合法的,
# 只是语义完全不是作者想的那样。
#
# 它能活到今天,是三件事凑齐的:
#
# 1. 既有的那些护栏**只 grep 脚本的内容**,从来没有真的执行过这份脚本;
# 2. 开发机是 Windows,脚本的目标是 Linux,没有人顺手跑过一次;
# 3. ``test_deploy_coexist.py`` 里那条 ASCII 护栏当时只 parametrize 了
#    ``uninstall.sh`` 和 ``footprint.sh`` 两份 —— **漏掉的恰好是 install.sh**。
#
# 所以这一节按**名单**扫,而且名单是 glob 出来的:新加一份 shell 脚本,它自动
# 被盖上,不需要谁记得回来补一行。

#: 扫哪些脚本。``deploy/*.sh``、``scripts/*.sh``,外加仓库根上的
#: ``field-*.sh`` —— 现场那几份手动脚本同样是要在狗上跑的。
def shell脚本() -> list[Path]:
    return (sorted(DEPLOY.glob("*.sh"))
            + sorted((ROOT / "scripts").glob("*.sh"))
            + sorted(ROOT.glob("field-*.sh")))


#: 标识符里能出现的字符:ASCII 的字母数字下划线,**外加**任何非 ASCII 字符。
#: 后者不是笔误 —— 这条正则要能把「作者以为是标识符的那一串」整个圈出来,
#: 才好在报错里把它原样打给人看。
_标识符字符 = r"[A-Za-z0-9_\u0080-\U0010ffff]"
#: 至少含一个非 ASCII 字符的「标识符」。
_带中文的标识符 = r"(?:" + _标识符字符 + r"*[^\x00-\x7f]" + _标识符字符 + r"*)"

#: 命令位上的赋值:``名=值``、``local 名=值``、``名+=值``、``名[i]=值``。
#: **只认命令位(行首)** —— ``echo 版本=1`` 里那个 ``版本=`` 在 bash 眼里只是
#: 一个普通参数,不是赋值,拦它就是误报。
_赋值 = re.compile(
    r"^\s*(?:(?:local|export|declare|readonly|typeset)\s+(?:-\w+\s+)*)?"
    r"(" + _带中文的标识符 + r")(?:\[[^\]]*\])?\+?=")
#: 函数定义:``名() {``,带不带 ``function`` 都认。
_函数 = re.compile(r"^\s*(?:function\s+)?(" + _带中文的标识符 + r")\s*\(\s*\)")
#: 变量引用:``$名``、``${名}``、``${#名}``。**只认以非 ASCII 开头的** ——
#: ``$ROOT`` 后面跟一个中文字(``"$ROOT/根目录"``)是正当写法,不许拦。
_引用 = re.compile(r"\$\{?[#!]?([^\x00-\x7f]" + _标识符字符 + r"*)")
#: ``for 名 in ...`` / ``select 名 in ...`` / ``read 名`` 里的循环变量。
_循环变量 = re.compile(r"^\s*(?:for|select)\s+(" + _带中文的标识符 + r")\s+in\b")
_读入变量 = re.compile(r"^\s*read\s+(?:-\w+\s+)*(" + _带中文的标识符 + r")\b")

_扫描规则 = (
    (_赋值, "变量赋值"),
    (_函数, "函数定义"),
    (_引用, "变量引用"),
    (_循环变量, "循环变量"),
    (_读入变量, "read 的变量"),
)

_起heredoc = re.compile(r"<<-?\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s'\";&|()<>]+))")


def 可执行片段(行: str, 状态: int) -> tuple[str, int, str | None]:
    """把一行里**会被 bash 当代码看**的那部分抠出来。

    回 ``(代码, 新状态, heredoc 的结束标记)``。``状态`` 是跨行带过来的引号
    状态(0 没在引号里、1 在单引号里、2 在双引号里) —— bash 的引号是可以跨
    行的,footprint.sh 里那几段 awk 程序就是整块单引号。

    三件事:

    * **单引号里的内容整段抹成空格**。单引号里 ``$`` 不展开,``'$根'`` 是
      一串字面量,拦它就是误报到散文上。
    * **双引号里的内容留着**。``"$根/bin"`` 里那个引用是真的会展开(准确地说
      是展不开 —— bash 会把 ``$根`` 原样留着),这正是要拦的东西。
    * **注释从 ``#`` 起整段丢掉**,而 ``#`` 只有在引号外、且前面是空白或行首时
      才算注释开头 —— 跟 bash 的判法一样。
    """
    出: list[str] = []
    结束标记: str | None = None
    i, n = 0, len(行)
    while i < n:
        c = 行[i]
        if 状态 == 1:                      # 单引号里:原样抹掉
            出.append(" ")
            if c == "'":
                状态 = 0
            i += 1
            continue
        if 状态 == 2:                      # 双引号里:留着,但认转义
            if c == "\\":
                出.append("  ")
                i += 2
                continue
            出.append(c)
            if c == '"':
                状态 = 0
            i += 1
            continue
        # 状态 0:引号外
        if c == "\\":
            出.append("  ")
            i += 2
            continue
        if c == "'":
            状态 = 1
            出.append(" ")
            i += 1
            continue
        if c == '"':
            状态 = 2
            出.append(c)
            i += 1
            continue
        if c == "#" and (not 出 or 出[-1].isspace()):
            break                          # 注释,这一行到此为止
        if 行.startswith("<<", i) and not 行.startswith("<<<", i) and 结束标记 is None:
            命中 = _起heredoc.match(行, i)
            if 命中:
                结束标记 = next(g for g in 命中.groups() if g is not None)
                出.append(" " * (命中.end() - i))
                i = 命中.end()
                continue
        出.append(c)
        i += 1
    return "".join(出), 状态, 结束标记


def 代码行(文本: str) -> list[tuple[int, str]]:
    """整份脚本逐行走一遍,回 ``(行号, 可执行片段)``。**heredoc 的正文整段跳过。**

    heredoc 正文是喂给别的程序的数据(install.sh 那两段就是 ``/etc/d1max/env``
    的模板和一屏给人看的提示),里头的中文、``=``、``$`` 一律不是标识符。
    """
    行们 = 文本.split("\n")
    出: list[tuple[int, str]] = []
    状态 = 0
    i = 0
    while i < len(行们):
        代码, 状态, 结束标记 = 可执行片段(行们[i], 状态)
        出.append((i + 1, 代码))
        if 结束标记 is not None:
            j = i + 1
            while j < len(行们) and 行们[j].strip() != 结束标记:
                j += 1
            状态 = 0
            i = j + 1
            continue
        i += 1
    return 出


def 非ASCII标识符(文本: str) -> list[tuple[int, str, str]]:
    """挑出非 ASCII 的标识符,回 ``(行号, 种类, 那个标识符)``。

    **取舍写在这儿:宁可漏报,绝不误报到中文注释上。** 这条护栏的价值全在
    「红了就一定是真的」—— 它要是会因为某句中文注释而莫名其妙变红,第一个
    被撞红的人就会把它 skip 掉,然后 install.sh 那种 bug 会第二次活下来。
    所以:

    * 赋值**只认命令位**(行首,或 ``local``/``export`` 这类关键字之后)。
      bash 本来也只在命令位上认赋值,``echo 版本=1`` 不是赋值。
    * 引用**只认以非 ASCII 开头**的名字。``"$ROOT/根目录"`` 是正当写法。
    * 单引号里、注释里、heredoc 正文里的东西一律不看。
    * 字符串字面量里的中文**散文**照样放行 —— 只有 ``$中文`` 这种引用形状才拦。

    漏报的那一头长什么样,也说清楚:间接展开(``${!名}``)、``eval`` 拼出来的
    名字、跨行拼接出来的赋值,这里都看不见。真要写成那样,是另一种更该被
    code review 拦下来的问题。

    还有一类漏报值得点名:**ASCII 开头、中间夹中文**的名字,比如
    ``pip参数``。它的**赋值**拦得住(赋值规则不要求首字符非 ASCII),
    ``$pip参数`` 这类**引用**则放过 —— 因为引用规则一旦允许 ASCII 开头,
    ``"$ROOT/根目录"`` 这种正当写法就会被误伤。变量总得先赋值再用,
    所以只要赋值那一处被拦住,整个变量就跑不掉。
    """
    出: list[tuple[int, str, str]] = []
    for 行号, 代码 in 代码行(文本):
        for 规则, 种类 in _扫描规则:
            for 命中 in 规则.finditer(代码):
                出.append((行号, 种类, 命中.group(1)))
    return 出


def test_shell脚本名单不是空的():
    """**这一条是下面那个 glob 的活口。**

    ``shell脚本()`` 要是哪天因为目录改名、glob 写错而回了个空列表,下面每一条
    parametrize 出来的用例都会一起消失,而 pytest **不会报错** —— 它只会少收
    几条用例,测试照样全绿。空名单的护栏和没有护栏是一回事。
    """
    名单 = shell脚本()
    assert len(名单) >= 13, f"shell 脚本只扫到 {len(名单)} 份,glob 像是漏了:{名单}"
    名字 = {p.name for p in 名单}
    # 这三处各点一份名, 是为了「某个目录整个扫不到了」也会变红。
    assert "install.sh" in 名字, "install.sh 不在名单上 —— 这正是这一节的由来"
    assert "footprint.sh" in 名字
    assert any(n.startswith("field-") for n in 名字), "仓库根上的 field-*.sh 没被扫到"


@pytest.mark.parametrize("脚本", shell脚本(), ids=lambda 路径: 路径.name)
def test_shell标识符一律是ASCII(脚本: Path):
    """bash 的标识符只认 ASCII。中文变量名不是赋值,是一条找不到的命令。

    报错要指到**文件名、行号、那个标识符本身** —— 只说「有问题」的护栏,
    现场的人读不懂也改不动。
    """
    坏的 = 非ASCII标识符(脚本.read_text(encoding="utf-8"))
    assert not 坏的, "\n".join(
        [f"{脚本.name} 里有非 ASCII 的 shell 标识符 —— bash 不认,这些行跑不起来:"]
        + [f"  {脚本.name}:{行号}: {种类} {名!r}" for 行号, 种类, 名 in 坏的]
        + ["改成 ASCII(注释里的中文一个字都不用动)"])


def test_扫描器认得出中文标识符也放得过中文散文():
    """**这一条守的是上面那个扫描器自己。**

    一个什么都匹配不上的扫描器永远是绿的 —— 而这一节要防的恰恰是「护栏在,
    但它什么都没查」。所以在这儿用一份合成脚本两头都钉死:该拦的五种形状一
    种都不许漏,该放的中文注释/单引号/heredoc/字符串散文一处都不许误伤。
    """
    样本 = "\n".join([
        "#!/usr/bin/env bash",
        "# 注释里写 根=/opt/d1max 和 $根 都不算数",
        "根=/opt/d1max",
        "用户=${D1MAX_USER:-robot}",
        "说() { printf '%s' \"$*\"; }",
        'echo "落在 $根/bin 底下"',
        "echo '单引号里的 $根 不展开'",
        "for 一版 in a b; do :; done",
        "read -r 一行",
        'echo "版本=1 只是个参数, 不是赋值"',
        "ROOT=/opt/d1max",
        'echo "$ROOT/根目录 是正当写法"',
        "cat <<'模板'",
        "里面写 根=/opt/d1max 和 $根 都不算数",
        "模板",
        "echo 尾巴",
    ])
    命中 = 非ASCII标识符(样本)
    assert [(行号, 种类, 名) for 行号, 种类, 名 in 命中] == [
        (3, "变量赋值", "根"),
        (4, "变量赋值", "用户"),
        (5, "函数定义", "说"),
        (6, "变量引用", "根"),
        (8, "循环变量", "一版"),
        (9, "read 的变量", "一行"),
    ], f"扫描器的行为变了:{命中}"


@pytest.mark.parametrize("脚本", shell脚本(), ids=lambda 路径: 路径.name)
def test_每份shell脚本的语法都过得去(脚本: Path):
    """``bash -n``。

    它查不出中文标识符那类问题(``根=/opt/d1max`` 语法完全合法),所以它**不是**
    上面那条的替代品;但真的语法错还是要当场拦住,而且这是整份名单上唯一一次
    「让 bash 自己读一遍这份脚本」的机会。
    """
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    完 = subprocess.run(
        [bash, "-n", 脚本.as_posix()], capture_output=True, text=True, timeout=60)
    assert 完.returncode == 0, f"{脚本.name} 语法不过:{完.stderr}"
# ------------------------------------- 五、根下那个解释器是 wrapper, 不是软链
#
# **这一节存在的全部理由, 也是一件真事, 而且比第四节那件更难看见。**
#
# ``install.sh`` 的 2/7 步原来写的是:
#
#     ln -sfn "$ROOT/bin-venv/bin/python" "$ROOT/bin/python"
#
# 看着没毛病 —— venv 的解释器本来就是软链堆出来的。但 CPython 找 pyvenv.cfg
# 用的是 ``sys.executable`` **没有解析软链**的那个路径, 而且只看它自己那一级
# 目录和上一级。这条链落在 ``$ROOT/bin/`` 底下, 这两级都没有 pyvenv.cfg,
# 于是解释器判定自己不在 venv 里, ``sys.prefix`` 变成 ``/usr`` —— 之后每一次
# ``"$ROOT/bin/python" -m pip install`` 都写进**系统 site-packages**。
#
# 两种机器两种死法: 带 PEP 668 的(Ubuntu 23.04+)当场
# externally-managed-environment, 装不下去; JetPack/Ubuntu 22.04 上没有这道
# 闸, 它**不声不响地往系统 Python 里写**, 装完看着一切正常。而这台狗上还跑着
# 厂商的上装, 系统 Python 是共用的; ``uninstall.sh`` 的删除范围够不到系统
# site-packages —— 卸载卸不干净, ``install.sh`` 开头那句「盘上只有以上这些」
# 就成了空话, ``footprint.sh`` 的 diff 也永远差着一块。
#
# 这类 bug 靠读脚本是读不出来的(那一行完全合法), 靠既有测试也查不出来
# (它们只 grep 文本)。真跑一次量 ``sys.prefix`` 才看得见。所以这里**把
# 结论钉成文本护栏**: 盘上不许再出现这种写法, 谁改回去谁当场变红。
#
# **扫的是可执行片段, 不是注释。** ``install.sh`` 的 2/7 步故意把老写法原样
# 抄在注释里(不写下来, 下一个人三个月后又会觉得软链更干净), 那一行必须放过。
# ``docs/superpowers/plans/`` 底下那份归档计划里也留着老写法 —— 它是冻结的
# 历史记录, 不是交付件, 同样不在扫描范围内。


#: 这一节扫哪些文件: 所有 shell 脚本, 加上 ``deploy/`` 底下的非脚本交付件
#: (systemd 单元), 加上现场照着一行行敲的那两份文档。
def 装机相关文件() -> list[Path]:
    出 = list(shell脚本()) + sorted(DEPLOY.glob("*.service"))
    for 名 in ("装机清单.md", "真机现场执行单.md"):
        路径 = ROOT / "docs" / 名
        if 路径.exists():
            出.append(路径)
    return 出


#: 「把根下的解释器做成软链」这种写法。需求书给的形状就是 ``ln -s.*bin/python``。
_软链解释器 = re.compile(r"ln\s+-s\S*\s[^\n]*bin/python")


def 软链解释器的行(路径: Path) -> list[tuple[int, str]]:
    """挑出这份文件里「把解释器做成软链」的**可执行**行。

    shell 脚本走 ``代码行()`` —— 注释和 heredoc 正文都已经被抹掉, 所以 2/7 步
    那条留作记录的注释不会被误伤。别的文件(单元文件、文档)没有可执行语义,
    整行原样扫。
    """
    文本 = 路径.read_text(encoding="utf-8")
    if 路径.suffix == ".sh":
        行们 = 代码行(文本)
    else:
        行们 = [(i + 1, 行) for i, 行 in enumerate(文本.split("\n"))]
    return [(行号, 行.strip()) for 行号, 行 in 行们 if _软链解释器.search(行)]


def test_盘上不许再有指向根下解释器的软链():
    """``ln -sfn ... bin/python`` 一处都不许有。

    改回软链 = 把包装进系统 Python = 卸载卸不干净。这条是那件事唯一的看门人。
    """
    坏的 = [(路径, 行号, 行) for 路径 in 装机相关文件()
            for 行号, 行 in 软链解释器的行(路径)]
    assert not 坏的, "\n".join(
        ["把根下的解释器做成软链了 —— 这样 sys.prefix 会掉回 /usr, "
         "pip 会装进系统 site-packages:"]
        + [f"  {路径.relative_to(ROOT).as_posix()}:{行号}: {行}"
           for 路径, 行号, 行 in 坏的]
        + ["要的是一个 exec 到 bin-venv/bin/python 的 wrapper 脚本, "
           "理由见 install.sh 的 2/7 步"])


def test_这条软链护栏自己不是摆设():
    """**守的是上面那个扫描器。**

    一个什么都匹配不上的正则永远是绿的。这里两头钉死: 老写法一定被抓住,
    注释里那条记录一定放过, wrapper 的写法一定不被误伤。
    """
    样本 = "\n".join([
        "#!/usr/bin/env bash",
        '#   ln -sfn "$ROOT/bin-venv/bin/python" "$ROOT/bin/python"',
        'cat > "$ROOT/bin/python" <<WRAP',
        'exec "$ROOT/bin-venv/bin/python" "$@"',
        "WRAP",
        'ln -sfn "$ROOT/releases/$REL_NAME" "$ROOT/current"',
        'ln -sfn "$ROOT/bin-venv/bin/python" "$ROOT/bin/python"',
    ])
    命中 = [行号 for 行号, 行 in 代码行(样本) if _软链解释器.search(行)]
    assert 命中 == [7], f"扫描器的行为变了: {命中}"


def test_装机脚本把根下的解释器装成一个wrapper():
    """光「没有软链」不够 —— 还得真的有那个 wrapper, 而且是可执行的。

    没有这一条的话, 把那几行整段删掉也是绿的: 没有软链了, 但根下也没有解释器,
    单元里那两条 ExecStartPre 直接 203/EXEC。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    # heredoc 的结束标记没加引号, 所以 ``$ROOT`` 在写盘那一刻就展开成真路径,
    # 而 ``"\$@"`` 要转义一次才能原样落到 wrapper 里 —— 参数得透传给解释器,
    # 不能在生成的时候就被这一层吃掉。这里按**脚本里的写法**对, 不是按落盘结果。
    assert r'exec "$ROOT/bin-venv/bin/python" "\$@"' in 脚本, (
        "根下的 wrapper 不见了 —— 单元里两条 ExecStartPre 都是打 "
        "/opt/d1max/bin/python 的")
    assert 'chmod 0755 "$ROOT/bin/python"' in 脚本, (
        "wrapper 没有 chmod 0755, systemd 起不动它")
    # 老机器上装的是软链, 重跑时得先拆掉 —— ``-x`` 对「指向可执行文件的软链」
    # 也为真, 不显式拆一次的话, 已经装过的机器永远换不成 wrapper。
    assert '[[ -L "$ROOT/bin/python" ]]' in 脚本, (
        "没有把老机器上那条软链拆掉的那一步, 装过的机器换不成 wrapper")


def test_装机脚本的用法例子过得了包名正则():
    """例子里的包目录名必须真能装得进去。

    ``engine/release.py`` 的 ``_NAME_RE`` 只认 ``<日期>-<6 到 12 位十六进制>``,
    ``verify_package()`` 还要求目录名跟包里 release.json 的 name 一字不差。
    原来的例子写的是 ``d1max-2026-09-20-77b2de``, 带前缀 —— 照着敲的人会在
    3/7 步被 release install 拒掉, 而那时候前两步已经白跑了。
    **要改的是例子, 不是去放宽正则**: 目录名跟 manifest 对齐是故意的,
    它同时也是防路径穿越的那道闸。
    """
    例子 = re.compile(r"install\.sh\s+(\S*/)?([A-Za-z0-9._-]*\d{4}-\d{2}-\d{2}-[A-Za-z0-9._-]+)")
    查过 = 0
    for 路径 in [DEPLOY / "install.sh", ROOT / "docs" / "装机清单.md"]:
        文本 = 路径.read_text(encoding="utf-8")
        名字们 = [命中.group(2) for 命中 in 例子.finditer(文本)]
        # 装机清单里还有不跟在 install.sh 后面的裸例子, 一并收上来。
        名字们 += re.findall(r"`([A-Za-z0-9._-]*\d{4}-\d{2}-\d{2}-[0-9a-f]{6,12})`", 文本)
        名字们 += re.findall(r"`/home/robot/([A-Za-z0-9._-]+)`", 文本)
        for 名 in 名字们:
            查过 += 1
            assert _NAME_RE.match(名), (
                f"{路径.relative_to(ROOT).as_posix()} 里的例子 {名!r} 过不了 "
                f"_NAME_RE —— 照着它敲的人会在 3/7 步被 release install 拒掉。"
                f"改例子, 不要改正则")
    assert 查过 >= 3, f"一个包名例子都没扫到({查过} 个), 这条护栏像是瞎了"


def test_装机脚本在动盘之前就把两种装不下去的情况拦掉():
    """包目录不在、账号不在, 都要在 ``mkdir -p "$ROOT"`` **之前**失败。

    原来这两样都是往后拖的: 打错的路径一路带到 2/7 末尾的 ``cp -a`` 才炸,
    没有 robot 账号则是 1/7 的 ``chown`` 撞上 ``chown: invalid user`` —— 而
    ``chown`` 排在 ``mkdir -p`` 之后。两种都在 ``/opt/d1max`` 已经建出来之后
    中止, 留半个脚印给人收, 而 ``footprint.sh`` 的 diff 里会多出东西。
    失败要早、要便宜、要说人话。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    动盘 = 脚本.index('mkdir -p "$ROOT/releases"')
    包目录检查 = 脚本.index('if [[ ! -d "$PKG" ]]; then')
    账号检查 = 脚本.index('if ! id -u "$RUN_USER" >/dev/null 2>&1; then')
    assert 包目录检查 < 动盘, "包目录的检查排在 mkdir 后面了, 会留半个脚印"
    assert 账号检查 < 动盘, "账号的检查排在 mkdir 后面了, 会留半个脚印"
    # 报错里必须把那个路径原样打出来, 不然现场的人不知道自己敲错在哪儿。
    assert '$PKG" >&2' in 脚本, "包目录不存在的报错没把路径打出来"
    assert '$RUN_USER' in 脚本, "账号不存在的报错没把账号名打出来"


def test_当前版本名有显式的空默认值():
    """``CURRENT_REL`` 没有 ``current`` 链时要是空串, 不能是别的什么。

    原来是 ``CURRENT_REL=$(basename "$(readlink -f "$ROOT/current")")`` 一行
    打完 —— 链不在时 ``readlink -f`` 回的是那条路径本身, ``basename`` 于是给出
    字面量 ``current``。后面拿它跟 ``$REL_NAME`` 比、往回执里写, 靠的是
    「``current`` 永远过不了 ``_NAME_RE``」这个巧合。**巧合不是接口。**
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    默认 = 脚本.index("\nCURRENT_REL=\n")
    读链 = 脚本.index('CURRENT_REL=$(basename "$(readlink -f "$ROOT/current")")')
    assert 默认 < 读链, "CURRENT_REL 没有先给一个空默认值"
    assert '-e "$ROOT/current" || -L "$ROOT/current"' in 脚本, (
        "读链之前没判断链在不在 —— 断链也要能读出名字, 所以 -L 那一半不能省")


# ------------------------------------------------ 六、上机自检(2026-09-13)修的那几条
#
# 下面这一组守的是明天上机那一趟暴露出来的坑。每一条都写清楚**原来会怎么错**,
# 不然半年后有人只看见一句断言, 会以为它可以随手放宽。


def test_装机脚本绝不source那份给systemd用的env():
    """``/etc/d1max/env`` 是给 systemd ``EnvironmentFile=`` 用的, **不是 shell 脚本**。

    systemd 按字面量解析: 等号右边整段就是值, 不分词、不做命令替换、分号不是
    分隔符。而 ``. /etc/d1max/env`` 是让 bash 求值同一份文件 —— 这份文件由现场
    拿 nano 手填, 三种人手敲得出来的写法当场出事:

    * ``D1MAX_SN=C4 0221`` —— systemd 收下; bash 去执行 ``0221``, ``set -e``
      当场 127, 装机死在 7/7, 前六步白跑;
    * ``D1MAX_CONSOLE_TOKEN=a;reboot`` —— systemd 收下; bash **以 root 把
      reboot 跑了**;
    * ``D1MAX_PIN=$(...)`` —— systemd 当字面量; bash 做命令替换。

    所以这里钉死: 装机脚本里不许再出现 source 那份文件的任何形状。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    代码 = "\n".join(片段 for _, 片段 in 代码行(脚本))
    for 形状 in (". /etc/d1max/env", "source /etc/d1max/env"):
        assert 形状 not in 代码, (
            f"install.sh 又去 source /etc/d1max/env 了({形状!r})。那份文件是 "
            f"systemd 按字面量解析的, shell 求值它等于让现场手填的一行字"
            f"以 root 身份跑起来")
    # 换上来的读法必须还在, 不然 D1MAX_SN 根本传不到 release activate。
    assert "read_env_value()" in 脚本, "读 env 的那个函数没了"
    assert 'sed -n "s/^[[:space:]]*$1=//p" /etc/d1max/env' in 脚本


def test_读env的规矩跟systemd对得上():
    r"""读法要跟 systemd 的解析一条一条对齐, **对不齐就是两边各读各的值**。

    而「两边各读各的值」正是自检第四项(identity)假失败、把一版好的自动回滚
    掉的那个根因 —— 服务侧经 ``EnvironmentFile=`` 读, 脚本侧自己读。

    这里钉两条最容易被「简化」掉的:

    * **同一个键写了两遍取最后一次**(systemd ``basic/env-util.c`` 的
      ``strv_env_replace``: 后面的赋值替换前面的), 所以要 ``tail -n 1``;
    * **不带引号的值两边的空白要剥掉**(``\r`` 也算空白)。现场用 nano 敲完
      常常多一个尾随空格, 不剥的话那道字符白名单会把一个 systemd 明明认得的
      值判死, 现场白挨一次 ``exit 3``。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    函数 = 脚本[脚本.index("read_env_value()"):]
    函数 = 函数[:函数.index("\n}\n") + 2]
    assert "tail -n 1" in 函数, "同一个键写两遍时要取最后一次 —— 这是 systemd 的行为"
    assert "s/^[[:space:]]*//" in 函数 and "s/[[:space:]]*$//" in 函数, (
        "值两边的空白没剥 —— nano 敲出来的尾随空格会让白名单误杀")


def test_env里的SN过一道字符白名单():
    """脏 SN 要在装机这一层挡住, 而不是等它去炸回传线程。

    ``engine/http_sink.py`` 里 ``sn`` **不转义、原样进 HTTP header**: 填成中文
    的话每一条回传都 ``UnicodeEncodeError``。而「装机时挡住脏 sn 是配置自检
    那一层的事」本来就记过账(挂账 142), 这里正是那一层。

    白名单是 ``^[A-Za-z0-9._-]+$``: 本项目见过的真 SN(``C40221``、
    ``D1M-0007``、``D1MAX-TEST-01``)全都过得去, 它挡的只是空格和标点。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "=~ ^[A-Za-z0-9._-]+$" in 脚本, "SN 的字符白名单没了"
    白名单 = re.compile(r"^[A-Za-z0-9._-]+$")
    for 真SN in ("C40221", "D1M-TEST", "D1M-0007", "D1MAX-01", "D1MAX-TEST-01", "SN-A1"):
        assert 白名单.match(真SN), f"白名单把一个真序列号挡了:{真SN}"
    for 脏的 in ("C4 0221", "a;reboot", '"C40221"', "序列号"):
        assert not 白名单.match(脏的), f"白名单放过了一个脏值:{脏的}"
    # 挡在 activate 之前才有意义 —— 挡在后面等于没挡。
    assert (脚本.index("=~ ^[A-Za-z0-9._-]+$")
            < 脚本.index("d1max_patrol.cli release activate"))


def test_两道守卫测的是解析出来的值而不是行在不在():
    """原来两道守卫是 ``grep -qE '^D1MAX_PIN=.+'`` / ``'^D1MAX_SN=.+'``。

    它问的是「文件里有没有一行长这样」, 而 ``D1MAX_PIN=`` 后面跟几个空格
    它判「有 PIN」—— systemd 剥完空白读出来却是空串, 于是脚本照常 stop/start,
    机器照样进那个每 5 秒刷一条日志的启动循环。**守卫在, 墙也在。**
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert '[[ -z "${D1MAX_SN//[[:space:]]/}" ]]' in 脚本
    assert '[[ -z "$(read_env_value "$key")" ]]' in 脚本, "站点值那一道也要按解析出来的值判"
    代码 = "\n".join(片段 for _, 片段 in 代码行(脚本))
    assert "grep -qE '^D1MAX_" not in 代码, "又退回去数行了, 要测的是解析出来的值"


def test_env文件建出来就是0600():
    """这份文件里有设备 PIN 和回传密钥(脚本自己写着「这一行是密钥」)。

    ``cat >`` 在 root 的 umask 022 下建出来是 **0644** —— 而这台狗上还跑着
    厂商的上装、系统 Python 是共用的, 同机任何一个账号都读得到。
    读它的两方都是 root(systemd 的 ``EnvironmentFile=`` 由 PID 1 读完再降到
    ``User=robot``; 7/7 步这个脚本也是 root), 0600 两边都够用。

    ``chmod`` 那一行必须在 ``if`` **外面**: 这之前装出来的机器盘上躺着的是
    0644 的那一份, 重跑一次才收得紧。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "install -m 0600 /dev/null /etc/d1max/env" in 脚本, "建出来的时候就得是 0600"
    行们 = 脚本.split("\n")
    收紧 = [i for i, 行 in enumerate(行们) if 行 == "chmod 0600 /etc/d1max/env"]
    assert 收紧, "没有顶格的 chmod 0600 —— 老机器重跑收不紧(缩进了就是在 if 里面)"
    重载 = next(i for i, 行 in enumerate(行们) if 行 == "systemctl daemon-reload")
    assert 收紧[0] < 重载, "收紧权限排在 daemon-reload 后面了"


def test_根下装依赖那一步有哨兵挡着重跑():
    """**重跑是现场「填完 SN 让它生效」的唯一路子**(见 docs/装机清单.md 二)。

    2/7 那条 ``pip install "$TMP_PKG"`` 原来在任何 ``if`` 外面: ``pyproject.toml``
    用 ``setuptools.build_meta``, PEP 517 的构建隔离每趟都要现拉 setuptools,
    于是**重跑必联网**。离线现场跑第二趟忘了带 ``D1MAX_PIP_ARGS=`` 就挂死在
    2/7(pip 默认 5 次重试、每次 15 秒), ``set -e`` 中止, 连 ``stop``/``start``
    都不跑 —— SN 永远不生效, 而现场看到的是「装到一半不动了」。

    哨兵的讲究跟 4/7 那个一样: **最后一行才落**, 它在就等于装齐了; 里头存的是
    版本名, 换一版内容对不上照样重装。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert 'ROOT_SENTINEL="$ROOT/bin-venv/.deps-ok"' in 脚本
    行们 = 脚本.split("\n")
    落哨兵 = [i for i, 行 in enumerate(行们) if '> "$ROOT_SENTINEL"' in 行]
    装包 = [i for i, 行 in enumerate(行们)
            if '-m pip install --quiet $PIP_ARGS "$TMP_PKG"' in 行]
    assert len(落哨兵) == 1 and len(装包) == 1
    assert 装包[0] < 落哨兵[0], "哨兵落在装包之前 —— 装到一半断了会被当成装齐了"
    assert 行们[装包[0]].startswith("  "), "那条 pip 没有缩进, 说明它还在 if 外面"


def test_包目录名在动盘之前就核过():
    """名字不对的包, 3/7 的 ``release install`` 必拒。

    而那时候 ``/opt/d1max`` 已经建出来、根下那个跟版本无关的解释器也装完了
    (离线现场还为它插过一次 U 盘), 现场要回头收拾半个脚印。跟另外两条前置
    检查同一条理由: 失败要早、要便宜、要说人话。

    **脚本里那个正则要跟 ``_NAME_RE`` 判得一样** —— 松一点就是「脚本放行、
    3/7 才拒」, 等于白加; 紧一点就是「能装的包被脚本拒了」。
    """
    脚本 = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    命中 = re.search(r'\[\[ ! "\$REL_NAME" =~ (\S+) \]\]', 脚本)
    assert 命中, "找不到包目录名的前置检查"
    脚本里的 = re.compile(命中.group(1))
    样本 = ("2026-09-20-77b2de", "2026-09-13-abcdef123456",
            "d1max-2026-09-20-77b2de", "2026-09-20-77B2DE", "2026-09-20-77b2",
            "2026-9-20-77b2de", "2026-09-20-77b2de1234567", "current", "")
    for 名 in 样本:
        assert bool(脚本里的.match(名)) == bool(_NAME_RE.match(名)), (
            f"脚本里的包名正则跟 engine/release.py 的 _NAME_RE 判得不一样:{名!r}")
    assert 脚本.index("REL_NAME=$(basename") < 脚本.index('mkdir -p "$ROOT/releases"'), (
        "包名检查排在 mkdir 后面了, 会留半个脚印")


def test_卸载脚本真删之前有一道root闸():
    """这脚本第一个不可逆的动作**恰恰不需要 root**。

    ``reap_agent`` 收掉 patrol_agent(agent 是登录用户用 field-agent.sh 起的,
    同一个用户 ``kill -TERM`` 就够), SDK 会话一丢, 不重启 RK3588 就再也抢不
    回来。真正需要 root 的是后面那几条 ``rm /opt/d1max``。没有这道闸, 一次
    忘了 sudo 的运行会: 先把控制权永久交还给上装 → 再在第一条 rm 上 EACCES
    → ``set -e`` 打死 → 盘上一个字节没卸、5/5 回执也打不出来。

    闸要排在**参数解析之后**: ``--dry-run`` 和 ``-h`` 什么都不动, 不该要 root。
    """
    脚本 = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert '[ "$DO_IT" = 1 ] && [ "$(id -u)" != 0 ]' in 脚本, (
        "root 闸没有放过 --dry-run —— 只看一眼要删什么不该要 root")
    闸 = 脚本.index('[ "$(id -u)" != 0 ]')
    assert 脚本.index("while [ $# -gt 0 ]; do") < 闸, "闸排在参数解析之前了"
    assert 闸 < 脚本.index("\nreap_agent\n"), (
        "root 闸排在 reap_agent 后面 —— 控制权已经交出去了才拦, 等于没拦")


def test_删不掉的处数要记账并且写进回执():
    """一条 ``rm`` 失败不能把后面几处和 5/5 回执一起带走。

    5/5 回执是现场判断「卸干净没有」的唯一凭据 —— 死在第一条 ``rm`` 上, 等于
    既没卸干净、又不告诉人。所以失败降级成记账, 回执里当着人的面说出来。
    """
    脚本 = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "\nRM_FAILED=0\n" in 脚本, "没有给 RM_FAILED 一个显式的 0 初值"
    assert 脚本.count("RM_FAILED=$((RM_FAILED + 1))") == 2, (
        "两个删除函数里都要记账 —— 少一个就是那一类失败被静默吞掉")
    assert 'if [ "$RM_FAILED" != 0 ]; then' in 脚本, "回执里没报没删掉的处数"
    assert 脚本.index("5/5 回执") < 脚本.index('if [ "$RM_FAILED" != 0 ]; then')


def test_单元文件的行尾被钉成LF():
    r"""``.service`` 里混进一个 ``\r``, 坏得比 Makefile 还安静。

    ``ExecStart=... --host 0.0.0.0\r`` 里那个 ``\r`` 会被当成参数的一部分,
    服务起来就是「地址解析不了」, 配上 ``Restart=always`` 就是每 5 秒刷一条
    日志的启动循环 —— 而现场看到的错跟行尾一点关系都没有。

    眼下工作区里是 LF, 所以今天没坏; 但这是交付件, 换一台 Windows 机器重新
    clone 出来就会是 CRLF, **那时候才发现就晚了**。
    """
    属性 = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\*\.service\s+text\s+eol=lf\s*$", 属性), (
        ".gitattributes 里没把 *.service 钉成 LF")
    assert re.search(r"(?m)^deploy/d1max-agent-start\s+text\s+eol=lf\s*$", 属性), (
        ".gitattributes 里没把代理的启动脚本钉成 LF(它没有 .sh 后缀,上面那几条盖不到它)")
    单元们 = list(DEPLOY.glob("*.service")) + [DEPLOY / "d1max-agent-start"]
    assert len(单元们) >= 2, "deploy/ 底下一个 .service 都没有?"
    for 路径 in 单元们:
        assert b"\r" not in 路径.read_bytes(), f"{路径.name} 里有回车符"
