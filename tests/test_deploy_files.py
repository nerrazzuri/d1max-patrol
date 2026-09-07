"""装机那几个文件。**测的是它们的约定,不是它们的手艺。**

真机上跑不跑得起来只有真机说了算(见 docs/真机待验证清单.md)。这里守的是几条
一旦写错就会在客户现场变成事故的硬约定:守卫必须在服务之前跑、单元必须指着
符号链接而不是某一版、脚本必须能重跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


@pytest.fixture
def 服务单元() -> str:
    return (DEPLOY / "d1max-patrol.service").read_text(encoding="utf-8")


@pytest.fixture
def 守卫单元() -> str:
    return (DEPLOY / "d1max-bootguard.service").read_text(encoding="utf-8")


@pytest.fixture
def 装机脚本() -> str:
    return (DEPLOY / "install.sh").read_text(encoding="utf-8")


def test_三个文件都在(服务单元, 守卫单元, 装机脚本):
    assert (DEPLOY / "install.sh").exists()
    assert (DEPLOY / "d1max-patrol.service").exists()
    assert (DEPLOY / "d1max-bootguard.service").exists()
    # fixture 本身已经证明这三个文件读得出内容,这里再显式断言一次,
    # 免得将来有人把 fixture 换成别的来源却没人发现断言早就名不副实。
    assert 服务单元
    assert 守卫单元
    assert 装机脚本


def test_服务指着符号链接而不是某一版(服务单元):
    """指死某一版的话,换版本就换不动,回滚也回不去 —— 双槽整个白做。"""
    assert "/opt/d1max/current/" in 服务单元
    assert "/opt/d1max/releases/" not in 服务单元


def test_守卫排在服务之前(守卫单元, 服务单元):
    """顺序反了,守卫就永远在坏版本已经失败之后才跑,救不了任何东西。"""
    assert "Before=d1max-patrol.service" in 守卫单元
    assert "d1max-bootguard.service" in 服务单元      # 服务 After= 它


def test_守卫是一次性的而且失败不拦开机(守卫单元):
    """三条分别断言,不用 or 兜底。

    之前写成三选一的 or,`RemainAfterExit=yes` 恒真,删掉
    `SuccessExitStatus=0 1` 那一行(真正兜住"守卫挂了也不拦开机"的机制)
    测试照样绿。这条具体值必须原样在,删掉就是把这层保证撤了。
    """
    assert "Type=oneshot" in 守卫单元
    assert "RemainAfterExit=yes" in 守卫单元
    assert "SuccessExitStatus=0 1" in 守卫单元


def test_服务会自己重启但不无限刷屏(服务单元):
    assert "Restart=always" in 服务单元
    assert "RestartSec=" in 服务单元


def test_脚本能重跑():
    """装机的人会重跑它 —— 装到一半断了、参数填错了、第二台机器。"""
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "mkdir -p" in text
    # 幂等的证据:创建目录、写单元、启用服务,都要是"再来一次也不出错"的形式。
    assert "systemctl enable" in text


def test_脚本不把口令写死在里头():
    """这个仓是私有的,但**四个交付文件**都是要发给客户现场的人的。

    之前只查了 install.sh,两个 .service 和装机清单同样会被发出去,
    漏查它们等于这条护栏只盖了四分之一的交付面。
    """
    交付文件 = (
        DEPLOY / "install.sh",
        DEPLOY / "d1max-patrol.service",
        DEPLOY / "d1max-bootguard.service",
        ROOT / "docs" / "装机清单.md",
    )
    for 文件 in 交付文件:
        text = 文件.read_text(encoding="utf-8")
        for 不该出现 in ("12345678", "XG2WIFI"):
            assert 不该出现 not in text, f"{文件.name} 里不该出现 {不该出现!r}"


def test_装机清单存在而且提到上装那一步():
    """§7.1 纪律 1:上装属性必须是装机时被记下来的,不是靠人记得。"""
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "上装" in text
    assert "release install" in text


# ------------------------------------------------------------- 裁定 1: ExecStart

def test_服务的ExecStart指着真实存在的入口(服务单元):
    """cli.py 的 build_parser() 里没有 serve 这个子命令 —— 指着它的话,

    Restart=always 会让服务每 5 秒 SystemExit 2 一次,把日志刷成一堵墙。
    真正的 HTTP 服务入口是 app/server.py 的 main(),pyproject.toml 里
    注册成 d1max-app,这里直接用 -m 调那个模块。
    """
    assert "-m d1max_patrol.app.server" in 服务单元
    assert "cli serve" not in 服务单元


# ------------------------------------------------------------- 裁定 2: 槽内 venv

def test_装机脚本会给这一版建自己的venv(装机脚本):
    """服务单元的 ExecStart 指着 <槽>/venv/bin/python,这个解释器得有人建。

    install.sh 只把包装进根下的 bin-venv,engine/release.py 的 stage() 只是
    shutil.copytree,谁都没建过槽内的 venv —— 照抄的话那个解释器不存在。
    双槽的意义之一就是回滚要把依赖也退回去,所以是每版一个 venv,不是
    服务共用根解释器。
    """
    assert "python3 -m venv" in 装机脚本
    assert "venv/bin/python" in 装机脚本
    # 幂等的证据要断言完整形状,不能只查裸 "-x" —— 2/7 步给根解释器的判据
    # 里也有 "-x",查裸字符串的话删掉这一步槽内 venv 的幂等判据,测试照样
    # 绿。这里连槽路径变量一起断言,才是真的在测这一步。
    assert '[[ ! -x "$槽/venv/bin/python" ]]' in 装机脚本


# ------------------------------------------------------------- 裁定 3: D1MAX_SN

def test_两个单元都读EnvironmentFile(服务单元, 守卫单元):
    """D1MAX_SN 的来源必须唯一 —— 命令行 release activate 记进 pending.json 的

    SN,跟 HTTP 服务侧解析出来的 SN 得是同一个来源,否则重启后自检第四项
    (identity)会假失败,把一版好的自动回滚掉。前缀的 '-' 是"文件不在也别
    拦启动",不能省。
    """
    assert "EnvironmentFile=-/etc/d1max/env" in 服务单元
    assert "EnvironmentFile=-/etc/d1max/env" in 守卫单元


def test_装机脚本播种env文件但不覆盖已有的(装机脚本):
    """现场填过的 D1MAX_SN 不能被重跑抹掉。"""
    assert "/etc/d1max/env" in 装机脚本
    # 幂等的证据:先判文件在不在,不在才写。
    assert "-e /etc/d1max/env" in 装机脚本 or "-f /etc/d1max/env" in 装机脚本
    assert "D1MAX_SN" in 装机脚本


def test_装机清单里SN要在activate之前填():
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "/etc/d1max/env" in text
    assert "D1MAX_SN" in text
    assert "release activate" in text


# ------------------------------------------------------------- fix1: 评审回来的修复

def test_服务对守卫用Wants不用Requires(服务单元):
    """systemd.unit(5):跟顺序依赖(After=)同时存在时,Requires= 会在被依赖

    单元没有成功激活时连带不启动本单元。守卫是 Type=oneshot,只有以
    SuccessExitStatus 里的码退出才算成功——`/opt/d1max/bin/python` 缺失或
    损坏这种真机会发生的坏法,恰恰是守卫本该兜底、而不是自己也 failed 的
    场景。用 Requires= 的话,守卫一 failed,主服务跟着起不来,机器连网页
    都开不出来——没有服务严格地更坏。顺序依然要保证,所以 After= 必须还在。
    """
    assert "Wants=d1max-bootguard.service" in 服务单元
    assert "Requires=d1max-bootguard.service" not in 服务单元
    assert "After=network-online.target d1max-bootguard.service" in 服务单元


def test_脚本重跑到已经是这一版时跳过切换但仍然重启(装机脚本):
    """release activate 撞见"已经是在跑的这一版"会报错退出(cli.py 把

    ReleaseError 变成 exit 2,install.sh 有 set -euo pipefail),脚本会
    就地中止,restart 走不到——而 restart 正是"填完 SN 让它生效"的唯一
    手段。这里断言的是跳过切换那个具体分支的形状,不是裸查 "readlink"
    这种在别处也可能出现的字符串。
    """
    assert 'basename "$(readlink -f "$根/current"' in 装机脚本
    assert '"$当前" == "$名字"' in 装机脚本
    assert "已经是在跑的那一版了,跳过切换" in 装机脚本
    # restart 不能被套进上面那个分支里 —— 顶格(不缩进)才说明它在
    # if/else 的 fi 之后,两条路都会走到,不是只有切换成功那一路才走。
    assert "\nsystemctl restart d1max-patrol.service" in 装机脚本
