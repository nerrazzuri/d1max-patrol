"""装机那几个文件。**测的是它们的约定,不是它们的手艺。**

真机上跑不跑得起来只有真机说了算(见 docs/真机待验证清单.md)。这里守的是几条
一旦写错就会在客户现场变成事故的硬约定:守卫必须在每一次服务启动之前跑一遍、
守卫自己挂了不许拦住服务启动、单元必须指着符号链接而不是某一版、脚本必须能重跑。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


@pytest.fixture
def 服务单元() -> str:
    return (DEPLOY / "d1max-patrol.service").read_text(encoding="utf-8")


#: 守卫那一行的完整形状。前缀的 '-'、根下(不是 current 下)的解释器、
#: 子命令三样缺一不可 —— 分开断言各自的理由见下面几条用例。
GUARD_LINE = ("ExecStartPre=-/opt/d1max/bin/python "
              "-m d1max_patrol.cli release boot-guard")

#: 任务包守卫那一行。**跟上面那条是两回事,两条都要**(评审复评 finding 4):
#: 上面那条修版本目录的链,这条修 /opt/d1max/bundles 底下那两条链。
BUNDLE_GUARD_LINE = ("ExecStartPre=-/opt/d1max/bin/python "
                     "-m d1max_patrol.cli bundle guard")


@pytest.fixture
def 装机脚本() -> str:
    return (DEPLOY / "install.sh").read_text(encoding="utf-8")


def test_两个文件都在而且没有第二个单元(服务单元, 装机脚本):
    """交付的单元**只剩一个**。

    d1max-bootguard.service 连同它 WantedBy=multi-user.target 的形态一起
    删掉了:守卫挪成了主单元的 ExecStartPre=。两个单元同时存在的话,真开机
    时守卫会被数两次(attempts 一次开机加二),第二次开机就把一版本来健康
    的退掉 —— 比守卫根本没跑还危险,所以"仓里不再有它"本身是条硬约定。
    """
    assert (DEPLOY / "install.sh").exists()
    assert (DEPLOY / "d1max-patrol.service").exists()
    assert not (DEPLOY / "d1max-bootguard.service").exists()
    # fixture 本身已经证明这两个文件读得出内容,这里再显式断言一次,
    # 免得将来有人把 fixture 换成别的来源却没人发现断言早就名不副实。
    assert 服务单元
    assert 装机脚本


def test_服务指着符号链接而不是某一版(服务单元):
    """指死某一版的话,换版本就换不动,回滚也回不去 —— 双槽整个白做。"""
    assert "/opt/d1max/current/" in 服务单元
    assert "/opt/d1max/releases/" not in 服务单元


def test_守卫在每一次服务启动之前跑一遍(服务单元):
    """顺序反了,守卫就永远在坏版本已经失败之后才跑,救不了任何东西。

    原来这条靠守卫单元的 ``Before=d1max-patrol.service`` + 服务单元的
    ``After=`` 保证。**换成 ``ExecStartPre=`` 之后顺序是 systemd 的语义
    保证的**(ExecStartPre 全部跑完才轮到 ExecStart),而且顺带把原来那个
    洞补上了:守卫现在每一次服务启动都数一遍,不只是真开机那一次 ——
    不带上装的机器升级走的正是 ``systemctl restart``,根本不开机。
    """
    assert GUARD_LINE in 服务单元
    # ExecStartPre 排在 ExecStart 之前 —— 顺序在文件里也看得见。
    assert 服务单元.index("ExecStartPre=") < 服务单元.index("\nExecStart=")
    # 守卫用的是根下那个跟版本无关的解释器,不是 current 下那一版自己的。
    assert "/opt/d1max/bin/python -m d1max_patrol.cli release boot-guard" in 服务单元


def test_任务包守卫也挂在每一次服务启动之前(服务单元):
    """**评审复评 finding 4:修复路径写好了,却没有任何一个地方调它。**

    上面那条守的是版本目录(``/opt/d1max/releases`` + ``current`` 链)。
    任务包是另一套链(``/opt/d1max/bundles`` 底下的 ``current``/``previous``),
    ``engine/bundle.py`` 的 ``guard_bundle()`` 是「apply 连着换两条链,两次
    之间断电」唯一的出路 —— 而 ``docs/任务包格式.md`` 已经对客户写着「开机时
    照着它把链修回一个能用的样子」。没人调的话那句话就是假的,而现场看到的
    是狗把盘上唯一那份好包拉黑之后再也装不回去。

    **两条都要,而且都是 ExecStartPre。** 不带上装的机器升级走的是
    ``systemctl restart``(见 ``engine/selfcheck.py`` 的 ``restart_plan()``),
    根本不开机 —— 挂成独立的 oneshot 单元的话,守卫在最需要它的那条路上
    永远不跑,这正是版本守卫当初挪过来的理由。
    """
    行 = [ln for ln in 服务单元.splitlines() if ln.startswith("ExecStartPre=")]
    assert len(行) == 2                      # 版本一条,任务包一条,不多不少
    # **断言的对象必须是从 service 文件里读出来的那一行**,不是本文件里的
    # 字面量常量(评审复评第 3 轮 N7:原来那句
    # ``BUNDLE_GUARD_LINE.startswith("ExecStartPre=-")`` 比的是同一个文件里的
    # 常量跟它自己的前缀 —— **恒真**,跟 service 文件毫无关系,而它偏偏长在
    # 交付面守卫的测试文件里)。
    # 挑行用的是**不含那个 '-' 的弱特征**,所以真把 '-' 从 service 里拿掉时,
    # 红的是下面那一句,而不是先被 ``BUNDLE_GUARD_LINE in 服务单元`` 拦掉。
    任务包行 = [ln for ln in 行 if ln.endswith("d1max_patrol.cli bundle guard")]
    assert len(任务包行) == 1
    # 前缀的 '-' 跟版本守卫同一条理由:安全网不该比它防的问题更危险。
    assert 任务包行[0].startswith("ExecStartPre=-")
    assert BUNDLE_GUARD_LINE in 服务单元
    assert 服务单元.index(BUNDLE_GUARD_LINE) < 服务单元.index("\nExecStart=")
    # **根下那个跟版本无关的解释器**,不是 current 下的 —— 一个坏到解释器都
    # 装歪了的版本,不该把自己的救生索也带坏。
    assert "/opt/d1max/current/venv/bin/python -m d1max_patrol.cli" \
        not in 服务单元


def test_守卫自己挂了也不拦服务启动(服务单元):
    """原来这层保证由守卫单元的 ``SuccessExitStatus=0 1`` 兜着。

    守卫改成 ``ExecStartPre=`` 之后,**那条兜底的活归行首那个 ``-``**:
    没有它的话,``/opt/d1max/bin/python`` 缺失或损坏 —— 正是守卫本该兜底
    的那种坏法 —— 会让 ExecStartPre 失败并中止整个服务启动,机器连网页都
    开不出来。那就是我们刚用 ``Wants=`` 替掉 ``Requires=`` 修掉的那个陷阱
    换个地方重犯。安全网不该比它防的问题更危险。

    ``WorkingDirectory`` 那个 ``-`` 是同一条理由的第二半:``current`` 悬空
    (指着一版已经被删掉的目录)时,没有 ``-`` 的话 systemd 进不去工作目录,
    ExecStartPre 自己就跑不起来 —— 而修 ``current`` 正是守卫要干的事。

    两条分别断言,不用 or 兜底:写成二选一的 or 时,删掉真正兜住这层保证
    的那一样,测试照样绿。
    """
    assert "ExecStartPre=-/opt/d1max/bin/python" in 服务单元
    assert "WorkingDirectory=-/opt/d1max/current" in 服务单元


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
    """这个仓是私有的,但**交付文件**都是要发给客户现场的人的。

    之前只查了 install.sh,.service 和装机清单同样会被发出去,漏查它们
    等于这条护栏只盖了一小半的交付面。名单集中在 ``交付文件()`` 里,
    交付面增减一份文件时,这条护栏跟着它走,不会漏。
    """
    for 文件 in 交付文件():
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

def test_单元读EnvironmentFile(服务单元):
    """D1MAX_SN 的来源必须唯一 —— 命令行 release activate 记进 pending.json 的

    SN,跟 HTTP 服务侧解析出来的 SN 得是同一个来源,否则重启后自检第四项
    (identity)会假失败,把一版好的自动回滚掉。前缀的 '-' 是"文件不在也别
    拦启动",不能省。

    守卫那一侧原来靠 d1max-bootguard.service 上同名的一行读同一份文件;
    守卫挪成本单元的 ExecStartPre 之后,它跟服务共用这一行 ——"同一个来源"
    这条约束从"两处写得一样"变成了结构上的。
    """
    assert "EnvironmentFile=-/etc/d1max/env" in 服务单元


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

def test_单元里不再有对守卫单元的任何依赖(服务单元, 装机脚本):
    """守卫单元没了,单元里那两行(``After=``/``Wants=``)也得跟着走干净。

    原来那两行守的是"守卫一 failed 不能连带让主服务起不来"(``Wants=`` 而
    不是 ``Requires=``)。那层保证现在归 ``ExecStartPre=`` 行首的 ``-``,
    由上面 ``test_守卫自己挂了也不拦服务启动`` 盯着;这里盯的是另一半:
    留一行指着一个不存在的单元,会让下一个读这个文件的人以为守卫还是那个
    形态,而那正是"守卫被数两次"那类事故的温床。

    ``After=network-online.target`` / ``Wants=network-online.target`` 保留
    —— 那跟守卫无关,是网卡的事。

    ``install.sh`` 那一侧同样要**幂等地**把老机器上已经装着的那个单元清掉:
    仓里删掉不等于现场那台机器上删掉了,而现场留着它就是"数两次"。
    """
    # 查的是**指令行**,不是"这几个字不许出现" —— 单元里那段讲清楚守卫为什么
    # 从独立单元挪成 ExecStartPre 的注释,恰恰是这次改动最该留下的东西。
    指令行 = [ln for ln in 服务单元.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [ln for ln in 指令行 if "d1max-bootguard" in ln]
    assert "After=network-online.target" in 服务单元
    assert "Wants=network-online.target" in 服务单元
    assert "systemctl disable --now d1max-bootguard.service" in 装机脚本
    assert "rm -f /etc/systemd/system/d1max-bootguard.service" in 装机脚本
    # 清老单元要排在 daemon-reload 之前,不然这一次 reload 读到的还是它。
    assert (装机脚本.index("rm -f /etc/systemd/system/d1max-bootguard.service")
            < 装机脚本.index("systemctl daemon-reload"))
    assert "systemctl enable d1max-patrol.service" in 装机脚本


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
#: 早先这里是字面量 ``":10010"``,**带冒号** —— 于是"端口 10010""参见 10010
#: 那条"这种更可能真的被写出来的形状全都逃得过(终评实测漏网)。改成看**数字
#: 本身**,前后不许再跟数字,免得误伤 ``1100109`` 这类无关的串。
VENDOR_WS_PORT = re.compile(r"(?<!\d)10010(?!\d)")


def 交付文件() -> tuple[Path, ...]:
    """会被发到客户现场那几份。**护栏都按这一份名单铺。**"""
    return (
        DEPLOY / "install.sh",
        DEPLOY / "d1max-patrol.service",
        ROOT / "docs" / "装机清单.md",
        ROOT / "docs" / "任务包格式.md",
        ROOT / "docs" / "鉴权与控制权.md",
        ROOT / "docs" / "值守与告警.md",
    )


def test_交付文件里不许出现厂商导航端口():
    """10010 是厂商导航 WebSocket 的端口,我们的 HTTP 服务在 8095。

    写错的后果是整台机器的验收全废:三条验收命令全部 connection refused,
    于是上装属性永远记不上,于是这台机器永远不许升级(precheck 的 payload
    那一项拦着)。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        assert VENDOR_WS_PORT.search(text) is None, \
            f"{文件.name} 里不该出现厂商导航端口(不管带不带冒号)"


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


# ----------------------------------------------- 第 6 卷:鉴权与控制权交付面

#: 装机脚本会当场随机生成 PIN,所以任何**字面量** PIN 出现在交付文件里都是
#: 错的 —— 不是「示例」,是一台真机器的钥匙被印在了发给客户的纸上。
PIN_字面量 = (
    re.compile(r"D1MAX_PIN\s*=\s*\d"),
    re.compile(r'"pin"\s*:\s*"\d'),
    re.compile(r"--pin\s+\d"),
    re.compile(r"设备 ?PIN 是 \d"),
)

#: 一串正好六位的数字。**不查某一个具体的数** —— 查的是形状,因为每台机器
#: 的 PIN 都不一样,而"照抄一条真命令进文档"这件事每台机器都可能犯一次。
六位数 = re.compile(r"(?<!\d)\d{6}(?!\d)")

#: 出现在这个字附近的六位数才算 PIN。光凭"六位数"会误伤
#: (`docs/任务包格式.md` 里的版本号上界 999999 就是一个),所以要看上下文。
PIN_上下文 = re.compile(r"PIN|--pin|\"pin\"")

#: 六位数左右各看这么多个字符,判它是不是长在 PIN 边上。
PIN_窗口 = 160

#: 「某某口令是这个」的几种写法。上面 ``test_脚本不把口令写死在里头`` 查的是
#: 两个**具体的串**(热点密码和 SSID),这里查的是**形状** —— 热点密码会换、
#: SSH 口令本来就不是那两个串里的任何一个,只钉具体值的话,下一次泄的是别的
#: 东西,那条护栏一样绿。
口令字面量 = (
    re.compile(r"(?i)\bpsk\s*[:=]\s*\S"),
    re.compile(r"(?i)\bpass(word|wd)?\s*[:=]\s*\S"),
    re.compile(r"(密码|口令)\s*(是|为)?\s*[:：=]\s*\S"),
    re.compile(r"sshpass"),
)


def test_交付文件里不许出现PIN字面量():
    """这一卷开始,交付文档里会出现取 token 的命令。

    照抄一条带着真 PIN 的命令进文档,等于把一台机器的钥匙印在发给客户的纸
    上 —— 而这份文档会被复制到每一个站点。命令里只许出现 ``$D1MAX_PIN``
    这种从 ``/etc/d1max/env`` 取值的写法。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        for 模式 in PIN_字面量:
            assert 模式.search(text) is None, \
                f"{文件.name} 里出现了 PIN 字面量:{模式.pattern}"


def test_交付文件里不许有长在PIN边上的六位数():
    """上一条查的是四种**写法**,这一条查的是**形状**。

    PIN 是六位数字,每台机器一个;拿具体某个数去查等于只防住已经犯过的那
    一次。所以这里找的是"六位数字长在 PIN 这个字附近",不管它是哪六位。
    单纯的六位数不算 —— `docs/任务包格式.md` 里的版本号上界 999999 是正当
    的,它周围没有 PIN。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        for m in 六位数.finditer(text):
            窗 = text[max(0, m.start() - PIN_窗口):m.end() + PIN_窗口]
            assert PIN_上下文.search(窗) is None, \
                f"{文件.name} 里 {m.group()} 长在 PIN 边上,像是抄了一台真机器的 PIN"


def test_交付文件里不许出现口令字面量():
    """§6.5:狗的热点密码出厂固定、改不了,SSH 口令一样是长期凭证。

    把任何一个印进发到现场的文件里,等于把"射程之内任何人"这个攻击面扩大
    到"拿到过任何一份交付文档的人",而且撤不回来 —— 文档会被复制到每一个
    站点、贴进每一张工单。要举例一律用占位符。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        for 模式 in 口令字面量:
            assert 模式.search(text) is None, \
                f"{文件.name} 里出现了口令字面量:{模式.pattern}"


def test_鉴权与控制权那份文档在名单上():
    """交付面增减一份文件,泄密护栏要跟着它走 —— 这一条守的就是那个「跟」。"""
    assert ROOT / "docs" / "鉴权与控制权.md" in 交付文件()


def test_值守与告警那份文档在名单上():
    """第 8 卷加的第六份交付件。

    加一份要发到现场的文档而**忘了把它挂进名单**,是这条护栏最现实的失效
    方式 —— 上面每一条泄密用例都只遍历 ``交付文件()``,不在名单上的文件被
    扫不到,而且扫不到的时候一条测试都不会红,看起来跟干净一模一样。
    """
    assert ROOT / "docs" / "值守与告警.md" in 交付文件()


def test_鉴权文档说清了三个时间尺度():
    """§6.4:三个尺度不许混。文档上混了,现场的人就会去调错的那个数。"""
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    for 关键词 in ("0.6 秒", "30 秒", "12 小时", "30 分钟"):
        assert 关键词 in text, f"三个时间尺度里少了 {关键词}"


def test_鉴权文档说清了姓名不核实():
    """§6.3:界面上不许把「张三报了个名」写成「已登录:张三」。"""
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    assert "不核实" in text
    assert "operator_verified" in text


def test_鉴权文档讲了热点是敌意网络():
    """§6.5:不许假设「局域网 = 可信」。"""
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    assert "射程" in text
    assert "只读" in text


def test_鉴权文档没把热点说成只能看():
    """**这一条挡的是一份会在应急通道上给出反向指引的文档。**

    换到什么凭证是二维的:在哪个网 × 怎么解锁。``Guard.unlock``(明文 PIN)
    才按通道判只读,``Guard.unlock_proof``(质询-应答)是**无条件**发完整凭证
    —— 而手机 app 走的正是质询-应答,它是这个产品唯一的操作端。

    文档要是只写"热点 → 只读 → 不能动机器",现场的人在客户网崩了的时候会
    据此放弃热点这条应急通道,去派人进带电区抱狗 —— 而他掏出手机连热点本来
    就能把狗开走。所以这里查的是:那张表上"热点 + 质询-应答 → 完整凭证"这
    一行在不在。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    行 = [ln for ln in text.splitlines()
          if "热点" in ln and "质询-应答" in ln and "完整" in ln]
    assert 行, "鉴权与控制权.md 没说清「热点上走质询-应答换到的是完整凭证」"


def test_鉴权文档说破了热点上token也是明文的():
    """``auth.py`` 早就承认"这条通道上 PIN 和 token 都要当成已经公开",

    交付文档却只讲 PIN。而攻击者要的本来就是 token 不是 PIN:token 在同一条
    没有 TLS 的链路上原样飞回手机,抄走一个正在用的就够了。不写这一句,第七
    节"PIN 只有六位"那段会把人引向"把 PIN 加长"——那条路在热点上等于没做。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    行 = [ln for ln in text.splitlines() if "token" in ln and "抄" in ln]
    assert 行, "鉴权与控制权.md 没说破「热点上 token 本身也在明文链路上」"


def test_鉴权文档没把急停说过头():
    """"任何时候都按得下去"成立的前提是"你已经有 token"。

    名额闸(§3.6)排在签发 token **之前**:三个远端名额满了的时候,第四个人
    连凭证都换不到,``READONLY_OPEN_PATHS`` 对他没有意义。代码是合规格的,
    过头的是文档 —— 而现场会照着文档做判断。
    """
    for 名 in ("鉴权与控制权.md", "手机app.md"):
        text = (ROOT / "docs" / 名).read_text(encoding="utf-8")
        for m in re.finditer("任何时候都按得下去", text):
            窗 = text[max(0, m.start() - 400):m.end() + 400]
            assert "已经连上" in 窗, \
                f"{名} 把急停说过头了 —— 得写明前提是「已经连上」"


def test_鉴权文档写了通道判据的前提():
    """待办 47:整套通道分档建立在「``client`` 是 TCP 对端地址」上。

    哪天有人在前面加一层反向代理、改成读 ``X-Forwarded-For``,分档会静默
    失效 —— 伪造一个头就能把自己抬成最信任的那一档,而且没有一条测试会红。
    现场运维要动网络拓扑之前得先在这份文档里读到这句话。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    assert "X-Forwarded-For" in text
    assert "反向代理" in text


#: 鉴权文档的九个小节标题。**关键词护栏挡不住"把正文删空、只留关键词"** ——
#: 评审实证:把这份文档砍到 128 字节、只留几个关键词,上面那四条全绿。所以再
#: 加一层查形状的:标题在不在、篇幅够不够。
鉴权文档小节 = (
    "## 零、", "## 一、", "## 二、", "## 三、", "## 四、",
    "## 五、", "## 六、", "## 七、", "## 八、",
)

#: 这份文档现在 6700 多字。定 6000 是留了余量的下界 —— 它不是在管字数,是在
#: 拦"内容被掏空,只剩下够骗过关键词检查的骨架"。
鉴权文档最少字数 = 6000


def test_鉴权文档没有被掏空():
    """上面四条查的是关键词,而关键词是可以只留关键词的。

    一份只剩「30 分钟 operator_verified 射程 X-Forwarded-For」的文档能让那
    四条全绿,却对现场的人一点用都没有 —— 而这份文档是交付面上唯一讲清楚
    「谁能动这台机器」的东西。所以这里查两件形状上的事:九个小节还在不在,
    篇幅还够不够。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    for 标题 in 鉴权文档小节:
        assert 标题 in text, f"鉴权与控制权.md 少了小节 {标题}"
    assert len(text) > 鉴权文档最少字数, (
        f"鉴权与控制权.md 只剩 {len(text)} 字,像是被掏空了")


def test_验收命令都带上了token():
    """**这是在补一个真的缺陷。** 服务的 ExecStart 带 ``--host 0.0.0.0``,

    于是 ``check_exposure()`` 逼着必须有 PIN;而 ``Guard.gate()`` 没有本机
    豁免(``tests/app/test_auth.py`` 里有一条断言守着 127.0.0.1 也要 401)。
    所以第三节里那几条裸 ``curl`` 今天在真机上全部返回 401 —— 装机的人会
    以为服务坏了,或者更糟:以为「返回了东西」就算过了。
    """
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "/api/auth" in text                    # 先换 token
    assert "$D1MAX_PIN" in text                   # 从 env 取,不写死
    assert "Authorization: Bearer" in text
    for 接口 in ("/api/release", "/api/selfcheck", "/api/identity"):
        # 每一条验收 curl 都得带上 token,不能只改一条。
        assert f"$TOKEN\" http://127.0.0.1:8095{接口}" in text \
            or f"$TOKEN' http://127.0.0.1:8095{接口}" in text, \
            f"{接口} 那条验收命令没带 token"


def test_装机清单里没有裸curl():
    """光"每条都带 token"不够 —— 漏掉的那一条是**裸的**,上面那条查不出。

    第四节演练自动回滚那一步也要打 `/api/release`,它当年也是裸的。
    """
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    for 行 in text.splitlines():
        if "http://127.0.0.1:8095/api/" not in 行:
            continue
        assert "$TOKEN" in 行 or "/api/auth" in 行, f"这条 curl 没带 token:{行.strip()}"


def test_装机清单验收里查手写的SN():
    """§6.2:SN 必须装机时手写。``provisional`` 查不出这一条 —— 从设备树

    读出来的模组序列号它算「不是临时的」,而那个号换一次主板就变。
    """
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "hand_written" in text
