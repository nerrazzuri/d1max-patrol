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


# --------------------------------------------------- fix2: 第 4 卷终评 A 组

#: 我们的 HTTP 服务真正听的端口(``app/server.py`` 的 ``DEFAULT_PORT``)。
HTTP_PORT = "8095"
#: 厂商导航 WebSocket 的端口。**不是我们的** —— 交付文件里出现它就是错的。
VENDOR_WS_PORT = ":10010"


def 交付文件() -> tuple[Path, ...]:
    """会被发到客户现场那几份。**护栏都按这一份名单铺。**"""
    return (
        DEPLOY / "install.sh",
        DEPLOY / "d1max-patrol.service",
        DEPLOY / "d1max-bootguard.service",
        ROOT / "docs" / "装机清单.md",
    )


def test_交付文件里不许出现厂商导航端口():
    """10010 是厂商导航 WebSocket 的端口,我们的 HTTP 服务在 8095。

    写错的后果是整台机器的验收全废:三条验收命令全部 connection refused,
    于是上装属性永远记不上,于是这台机器永远不许升级(precheck 的 payload
    那一项拦着)。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        assert VENDOR_WS_PORT not in text, f"{文件.name} 里不该出现 {VENDOR_WS_PORT}"


def test_装机脚本和清单都用8095():
    """光"没有 10010"不够 —— 得真的有人在说 8095,否则整段被删掉也照样绿。"""
    assert HTTP_PORT in (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert HTTP_PORT in (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")


def test_服务听在所有地址而不是只听本机(服务单元):
    """``DEFAULT_HOST`` 是 127.0.0.1,而 ``--host`` **没有**环境变量兜底

    (只有 --pin/--sn/--nickname 有)—— 不写在 ExecStart 上就没有第二个地方
    能把它改掉。手机 app 连的是 192.168.168.100:8095,只听本机的机器它根本
    连不上。
    """
    assert "--host 0.0.0.0" in 服务单元


def test_装机脚本第一次写env时当场生成PIN(装机脚本):
    """``--host 0.0.0.0`` 和 ``D1MAX_PIN`` 是一对,拆不开。

    ``check_exposure()`` 见到非本机地址而没有 PIN 会 ``SystemExit``;配上
    ``Restart=always`` + ``RestartSec=5``,那就是每 5 秒刷一条日志的启动
    循环。所以模板里那一行不能留空,得当场生成一个填进去。
    """
    assert "secrets.randbelow" in 装机脚本
    assert "D1MAX_PIN=%s" in 装机脚本            # printf 追加,不是留空的模板行
    # **只在文件不存在时生成** —— 跟 D1MAX_SN 同一条纪律,现场填过的不许覆盖。
    assert "-e /etc/d1max/env" in 装机脚本
    # 生成完要大声打出来,不然现场的人不知道这台机器的 PIN 是什么。
    assert "这台机器的设备 PIN 是" in 装机脚本


def test_装机脚本在PIN空着时停下来而不是把机器丢进启动循环(装机脚本):
    """跟 SN 那条警告不是一回事:SN 空只是**可能**被误判回滚,PIN 空是

    **必定**起不来。让它崩成每 5 秒一条日志的启动循环,比停下来喊一声糟得多
    —— 现场看到的是一堵日志墙。所以这里断言的是三样具体的东西:判据、
    ``exit 3``、以及它排在 ``systemctl restart`` **之前**。
    """
    assert "grep -qE '^D1MAX_PIN=.+' /etc/d1max/env" in 装机脚本
    assert "exit 3" in 装机脚本
    assert (装机脚本.index("exit 3")
            < 装机脚本.index("\nsystemctl restart d1max-patrol.service"))


def test_装机清单里有记PIN那一条():
    """§7.9 第 2 步「设置设备 PIN」。之前整条规格项在交付面上一个字都没有。"""
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "D1MAX_PIN" in text
    assert "--host 0.0.0.0" in text          # 为什么必须有 PIN,得说清楚
    assert "登记表" in text                   # 抄到哪儿去


def test_装机脚本把填好的SN透传给activate(装机脚本):
    """``sudo`` 默认 ``env_reset``,``D1MAX_SN`` 不在 ``env_keep`` 里。

    脚本自己不 source ``/etc/d1max/env`` 的话,``release activate`` 拿到的是
    空值,``cli.py`` 的 ``resolve()`` 落到设备树/MAC 兜底;而服务侧经
    ``EnvironmentFile=`` 拿到的是人填的真值 —— 重启后自检第四项恒红,
    ``postcheck_verdict`` 判 ROLLBACK,一版好的被退回去。首次装机因
    ``src=""`` 走 ``release.stuck`` 侥幸不炸,**从第二次升级起每次必炸**。
    """
    assert ". /etc/d1max/env" in 装机脚本                   # 读进脚本自己的环境
    assert "set -a" in 装机脚本 and "set +a" in 装机脚本      # 读进来的要导出去
    # 光 source 不够:sudo 那一行必须显式把它带过去。
    assert 'D1MAX_SN="${D1MAX_SN:-}"' in 装机脚本
    assert (装机脚本.index(". /etc/d1max/env")
            < 装机脚本.index('D1MAX_SN="${D1MAX_SN:-}"'))


def test_装机清单里有故意做坏的包那次演练():
    """§8.5:回滚要真跑过,而且演练**必须包含一次故意做坏的升级包** ——

    规格明说这一层「用装机验收里的一次演练覆盖」。
    """
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "演练" in text
    assert "release install" in text
    assert "拒收" in text                     # 看到拒收就是对的,得写明白
    assert "release.json" in text             # 改文件不改哈希,坏法要说清楚


def test_装机清单验收里查provisional():
    """§7.9 第 1 步:``provisional == true`` 的机器不许出厂。

    ``true`` 说明 SN 还在拿 MAC 兜底,以后每次升级都会把好版本判成失败。
    """
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "provisional" in text
    assert "/api/identity" in text


def test_装机脚本从临时副本装不污染待哈希的包目录(装机脚本):
    """``pyproject.toml`` 用 ``setuptools.build_meta``,pip 对本地目录是就地

    构建:会往源目录写 ``*.egg-info/`` 和 ``__pycache__/``。直接
    ``pip install "$包"`` 的话,紧接着 ``release install`` 算 ``tree_sha256``
    对账当场对不上,``set -e`` 把装机中止在那一步;直接 ``pip install "$槽"``
    则是把已落槽的那一版写脏,以后任何一次重新校验都会判它坏掉。
    """
    assert "mktemp -d" in 装机脚本
    assert 'pip install --quiet "$包"' not in 装机脚本
    assert 'pip install --quiet "$槽"' not in 装机脚本
    # 临时目录得有人清 —— trap 保证 set -e 中途退出、Ctrl+C 也清得掉。
    assert "trap 清理 EXIT" in 装机脚本
    assert "rm -rf" in 装机脚本


def test_根目录写死不给环境变量覆盖(装机脚本, 服务单元):
    """单元里 ``/opt/d1max`` 是逐字写死的,``AppContext.release_root`` 的默认

    值也是它。允许脚本被 ``D1MAX_ROOT`` 改而单元不跟着改,装出来的是一台
    脚本和服务各说各话的机器:包落在一个根下,服务读的是另一个根。
    """
    # 断言的是**那个展开**没了,不是"这三个字不许出现" —— 脚本里那段
    # 说明为什么去掉它的注释,恰恰是这条改动最该留下的东西。
    assert "${D1MAX_ROOT" not in 装机脚本
    assert "根=/opt/d1max" in 装机脚本
    # 单元那边是写死的,这条断言是上面那句"两边要一起改"的凭据。
    assert "/opt/d1max" in 服务单元
    # User= 那个覆盖保留了,但单元里同样是写死的 —— 注明过才算数。
    assert "D1MAX_USER" in 装机脚本
    assert "User=robot" in 服务单元
