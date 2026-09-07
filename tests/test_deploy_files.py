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
    assert "Type=oneshot" in 守卫单元
    # 守卫挂了不能把开机拦住 —— 安全网不该比它防的问题更危险。
    assert "SuccessExitStatus" in 守卫单元 or "-/" in 守卫单元 \
        or "RemainAfterExit=yes" in 守卫单元


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
    """这个仓是私有的,但装机脚本是要发给客户现场的人的。"""
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    for 不该出现 in ("12345678", "XG2WIFI"):
        assert 不该出现 not in text


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
    # 幂等:已经建过就别重建。
    assert "-x" in 装机脚本


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
