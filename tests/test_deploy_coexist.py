"""共存与撤场:装机不覆盖别人的文件、卸载能卸干净。

这一份守的是两件现场要交代的事:

1. ``deploy/footprint.sh`` —— 装机前后各跑一次,拿盘上的文件清单当证据,
   证明我们只动了自己那几处。**它自己必须是只读的**,否则证据就是自证。
2. ``deploy/uninstall.sh`` —— 一键撤场,把我们装上去的东西收干净,
   顺带收掉正在跑的 ``patrol_agent``。

核心是**对账不变式**:``install.sh`` 里每一处 ``@写盘``,
``uninstall.sh`` 里都要有对应的 ``@删除``,反过来也一样。
有人日后往 install.sh 里加了一处写盘:

* 加了却没在 install.sh 里记 ``@写盘`` —— 被 :func:`test_装机脚本里的写盘动作都记在声明块上` 抓住;
* 记了 ``@写盘`` 却没在 uninstall.sh 里加 ``@删除`` ——
  被 :func:`test_装机写的每一处卸载都要收掉` 抓住。

**两条路都堵上了,这才是这份文件存在的理由。**
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_files import DEPLOY

装机脚本 = DEPLOY / "install.sh"
卸载脚本 = DEPLOY / "uninstall.sh"
足迹脚本 = DEPLOY / "footprint.sh"

# uninstall.sh 里写死的那几个常量。测试自己不假设它们的值,
# 下面 test_卸载脚本的根目录常量是写死的 会回去核对一遍,核对过了才拿来做展开。
卸载常量 = {
    "ROOT": "/opt/d1max",
    "ETC_DIR": "/etc/d1max",
    "UNIT_DIR": "/etc/systemd/system",
    "MAIN_UNIT": "d1max-agent.service",
    "OLD_UNIT": "d1max-bootguard.service",
    "LEGACY_UNIT": "d1max-patrol.service",
    "WANTS_LINK": "/etc/systemd/system/multi-user.target.wants/d1max-agent.service",
    "LEGACY_WANTS_LINK": "/etc/systemd/system/multi-user.target.wants/d1max-patrol.service",
}


def 读(路径: Path) -> str:
    return 路径.read_text(encoding="utf-8")


def 代码行(文本: str) -> list[str]:
    """把注释行摘掉,只留会真的执行的那些行。"""
    return [行 for 行 in 文本.splitlines() if not 行.lstrip().startswith("#")]


def 取声明(文本: str, 标签: str) -> set[str]:
    """把 ``# @标签 /某个/路径   说明`` 这种声明行读出来。

    注意 ``re.match``:标签必须紧跟在 ``#`` 后面。声明块前面那几段
    讲道理的散文里也会提到 ``@写盘`` 这些词,那些不算数,也不该算数。
    """
    出 = set()
    模式 = re.compile(r"#\s*" + 标签 + r"\s+(\S+)")
    for 行 in 文本.splitlines():
        命中 = 模式.match(行.strip())
        if 命中:
            出.add(命中.group(1))
    return 出


def 展开常量(文本: str) -> str:
    """把 ``$ROOT`` 一类的变量换成它们的字面值,好拿路径去文本里找。"""
    出 = 文本
    # 先换长的再换短的 —— 不然 ``$ROOTX`` 这种会被 ``$ROOT`` 先吃掉一半
    for 名 in sorted(卸载常量, key=len, reverse=True):
        出 = 出.replace("$" + 名, 卸载常量[名])
    return 出


# ------------------------------------------------------------------ 对账


def test_两个脚本都在盘上():
    """交付名单里加了名字,文件却没有 —— 先把这种低级情况挡掉。"""
    for 文件 in (装机脚本, 卸载脚本, 足迹脚本):
        assert 文件.exists(), f"{文件.name} 不在 deploy/ 下"


def test_声明块不是空的():
    """声明块被人整块删掉,下面所有对账都会变成空集合对空集合,全绿。

    所以先卡一个下界:少于这几条,说明有人把声明块拆了。
    """
    写盘 = 取声明(读(装机脚本), "@写盘")
    删除 = 取声明(读(卸载脚本), "@删除")
    保留 = 取声明(读(卸载脚本), "@保留")
    assert len(写盘) >= 10, f"install.sh 的 @写盘 只剩 {len(写盘)} 条,声明块被动过了"
    assert len(删除) >= 10, f"uninstall.sh 的 @删除 只剩 {len(删除)} 条,声明块被动过了"
    assert len(保留) >= 3, f"uninstall.sh 的 @保留 只剩 {len(保留)} 条,--keep-data 的口径被动过了"


def test_装机写的每一处卸载都要收掉():
    """**这条是整份文件的核心。**

    install.sh 的 ``@写盘`` 加上 ``@也删``,必须跟 uninstall.sh 的
    ``@删除`` 一模一样。差一条就是"装了卸不掉"或者"卸了不该卸的"。
    """
    写盘 = 取声明(读(装机脚本), "@写盘")
    也删 = 取声明(读(装机脚本), "@也删")
    删除 = 取声明(读(卸载脚本), "@删除")
    漏掉的 = (写盘 | 也删) - 删除
    assert not 漏掉的, (
        "install.sh 会写这几处而 uninstall.sh 不收:"
        f"{sorted(漏掉的)} —— 去 deploy/uninstall.sh 的声明块里各补一行 @删除,"
        "并且在代码里真的删它")
    多删的 = 删除 - (写盘 | 也删)
    assert not 多删的, (
        "uninstall.sh 要删这几处而 install.sh 从来没建过:"
        f"{sorted(多删的)} —— 删别人的东西是事故,先确认这是不是笔误")


def test_保留清单是写盘清单的子集():
    """``--keep-data`` 只能留我们自己装的东西,留不了别人的。"""
    写盘 = 取声明(读(装机脚本), "@写盘")
    保留 = 取声明(读(卸载脚本), "@保留")
    越界 = 保留 - 写盘
    assert not 越界, f"@保留 里这几条不在 install.sh 的 @写盘 上:{sorted(越界)}"


def test_每一处声明在卸载脚本的代码里真的出现过():
    """声明块可以写得很漂亮,代码里不删就是一纸空文。

    把 ``$ROOT`` 这些常量展开之后,每一条 ``@删除`` 的路径都要在
    真正执行的代码行里出现过。
    """
    正文 = 展开常量("\n".join(代码行(读(卸载脚本))))
    缺 = [路径 for 路径 in sorted(取声明(读(卸载脚本), "@删除")) if 路径 not in 正文]
    assert not 缺, f"这几条只在声明块里,代码里根本没删:{缺}"
    # **光出现过不够,要真交给 rm_sys。** 常量的赋值行(``WANTS_LINK=/etc/...``)也是一行代码,
    # 只查「出现过」的话,删掉那条 rm_sys 这里照样绿(W00c5e 突变)。每一条要么自己被 rm_sys,
    # 要么它的某一级上级目录被 rm_sys(默认模式最后整个删 /opt/d1max、/etc/d1max)。
    删的 = set()
    for 行 in 正文.splitlines():
        m = re.match(r'\s*rm_sys\s+"([^"]+)"', 行)
        if m:
            删的.add(m.group(1).rstrip("/"))
    没交给rm = [路径 for 路径 in sorted(取声明(读(卸载脚本), "@删除"))
               if not any(路径.rstrip("/") == 删 or 路径.startswith(删 + "/") for 删 in 删的)]
    assert not 没交给rm, f"这几条声明了 @删除,却没有哪一条 rm_sys 删到它们:{没交给rm}"


def test_卸载脚本的根目录常量是写死的():
    """根目录不许让环境变量覆盖,跟 install.sh 一个口径。

    顺带核对上面 ``卸载常量`` 那张表 —— 展开路径用的就是它,
    它跟脚本对不上的话,上一条对账就是在自说自话。
    """
    文本 = 读(卸载脚本)
    for 名, 值 in 卸载常量.items():
        assert re.search(rf"(?m)^{名}={re.escape(值)}$", 文本), (
            f"uninstall.sh 里 {名} 不再是写死的 {值} —— 要么改了值,"
            "要么被换成了可以由环境变量覆盖的写法")


def test_装机脚本里的写盘动作都记在声明块上():
    """**漏声明也要红。**

    扫 install.sh 真正执行的那些行,把里面出现的绝对路径和
    ``$ROOT/xxx`` 这种相对根目录的写法都捞出来,每一条都要落在声明块覆盖的范围里。
    有人新加一处写盘却忘了记 ``@写盘``,就卡在这儿。
    """
    声明 = 取声明(读(装机脚本), "@写盘") | 取声明(读(装机脚本), "@也删")
    行们 = 代码行(读(装机脚本))

    def 被覆盖(路径: str) -> bool:
        路径 = 路径.rstrip("/")
        for 一条 in 声明:
            一条 = 一条.rstrip("/")
            # 声明本身、声明的上级目录、声明底下的东西,都算在覆盖范围内
            if 路径 == 一条 or 一条.startswith(路径 + "/") or 路径.startswith(一条 + "/"):
                return True
        return False

    野的 = set()
    for 行 in 行们:
        for 路径 in re.findall(r"/(?:etc|opt|usr/local)/[A-Za-z0-9_./-]*", 行):
            if not 被覆盖(路径):
                野的.add(路径)
        for 段 in re.findall(r"\$ROOT/([A-Za-z0-9_.-]+)", 行):
            if not 被覆盖("/opt/d1max/" + 段):
                野的.add("/opt/d1max/" + 段)
    assert not 野的, (
        f"install.sh 碰了这几处而声明块上没有:{sorted(野的)} —— "
        "去 deploy/install.sh 顶上的对账声明块补 @写盘,再去 uninstall.sh 补 @删除")


# ------------------------------------------------- patrol_agent 那一段


def 取函数体(文本: str, 名字: str) -> str:
    """把 ``名字() { ... }`` 这一段抠出来。收尾认的是顶格的 ``}``。"""
    行们 = 文本.splitlines()
    开始 = None
    for 序号, 行 in enumerate(行们):
        if re.match(rf"^{名字}\(\)\s*\{{", 行):
            开始 = 序号
            break
    assert 开始 is not None, f"找不到函数 {名字}()"
    for 序号 in range(开始 + 1, len(行们)):
        if 行们[序号].rstrip() == "}":
            return "\n".join(行们[开始:序号 + 1])
    raise AssertionError(f"函数 {名字}() 没有收尾的 }}")


def test_收掉agent的四级动作一个都不能少():
    """项目主人要的是"不留痕迹",这四级少一级就留得住。

    顺序是:FIFO 的 shutdown -> 等它自己退 -> TERM -> KILL,
    外加一条 ``pgrep`` 兜底,给那些 pidfile 丢了的情况。
    """
    文本 = 读(卸载脚本)
    收 = 取函数体(文本, "reap_agent")
    assert "shutdown" in 收, "第一级没了:应该先往 FIFO 写一条 shutdown 让它自己优雅退出"
    assert "kill -TERM" in 收, "第三级没了:TERM"
    assert "kill -KILL" in 收, "第四级没了:KILL"
    assert "wait_gone" in 收, "第二级没了:发信号之前要等它自己退"
    assert "pgrep" in 文本, "兜底没了:pidfile 丢了的时候要靠 pgrep 找"


def test_往FIFO里写必须带超时():
    """**没有读端的 FIFO,写端会一直阻塞。**

    agent 已经死了而管道还留在盘上,正是最常见的那一种情形。
    不掐超时,一键卸载就挂在这一行上,现场只能 Ctrl+C。
    """
    收 = 取函数体(读(卸载脚本), "reap_agent")
    写管道的行 = [行 for 行 in 收.splitlines() if "AGENT_FIFO" in 行 and ">" in 行]
    assert 写管道的行, "reap_agent 里找不到往 FIFO 写的那一行"
    for 行 in 写管道的行:
        assert "timeout" in 行, f"往 FIFO 写却没带 timeout,会永远卡住:{行.strip()}"


def test_pgrep不会把自己杀掉():
    """**这条写不对就是事故。**

    ``pgrep -f patrol_agent`` 会匹配到自己的命令行、也会匹配到
    调它的那个 shell。匹出来的 pid 里必须排掉 ``$$`` 和 ``$PPID``。
    """
    找 = 取函数体(读(卸载脚本), "agent_pids")
    assert "$$" in 找, "agent_pids 里没有排掉自己($$)"
    assert "$PPID" in 找, "agent_pids 里没有排掉父进程($PPID)"
    assert "continue" in 找, "排掉之后要跳过,不是记下来接着杀"


def test_pgrep的匹配要收紧():
    """裸的 ``pgrep -f patrol_agent`` 会把 ``grep patrol_agent`` 也算进去。"""
    匹配们 = [行 for 行 in 代码行(读(卸载脚本)) if "pgrep" in 行]
    assert 匹配们, "找不到 pgrep 那一行"
    for 一条 in 匹配们:
        assert re.search(r"\(\^\|/\)patrol_agent", 一条), (
            f"pgrep 的模式太松,无辜进程会被匹进来:{一条.strip()}")


def test_等待都有上限():
    """``wait_gone`` 不许出现无限循环 —— 现场没人陪它等。"""
    等 = 取函数体(读(卸载脚本), "wait_gone")
    assert "while true" not in 等 and "while :" not in 等, "wait_gone 里有无限循环"
    assert "limit" in 等, "wait_gone 没有上限参数"
    文本 = 读(卸载脚本)
    assert "wait_gone \"$pid\" 15" in 文本, "等它自己退的上限应该是 15 秒"


def test_释放SDK会话的代价要写在屏幕上():
    """项目主人知情之后仍然要这样做,那就得让现场每个按下这个键的人也知情。

    这段话不是文档里的一句提醒,是必须打在屏幕上的 —— 而且 dry-run 也要打。
    """
    说 = 取函数体(读(卸载脚本), "announce")
    assert "SDK" in 说, "没说这一步会释放 SDK 会话"
    assert "RK3588" in 说, "没说要重启 RK3588 才能再抢回控制权"
    assert "#46" in 说 and "#47" in 说, "没给真机验证清单的条目号"
    assert "field-agent.sh" in 说, "没说怎么重新接管"
    assert "不可逆" in 说, "没把不可逆这三个字说出来"


def test_announce排在动手之前():
    """先说后做。announce 排在 reap_agent 后面的话,人是在事后才看到代价的。"""
    正文 = "\n".join(代码行(读(卸载脚本)))
    位置 = {名: 正文.rindex(名) for 名 in ("announce", "reap_agent", "stop_service", "wipe_disk")}
    assert 位置["announce"] < 位置["reap_agent"] < 位置["stop_service"] < 位置["wipe_disk"], (
        f"主流程的次序不对:{位置}")


def test_默认是全清而不是保守清():
    """需求变更之后,默认(不带参数)就是全部清掉。

    ``--purge`` 这个开关已经作废,换成了反过来的 ``--keep-data``。
    """
    文本 = 读(卸载脚本)
    assert "--keep-data" in 文本
    assert "--dry-run" in 文本
    assert "--agent-root" in 文本
    assert "--purge-agent-binary" in 文本
    assert not re.search(r"--purge(?![-a-z])", 文本), "--purge 已经作废,不该再出现"
    assert "D1MAX_PURGE_CONFIRM" not in 文本, "项目主人要的是一键,不要二次确认"


def test_二进制默认不删():
    """``motion/patrol_agent`` 是源码目录里编出来的产物,不是装机脚本装的。

    默认删掉它,下次开工的人会莫名其妙地找不到东西。
    """
    收 = 取函数体(读(卸载脚本), "reap_agent")
    assert "PURGE_BIN" in 收, "删二进制没有挂在开关上"
    assert re.search(r'if \[ "\$PURGE_BIN" = 1 \]', 收), (
        "删二进制应该只在 --purge-agent-binary 下发生")


# ------------------------------------------------------------ 删除的红线


def test_rm的形状只有一种():
    """**一个 rm -rf 都不许吃未加引号、或者可能为空的变量。**

    整份脚本里只允许两处 ``rm -rf``,都长成 ``rm -rf -- "$target"``,
    而且都在 ``rm_sys`` / ``rm_agent_trace`` 里面 —— 前面有空值检查和范围检查挡着。

    **两处现在都写成 ``if rm -rf -- "$target"; then``**:删不掉只记一笔
    (``RM_FAILED``),不让 ``set -e`` 把后面几处和 5/5 回执一起带走。外面套的
    那个 ``if`` 不改 ``rm`` 吃的东西 —— 它吃的仍然是刚被两把锁验过的、
    加了引号的那个变量,而这条断言守的正是这件事。
    """
    文本 = 读(卸载脚本)
    出现 = []
    for 一条 in re.findall(r"(?m)^\s*(?:if\s+)?rm\s+[^\n]*", 文本):
        一条 = 一条.strip()
        一条 = re.sub(r"^if\s+", "", 一条)
        一条 = re.sub(r"\s*;\s*then$", "", 一条)
        出现.append(一条)
    assert 出现, "找不到任何 rm —— 这个脚本还卸什么"
    for 一条 in 出现:
        assert 一条 == 'rm -rf -- "$target"', f"rm 的形状不对:{一条}"
    assert len(出现) == 2, f"rm 的处数变了({len(出现)} 处),新加的那处有没有过两把锁"


def test_删之前有空值检查和范围检查():
    """两把锁:目标不许为空,目标必须长成允许的形状之一。"""
    for 名字 in ("rm_sys", "rm_agent_trace"):
        体 = 取函数体(读(卸载脚本), 名字)
        assert re.search(r'if \[ -z "\$target" \]', 体), f"{名字} 没有空值检查"
        assert "case \"$target\" in" in 体, f"{名字} 没有范围检查"
        assert "拒绝删除" in 体, f"{名字} 落在范围外时没有拒绝"
        锁位置 = 体.index("case \"$target\" in")
        删位置 = 体.index("rm -rf")
        assert 锁位置 < 删位置, f"{名字} 的范围检查排在 rm 后面,等于没锁"


def test_范围只盖我们自己的东西():
    """``rm_sys`` 的白名单里不许出现能匹配到别人东西的形状。

    W01 之后巡检数据根挪到了 ``/var/lib/d1max``(版本槽外面,理由见
    ``deploy/d1max-patrol.service`` 里 ``D1MAX_DATA_ROOT`` 那段注释),
    所以白名单多了这一条 —— 仍然是我们自己专用、不跟别家共用的路径。
    """
    体 = 取函数体(读(卸载脚本), "rm_sys")
    形状 = re.findall(r"(?m)^\s*(/[^)\s|]+(?:\|/[^)\s|]+)*)\)\s*;;", 体)
    assert 形状, "rm_sys 里读不出白名单"
    # W01b 的两处在 /opt、/etc/d1max 之外,但都是**精确路径、不带通配**,
    # 而且名字里带 d1max —— 匹配不到别人的东西。
    精确的 = {"/usr/local/sbin/d1max-privileged", "/usr/local/sbin/d1max-restart-now",
           "/etc/sudoers.d/d1max"}
    for 一组 in 形状:
        for 一条 in 一组.split("|"):
            if 一条 in 精确的:
                assert "*" not in 一条 and "d1max" in 一条
                continue
            assert 一条.startswith("/opt/d1max") or 一条.startswith("/etc/d1max") \
                or 一条.startswith("/var/lib/d1max") \
                or 一条.startswith("/etc/systemd/system/"), f"白名单里有越界的形状:{一条}"
            if 一条.startswith("/etc/systemd/system/"):
                assert "d1max-" in 一条, f"systemd 那一条没有限定 d1max-:{一条}"


def test_碰都不许碰别人的目录():
    """ROS、别家的栈、不叫 d1max-* 的单元 —— 一个字节都不动。"""
    文本 = 读(卸载脚本)
    for 禁 in ("/opt/release", "/opt/ros", "/opt/ROS"):
        assert 禁 not in 文本, f"uninstall.sh 里出现了 {禁}"
    单元们 = set(re.findall(r"[A-Za-z0-9_.*-]+\.service", 文本))
    assert 单元们, "读不出任何 systemd 单元名"
    for 单元 in 单元们:
        assert 单元.startswith("d1max-"), f"这个单元不叫 d1max-*,不该被这个脚本碰:{单元}"


# ---------------------------------------------------------- footprint 只读


def test_足迹脚本一个字节都不往被查的目录里写():
    """**取证的工具自己会改现场,那证据就没有意义了。**"""
    文本 = 读(足迹脚本)
    行们 = 代码行(文本)
    写动作 = re.compile(r"(?<![\w-])(rm|mv|cp|chmod|chown|touch|tee|dd|install|truncate)(?![\w-])")
    for 行 in 行们:
        命中 = 写动作.search(行)
        assert not 命中, f"footprint.sh 里出现了写动作 {命中.group(0)}:{行.strip()}"
    被查的 = re.compile(r"/(?:etc|opt|usr/local)(?:/|\b)")
    for 行 in 行们:
        if ">" in 行 or "mkdir" in 行:
            # ``2>/dev/null`` 和 ``exec 3<>/dev/tcp/...`` 不算写盘
            清掉兜底 = 行.replace("2>/dev/null", "").replace(">/dev/null", "")
            assert not 被查的.search(清掉兜底), (
                f"footprint.sh 有一行既在重定向/建目录、又提到被查的目录:{行.strip()}")


def test_足迹脚本不会走遍整块盘():
    """``find /`` 在 Orin 上要跑很久,而且会把别人的东西全翻一遍。"""
    行们 = 代码行(读(足迹脚本))
    for 行 in 行们:
        assert not re.search(r"find\s+/\s", 行), f"出现了 find /:{行.strip()}"
        if re.search(r"(?<![\w-])find(?![\w-])", 行):
            assert "-maxdepth" in 行, f"这条 find 没有 -maxdepth:{行.strip()}"
            assert "timeout" in 行 or "/dev/null" in 行, (
                f"这条 find 没有 timeout 兜底:{行.strip()}")


def test_足迹脚本每条采集都兜底():
    """照 ``scripts/orin-survey.sh`` 的路子:一条采不到,不能把整次采集带崩。"""
    文本 = 读(足迹脚本)
    assert not re.search(r"(?m)^set -e", 文本), (
        "footprint.sh 不该开 set -e —— 机器上可能没有 ss、stat 的某些参数,"
        "一条采集失败要能接着往下走")
    assert re.search(r"(?m)^set -u", 文本), "但是 set -u 要开着,变量打错字要能发现"


def test_足迹脚本的输出落在runs底下():
    """默认输出路径来自参数,没给参数就落在 runs/footprint/ —— 不许落到被查的目录里。"""
    文本 = 读(足迹脚本)
    assert re.search(r"(?m)^DEFAULT_OUT_DIR=runs/footprint$", 文本), (
        "默认输出目录不再是 runs/footprint")


# ------------------------------------------------------- 标识符必须是 ASCII


@pytest.mark.parametrize("脚本", [装机脚本, 卸载脚本, 足迹脚本], ids=lambda 路径: 路径.name)
def test_bash的标识符只能用ASCII(脚本: Path):
    """**bash 的标识符只认 ASCII 字母和下划线。**

    ``根=/opt/d1max`` 这种写法不是赋值,而是一条"找不到的命令" ——
    配上 ``set -euo pipefail``,脚本会停在那一行,``bash -n`` 还查不出来
    (语法是合法的,只是语义完全不是作者想的那样)。
    注释和屏幕上打的中文不受影响,受影响的只有变量名和函数名。

    **``装机脚本`` 是后补进这个名单的。** 它原来不在,于是 install.sh 里那一
    整套中文变量名从来没被这条护栏看过一眼,而那份脚本一行都跑不起来 ——
    漏掉的那一份恰好是唯一真出过事的那一份。名单式的护栏最容易这样悄悄失效,
    所以 ``test_deploy_guards.py`` 里那一节改成按 glob 扫,不再靠人记得补名字。
    """
    行们 = 代码行(读(脚本))
    赋值 = re.compile(r"(?:^|[\s;(&|])([^\s;=(&|]+)=[^=]")
    函数 = re.compile(r"(?:^|\s)([^\s()]+)\(\)\s*\{")
    引用 = re.compile(r"\$\{?#?(\w+)")
    for 行 in 行们:
        for 名 in 赋值.findall(行):
            assert 名.isascii(), f"赋值目标不是 ASCII,bash 不会当它是赋值:{行.strip()}"
        for 名 in 函数.findall(行):
            assert 名.isascii(), f"函数名不是 ASCII:{行.strip()}"
        for 名 in 引用.findall(行):
            assert 名.isascii(), f"变量引用不是 ASCII,展不开:{行.strip()}"


# --------------------------------------------------------------- 真的跑一遍


def 有bash() -> str | None:
    """找一个**看得见这个仓库**的 bash。

    Windows 上 ``shutil.which("bash")`` 多半先撞上 System32 里那个 ``bash.exe``
    —— 那是 WSL 的入口, 它看不见 ``D:/...`` 这种路径, 跑起来一律 127。
    所以逐个候选探一下: 能 ``test -f`` 到这份脚本的那个才算数。
    """
    候选: list[str] = []
    找到的 = shutil.which("bash")
    if 找到的:
        候选.append(找到的)
    for 前缀 in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMW6432", ""),
                "C:/Program Files", "C:/Program Files (x86)"):
        if 前缀:
            候选.append(str(Path(前缀) / "Git" / "bin" / "bash.exe"))
            候选.append(str(Path(前缀) / "Git" / "usr" / "bin" / "bash.exe"))
    for 一个 in 候选:
        if not Path(一个).exists():
            continue
        try:
            完 = subprocess.run(
                [一个, "-c", 'test -f "$1"', "_", 装机脚本.as_posix()],
                capture_output=True, timeout=60)
        except OSError:
            continue
        if 完.returncode == 0:
            return 一个
    return None


@pytest.mark.parametrize("脚本", [卸载脚本, 足迹脚本], ids=lambda 路径: 路径.name)
def test_语法过得去(脚本: Path):
    """``bash -n``。查不出标识符那类问题,但语法错还是要当场拦住。"""
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    完 = subprocess.run(
        [bash, "-n", 脚本.as_posix()], capture_output=True, text=True, timeout=60)
    assert 完.returncode == 0, f"{脚本.name} 语法不过:{完.stderr}"


def test_卸载脚本真的能跑一遍dry_run(tmp_path: Path):
    """**只跑 dry-run。**

    真跑一遍会去动 ``/opt/d1max`` 和 ``/etc/d1max`` —— 在开发机或者 CI 上
    那可能是别人正在用的东西。想验证真删,去一台一次性的机器上手工跑。
    """
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    (tmp_path / "runs" / "field-logs").mkdir(parents=True)
    (tmp_path / "motion").mkdir()
    留着的 = tmp_path / "runs" / "d1max-agent.pid"
    留着的.write_text("999999\n", encoding="utf-8")
    二进制 = tmp_path / "motion" / "patrol_agent"
    二进制.write_text("", encoding="utf-8")
    完 = subprocess.run(
        [bash, 卸载脚本.as_posix(), "--dry-run", "--agent-root", "."],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert 完.returncode == 0, f"dry-run 退出码不是 0:{完.stderr}"
    输出 = 完.stdout
    assert "dry-run" in 输出
    assert "RK3588" in 输出, "dry-run 也必须把释放 SDK 会话的代价打出来"
    # 代价那段话要排在动手那一段前面
    assert 输出.index("RK3588") < 输出.index("2/5"), "代价那段话排在了动手之后"
    assert "[dry-run] 会删" in 输出, "dry-run 应该把要删的东西列出来"
    # **dry-run 一个文件都不许动**
    assert 留着的.exists(), "dry-run 把 pid 文件删了"
    assert 二进制.exists(), "dry-run 把二进制删了"


def test_没装过的机器上dry_run也干净退出(tmp_path: Path):
    """幂等:什么都没有的目录上跑一遍,不该报错。"""
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    完 = subprocess.run(
        [bash, 卸载脚本.as_posix(), "--dry-run", "--keep-data", "--agent-root", "."],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert 完.returncode == 0, f"空目录上退出码不是 0:{完.stderr}"


def test_不认识的参数要挡住(tmp_path: Path):
    """打错开关就把整台机器清了,那太贵了。"""
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    完 = subprocess.run(
        [bash, 卸载脚本.as_posix(), "--purge"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert 完.returncode == 2, "不认识的参数应该退 2,而不是照常往下跑"


def test_足迹脚本能采能比(tmp_path: Path):
    """装机前采一次、装机后比一次 —— 把这条路真的走一遍。

    被查的目录用 ``D1MAX_FOOTPRINT_DIRS`` 指到临时目录上,
    免得在开发机上去翻 ``/etc`` 和 ``/opt``。
    """
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    场地 = tmp_path / "site"
    场地.mkdir()
    (场地 / "vendor-a.conf").write_text("vendor\n", encoding="utf-8")
    # 目录名用 ASCII —— 中文经环境变量过一遍 Windows 上的 bash 会变样,
    # 那就变成在验 Windows 的编码, 不是在验这个脚本
    环境 = {**os.environ, "D1MAX_FOOTPRINT_DIRS": "site:2"}

    采 = subprocess.run(
        [bash, 足迹脚本.as_posix(), "snapshot", "before.txt"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        encoding="utf-8", env=环境)
    assert 采.returncode == 0, f"snapshot 退出码不是 0:{采.stderr}"
    快照 = (tmp_path / "before.txt").read_text(encoding="utf-8")
    assert "FILE|" in 快照, "快照里没有文件条目"
    assert "PORT|" in 快照, "快照里没有端口条目"

    # 什么都没变 —— 应该退 0
    没变 = subprocess.run(
        [bash, 足迹脚本.as_posix(), "diff", "before.txt"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        encoding="utf-8", env=环境)
    assert 没变.returncode == 0, f"什么都没变却退了非 0:{没变.stdout}{没变.stderr}"

    # 冒出一个不是我们的文件 —— 必须退非 0,而且要点名
    (场地 / "vendor-b.conf").write_text("payload\n", encoding="utf-8")
    变了 = subprocess.run(
        [bash, 足迹脚本.as_posix(), "diff", "before.txt"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        encoding="utf-8", env=环境)
    assert 变了.returncode != 0, "别人的文件变了却还是退 0 —— 这条证据就废了"
    assert "不是我们的" in 变了.stdout, f"没点名:{变了.stdout}"
    assert "vendor-b.conf" in 变了.stdout, f"没把新冒出来的文件列出来:{变了.stdout}"


def test_足迹脚本的子命令要认得():
    """瞎给一个子命令,应该给用法然后退 2,而不是默默什么都不做。"""
    bash = 有bash()
    if not bash:
        pytest.skip("这台机器上没有 bash")
    完 = subprocess.run(
        [bash, 足迹脚本.as_posix(), "no-such-subcommand"],
        capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert 完.returncode == 2, f"退出码应该是 2,实际是 {完.returncode}"
