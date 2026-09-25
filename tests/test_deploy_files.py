"""装机那几个文件。**测的是它们的约定,不是它们的手艺。**

真机上跑不跑得起来只有真机说了算(见 docs/真机待验证清单.md)。这里守的是几条
一旦写错就会在客户现场变成事故的硬约定:守卫必须在每一次服务启动之前跑一遍、
守卫自己挂了不许拦住服务启动、单元必须指着符号链接而不是某一版、脚本必须能重跑。
"""

from __future__ import annotations

import re
import subprocess
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


def test_代理单元也挂开机守卫():
    """W00c5d 第三部分:站点下发版本、代理自己切。新版代理起不来,守卫数够次数退回上一版。"""
    unit = (DEPLOY / "d1max-agent.service").read_text(encoding="utf-8")
    assert GUARD_LINE in unit
    assert unit.index("ExecStartPre=") < unit.index("\nExecStart=")


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


def test_服务把巡检数据指到版本槽外面(服务单元):
    """W01:没有这一行,--runs-root 默认相对路径 runs,落在
    WorkingDirectory=/opt/d1max/current 也就是版本槽里,升两次级就被 prune 删掉。"""
    assert "Environment=D1MAX_DATA_ROOT=/var/lib/d1max" in 服务单元


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
    # 幂等的证据要断言完整形状,不能只查裸的判据 —— 2/7 步给根解释器也有
    # 一个,查一个泛的字符串的话删掉这一步槽内 venv 的幂等判据,测试照样绿。
    # 这里连槽路径变量一起断言,才是真的在测这一步。
    # **判据换成哨兵了**(见 test_槽内venv的幂等判据看哨兵不看解释器):
    # "解释器在不在" 判不出 "依赖装完没有"。
    assert 'SENTINEL="$SLOT/venv/.deps-ok"' in 装机脚本
    assert '[[ ! -e "$SENTINEL" ]]' in 装机脚本


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
    就地中止,stop/start 走不到——而它们正是"填完 SN 让它生效"的唯一
    手段(W01 之后,重跑顺带把老槽的数据搬到 /var/lib/d1max,所以中间多了
    一步 migrate-data,不再是单条 restart)。这里断言的是跳过切换那个具体
    分支的形状,不是裸查 "readlink" 这种在别处也可能出现的字符串。
    """
    assert 'basename "$(readlink -f "$ROOT/current"' in 装机脚本
    assert '"$CURRENT_REL" == "$REL_NAME"' in 装机脚本
    assert "已经是在跑的那一版了,跳过切换" in 装机脚本
    # stop 和 start 都不能被套进上面那个分支里 —— 顶格(不缩进)才说明它们在
    # if/else 的 fi 之后,两条路都会走到,不是只有切换成功那一路才走。
    assert "\nsystemctl stop d1max-patrol.service || true" in 装机脚本
    assert "\nsystemctl start d1max-patrol.service" in 装机脚本


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
        # 共存这一组也要发到现场:装机前后各跑一次 footprint.sh 留证据,
        # 撤场时跑 uninstall.sh。挂在同一份名单上,护栏(口令、端口、
        # PIN 字面量、厂商协议特征、骨架下界)就自动全都罩到它们头上。
        DEPLOY / "footprint.sh",
        DEPLOY / "uninstall.sh",
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
    ``exit 3``、以及它排在 ``systemctl start``(W01 之前是 ``restart``)
    **之前**。
    """
    # 判据测的是**解析出来的值**,不是「文件里有没有一行长这样」。
    # 原来这里是 grep -qE '^D1MAX_PIN=.+',而 `D1MAX_PIN=   `(尾随空格)
    # 它判「有 PIN」,systemd 剥完空白读出来却是空串 —— 守卫在,日志墙也在。
    # (「不许退回去数行」那一条在 test_deploy_guards.py 里,那边有剥注释的工具 ——
    #  这里的脚本正文是连注释一起读进来的,而注释里正写着原来那个形状。)
    assert '[[ -z "${D1MAX_PIN_VALUE//[[:space:]]/}" ]]' in 装机脚本
    assert "exit 3" in 装机脚本
    assert (装机脚本.index("exit 3")
            < 装机脚本.index("\nsystemctl start d1max-patrol.service"))


def test_装机清单里有记PIN那一条():
    """§7.9 第 2 步「设置设备 PIN」。之前整条规格项在交付面上一个字都没有。"""
    text = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "D1MAX_PIN" in text
    assert "--host 0.0.0.0" in text          # 为什么必须有 PIN,得说清楚
    assert "登记表" in text                   # 抄到哪儿去


def test_装机脚本把填好的SN透传给activate(装机脚本):
    """``sudo`` 默认 ``env_reset``,``D1MAX_SN`` 不在 ``env_keep`` 里。

    脚本自己不读 ``/etc/d1max/env`` 的话,``release activate`` 拿到的是
    空值,``cli.py`` 的 ``resolve()`` 落到设备树/MAC 兜底;而服务侧经
    ``EnvironmentFile=`` 拿到的是人填的真值 —— 重启后自检第四项恒红,
    ``postcheck_verdict`` 判 ROLLBACK,一版好的被退回去。首次装机因
    ``src=""`` 走 ``release.stuck`` 侥幸不炸,**从第二次升级起每次必炸**。

    **读法是照 systemd 的字面量解析抄的,不是 source。** 理由见
    ``test_装机脚本绝不source那份给systemd用的env``。
    """
    assert "read_env_value D1MAX_SN" in 装机脚本            # 读进脚本自己的变量
    # 光读出来不够:sudo 那一行必须显式把它带过去。
    assert 'D1MAX_SN="${D1MAX_SN:-}"' in 装机脚本
    assert (装机脚本.index("read_env_value D1MAX_SN")
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
    ``pip install "$PKG"`` 的话,紧接着 ``release install`` 算 ``tree_sha256``
    对账当场对不上,``set -e`` 把装机中止在那一步;直接 ``pip install "$SLOT"``
    则是把已落槽的那一版写脏,以后任何一次重新校验都会判它坏掉。
    """
    assert "mktemp -d" in 装机脚本
    assert 'pip install --quiet "$PKG"' not in 装机脚本
    assert 'pip install --quiet "$SLOT"' not in 装机脚本
    # 临时目录得有人清 —— trap 保证 set -e 中途退出、Ctrl+C 也清得掉。
    assert "trap cleanup EXIT" in 装机脚本
    assert "rm -rf" in 装机脚本


def test_根目录写死不给环境变量覆盖(装机脚本, 服务单元):
    """单元里 ``/opt/d1max`` 是逐字写死的,``AppContext.release_root`` 的默认

    值也是它。允许脚本被 ``D1MAX_ROOT`` 改而单元不跟着改,装出来的是一台
    脚本和服务各说各话的机器:包落在一个根下,服务读的是另一个根。
    """
    # 断言的是**那个展开**没了,不是"这三个字不许出现" —— 脚本里那段
    # 说明为什么去掉它的注释,恰恰是这条改动最该留下的东西。
    assert "${D1MAX_ROOT" not in 装机脚本
    # **顶格整行匹配,不许用裸子串。** 变量名从 `根` 换成 `ROOT` 之后,裸的
    # "ROOT=/opt/d1max" 同时是脚本里 /etc/d1max/env 模板那行
    # "D1MAX_RELEASE_ROOT=/opt/d1max" 的子串 —— 拿子串断言的话,真把这一行
    # 赋值删掉,这条护栏照样绿。
    assert re.search(r"(?m)^ROOT=/opt/d1max$", 装机脚本), \
        "install.sh 里那行写死的 ROOT=/opt/d1max 不见了"
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


#: 「急停说过头」这条要查的,除了 ``交付文件()`` 名单还有这几份。**它们不是
#: 交付件**,但同样会有人照着它们做判断:手机 app 那份是写给做 app 的人的,
#: 内部安全说明是那句前提的出处。
#:
#: 原来这条护栏手写着 ``("鉴权与控制权.md", "手机app.md")`` 两个名字,**而这
#: 两个里只有一个在交付名单上** —— 也就是说交付面增加第三份讲急停的文档时,
#: 这条护栏不会跟着盖过去,而且一条测试都不会红(终审必修 10 第三小条)。
#: 改成「名单 + 白名单」之后,交付面那一半自己会跟着 ``交付文件()`` 走。
急停还要查的非交付件 = (
    ROOT / "docs" / "手机app.md",
    ROOT / "docs" / "内部安全说明.md",
)


def test_鉴权文档没把急停说过头():
    """"任何时候都按得下去"成立的前提是"你已经有 token"。

    名额闸(§3.6)排在签发 token **之前**:三个远端名额满了的时候,第四个人
    连凭证都换不到,``READONLY_OPEN_PATHS`` 对他没有意义。代码是合规格的,
    过头的是文档 —— 而现场会照着文档做判断。

    **覆盖面从手写的两个名字换成了 ``交付文件()`` 加白名单**,理由见
    ``急停还要查的非交付件`` 上面那段。
    """
    for 文件 in 交付文件() + 急停还要查的非交付件:
        text = 文件.read_text(encoding="utf-8")
        for m in re.finditer("任何时候都按得下去", text):
            窗 = text[max(0, m.start() - 400):m.end() + 400]
            assert "已经连上" in 窗, \
                f"{文件.name} 把急停说过头了 —— 得写明前提是「已经连上」"


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


# ------------------------------------------- fix3: 交付面终审必修 2/3/4/5
#
# 这四条的共同形状是**两份交付件对同一件事说的话对不上**,而每一份单独读都
# 挑不出毛病 —— 所以这一组护栏钉的全是「两份之间的那条线」,不是某一份的
# 内容对不对。

def test_槽内venv的幂等判据看哨兵不看解释器(装机脚本):
    """必修 2:``python3 -m venv`` 一跑完解释器就存在且可执行,

    此后 pip 装到哪一步断掉(网断、盘满、Ctrl+C),重跑都会判「已经建过了」
    把整段跳过,留下一个解释器在、依赖不全的槽。脚本接着走到 7/7
    ``systemctl restart``,ExecStart 指的正是这个解释器 —— ImportError +
    ``Restart=always`` + ``RestartSec=5`` 就是一堵日志墙。这正是脚本开头
    那句「可以重跑」的反面。

    所以判据必须是「依赖装完没有」,而**哨兵要在最后一条 pip 之后才落** ——
    落在前面的话 ``set -e`` 中途退出时它已经在盘上,门就白改了。
    """
    assert '[[ ! -x "$SLOT/venv/bin/python" ]]' not in 装机脚本, \
        "槽内 venv 的门又退回「解释器在不在」了"
    assert 'SENTINEL="$SLOT/venv/.deps-ok"' in 装机脚本
    assert '[[ ! -e "$SENTINEL" ]]' in 装机脚本
    # 半成品一律推倒重来 —— 不推的话 pip 会认为装了一半的包已经装上了。
    assert 'rm -rf "$SLOT/venv"' in 装机脚本
    # **顺序是这条护栏的全部**:哨兵落在最后一条 pip install 之后。
    落哨兵 = 装机脚本.index('> "$SENTINEL"')
    最后一条pip = 装机脚本.rindex("-m pip install")
    assert 最后一条pip < 落哨兵, "哨兵落在 pip 之前 —— 中途断了它照样在盘上"


def test_升pip不在每次重跑都要走的路上(装机脚本):
    """必修 3(a):原来 2/7 的 ``--upgrade pip`` 在 ``if`` **外面**,

    于是每一次重跑都强制联网一次 —— 而重跑正是「填完 SN 让它生效」的路子
    (见 docs/装机清单.md 二)。离线现场照着清单跑第二趟就会卡死在 2/7,
    ``set -e`` 直接中止,连 ``systemctl restart`` 都走不到,SN 永远不生效。

    查的是**缩进**:顶格那一行就是在 ``if`` 外面。
    """
    升pip行 = [ln for ln in 装机脚本.splitlines() if "--upgrade pip" in ln]
    # 今天是两行(2/7 根解释器一行、4/7 槽内 venv 一行),我数过。
    assert len(升pip行) == 2, f"升 pip 的行数变了:{升pip行}"
    for 行 in 升pip行:
        assert 行[:1].isspace(), f"这一行升 pip 顶格写,等于在 if 外面:{行}"
    # 升不上去不该中止装机 —— venv 自带的那个 pip 装得动我们的包。
    for 行 in 升pip行:
        assert 行.rstrip().endswith("\\"), f"升 pip 那一行没接住失败:{行}"


def test_离线pip参数每一处pip都带上了(装机脚本):
    """必修 3(b):清单第一节承诺离线 wheel 这条路,脚本得真有这个口子。

    **只带一半比一处都不带更坏**:现场看到的是「装到一半不动了」,而不动的
    是没带参数的那一处,最难查的那种。
    """
    assert "D1MAX_PIP_ARGS" in 装机脚本
    assert "PIP_ARGS=${D1MAX_PIP_ARGS:-}" in 装机脚本
    # **只数可执行行, 注释里提到 pip install 的不算。** 2/7 步那段解释「软链会
    # 让 pip 装进系统 site-packages」的注释里就原样写着这五个字, 它不是一条要
    # 带离线参数的命令。判据是行首第一个非空字符是不是 ``#``。
    pip行 = [ln for ln in 装机脚本.splitlines()
             if "-m pip install" in ln and not ln.lstrip().startswith("#")]
    # 今天是六条(2/7 三条、4/7 三条:各自根包一条 + W00b 三个包一条),我数过。
    assert len(pip行) == 6, f"pip install 的条数变了:{pip行}"
    for 行 in pip行:
        assert "$PIP_ARGS" in 行, f"这一条 pip 没带离线参数:{行.strip()}"
    # 清单那一侧要写清楚怎么用,否则现场不知道有这个口子。
    清单 = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "D1MAX_PIP_ARGS" in 清单
    assert "--find-links" in 清单 and "--no-index" in 清单


def test_清单和脚本对归档说的是同一句话(装机脚本):
    """必修 3:清单原来承诺「能被 ``pip install`` 认的路径/**归档**」,

    而脚本的 ``cp -a "$PKG/."``、``basename "$PKG"``、``release install "$PKG"``
    三处都只接受目录 —— 给一个 ``.tar.gz`` 在第一处就炸。照清单备料的人到
    现场会发现备的东西用不上。**两份必须一致**,所以这条护栏同时读两份。
    """
    清单 = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "归档" not in 清单.split("## 二、")[0] or "不认" in 清单, \
        "清单第一节还在承诺归档,而脚本不认"
    assert "pip install` 认的路径/归档" not in 清单
    assert ".tar.gz" in 清单                      # 明说哪种输入不支持
    assert "不收归档" in 装机脚本                   # 脚本这一侧注明同一件事


def test_装机清单说清了要跑两趟(装机脚本):
    """必修 4:``/etc/d1max/env`` 是 5/7 才写出来的,而 5/7 到 7/7 是同一次

    运行、中间不停 —— 人插不进去。清单原来把「填 ``D1MAX_SN``」排在 6/7 与
    7/7 之间,照着做的人会得到一台顶着 MAC 假 SN 的机器,正是清单自己在同
    一条勾选项里警告的那个后果。**脚本的注释早就写着「重跑这个脚本正是现场
    『填完 SN 让它生效』的路子」,而清单从头到尾没有一处说要重跑。**

    这条护栏钉的是「重跑」这件事本身 —— 免得将来又被改回一趟。
    """
    清单 = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    assert "两趟" in 清单
    一趟 = 清单.index("### 第一趟")
    填SN = 清单.index("### 两趟之间：填 SN")
    二趟 = 清单.index("### 第二趟")
    assert 一趟 < 填SN < 二趟, "两趟的顺序不对:填 SN 要夹在两趟之间"
    # 第二趟为什么管用,要写出来 —— 不写的话下一个人会以为它是多余的一步。
    assert "跳过切换" in 清单 and "无条件" in 清单
    # 脚本那一侧的两个形状是这条路子成立的前提,一起钉住。
    assert "已经是在跑的那一版了,跳过切换" in 装机脚本
    assert "\nsystemctl start d1max-patrol.service" in 装机脚本


def test_装机清单把记上装挪到服务起来之后():
    """必修 4 的另一半:``PUT /api/identity/payload`` 原来排在 6/7,

    **可服务要到 7/7 才第一次起来** —— 首装机器在 6/7 那一刻端口上没人听,
    照着敲只会得到「连不上」。
    """
    清单 = (ROOT / "docs" / "装机清单.md").read_text(encoding="utf-8")
    记上装 = 清单.index("### 第二趟之后：记上装")
    二趟 = 清单.index("### 第二趟")
    assert 二趟 < 记上装, "记上装又排到第二趟之前去了 —— 那时候端口上没人听"
    # 那条 PUT 要落在这一段里,不能还留在 6/7 那一条勾选项上。
    assert 清单.index("/api/identity/payload", 记上装) > 记上装


def test_控制权到期两份文档互相限定():
    """必修 5:两句在机制上都对 —— 一句讲**没人接管**时租约过期(任务照跑,

    只停遥控那一路并报一条 P1),一句讲**接管中**过期(狗停在原地,绝不自己
    接着走)。**但两句原来都用了无限定的全称写法**,一句还是小节标题。现场
    同时拿到这两份,谁都不知道该信哪个 —— 而这件事错一次就是「以为狗会停,
    结果它照跑」或者反过来。
    """
    鉴权 = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    值守 = (ROOT / "docs" / "值守与告警.md").read_text(encoding="utf-8")
    # 鉴权这一侧:全称写法要带上限定词,并且指得到另一份的那一节。
    for 行 in [ln for ln in 鉴权.splitlines() if "狗不会因此停下" in ln]:
        assert "未接管时" in 行, f"这一行还是无限定的全称写法:{行.strip()}"
    assert "值守与告警.md` 五、规矩三" in 鉴权
    # 值守这一侧:规矩三的标题要说明白它讲的是接管中。
    标题 = [ln for ln in 值守.splitlines() if ln.startswith("### 规矩三")]
    assert len(标题) == 1, f"规矩三的标题不止一行:{标题}"
    assert "接管中" in 标题[0], f"规矩三的标题还是全称写法:{标题[0]}"
    assert "鉴权与控制权.md" in 值守
    # 死指针:第 8 卷已经给了答案,「不是这一层的事」这句不许再留着。
    assert "逻辑那一层的事" not in 鉴权
    assert "teleop.emergency_stop" in 鉴权


# --------------------------------------- fix4: 交付面终审必修 7/8/10(三)/11
#
# 这一组的共同形状是**文档自己指着自己、或者文档跟代码里那个常量对不上**,
# 而每一句单独读都通顺 —— 所以护栏钉的是「那条指向」本身。

#: 鉴权文档的小节标题长什么样。抽的是 ``## 六、`` 里那个中文序号。
小节标题 = re.compile(r"^## ([零一二三四五六七八九十]+)、", re.M)

#: 文中「第 X 节」的说法。**连写的形式也要收** —— 「第一、二、三节」
#: 「第六、第七节」都是一句话里指着好几节,要拆开逐个核。
节引用 = re.compile(
    r"第([零一二三四五六七八九十]+(?:、第?[零一二三四五六七八九十]+)*)节")


def 抽节号(片段: str) -> set[str]:
    """把一段话里所有「第 X 节」拆成 {'六', '七'} 这样的序号集合。"""
    出 = set()
    for m in 节引用.finditer(片段):
        for 段 in m.group(1).split("、"):
            出.add(段.lstrip("第"))
    return 出


def test_鉴权文档的交叉引用都指得到真的小节():
    """必修 7:这份文档靠交叉引用导航(第零节那张表专门有一列叫「挡他的是哪
    一节」),**指错就是把人送错地方**。

    终审在这份文档里逐条核出了四处指错的(第零节那张表、「狗从不核实名字
    (第六节)」、「(第二节规则 4)」、第七节两处「上一节」)。四处都是手写的,
    今天改对了明天还会错 —— 所以这里钉一条机器能看的:**文中每一个「第 X 节」
    指的那一节,必须真的存在。**

    这条只能证伪「指向不存在的小节」,证不了「指的是不是对的那一节」;
    「指得对不对」那一半由下面 ``test_鉴权文档零节的表跟正文指着同一批小节``
    钉住那处最要命的自相矛盾。

    **带 ``.md`` 的行整行跳过** —— 那是指着**别的文档**的小节(比如
    「见 ``docs/装机清单.md`` 第三节」),拿本文档的小节表去核它是错的。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    有的小节 = set(小节标题.findall(text))
    assert len(有的小节) >= 8, f"鉴权文档的小节抽不出来了:{有的小节}"
    引用数 = 0
    for 行 in text.splitlines():
        if ".md" in 行:                     # 指着别的文档,不归这条管
            continue
        for 序号 in 抽节号(行):
            引用数 += 1
            assert 序号 in 有的小节, \
                f"鉴权与控制权.md 指着一节不存在的「第{序号}节」:{行.strip()}"
    # 别让这条变成恒真:文档里本来就有十来处交叉引用,一处都抽不到说明
    # 上面那两条正则跟文档的写法脱节了,而不是文档干净了。
    assert 引用数 >= 5, f"只抽到 {引用数} 处交叉引用,像是正则跟文档脱节了"


def test_鉴权文档零节的表跟正文指着同一批小节():
    """必修 7 里最要命的那一处:**一节之内自相矛盾。**

    第零节那张假想敌表给「同一栋楼里的人」标的是一列「挡他的是哪一节」,
    而同一节正文里又自己说了一遍「挡他的是 PIN 本身……详见第 X 节」。
    终审核出来的时候,表里写的是「第一、二、三节」,正文写的是
    「第六、第七节」—— 两句话隔了二十几行,谁都不会同时读到。

    **这条不查它们指的是哪一节**(那会把「今天这个答案」焊死,下次整份文档
    重排小节就得改测试),只查**两处指的是同一批**。表和正文各自改各自的,
    正是这种矛盾长出来的方式。
    """
    text = (ROOT / "docs" / "鉴权与控制权.md").read_text(encoding="utf-8")
    零节 = text.split("## 一、")[0]
    表行 = [行 for 行 in 零节.splitlines()
           if 行.startswith("|") and "同一栋楼里的人" in 行]
    assert len(表行) == 1, f"假想敌表里「同一栋楼里的人」那一行找不到或者不止一行:{表行}"
    段 = [p for p in 零节.split("\n\n") if "挡他的是 PIN 本身" in p]
    assert len(段) == 1, "第零节正文里「挡他的是 PIN 本身」那一段找不到或者不止一段"
    表指 = 抽节号(表行[0])
    正文指 = 抽节号(段[0])
    assert 表指, "假想敌表那一行没指任何小节了"
    assert 表指 == 正文指, (
        f"第零节自相矛盾:表里说第 {sorted(表指)} 节,同一节正文说第 {sorted(正文指)} 节")


def test_值守文档把守死人和看门狗说清了():
    """必修 8:这份文档开篇写着「名词只有两个要先说清」,而正文里另外两个

    只有我们自己知道的词从没解释过 —— 「守死人」(5.1 和第七节那张表)和
    「值守的看门狗自己死了」(P1 典型事项里)。值守员读到它们的时候,既不知道
    那是什么,也不知道该做什么。

    所以这条查的是:**两个词都在开篇那段名词里出现过,而且各自带着一句能
    照着做的话** —— 守死人得说出那 0.6 秒,看门狗得说出处置(重启服务)。
    """
    值守 = (ROOT / "docs" / "值守与告警.md").read_text(encoding="utf-8")
    开篇 = 值守.split("## 一、")[0]
    for 词 in ("守死人", "值守的看门狗"):
        assert 词 in 开篇, f"开篇的名词那一段没说清「{词}」"
    assert "0.6 秒" in 开篇, "「守死人」那句没说出它多久松手"
    assert "重启" in 开篇, "「看门狗自己死了」那句没给处置"


def test_值守文档把屏会撒谎那一条落成了现场处置():
    """必修 8 里最要紧的一条:值守屏在解析失败的时候会**主动打出**

    「未解决的告警里没有 P1」,而狗上正挂着 P1。文档诚实地承认了这件事,
    但原来整段长在「附:给接手维护的人」那一节里,处置写的是「谁将来动这一段
    要么……要么……」—— **给开发的**。值守员读完只知道「我这块屏会撒谎」,
    不知道该怎么办。

    这条查两件事:那一节现在明确标着「与现场无关,供维护者」;**而正文里
    另有一段,给的是值守员当班时能照着做的动作**。
    """
    值守 = (ROOT / "docs" / "值守与告警.md").read_text(encoding="utf-8")
    assert "## 附：以下与现场无关，供维护者" in 值守, \
        "那一节附录还没标明它不是给现场看的"
    正文 = 值守.split("## 附：")[0]
    # W00c5a 起值守屏在站点上,那句话变成 P1 下面的「没有」。
    段 = [p for p in 正文.split("\n\n") if "P1「没有」" in p]
    assert len(段) == 1, \
        "正文里没有(或者不止一段)讲「屏会主动说没有 P1」的现场处置"
    for 要有 in ("现场处置", "读于", "再问一次"):
        assert 要有 in 段[0], f"那条现场处置里没有「{要有}」,值守员照着做不了"


#: 「第 N 卷」这种分卷编号。**客户不知道第 8 卷是什么**,更不知道它已经过去
#: 了 —— 写在交付件上等于给了一句无法兑现的时间承诺。
分卷说法 = (
    re.compile(r"第 ?[0-9零一二三四五六七八九十]+ ?卷"),
    re.compile(r"(?:这一|那一|本)卷"),
)


def test_交付件里不许出现分卷编号():
    """终审必修 6 顺带那一条:交付件里所有「第 N 卷」的说法都得换掉。

    「跟排程器一起在第 8 卷落地」这种话在客户手上没有意义:他不知道第 8 卷
    是什么,也无从知道它已经过去了,读到的只是一句兑现不了的时间承诺。
    「这一卷」「本卷」同理,它们是我们内部的分期口径。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        for 模式 in 分卷说法:
            m = 模式.search(text)
            assert m is None, \
                f"{文件.name} 里还留着分卷编号:{m.group()!r}"


# ---------------------------------------------------- 必修 11:内部安全说明

#: 从交付面挪出去的那份。**它不是交付件** —— 挪出去的全部意义就在这一点上,
#: 所以「它不在名单上」本身要有一条测试钉着。
内部安全说明 = ROOT / "docs" / "内部安全说明.md"


def test_内部安全说明不在交付名单上():
    """必修 11:六份交付件单看每一份都是负责任的安全披露,**摞在一起是一份

    完整的可利用性分析**。这一版的处理是把「为什么做得到」整段挪进这份内部
    说明,交付面只留「现场要做的事」。

    **挪出去的全部意义就在「它不发到现场」这一点上。** 谁哪天顺手把它加进
    ``交付文件()``(比如为了让泄密护栏也扫它),这次切分当场白做,而且不会有
    任何别的信号 —— 所以这条护栏是反着断言的:它必须**不在**名单上。
    扫它用的是下面那条单独的用例。
    """
    assert 内部安全说明.exists(), "docs/内部安全说明.md 不在盘上"
    assert 内部安全说明 not in 交付文件(), \
        "内部安全说明被加进交付名单了 —— 那它就跟着发到每一个站点去了"


def test_内部安全说明自己也不许写口令和PIN字面量():
    """**它不是交付件,但它写的是「我们这一版有哪些已知弱点」。**

    这种东西一旦同时写上真口令、真 PIN、SSID 或者厂商协议样例,就从「已知
    弱点清单」变成了「怎么打进去的操作指南」,而这个仓里有人、有备份、有
    导出。所以交付面那几条形状护栏,这一份照样要过一遍。
    """
    text = 内部安全说明.read_text(encoding="utf-8")
    for 不该出现 in ("12345678", "XG2WIFI"):
        assert 不该出现 not in text, f"内部安全说明里不该出现 {不该出现!r}"
    for 模式 in PIN_字面量 + 口令字面量:
        assert 模式.search(text) is None, \
            f"内部安全说明里出现了口令/PIN 字面量:{模式.pattern}"
    assert VENDOR_WS_PORT.search(text) is None, \
        "内部安全说明里出现了厂商导航端口"


def test_交付件不许指向那份不发出去的文档():
    """**交付面不许写「详见随附《内部安全说明》」这类话。**

    第一版把分析挪走之后, 在交付面留了四处「详见随附《内部安全说明》」。
    那是一句会当场兑现代价的话:客户手里**没有**这份「随附」的东西 ——
    要么这份手册在骗人, 要么有人为了兑现这句话真把它发过去, 而那正是必修 11
    整个切分要防的事。两条路都通向白做。

    所以交付面的写法是:**说清楚这是本版已知限制、说清楚现场要做什么、
    然后停在那儿**, 不给读者一个可以去追的文件名。真要做安全评审的客户走
    厂家支持通道单独索取, 那是一次有人把关的分发, 不是随每一台机器出厂。
    """
    for 文件 in 交付文件():
        text = 文件.read_text(encoding="utf-8")
        assert "内部安全说明" not in text, (
            f"{文件.name} 指向了 docs/内部安全说明.md —— 那份文档不在交付名单上,"
            "客户手里没有它")


# ------------------------------------------- 必修 10 附带:PIN 位数的第三处

#: 「六位数字」「6 位数字」这类**白纸黑字**的说法。``tests/test_deploy_guards.py``
#: 把 ``install.sh`` 的生成器和泄密正则拴在了 ``PIN_位数`` 上,**第三处 ——
#: 文档里这些说法 —— 当时故意没拴**:那两份文档正在被改,「一百万种」那一段
#: 本来就要整段搬进内部安全说明,拴上去等于拦着别人做对的事。
#:
#: 现在两份文档定稿了,这条把第三处补上。
位数说法 = re.compile(r"([零一二三四五六七八九十]+|\d+) ?位数字")

#: 中文数位。只到十 —— 再多的话「位数字」这种说法本身就该换写法了。
中文数位 = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def test_文档里白纸黑字的PIN位数跟生成器是同一个数():
    """必修 10 的第三处:PIN 位数在**三处互不知情**的地方各被假设了一遍。

    ``deploy/install.sh`` 的 ``randbelow(10**6)`` 决定它是几位;
    ``六位数`` 那条泄密正则假设它是几位;**而两份交付文档和内部安全说明里
    还有白纸黑字的「六位数字」「6 位数字」**。前两处已经被
    ``tests/test_deploy_guards.py`` 的 ``PIN_位数`` 拴住了,第三处是这一条。

    谁把生成器改成八位而没改文档,文档当场变成假话,**而在这条护栏之前一条
    测试都不会红**。

    ``PIN_位数`` 从那边 import,**不在这里再抄一个 6** —— 再抄一个就又多了
    一处要同步的说法,正是这条要修的病。
    **import 写在函数里是因为两份测试文件互相 import**:那边 import 这边的
    ``交付文件()``,放到模块顶上就成环了。
    """
    from tests.test_deploy_guards import PIN_位数

    要查 = 交付文件() + (内部安全说明,)
    命中 = []
    for 文件 in 要查:
        text = 文件.read_text(encoding="utf-8")
        for m in 位数说法.finditer(text):
            原 = m.group(1)
            说的是 = 中文数位[原] if 原 in 中文数位 else int(原)
            assert 说的是 == PIN_位数, (
                f"{文件.name} 里写着「{m.group()}」,而生成器给的是 "
                f"{PIN_位数} 位 —— 这句话现在是假话")
            命中.append(文件.name)
    # **我在 diff 里数过:今天是三处**(install.sh 的 7/7 报错提示、
    # docs/装机清单.md 记 PIN 那一条的 <六位数字>、docs/鉴权与控制权.md
    # 第七节第一条),加上内部安全说明里搬过去的那一段,一共四处。
    # 下界写 3 是留了余量的:这条护栏不是在管有几处,是在拦「一处都没有了,
    # 于是它变成一条恒真的用例」。
    assert len(命中) >= 3, f"只找到 {len(命中)} 处白纸黑字的位数说法:{命中}"


def test_内部安全说明里那句一百万种也跟着位数走():
    """「一百万种」是 PIN 位数的另一种写法,而它不长「位数字」这个样子,

    上面那条正则收不到它。这一句是**六位**这个前提的直接产物:PIN 改成八位
    的那天它一样变成假话,而它恰恰是那一节整段论证的落点。
    """
    from tests.test_deploy_guards import PIN_位数

    text = 内部安全说明.read_text(encoding="utf-8")
    if "一百万种" in text:
        assert PIN_位数 == 6, (
            "内部安全说明里还写着「一百万种」,而 PIN 位数已经不是 6 了 —— "
            f"{PIN_位数} 位是 10 的 {PIN_位数} 次方种,那一段要重写")


# ---------------------------------------------------------- W01:数据根

def test_装机脚本建数据根并在停服务后起新版前把老槽的数据搬过去(装机脚本):
    """W01。目录归 robot,不然服务写不进去(只在第一次建目录时 chown,见下面
    单独那条断言)。迁移必须在服务**真的停着**的时候跑:老版本还活着就可能
    正往槽里的 queue.jsonl / runs/ 追加,搬到一半的文件会被当成已完整拷走。
    所以顺序是 stop → is-active 判着确实停了 → migrate-data → start,
    停不掉就跳过搬迁,不是 migrate → restart。"""
    assert re.search(r'mkdir -p .*"/var/lib/d1max"', 装机脚本)
    assert 'chown "$RUN_USER":"$RUN_USER" "/var/lib/d1max"' in 装机脚本
    stop = 装机脚本.index("systemctl stop d1max-patrol.service")
    is_active = 装机脚本.index("systemctl is-active --quiet d1max-patrol.service")
    mig = 装机脚本.index("release migrate-data")
    start = 装机脚本.index("systemctl start d1max-patrol.service")
    assert stop < is_active < mig < start
    assert ('\n    "$ROOT/bin/python" -m d1max_patrol.cli release migrate-data'
           in 装机脚本)
    代码 = [行 for 行 in 装机脚本.splitlines()
          if 行.strip() and not 行.lstrip().startswith("#")
          and not 行.lstrip().startswith("echo")]
    assert not any("systemctl restart d1max-patrol" in 行 for 行 in 代码)


def test_卸载脚本默认删数据根而keep_data留着():
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "# @删除 /var/lib/d1max" in text
    assert "# @保留 /var/lib/d1max" in text
    assert 'rm_sys "/var/lib/d1max"' in text
    assert "/var/lib/d1max|/var/lib/d1max/*)" in text


def test_卸载脚本keep_data时releases里还有migrated就不删():
    """搬迁时因重名没搬进数据根的那份留在槽里的 ``*.migrated`` 里 ——
    ``--keep-data`` 图的就是不丢数据,这时候把 ``releases`` 整个删掉就白留了。"""
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "*.migrated" in text
    assert text.index("*.migrated") > text.index('rm_sys "$ROOT/current"')


def _按段(服务单元: str) -> dict[str, list[str]]:
    段, 当前 = {}, None
    for 行 in 服务单元.splitlines():
        行 = 行.strip()
        if not 行 or 行.startswith("#"):
            continue
        if 行.startswith("[") and 行.endswith("]"):
            当前 = 行
            段.setdefault(当前, [])
            continue
        assert 当前 is not None, f"段头之前出现了键: {行}"
        段[当前].append(行.split("=", 1)[0])
    return 段


def test_起动次数上限那一行写在Unit段而不是Service段(服务单元):
    """W01c 真机发现:``StartLimitIntervalSec=`` 是 ``[Unit]`` 段的键。写在
    ``[Service]`` 段 systemd 只会记一句 "Unknown key name … ignoring" 然后**按
    默认的 5 次/10 秒**限起 —— 跟单元注释里承诺的「起不来就再试」正好相反,
    还顺带把注释说的那道守卫数满 MAX_BOOT_ATTEMPTS 的前提抽掉了。
    ``systemd-analyze verify`` 在本机就能复现那句 ignoring。
    """
    段 = _按段(服务单元)
    assert "StartLimitIntervalSec" in 段["[Unit]"], "要写在 [Unit] 段"
    assert "StartLimitIntervalSec" not in 段["[Service]"], "写在 [Service] 段会被 systemd 忽略"
    assert "StartLimitIntervalSec=0" in 服务单元
    # 顺手把「哪个键属于哪个段」的另外两个常错的也钉住,免得下次挪错方向。
    for 键 in ("Restart", "RestartSec", "ExecStart"):
        assert 键 in 段["[Service]"], f"{键} 是 [Service] 段的键"


# ------------------------------------------------------------ W01b:特权助手与 sudoers

def test_装机脚本把特权助手装到usr_local_sbin而不是opt_d1max_bin(装机脚本):
    """sudo 白名单指着的脚本必须 root 拥有。``/opt/d1max/bin`` 整棵归 robot,
    放那儿等于把 root 送给 robot。"""
    assert ('install -m 0755 -o root -g root "$HELPER_SRC" /usr/local/sbin/d1max-privileged'
            in 装机脚本)
    代码 = [行 for 行 in 装机脚本.splitlines() if 行.strip() and not 行.lstrip().startswith("#")]
    assert not any("d1max-privileged" in 行 and "$ROOT/bin" in 行 for 行 in 代码)


def test_sudoers先visudo校验再落盘(装机脚本):
    """一份坏 sudoers 会锁死整机的 sudo —— 现场就只剩重刷系统这一条路。"""
    校验 = 装机脚本.index('visudo -cf "$SUDOERS_SRC"')
    落盘 = 装机脚本.index('install -m 0440 -o root -g root "$SUDOERS_SRC" /etc/sudoers.d/d1max')
    assert 校验 < 落盘


def test_单元和助手优先取包内deploy(装机脚本):
    """W01b 之后 ``deploy/`` 在包里;老包没带就退回脚本自己所在的目录。"""
    assert 'UNIT_SRC="$PKG/deploy/d1max-patrol.service"' in 装机脚本
    assert 'HELPER_SRC="$PKG/deploy/d1max-privileged"' in 装机脚本
    assert 'SUDOERS_SRC="$PKG/deploy/sudoers-d1max"' in 装机脚本
    assert '$(dirname "$0")/d1max-patrol.service' in 装机脚本, "老包的退路不能删"
    assert 'install -m 0644 "$UNIT_SRC" /etc/systemd/system/' in 装机脚本


def test_卸载脚本删助手和sudoers():
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "# @删除 /usr/local/sbin/d1max-privileged" in text
    assert "# @删除 /etc/sudoers.d/d1max" in text
    assert 'rm_sys "/usr/local/sbin/d1max-privileged"' in text
    assert 'rm_sys "/etc/sudoers.d/d1max"' in text
    # rm_sys 的范围锁也要认得这两处,不然声明了也删不掉(它只会打一句「拒绝删除」)。
    assert "/usr/local/sbin/d1max-privileged) ;;" in text
    assert "/etc/sudoers.d/d1max) ;;" in text


def test_足迹勘察覆盖助手和sudoers目录():
    text = (DEPLOY / "footprint.sh").read_text(encoding="utf-8")
    assert '"/usr/local/sbin:1"' in text
    assert '"/etc/sudoers.d:1"' in text


def test_装机脚本把restart_now装成root专用且不进sudoers(装机脚本):
    """内部入口只能由 systemd-run(root)调;0700 root:root,sudoers 里没有它。"""
    assert ('install -m 0700 -o root -g root "$RESTART_NOW_SRC" /usr/local/sbin/d1max-restart-now'
            in 装机脚本)
    assert 'RESTART_NOW_SRC="$PKG/deploy/d1max-restart-now"' in 装机脚本
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "# @删除 /usr/local/sbin/d1max-restart-now" in text
    assert 'rm_sys "/usr/local/sbin/d1max-restart-now"' in text
    assert "/usr/local/sbin/d1max-restart-now) ;;" in text


# ------------------------------------------ W00b:robot-agent 装进槽、单元装而不 enable

def test_装机脚本把三个包按依赖顺序装进槽venv(装机脚本):
    行们 = 装机脚本.splitlines()

    def 行号(片段: str) -> int:
        命中 = [i for i, 行 in enumerate(行们)
              if 片段 in 行 and not 行.lstrip().startswith("#") and "[[ -d" not in 行]
        assert 命中, f"装机脚本里没有 {片段!r}"
        return 命中[0]

    i_root = 行号('pip install --quiet $PIP_ARGS "$TMP_SLOT_PKG"')
    i_c = 行号('"$TMP_SLOT_PKG/packages/contract[mqtt]"')
    i_s = 行号('"$TMP_SLOT_PKG/packages/adapter-sim"')
    i_d = 行号('"$TMP_SLOT_PKG/packages/adapter-d1max"')
    i_a = 行号('"$TMP_SLOT_PKG/packages/robot-agent"')
    assert i_root < i_c < i_s < i_d < i_a, \
        "先根包,再 contract、adapter-sim、adapter-d1max(W00d)、robot-agent"
    assert '"$TMP_PKG/packages/adapter-d1max"' in 装机脚本, "根解释器那一份也要装"
    assert "$PIP_ARGS" in 行们[i_c - 1], "三个包那一条 pip 也要带离线参数"


def test_agent单元装而不enable(装机脚本):
    """站点 broker 在 W00c 才有,现在起了也连不上。装好、留着,W00c 一到就是 systemctl enable。"""
    assert 'install -m 0644 "$AGENT_UNIT_SRC" /etc/systemd/system/' in 装机脚本
    assert 'AGENT_UNIT_SRC="$PKG/deploy/d1max-agent.service"' in 装机脚本
    代码 = [行 for 行 in 装机脚本.splitlines() if 行.strip() and not 行.lstrip().startswith("#")]
    assert not any("enable" in 行 and "d1max-agent" in 行 for 行 in 代码), "W00b 不 enable"
    assert not any("start d1max-agent" in 行 for 行 in 代码)
    assert "# @写盘 /etc/systemd/system/d1max-agent.service" in 装机脚本


def test_agent单元的形状():
    单元 = (DEPLOY / "d1max-agent.service").read_text(encoding="utf-8")
    段 = _按段(单元)
    assert "User" in 段["[Service]"] and "User=robot" in 单元
    # W00c5d 第三部分内部评审 B1:参数在这一版带的启动脚本里,单元只指着它(退回时参数跟着回来)。
    assert "\nExecStart=/opt/d1max/current/deploy/d1max-agent-start\n" in 单元
    脚本 = (DEPLOY / "d1max-agent-start").read_text(encoding="utf-8")
    for f in ("ca.crt", "robot.crt", "robot.key"):            # W00c1:站点 broker 要 mTLS
        assert f"/etc/d1max/tls/{f}" in 脚本, f
    assert "Environment=D1MAX_DATA_ROOT=/var/lib/d1max" in 单元
    assert "EnvironmentFile=-/etc/d1max/env" in 单元
    # W00c5d(决策 8):运行记录落发件箱;缺省在数据根下,env 文件可以改到临时硬盘。
    assert '--outbox "${D1MAX_OUTBOX:' in 脚本 and "--runs-root" not in 脚本
    assert 单元.index("Environment=D1MAX_OUTBOX=") < 单元.index("EnvironmentFile=")
    assert "Restart=always" in 单元 and "RestartSec=" in 单元
    assert "StartLimitIntervalSec" in 段["[Unit]"]
    assert "WantedBy" in 段["[Install]"]


def _启动脚本跑一遍(tmp_path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """假的双槽:releases/<版本>/{deploy/d1max-agent-start, venv/bin/d1max-agent(打出参数)},
    current 链指着它;从 current 那条路径起脚本(跟单元一样)。"""
    import os
    import shutil as _sh
    slot = tmp_path / "opt" / "releases" / "2026-09-25-bbbbbb"
    (slot / "deploy").mkdir(parents=True)
    (slot / "venv" / "bin").mkdir(parents=True)
    _sh.copy2(DEPLOY / "d1max-agent-start", slot / "deploy" / "d1max-agent-start")
    fake = slot / "venv" / "bin" / "d1max-agent"
    fake.write_text('#!/bin/sh\necho "$0"\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    (tmp_path / "opt" / "current").symlink_to(slot)
    return subprocess.run([str(tmp_path / "opt" / "current" / "deploy" / "d1max-agent-start")],
                          capture_output=True, text=True, timeout=10,
                          env={"PATH": os.environ["PATH"], **env})


def test_agent启动脚本_用自己那一版的代码_参数从env来(tmp_path):
    base = {"D1MAX_SITE_MQTT": "mqtts://site:8883", "D1MAX_OUTBOX": "/var/lib/d1max/outbox",
            "D1MAX_MAP": "estate:1", "D1MAX_HOME": "0,0,0"}
    got = _启动脚本跑一遍(tmp_path, base)
    assert got.returncode == 0, got.stderr
    lines = got.stdout.splitlines()
    assert lines[0].endswith("/releases/2026-09-25-bbbbbb/venv/bin/d1max-agent"), \
        "按脚本自己所在的那一版找代码,不再经过 current 链"
    args = lines[1:]
    assert args[args.index("--transport") + 1] == "mqtts://site:8883"
    assert args[args.index("--hal") + 1] == "sim", "缺省仿真:W00d 真机验收过了才改"
    assert args[args.index("--map") + 1] == "estate:1"
    (tmp_path / "opt" / "current").unlink()
    import shutil as _sh
    _sh.rmtree(tmp_path / "opt")
    more = {"D1MAX_HAL": "d1max", "D1MAX_AGENT_ARGS": "--mapping --sidecar 127.0.0.1:8090"}
    got = _启动脚本跑一遍(tmp_path, base | more)
    args = got.stdout.splitlines()[1:]
    assert args[args.index("--hal") + 1] == "d1max"
    assert args[-3:] == ["--mapping", "--sidecar", "127.0.0.1:8090"], "其余参数按空白拆开"


def test_agent启动脚本_少了站点地址说清楚_不起(tmp_path):
    got = _启动脚本跑一遍(tmp_path, {"D1MAX_OUTBOX": "/x", "D1MAX_MAP": "m:1",
                                     "D1MAX_HOME": "0,0,0"})
    assert got.returncode != 0 and "D1MAX_SITE_MQTT" in got.stderr and got.stdout == ""


def test_agent启动脚本是可执行的_仓库里记着():
    import os
    assert os.access(DEPLOY / "d1max-agent-start", os.X_OK)
    ls = subprocess.run(["git", "ls-files", "-s", "deploy/d1max-agent-start"], cwd=ROOT,
                        capture_output=True, text=True).stdout
    assert ls.startswith("100755"), "git 里要记成可执行:打包、下发之后还要能直接跑"


def test_agent单元没有注册文件就不起_不在那儿每5秒崩一次():
    单元 = (DEPLOY / "d1max-agent.service").read_text(encoding="utf-8")
    assert "ConditionPathExists" in _按段(单元)["[Unit]"]
    assert "\nConditionPathExists=/etc/d1max/registration.json\n" in 单元


WHEELS = ROOT / "dist" / "wheels-aarch64-py310"


def _轮子名(包: str) -> str:
    return re.sub(r"[-_.]+", "_", 包).lower()


def test_离线轮子覆盖三个包的外部依赖_含contract的mqtt扩展():
    """install.sh 装 ``packages/contract[mqtt]``;``--no-index`` 下 paho-mqtt 没有轮子就在 2/7 断掉,
    现场看到的是「装到一半不动了」。三个包的外部依赖(本仓的包除外)与 mqtt 扩展,每个都要有轮子,
    而且 SHA256SUMS.txt 里要有它那一行、哈希对得上。"""
    import hashlib
    try:
        import tomllib
    except ImportError:                  # 3.10:pytest 自己依赖 tomli
        import tomli as tomllib
    本仓 = {"d1max-patrol", "d1max-contract", "d1max-adapter-sim", "d1max-adapter-d1max",
          "d1max-robot-agent"}
    要的: set[str] = set()
    for 包 in ("contract", "adapter-sim", "adapter-d1max", "robot-agent"):
        proj = tomllib.loads((ROOT / "packages" / 包 / "pyproject.toml").read_text("utf-8"))
        deps = list(proj["project"].get("dependencies", []))
        if 包 == "contract":
            deps += proj["project"]["optional-dependencies"]["mqtt"]
        for d in deps:
            名 = re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0]
            if 名 not in 本仓:
                要的.add(_轮子名(名))
    assert "paho_mqtt" in 要的
    清单 = {}
    for 行 in (WHEELS / "SHA256SUMS.txt").read_text("utf-8").splitlines():
        哈希, 文件 = 行.split(" *", 1)
        清单[文件] = 哈希
    有的 = {_轮子名(w.name.split("-", 1)[0]): w for w in WHEELS.glob("*.whl")}
    for 名 in sorted(要的):
        assert 名 in 有的, f"{名} 在 {WHEELS.name} 里没有轮子"
        w = 有的[名]
        assert 清单.get(w.name) == hashlib.sha256(w.read_bytes()).hexdigest(), w.name
    assert set(清单) == {w.name for w in WHEELS.glob("*.whl")}, "SHA256SUMS 与目录不一致"


def test_agent入口在robot_agent的scripts里():
    toml = (ROOT / "packages" / "robot-agent" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'd1max-agent = "d1max_agent.main:main"' in toml


def test_卸载脚本删agent单元():
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "# @删除 /etc/systemd/system/d1max-agent.service" in text
    assert 'rm_sys "$UNIT_DIR/$AGENT_UNIT"' in text
    assert 'for u in "$MAIN_UNIT" "$AGENT_UNIT" "$OLD_UNIT"; do' in text


def test_根下的解释器venv也装三个包_不然别名壳一import就炸(装机脚本):
    """W00b 阻断项(自查):根包 import 引擎就要 ``d1max_agent``(W00b 起引擎在 robot-agent,
    W00c4 删了根包里的别名壳)。
    boot-guard、bundle guard、3/7 的 ``release install``、7/7 的 ``release activate`` /
    ``migrate-data`` 都从 ``/opt/d1max/bin/python``(2/7 建的 bin-venv)跑 —— 那里只装根包的话,
    装机在 3/7 就炸,开机守卫一次都跑不了。"""
    行们 = 装机脚本.splitlines()

    def 行号(片段: str, 起: int = 0) -> int:
        命中 = [i for i, 行 in enumerate(行们)
              if i >= 起 and 片段 in 行 and not 行.lstrip().startswith("#") and "[[ -d" not in 行]
        assert 命中, f"装机脚本里没有 {片段!r}"
        return 命中[0]

    i_root = 行号('"$ROOT/bin/python" -m pip install --quiet $PIP_ARGS "$TMP_PKG"')
    i_c = 行号('"$TMP_PKG/packages/contract[mqtt]"', i_root)
    i_s = 行号('"$TMP_PKG/packages/adapter-sim"', i_root)
    i_a = 行号('"$TMP_PKG/packages/robot-agent"', i_root)
    i_3of7 = 行号('say "3/7')
    assert i_root < i_c < i_s < i_a < i_3of7, "2/7 里根包之后、3/7 之前,把三个包装进 bin-venv"
    assert "$ROOT/bin/python" in 行们[i_c - 1], "装进的是 bin-venv,不是槽 venv"


# ------------------------------------------------------------ W00c1:站点主机的部署文件

SITE_DEPLOY = DEPLOY / "site"


def test_站点两个单元的形状():
    m = (SITE_DEPLOY / "d1max-mosquitto.service").read_text(encoding="utf-8")
    s = (SITE_DEPLOY / "d1max-site.service").read_text(encoding="utf-8")
    for u in (m, s):
        段 = _按段(u)
        assert "StartLimitIntervalSec" in 段["[Unit]"] and "ConditionPathExists" in 段["[Unit]"]
        assert "User=d1max-site" in u and "Restart=always" in u
        for k in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
                  "PrivateTmp=yes", "ReadWritePaths=/var/lib/d1max-site"):
            assert k in 段["[Service]"] or k in u, k
    assert "ExecStart=/usr/sbin/mosquitto -c /var/lib/d1max-site/broker/mosquitto.conf" in m
    assert "d1max-site --home /var/lib/d1max-site serve" in s
    assert "After=network-online.target d1max-mosquitto.service" in s


def test_站点安装脚本不碰狗_狗的安装脚本不碰站点():
    site = (SITE_DEPLOY / "install-site.sh").read_text(encoding="utf-8")
    dog = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "d1max-site" not in dog and "site-node" not in dog and "mosquitto" not in dog
    assert "d1max-agent" not in site and "robot-agent" not in site
    assert "set -euo pipefail" in site
    assert site.index("apt-get update") < site.index("apt-get install"), "新机器不 update 装不上"
    assert "enable --now d1max-mosquitto.service d1max-site.service" in site
    assert 'if [[ ! -f "$HOME_DIR/site.json" ]]' in site, "重跑不许重建 CA"
    assert 'packages/contract[mqtt]" "$PKG/packages/site-node"' in site


def test_站点安装脚本语法():
    import subprocess
    r = subprocess.run(["bash", "-n", str(SITE_DEPLOY / "install-site.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
