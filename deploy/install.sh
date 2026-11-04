#!/usr/bin/env bash
# D1 Max 巡检:装机。**可以重跑** —— 装到一半断了、参数填错了、换台机器,
# 直接再跑一遍就是。每一步都在屏幕上说人话,因为跑它的人多半不写代码。
set -euo pipefail

# ---------------------------------------------------------- 对账声明块
#
# **下面这几行是给机器读的,不是装饰。** 这份脚本往盘上写的每一处都在这里
# 记一行 @写盘,deploy/uninstall.sh 里对应地记 @删除 / @保留,
# tests/test_deploy_coexist.py 把两边对起来:
#
#   * 这里新加一处 @写盘 而 uninstall.sh 没跟上 —— 那条测试当场红;
#   * uninstall.sh 删了一处这里没记过的东西 —— 同样红。
#
# 加写盘动作的人请同时加这里的一行。**漏了就是卸载卸不干净。**
#
# @写盘 /opt/nexwar                                                       根目录(下面那条 mkdir -p 建的)
# @写盘 /opt/nexwar/releases                                              版本槽(只有代码和 venv;巡检数据在 /var/lib/nexwar)
# @写盘 /opt/nexwar/bin                                                   跟版本无关的解释器 wrapper
# @写盘 /opt/nexwar/bin-venv                                              上面那个解释器的 venv
# @写盘 /opt/nexwar/current                                               release activate 摆的版本链
# @写盘 /opt/nexwar/pending.json                                          在途升级标记,代理切版本时落在根下
# @写盘 /opt/nexwar/rolled_back.json                                      开机守卫退回上一版时留的条子,代理报给站点后删
# @写盘 /var/lib/nexwar                                                   数据根(代理的发件箱、事件簿、幂等记录),升级回滚都不碰它
# @写盘 /etc/nexwar                                                       配置目录(站点签发的证书包、注册文件也放这儿,由人拷来)
# @写盘 /etc/nexwar/env                                                   现场值:站点地址、地图、原点、适配器、SN
# @写盘 /etc/nexwar/release-pub.pem                                       发行公钥(W30;包里带了才装,验站点下发的版本)
# @写盘 /etc/nexwar/release-pub.d                                         换钥匙时另外认的发行公钥(A3;跟包里的一样)
# @写盘 /etc/systemd/system/nexwar_agent.service                           代理单元(狗上只有它一个服务,W00c5e)
# @写盘 /etc/systemd/system/multi-user.target.wants/nexwar_agent.service  systemctl enable 生成的自启链
# @写盘 /etc/systemd/system/nexwar_localizer.service                       定位器单元(W09b;env 里写了要用才 enable)
# @写盘 /etc/systemd/system/nexwar_obstacles.service                       感知节点单元(W11;env 里写了要用才 enable)
# @写盘 /etc/systemd/system/nexwar_persons.service                         人员检测节点单元(W24;只装不 enable)
# @写盘 /etc/systemd/system/nexwar_nav2.service                            Nav2 + 转接节点单元(W47;env 里写了要用才 enable)
# @写盘 /etc/systemd/system/nexwar_lidar_merge.service                     前后雷达合并单元(W09i;只装不 enable)
# @写盘 /etc/systemd/system/nexwar_sidecar.service                         运动控制旁路单元(工单 7;只装不 enable;程序在机器上编)
# @写盘 /etc/systemd/system/nexwar_zenohd.service                          自带算力板的 zenoh 路由(A5;只装不 enable;没填网关不起)
# @写盘 /etc/systemd/system/nexwar_bind.service                            配网服务(W42;只装,factory burn 才 enable;没烧写不起)
# @写盘 /etc/systemd/timesyncd.conf.d/nexwar.conf                          对时配置:向站点主机对时(W09d)
# @写盘 /etc/chrony/conf.d/nexwar.conf                                     对时归 chrony 管的狗:向站点主机对时(决策 61)
# (另:厂家 /etc/chrony/chrony.conf 里指向厂家板的 server/pool/peer 行注释掉,第一次改之前原文件留在
#  /etc/nexwar/chrony.conf.orig;用户 2026-11-03 同意,W70)
# @写盘 /usr/local/lib/nexwar                                             特权执行器和它验签用的模块(W55;root 属主,不从版本目录跑代码)
# @写盘 /etc/nexwar/unbind-pub.d                                          随发行新增的解绑公钥(W57;换钥匙用)
# @写盘 /etc/nexwar/unbind-revoked.txt                                    作废的解绑公钥编号(W57)
# @写盘 /etc/nexwar/factory-pub.pem                                       出厂公钥(W63;出厂装证书时核对用)
# @写盘 /etc/nexwar/factory-pub.d                                         换钥匙时另外认的出厂公钥(W63)
# @写盘 /etc/nexwar_ops.json                                              特权执行器的配置(W55)
# @写盘 /var/lib/nexwar_ops                                               特权执行器的请求、结果目录(W55)
# @写盘 /etc/systemd/system/nexwar_ops.path                               特权执行器的路径单元(W55)
# @写盘 /etc/systemd/system/nexwar_ops.service                            特权执行器(W55;root、一次性)
# @写盘 /etc/systemd/system/paths.target.wants/nexwar_ops.path            上面路径单元的自启链
# @写盘 /etc/systemd/journald.conf.d/nexwar.conf                          日志留盘 14 天、最多 500 MB(W55:远程取日志、重启不丢)
#
# 这份脚本不写、但 uninstall.sh 要负责收掉的(老机器上可能还在;这里只 rm/disable 它们,从来不写):
#
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_localizer.service 定位器的自启链(装机只装不 enable;有人手工 enable 过才有)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_obstacles.service 感知节点的自启链(同上)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_persons.service 人员检测节点的自启链(同上)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_nav2.service    Nav2 单元的自启链(同上)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_lidar_merge.service 前后雷达合并的自启链(同上)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_zenohd.service zenoh 路由的自启链(同上)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_bind.service   配网服务的自启链(factory burn 时 enable 的)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_sidecar.service 运动控制旁路的自启链(同上)
# @也删 /etc/systemd/system/nexwar_bootguard.service 老的守卫单元
# @也删 /etc/systemd/system/nexwar_patrol.service 老服务(W00c5e 退役)
# @也删 /etc/systemd/system/multi-user.target.wants/nexwar_patrol.service 老服务的自启链
# @也删 /usr/local/sbin/nexwar_privileged 特权助手(W00c5e 退役:代理运行时不需要 root)
# @也删 /etc/sudoers.d/nexwar 助手的 sudo 白名单
# @也删 /usr/local/sbin/nexwar_restart_now 老服务重启的内部入口
# @也删 /opt/nexwar/bundles 老服务的任务包目录(W00c5e:任务随命令从站点来,狗上不落任务包)
#
# **盘上只有以上这些。** 所有 pip install 都落在 /opt/nexwar 底下的 venv 里,
# python3 -m venv 只读系统 python3,不往系统 site-packages 里写东西。

# **写死,不给环境变量覆盖。** systemd 单元里 /opt/nexwar 是逐字写死的
# (WorkingDirectory=、Environment=NEXWAR_RELEASE_ROOT=、两条 Exec* 的路径)。允许这里
# 被 NEXWAR_ROOT 改而单元不跟着改,装出来的是一台脚本和服务各说各话的机器:
# 包落在一个根下,服务读的是另一个根。**要换根目录,这里和单元一起改。**
ROOT=/opt/nexwar
# 单元里的 User=robot 同样是写死的。留这个覆盖是给"客户那边账号不叫 robot"
# 用的,但**用了就得同时改单元里的 User=**,否则服务起来的身份跟目录属主对不上。
RUN_USER=${NEXWAR_USER:-robot}
# **只收目录,不收归档。** 下面 cp -a "$PKG/." 、basename "$PKG" 、以及
# release install "$PKG" 要算的那一遍 tree_sha256,三处都只认目录;给一个
# .tar.gz 会在第一处就炸。docs/装机清单.md 一节写的也是"只承诺目录",
# **两份必须一起改** —— 哪天真要支持归档,是先解包再走同一条路,不是
# 把这里放宽。
PKG=${1:-}
# 离线现场的透传口子。Orin NX 出不了外网时,把 aarch64 轮子拷到 U 盘上,
# 然后这样跑:
#   sudo NEXWAR_PIP_ARGS='--no-index --find-links=/media/<U盘>/wheels' \
#     bash install.sh <包目录>
# 下面每一处 pip install 都带上它 —— 只带一半的话,离线现场会在没带的
# 那一处卡住,而现场看到的是"装到一半不动了",最难查的那种。
# **展开时故意不加引号**:这个变量装的是好几个参数,加引号会被当成一个
# 带空格的长参数传给 pip。它只从运维手里来,不从网上来。
PIP_ARGS=${NEXWAR_PIP_ARGS:-}
# 包里带了离线轮子(`release pack --wheels`;站点下发的版本就是只从它装的)而运维没给 NEXWAR_PIP_ARGS:
# 只从包里的装(W35,2026-10-13 C40221:清单写的 /home/robot/wheels 在那只狗上不存在,包里其实带着)。
# 给了(哪怕给的是空的)就听给的。路径里有空白的不自动用 —— PIP_ARGS 展开时不加引号,会被拆开。
if [[ -z "${NEXWAR_PIP_ARGS+x}" && -n "$PKG" && -d "$PKG/wheels" ]]; then
  PKG_WHEELS="$(cd "$PKG/wheels" && pwd)"
  if [[ "$PKG_WHEELS" =~ [[:space:]] ]]; then
    echo "  提示: 包里带了离线轮子,但路径里有空白($PKG_WHEELS),不自动用;要用就把包挪到没有空白的路径下。"
  else
    PIP_ARGS="--no-index --find-links=$PKG_WHEELS"
    echo "包里带了离线轮子:依赖只从 $PKG_WHEELS 装(要走别处就自己给 NEXWAR_PIP_ARGS)。"
  fi
fi

# **包目录名不是随便起的。** engine/release.py 的 _NAME_RE 只认
# <日期>-<6 到 12 位十六进制>,verify_package() 还要求这个目录名跟包里
# release.json 的 name 一字不差 —— 那是包校验的第二关(第一关是整棵树的
# tree_sha256)。所以例子里**不能**带 nexwar_ 之类的前缀:带了的话 3/7 的
# release install 必拒,而那时候前两步已经白跑了。
# **要改的是这个例子和 docs/装机清单.md,不是去放宽 _NAME_RE** —— 目录名跟
# manifest 对齐是故意的,它同时也是防路径穿越的那道闸。
usage() {
  echo "用法: sudo bash install.sh <包目录>" >&2
  echo "例:   sudo bash install.sh /home/robot/2026-09-20-77b2de" >&2
  echo "      包目录名必须长成 <日期>-<6 到 12 位十六进制> 的样子,而且跟包里" >&2
  echo "      release.json 的 name 一字不差。" >&2
}

say() { printf '\n>>> %s\n' "$*"; }

# pip 会往它装的那份源目录里写东西(见 2/7 的注释),所以每次都从临时副本装。
# 两个临时目录统一在这儿清 —— 中途 set -e 退出、被 Ctrl+C 打断也清得掉。
TMP_PKG=
TMP_SLOT_PKG=
cleanup() {
  [[ -n "$TMP_PKG" ]] && rm -rf "$TMP_PKG"
  [[ -n "$TMP_SLOT_PKG" ]] && rm -rf "$TMP_SLOT_PKG"
  return 0
}
trap cleanup EXIT

if [[ -z "$PKG" ]]; then
  usage
  exit 2
fi

# **包目录不在就停在这儿,别往下走。** 原来这里只判空串,一个打错的路径会被
# 一路带到 2/7 末尾的 cp -a "$PKG/." 才炸 —— 而那时候 /opt/nexwar 已经建出来了、
# 根下那个跟版本无关的解释器也装完了。现场看到的是一条 cp 的报错,还得回头收拾
# 半个脚印(footprint.sh 的 diff 里会多出东西)。失败要早、要便宜、要说人话。
if [[ ! -d "$PKG" ]]; then
  echo "错误: 包目录不存在,或者它不是一个目录: $PKG" >&2
  echo "      **只收目录,不收 .tar.gz 之类的归档**,理由见脚本开头那段。" >&2
  usage
  exit 2
fi

# **账号不在也停在这儿。** 原来是 1/7 的 chown 撞上 chown: invalid user,而那
# 一行排在 mkdir -p 之后 —— 客户机器上没有 robot 这个账号的话,脚本会在
# /opt/nexwar 已经建出来之后中止,留半个脚印给人收,报错还是 chown 的行话。
# 这个检查排在 mkdir 之前,盘上一个字节都不会动。
# **包目录名当场核一遍,别拖到 3/7 才拒。** engine/release.py 的 _NAME_RE 只认
# <日期>-<6 到 12 位十六进制>,verify_package() 还要求这个名字跟包里 release.json
# 的 name 一字不差。名字不对的话 3/7 的 release install 必拒 —— 而那时候
# /opt/nexwar 已经建出来、根下那个跟版本无关的解释器也装完了(离线现场还为它
# 插过一次 U 盘),现场要回头收拾半个脚印。跟上面两条前置检查同一条理由:
# 失败要早、要便宜、要说人话。
# **这个正则要跟 _NAME_RE 一字不差**,放宽它不是这里的事 —— 目录名跟 manifest
# 对齐是故意的,它同时也是防路径穿越的那道闸。
# REL_NAME 在这里就算出来:2/7 的哨兵(见下面)和 4/7 的槽名都要用它。
REL_NAME=$(basename "$PKG")
if [[ ! "$REL_NAME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{6,12}$ ]]; then
  echo "错误: 包目录名不对: $REL_NAME" >&2
  echo "      必须长成 <日期>-<6 到 12 位十六进制>,例如 2026-09-20-77b2de。" >&2
  echo "      **不能带 nexwar_ 之类的前缀** —— 目录名要跟包里 release.json 的" >&2
  echo "      name 一字不差,那是包校验的第二关(第一关是整棵树的 tree_sha256)。" >&2
  echo "      包是在笔记本上用 release pack 打的,默认就是这个格式;名字不对" >&2
  echo "      多半是拷贝的时候被改过,或者手工建了个目录。" >&2
  exit 2
fi

# **包里的代理启动脚本也当场核一遍**(W00c5 外审阻断 1)。代理单元的 ExecStart 就是它;没有它的包
# (老服务那一代)7/7 的 release activate 必拒 —— 而那时候 5/7 已经把老服务清掉了,狗上什么服务都
# 没有。跟上面几条同一个理由:失败要早,盘上一个字节都还没动。执行位不在这儿查:U 盘(FAT/exFAT)
# 拷一趟就没了,3/7 落槽时会补上。
if [[ -L "$PKG/deploy/nexwar_agent_start" || ! -f "$PKG/deploy/nexwar_agent_start" ]]; then
  echo "错误: 包里没有代理的启动脚本 deploy/nexwar_agent_start(或者它是个链接): $PKG" >&2
  echo "      这个装机脚本装的是代理,代理单元直接执行包里的这个脚本;老服务那一代的包" >&2
  echo "      装不了。在笔记本上用现在的 master 重新 release pack 一个包。" >&2
  exit 2
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  echo "错误: 这台机器上没有账号 $RUN_USER,装不下去。" >&2
  echo "      systemd 单元里的 User= 也是写死 robot 的。客户那边账号不叫 robot" >&2
  echo "      的话:先把账号建出来,或者 sudo NEXWAR_USER=<账号名> bash install.sh," >&2
  echo "      **并且同时改单元里的 User=** —— 只改一边,服务的身份跟目录属主对不上。" >&2
  exit 2
fi

# 改名迁移(2026-11,d1max → nexwar):改名前装的机器先把目录、配置、单元换成新名字,下面才按新名字查、装。
# 没有老东西什么都不做;新旧都有内容时它停下(退出 2),这里跟着停。装完在最后 finish 把原来开着的单元开回来。
if [[ -f "$PKG/deploy/nexwar_migrate" ]]; then
  sh "$PKG/deploy/nexwar_migrate" prepare || exit 2
fi

# W12 外审阻断:真狗的速度换算系数是老模板的猜测值(0.4 / 1.0)的话,限速区限不住。**什么都还没改就停**,
# 让人先改 /etc/nexwar/env(sim 不拦)。代理起来时也按同一个范围拒,这里只是早点说。老包没带这个脚本就跳过。
if [[ -f "$PKG/deploy/nexwar_units_check" ]] && ! sh "$PKG/deploy/nexwar_units_check" /etc/nexwar/env; then
  echo "      没装:/etc/nexwar/env 改好了再跑一次。" >&2
  exit 2
fi

say "1/7 建目录 $ROOT 和数据根 /var/lib/nexwar"
mkdir -p "$ROOT/releases" "$ROOT/bin"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT"
# **巡检数据根,在版本槽外面。** 服务单元的 Environment=NEXWAR_DATA_ROOT 指着它,
# uninstall.sh 默认删、--keep-data 留。路径写死,理由跟 $ROOT 一样:单元里是字面量。
# **chown 只在目录是这一趟新建的时候才做。** 重跑是这份脚本的日常路径(填完
# SN 让它生效、换一版重装),这时候 /var/lib/nexwar 早就在,而且里面正躺着
# 服务自己写出来的 runs/、queue.jsonl —— 服务已经把它们的属主设成了
# $RUN_USER;这里要是无条件 chown -R 一遍,每次重跑都要把这一整棵越长越大
# 的数据树递归 chown 一遍,纯粹的浪费,而且万一现场手工改过属主(比如临时
# 用 root 抢救过一次数据)这一行会把那次手工操作悄悄抹掉。只有目录还不在、
# 也就是真的第一次建它的时候,才需要把它的属主设成服务要用的那个用户。
if [ ! -d /var/lib/nexwar ]; then
  mkdir -p "/var/lib/nexwar"
  chown "$RUN_USER":"$RUN_USER" "/var/lib/nexwar"
fi

say "2/7 装一个跟版本无关的解释器到 $ROOT/bin"
# 守卫要用它。**它不能在任何一版目录里** —— 版本坏了,救生索还得在。
# **这里放的是一个 wrapper 脚本,不是软链。改回软链就是把包装进系统 Python。**
# 原来写的是:
#   ln -sfn "$ROOT/bin-venv/bin/python" "$ROOT/bin/python"
# CPython 找 pyvenv.cfg 用的是 sys.executable **没有解析软链**的那个路径,而且
# 只看它自己那一级目录和上一级。软链落在 $ROOT/bin/ 下,这两级都没有
# pyvenv.cfg,于是解释器判定自己不在 venv 里,sys.prefix 变成 /usr —— 后面每
# 一次 "$ROOT/bin/python" -m pip install 都写进**系统 site-packages**。
# 两种机器两种死法:带 PEP 668 的(Ubuntu 23.04+)当场
# externally-managed-environment,装不下去;JetPack/Ubuntu 22.04 上没有这道闸,
# 它**不声不响地往系统 Python 里写**,装完看着一切正常。
# 而这台狗上还跑着厂商的上装,系统 Python 是共用的;uninstall.sh 的删除范围
# 够不到系统 site-packages —— 卸载卸不干净,脚本开头那句"盘上只有以上这些"
# 就成了空话。
# wrapper 是一个真的可执行文件,exec 出去之后 sys.executable 就是
# bin-venv/bin/python 自己,pyvenv.cfg 在它的上一级,venv 认得出来。
# **三处调用方都得能用**:单元里两条 ExecStartPre 的 -m、下面 5/7 生成设备
# PIN 的 -c、以及这里 pip 的 -m。exec "$@" 原样透传,三种都过得去。
#
# 老机器上装的是软链,重跑时得换掉它:-x 对"指向可执行文件的软链"也为真,
# 不显式拆一次的话,已经装过的机器永远换不成 wrapper。
if [[ -L "$ROOT/bin/python" ]]; then
  rm -f "$ROOT/bin/python"
fi
# 外审 W55-2 C8:$ROOT 归 $RUN_USER,里面的解释器、venv、pip 它都改得了 —— **root 一样都不执行**,
# 建 venv、升 pip、装依赖全以 $RUN_USER 跑。
if [[ ! -x "$ROOT/bin/python" ]]; then
  sudo -u "$RUN_USER" python3 -m venv "$ROOT/bin-venv"
  # 以 $RUN_USER 写(目录归它:root 写的话,它布个链接就能把 root 的写入带到别处去)
  sudo -u "$RUN_USER" tee "$ROOT/bin/python" >/dev/null <<解释器包装
#!/bin/sh
# 由 deploy/install.sh 生成。**不要改成软链** —— 理由见 install.sh 的 2/7 步。
exec "$ROOT/bin-venv/bin/python" "\$@"
解释器包装
  sudo -u "$RUN_USER" chmod 0755 "$ROOT/bin/python"
  # **升 pip 收在这个 if 里面,而且失败不致命。** 原来它在 if 外面,于是
  # 每一次重跑都强制联网一次 —— 而重跑正是"填完 SN 让它生效"的路子(见
  # 7/7),离线现场照着清单跑第二趟就会卡死在这一步,set -e 直接中止在
  # 2/7,连服务都不会被 restart。升不上去本来也不是中止装机的理由:venv
  # 自带的那个 pip 装得动我们的包。
  sudo -u "$RUN_USER" "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS --upgrade pip \
    || echo "  (pip 没升上去,用 venv 自带的那个接着装 —— 不影响装机)"
fi
# **从一份临时副本装,绝不直接 pip install "$PKG"。** pyproject.toml 用的是
# setuptools.build_meta,pip 对本地目录做的是就地构建:会往源目录里写
# *.egg-info/ 和 __pycache__/。而下一步 release install 要对 "$PKG" 算一遍
# tree_sha256 跟包里 release.json 记的哈希对账 —— 源目录被写脏了,哈希当场
# 对不上,verify_package 报错,set -e 把装机整个中止在这一步。
# **这一条才是真正每趟都要联网的那一条。** 上面 153-157 行那段只把升 pip
# 那一句收进了 if,而 pyproject.toml 用的是 setuptools.build_meta,
# PEP 517 的构建隔离每一次都要现拉一份 setuptools —— 于是重跑必联网。
# 而重跑正是现场"填完 SN 让它生效"的唯一路子(见 7/7):离线现场跑第二趟
# 忘了带 NEXWAR_PIP_ARGS=,就会挂死在 2/7(pip 默认 5 次重试、每次 15 秒),
# 现场看到的是"装到一半不动了",set -e 中止,连 stop/start 都不跑,SN 永远不生效。
# 所以照 4/7 那个哨兵的形状给根下的 venv 也加一个,讲究完全一样:
# **哨兵在最后一行才落**,它在就等于这个包的依赖在根 venv 里装齐了。
# 哨兵里存的是版本名:换一版,内容对不上,照样重装 —— 升级路径不受影响。
ROOT_SENTINEL="$ROOT/bin-venv/.deps-ok"
ROOT_SENTINEL_WANT="nexwar_patrol $REL_NAME"
if [[ "$(cat "$ROOT_SENTINEL" 2>/dev/null)" != "$ROOT_SENTINEL_WANT" ]]; then
  TMP_PKG=$(mktemp -d)
  cp -a "$PKG/." "$TMP_PKG/"
  chown -R "$RUN_USER":"$RUN_USER" "$TMP_PKG"
  sudo -u "$RUN_USER" "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS "$TMP_PKG"
  # W00b 起引擎在 robot-agent 里(W00c4 删了根包的别名壳),根包 import 引擎就要那三个包。boot-guard、
  # bundle guard、3/7 的 release install、7/7 的 activate/migrate-data 全从这个解释器跑,
  # 所以这里也装(顺序按依赖)。老包没带 packages/ 就跳过 —— 那一版的根包里 engine 还是真身。
  if [[ -d "$TMP_PKG/packages/robot-agent" ]]; then
    sudo -u "$RUN_USER" "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS \
      "$TMP_PKG/packages/contract[mqtt]" \
      "$TMP_PKG/packages/adapter-sim" \
      "$TMP_PKG/packages/adapter-d1max" \
      "$TMP_PKG/packages/robot-agent"
  fi
  # 外审 W55-3 N1:哨兵在机器人账号的目录里 —— 以它写(root 写会跟着它布下的链接写到别处)
  printf '%s\n' "$ROOT_SENTINEL_WANT" | sudo -u "$RUN_USER" tee "$ROOT_SENTINEL" >/dev/null
else
  echo "  根下这一版的依赖已经装齐了($REL_NAME),跳过 —— 离线重跑不需要网。"
fi

say "3/7 把包落进槽里"
# 发行公钥(W30,决策 43):包里带了就装到 /etc/nexwar(装机是人在现场、包是人拿来的,可信);
# 之后站点下发的版本,狗都拿它验签名,验不过不装。包里没带、狗上也没有:这一次照装(--unsigned),
# 但站点下发的版本都会被拒,除非 /etc/nexwar/env 里写 NEXWAR_RELEASE_UNSIGNED_OK=1(只许开发、仿真)。
mkdir -p /etc/nexwar
if [[ -f "$PKG/deploy/release-pub.pem" ]]; then
  install -m 0644 -o root -g root "$PKG/deploy/release-pub.pem" /etc/nexwar/release-pub.pem
  echo "  装了发行公钥 /etc/nexwar/release-pub.pem"
fi
# 换钥匙(A3):包里 deploy/release-pub.d/*.pem 是另外也认的公钥。狗上这个目录**跟包里的一样**:
# 包里删掉的(吊销了的)狗上也删掉。包里没有这个目录就不动狗上的。
if [[ -d "$PKG/deploy/release-pub.d" ]]; then
  install -d -m 0755 -o root -g root /etc/nexwar/release-pub.d
  find /etc/nexwar/release-pub.d -maxdepth 1 -type f -name '*.pem' -delete
  for k in "$PKG"/deploy/release-pub.d/*.pem; do
    [[ -f "$k" ]] || continue
    install -m 0644 -o root -g root "$k" /etc/nexwar/release-pub.d/
    echo "  另外也认发行公钥 $(basename "$k")"
  done
fi
UNSIGNED=()
if [[ ! -f /etc/nexwar/release-pub.pem ]]; then
  echo "  注意:没有发行公钥,这一次不验签名;站点下发的版本会被拒(见 docs/W30-完工报告.md)" >&2
  UNSIGNED=(--unsigned)
fi
sudo -u "$RUN_USER" env NEXWAR_RELEASE_ROOT="$ROOT" \
  "$ROOT/bin/python" -m nexwar_patrol.cli release install ${UNSIGNED[@]+"${UNSIGNED[@]}"} "$PKG"

say "4/7 给这一版建自己的 venv"
# **每版一个 venv,不是服务共用根下那个解释器。** 服务单元的 ExecStart
# 指着 <槽>/venv/bin/python,这个解释器只能在这儿建 —— stage() 只是
# shutil.copytree,不会替我们建。选每版一个 venv 而不是共用根解释器的
# 理由:双槽的意义之一就是回滚要把依赖也一并退回去,依赖装在共用解释器
# 里的话,回滚只退代码不退依赖,退了也是白退。
# **装的是 "$PKG" 的一份临时副本,不是 "$SLOT" 本身。** pip 就地构建会往它装的
# 那份源目录里写 *.egg-info/ 和 __pycache__/;直接装 "$SLOT" 的话,落槽那一刻
# 算过的 tree_sha256 就跟盘上的字节对不上了,以后任何一次重新校验都会判这
# 一版坏掉。装出来的东西跟装 "$SLOT" 一模一样(两者是同一份内容),而 "$SLOT"
# 保持跟落槽那一刻逐字节一致。
# REL_NAME 在最上面那条包名校验旁边就算好了(2/7 的哨兵也要用它),这里不再算一遍。
SLOT="$ROOT/releases/$REL_NAME"
# **幂等的门看哨兵,不看解释器在不在。** python3 -m venv 一跑完,
# <槽>/venv/bin/python 就存在且可执行 —— 此后 pip 装到哪一步断掉(网断、
# 盘满、Ctrl+C),重跑都会判"已经建过了"把整段跳过,留下一个解释器在、
# 依赖不全的槽。脚本接着走到 7/7 start,ExecStart 指的正是这个解释器,
# ImportError + Restart=always + RestartSec=5 就是一堵日志墙。这正是脚本
# 开头那句"可以重跑"的反面。
# 哨兵**在依赖装完之后才落**,它在,就等于这一版的依赖装齐了。
# **按槽(按版本)判仍然是对的**:每版一个 venv,换一版就该重装一遍。
SENTINEL="$SLOT/venv/.deps-ok"
if [[ ! -e "$SENTINEL" ]]; then
  # 上一趟留下的半成品一律推倒重来。不推的话 venv 里可能躺着装了一半的
  # 包,pip 会认为它已经装上了而跳过 —— 那正是我们要修的那个静默。
  rm -rf "$SLOT/venv"
  TMP_SLOT_PKG=$(mktemp -d)
  cp -a "$PKG/." "$TMP_SLOT_PKG/"
  # pip 跑在 "$RUN_USER" 身份下,临时目录默认是 root 的 0700,不改属主它读不进去。
  chown -R "$RUN_USER":"$RUN_USER" "$TMP_SLOT_PKG"
  sudo -u "$RUN_USER" python3 -m venv "$SLOT/venv"
  # 升不上去不中止,理由同 2/7。
  sudo -u "$RUN_USER" "$SLOT/venv/bin/python" -m pip install --quiet $PIP_ARGS --upgrade pip \
    || echo "  (pip 没升上去,用 venv 自带的那个接着装 —— 不影响装机)"
  sudo -u "$RUN_USER" "$SLOT/venv/bin/python" -m pip install --quiet $PIP_ARGS "$TMP_SLOT_PKG"
  # W00b:robot-agent 这几个包也装进这一版的 venv。顺序按依赖:contract → adapter-sim →
  # adapter-d1max(W00d)→ robot-agent;contract 带 [mqtt](paho-mqtt,离线轮子目录里要有它)。老包没带 packages/
  # 就跳过 —— 那一版本来也没有 agent。
  if [[ -d "$TMP_SLOT_PKG/packages/robot-agent" ]]; then
    sudo -u "$RUN_USER" "$SLOT/venv/bin/python" -m pip install --quiet $PIP_ARGS \
      "$TMP_SLOT_PKG/packages/contract[mqtt]" \
      "$TMP_SLOT_PKG/packages/adapter-sim" \
      "$TMP_SLOT_PKG/packages/adapter-d1max" \
      "$TMP_SLOT_PKG/packages/robot-agent"
  fi
  # **最后一行才落哨兵。** 上面任何一步断了,set -e 会在这之前就退出,
  # 哨兵不在,下一趟整段重来 —— 这就是"依赖装完没有"这个判据的全部。
  printf 'nexwar_patrol %s\n' "$REL_NAME" | sudo -u "$RUN_USER" tee "$SENTINEL" >/dev/null
fi

# W68:Super-LIO 是必装的核心组件(建图和巡逻定位都用它,用户 2026-11-02 定)。源码随包带,在这台机器上
# 编(第一次约 7 分钟;这一版编过就直接用)。**编不成整个装机失败**:还没切版本、没动单元,机器人照旧跑
# 原来那一版,不留半装的机器人。老包没带编译脚本就跳过(那一版也不用它)。
# 外审 r1 #7:按文件在不在判、用 bash 跑(经 FAT/exFAT 拷过会丢执行位);用机器人账号编、目录归它 ——
# 远程升级(代理,同一账号)以后也在这里编新版。包里带了 Super-LIO 源码却没编译脚本 = 包不全,也失败。
if [[ -f "$PKG/deploy/nexwar_superlio_build" ]]; then
  echo "  编 Super-LIO(定位与建图必需;第一次约 7 分钟)…"
  install -d -o "$RUN_USER" -g "$RUN_USER" /opt/nexwar/superlio
  chown -R "$RUN_USER":"$RUN_USER" /opt/nexwar/superlio
  if ! sudo -u "$RUN_USER" env NEXWAR_SUPERLIO_PREFIX=/opt/nexwar/superlio \
      NEXWAR_SUPERLIO_STAMP="$SLOT/venv/superlio-root" bash "$PKG/deploy/nexwar_superlio_build"; then
    echo "装机失败:Super-LIO(定位与建图必需)编不成,日志在 /opt/nexwar/superlio/<版本>/build.log。" >&2
    echo "         机器人照旧跑原来那一版;补齐缺的系统包后重跑本脚本。" >&2
    exit 1
  fi
elif [[ -d "$PKG/packages/localizer/native/superlio" ]]; then
  echo "装机失败:包不全 —— 带了 Super-LIO 源码却没有 deploy/nexwar_superlio_build。机器人照旧跑原来那一版。" >&2
  exit 1
fi

say "5/7 装代理单元与环境文件;清掉老服务"
# **狗上只有 nexwar_agent.service 一个服务**(W00c5e:老的 HTTP 服务、手机直连、特权助手退役)。
# 单元优先取包里的 deploy/,老包没带就退回这个脚本自己所在的目录。单元装一次不随版本换:
# 启动参数在每一版带的 deploy/nexwar_agent_start 里(W00c5d),退回上一版时参数跟着回来。
AGENT_UNIT_SRC="$PKG/deploy/nexwar_agent.service"
[[ -f "$AGENT_UNIT_SRC" ]] || AGENT_UNIT_SRC="$(dirname "$0")/nexwar_agent.service"
install -m 0644 "$AGENT_UNIT_SRC" /etc/systemd/system/
# 定位器单元(W09b):**只装、不 enable** —— 真狗在 W09b 的真机项验过之前不起。开车的仍然只有代理:
# 定位器只读雷达、经本机定位桥给代理位姿,不碰旁路进程。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_localizer.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_localizer.service" /etc/systemd/system/
fi
# 感知节点单元(W11):同样**只装、不 enable** —— 真狗在 W11 的真机项验过之前不起。它只读雷达、给代理
# 局部栅格(开了的话给旁路进程发净空许可),不发速度。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_obstacles.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_obstacles.service" /etc/systemd/system/
fi
# 人员检测节点单元(W24):同样**只装、不 enable** —— 检测模型、推理环境在真狗上装好、W24 的真机项验过之前
# 不起。它只读相机、雷达,经本机人员桥报看到的人,不发速度。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_persons.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_persons.service" /etc/systemd/system/
fi
# Nav2 单元(W47):同样**只装、不 enable** —— 真狗在 W47 的真机项验过之前不起。要 ROS 2 Humble 的 Nav2 包
# (ros-humble-navigation2,离线装)。它不直接发速度:速度经代理过禁行区闸、守卫再给 HAL。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_nav2.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_nav2.service" /etc/systemd/system/
fi
# 前后雷达合并单元(W09i):同样**只装、不 enable** —— 后雷达外参在真机上标过、W09i 的真机项验过之前不起。
# 它只读两台雷达、发合并话题,不碰代理、不碰旁路进程。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_lidar_merge.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_lidar_merge.service" /etc/systemd/system/
fi
# 运动控制旁路单元(工单 7):同样**只装、不 enable** —— 旁路跑在哪台机器上按角色定,程序要链厂商 SDK、
# 在那台机器上编(包里不带)。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_sidecar.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_sidecar.service" /etc/systemd/system/
fi
# zenoh 路由单元(商业化 A5):装在我们自己的算力板上时,从厂商 Orin(网关)拉雷达、相机话题。同样
# **只装、不 enable**;`/etc/nexwar/env` 里没填 NEXWAR_GATEWAY(装在厂商 Orin 上)它自己就不起。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_zenohd.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_zenohd.service" /etc/systemd/system/
fi
# 配网服务单元(W42):**只装**;生产线烧写(nexwar_patrol factory burn)时 enable。没烧写过(没有
# /etc/nexwar/device.json)它自己就不起。老包没带就跳过。
if [[ -f "$PKG/deploy/nexwar_bind.service" ]]; then
  install -m 0644 "$PKG/deploy/nexwar_bind.service" /etc/systemd/system/
fi
# **老服务清掉。** 两个进程抢同一个旁路进程的控制权会出事,两个服务共用在途标记还会互相数
# 开机次数、替对方提交。失败不致命:新机器上本来就没有这些。
# 顺序:先停老服务、再删 sudoers、再删助手 —— 反过来的话中间有一瞬白名单指着一个不存在
# 的路径,谁在那一刻建出同名文件谁就是 root。
systemctl disable --now nexwar_patrol.service 2>/dev/null || true
rm -f /etc/systemd/system/nexwar_patrol.service
systemctl disable --now nexwar_bootguard.service 2>/dev/null || true
rm -f /etc/systemd/system/nexwar_bootguard.service
rm -f /etc/sudoers.d/nexwar
rm -f /usr/local/sbin/nexwar_privileged /usr/local/sbin/nexwar_restart_now
# 老服务的任务包目录(狗上不再落任务包,任务随命令从站点来)。
rm -rf "$ROOT/bundles"
# 现场值的唯一来源。**已经存在就绝不覆盖** —— 现场填过的值不能被重跑抹掉。
mkdir -p /etc/nexwar
ENV_FRESH=0
if [[ ! -e /etc/nexwar/env ]]; then
  ENV_FRESH=1
  # **先按 0600 建出空文件,再往里写。** `cat >` 在 root 的 umask 022 下建出来是 0644,
  # 同机别的账号都读得到。systemd 的 EnvironmentFile= 由 PID 1(root)读,不需要放宽。
  install -m 0600 /dev/null /etc/nexwar/env
  cat > /etc/nexwar/env <<'环境模板'
# D1 Max 运行环境(代理 nexwar_agent 读它)。装机脚本只在这个文件不存在时才写一次,
# 以后重跑 install.sh 不会碰它 —— 现场填过的值放心填。

# 这只狗的机身序列号(机身标签上抄)。只收字母、数字、点、下划线、减号。
NEXWAR_SN=

# 版本根目录,一般不用改。
NEXWAR_RELEASE_ROOT=/opt/nexwar

# ---------------------------------------------------------------- 站点
# 站点 broker 的地址,站点装好之后填:
#   mqtts://<站点主机>:8883
# 狗向站点主机对时(W09d;装机脚本按这个地址写 timesyncd 的配置,填了之后再跑一遍装机脚本)。
# 时间服务器不是这台主机的话另写:
#   NEXWAR_SITE_NTP=<主机>
# 证书包(ca.crt、robot.crt、robot.key)拷到 /etc/nexwar/tls/,注册文件拷成
# /etc/nexwar/registration.json。**没有注册文件代理就不起**(单元的 ConditionPathExists)。
NEXWAR_SITE_MQTT=
# 狗上现在载着的地图 <map_id>:<version>,与这张图上的原点 x,y,yaw。站点下发过地图之后,
# 代理以站点下发的那张为准,这两行只是第一次起来时的缺省。
NEXWAR_MAP=
NEXWAR_HOME=

# ---------------------------------------------------------------- 适配器
# sim = 仿真(动不了真狗)。**W00d 真机验收(真机验证 §3b)过了,才改成 d1max**,
# 同时在 NEXWAR_AGENT_ARGS 里带上实测过的换算参数,例如:
#   NEXWAR_AGENT_ARGS=--sidecar 127.0.0.1:8090 --mps-per-unit 1.0 --radps-per-unit 1.5
# (W12:不写就是 SDK 文档低速档的 1.0 m/s、1.5 rad/s;旁路进程要 --speed-level 1,默认就是)
NEXWAR_HAL=sim
# 自主级别(W00c6i):空着就按适配器定 —— d1max 是 supervised(goto、巡检只在有人用手机
# 「我在现场监护」时才收,监护一断就停;站点的排程、事件派遣不派给它),sim 是 autonomous。
# **真狗改成 autonomous 要等 W11 避障真机验收过了、用户同意**:
#   NEXWAR_AUTONOMY=autonomous
# 地图位姿从哪来(W09a):空着 = 里程锚定(人在手机上「设位置」)。**W09b 的定位器在这台狗上验过之后**
# 才改成 bridge(定位器的 ROS 节点经本机定位桥给代理位姿):
#   NEXWAR_LOCALIZER=bridge
# 导航后端(W10):空着 = 直线桥(过渡期)。**W10 的真机项(真机验证 §3e)验过之后**才改成
# planned(在规划栅格上规划、守禁行区与限速区):
#   NEXWAR_NAV=planned
# 局部避障(W11):空着 = 不用。**W11 的真机项(§3f)验过之后**、起了 nexwar_obstacles 单元,才改成 bridge
# (要 NEXWAR_NAV=planned):
#   NEXWAR_OBSTACLES=bridge
# 人员检测(W24):空着 = 不用。检测模型放到 /etc/nexwar/person.onnx、起了 nexwar_persons 单元、
# **W24 的真机项(§3m)验过之后**才改成 bridge:
#   NEXWAR_PERSONS=bridge
# 其余参数(按空白拆开接在代理参数后面),例如建图:--mapping
NEXWAR_AGENT_ARGS=

# 发件箱(断网暂存):缺省在 /var/lib/nexwar/outbox。临时硬盘到了改到它上面,并写挂载点
# (没挂上代理就不起,不会悄悄写到系统盘上):
#   NEXWAR_OUTBOX=/mnt/<临时盘>/outbox
#   NEXWAR_OUTBOX_MOUNT=/mnt/<临时盘>
环境模板
fi
chmod 0600 /etc/nexwar/env
# **老机器的 env 是老服务那一代的模板**:没有站点地址、地图、原点、适配器这几行(模板只在文件不存在
# 时写)。不动它,但把缺的键一个个说出来 —— 不说的话,现场照着清单填的时候找不到那一行。
if [[ "$ENV_FRESH" == 0 ]]; then
  LACKING=
  for key in NEXWAR_SITE_MQTT NEXWAR_MAP NEXWAR_HOME NEXWAR_HAL; do
    grep -qE "^[[:space:]]*$key=" /etc/nexwar/env || LACKING="$LACKING $key"
  done
  if [[ -n "$LACKING" ]]; then
    echo "  提示: /etc/nexwar/env 是老模板,缺这几行:$LACKING"
    echo "        照装机清单二补上;NEXWAR_HAL 先写 sim(W00d 真机验收过了才改 d1max)。"
  fi
fi
# 老机器的 env 里可能还有老服务用的几行(设备 PIN、回传地址与密钥):**不动它们**(不改人手写的
# 配置文件),只说一声它们已经没人读了。
if grep -qE '^[[:space:]]*NEXWAR_(PIN|CONSOLE_URL|CONSOLE_TOKEN)=' /etc/nexwar/env 2>/dev/null; then
  echo "  提示: /etc/nexwar/env 里的 NEXWAR_PIN / NEXWAR_CONSOLE_URL / NEXWAR_CONSOLE_TOKEN"
  echo "        是老服务用的,已经没人读了,可以删掉(手机只连站点,W00c5e)。"
fi
systemctl daemon-reload
systemctl enable nexwar_agent.service

# W55 远程运维。① 特权执行器:代理(robot 用户)要 root 才能做的几件事(重启我们的服务、重启机器人、
# 对时、换证书、装验过签的服务单元、清日志)交给它。执行器和验签用的两个小模块拷到 root 属主的
# /usr/local/lib/nexwar/ —— 版本目录属主是 robot,root 绝不跑那里的代码。
install -d -m 0755 -o root -g root /usr/local/lib/nexwar /usr/local/lib/nexwar/nexwar_contract
install -m 0755 -o root -g root "$PKG/deploy/nexwar_ops_runner" /usr/local/lib/nexwar/ops-runner
install -m 0644 -o root -g root /dev/null /usr/local/lib/nexwar/nexwar_contract/__init__.py
for m in ed25519 relsign; do
  install -m 0644 -o root -g root "$PKG/packages/contract/src/nexwar_contract/$m.py" /usr/local/lib/nexwar/nexwar_contract/
done
# 配网服务(W42)以 root 跑:它的代码也拷进 root 独占的 /usr/local/lib/nexwar/bind/,用系统 python -I 跑
# (外审 W55-1:以前从版本目录跑,机器人账号改得了)。换版本时由特权执行器验签以后换。在跑的话重起一下。
/usr/bin/python3 /usr/local/lib/nexwar/ops-runner --install-bind "$PKG" --lib-dir /usr/local/lib/nexwar
# W57:随发行换解绑钥匙 —— 包里带的解绑公钥(换钥匙时新增的)、作废名单装进 /etc/nexwar(配网服务认它们)
if [[ -d "$PKG/deploy/unbind-pub.d" ]]; then
  install -d -m 0755 -o root -g root /etc/nexwar/unbind-pub.d
  find /etc/nexwar/unbind-pub.d -maxdepth 1 -type f -name '*.pem' -delete
  for k in "$PKG"/deploy/unbind-pub.d/*.pem; do
    [[ -f "$k" ]] && install -m 0644 -o root -g root "$k" /etc/nexwar/unbind-pub.d/
  done
fi
if [[ -f "$PKG/deploy/unbind-revoked.txt" ]]; then
  install -m 0644 -o root -g root "$PKG/deploy/unbind-revoked.txt" /etc/nexwar/unbind-revoked.txt
fi
# W63:出厂公钥(出厂装证书 factory install-cert 时核对证书是我们签的)
if [[ -f "$PKG/deploy/factory-pub.pem" ]]; then
  install -m 0644 -o root -g root "$PKG/deploy/factory-pub.pem" /etc/nexwar/factory-pub.pem
fi
if [[ -d "$PKG/deploy/factory-pub.d" ]]; then
  install -d -m 0755 -o root -g root /etc/nexwar/factory-pub.d
  find /etc/nexwar/factory-pub.d -maxdepth 1 -type f -name '*.pem' -delete
  for k in "$PKG"/deploy/factory-pub.d/*.pem; do
    [[ -f "$k" ]] && install -m 0644 -o root -g root "$k" /etc/nexwar/factory-pub.d/
  done
fi
systemctl daemon-reload
systemctl try-restart nexwar_bind.service || true
install -d -m 0755 -o root -g root /var/lib/nexwar_ops
install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" /var/lib/nexwar_ops/req
install -d -m 0750 -o root -g "$RUN_USER" /var/lib/nexwar_ops/done
cat > /etc/nexwar_ops.json <<OPS
{"profile": "robot", "requester": "$RUN_USER", "spool": "/var/lib/nexwar_ops",
 "data_root": "/var/lib/nexwar", "release_root": "$ROOT", "pubkey": "/etc/nexwar/release-pub.pem"}
OPS
chmod 0644 /etc/nexwar_ops.json
install -m 0644 "$PKG/deploy/nexwar_ops.path" "$PKG/deploy/nexwar_ops.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nexwar_ops.path
# ② 日志留在盘上(重启不丢)、留 14 天、最多 500 MB;代理的账号进 systemd-journal 组,站点远程能取日志。
install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/nexwar.conf <<'JOURNAL'
# D1 Max(W55 远程运维):日志留盘,重启不丢;远程取日志、诊断包要用
[Journal]
Storage=persistent
SystemMaxUse=500M
MaxRetentionSec=14day
JOURNAL
install -d -m 2755 -o root -g systemd-journal /var/log/journal 2>/dev/null || install -d -m 2755 /var/log/journal
systemctl restart systemd-journald || true
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -aG systemd-journal "$RUN_USER"
fi
# ③ VPN 工具(站点 VPN:4G 上连回站点、工程师远程 SSH)。离线装不上只提示,局域网照常。
if ! command -v wg >/dev/null 2>&1; then
  apt-get install -y wireguard-tools >/dev/null 2>&1 \
    || echo "  提示: 没装上 wireguard-tools(离线?):站点 VPN 用不了,局域网照常。离线装法见装机清单「一」" >&2
fi

# 运行期要的系统件(决策 47:只查、只提示,不替厂家的系统装东西、改账号)。C40221 上三样都缺
# (2026-10-08):没有 ffmpeg 画面和录像都没有;人员检测要系统 Python 里的 onnxruntime;
# 继电器板的串口属 dialout 组,代理的账号不在组里就打不开。
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "  !! 没有 ffmpeg:画面(W00c5b)和连续录像(W18)都没有。离线装法见装机清单「一」" >&2
# 先收进变量再判:pipefail 下 grep -q 早退会让 ffmpeg 挨 SIGPIPE,整条管道算失败、误报。
elif [[ " $(ffmpeg -hide_banner -protocols 2>/dev/null | tr -s ' \n' ' ') " != *" srt "* ]]; then
  echo "  !! ffmpeg 不带 srt:画面推不到站点(录像不受影响)。装法见装机清单「一」" >&2
fi
if ! python3 -c 'import onnxruntime' >/dev/null 2>&1; then
  echo "  提示: 系统 Python 里没有 onnxruntime,人员检测节点(W24)起不来;不开人员检测可以不管" >&2
  # 工单 6(2026-10-28):只说「没有」现场要现查装法。只提示、不替系统装(决策 47)
  ort=""
  if [[ -n "${PKG_WHEELS:-}" ]]; then
    ort="$(ls "$PKG_WHEELS"/onnxruntime_gpu-*.whl 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -n "$ort" ]]; then
    echo "        包里带了 $(basename "$ort"),要开人员检测就装(不带依赖、不动系统的 numpy):" >&2
    pipc="python3 -m pip"                   # 拆开写:只是给人照抄的命令,脚本自己不装
    echo "        sudo $pipc install --no-deps --no-index $ort" >&2
  else
    echo "        装法见装机清单「一」onnxruntime 那条(Jetson 版 onnxruntime-gpu 离线轮子)" >&2
  fi
fi
if [[ " $(id -nG "$RUN_USER" 2>/dev/null) " != *" dialout "* ]]; then
  echo "  提示: $RUN_USER 不在 dialout 组,上装继电器板的串口打不开(W21);接了上装再:" >&2
  echo "        sudo usermod -aG dialout $RUN_USER && sudo systemctl restart nexwar_agent" >&2
fi

say "6/7 看一眼站点签发的证书包与注册文件"
if [[ -f /etc/nexwar/registration.json && -f /etc/nexwar/tls/ca.crt \
      && -f /etc/nexwar/tls/robot.crt && -f /etc/nexwar/tls/robot.key ]]; then
  echo "  都在:/etc/nexwar/registration.json、/etc/nexwar/tls/{ca.crt,robot.crt,robot.key}。"
else
  cat <<'提示'
  还没有(没有注册文件代理就不起,不会刷日志)。先在站点主机上给这只狗登记(enroll,
  见装机清单一),把给出的证书包拷到这台狗的 /etc/nexwar/tls/,注册文件拷成 /etc/nexwar/registration.json,
  在 /etc/nexwar/env 里填好 NEXWAR_SITE_MQTT、NEXWAR_MAP、NEXWAR_HOME,再:
    sudo systemctl start nexwar_agent
提示
fi
# W30b(决策 51):站点的证据公钥(证书包里的 evidence-pub.key)。没有照样起,照片、录像明文存,站点报 P2。
if [[ ! -f /etc/nexwar/evidence-pub.key ]]; then
  echo "  提示: 没有 /etc/nexwar/evidence-pub.key —— 照片、截图、录像会明文存在狗上(站点报 P2)。" >&2
  echo "        从站点证书包拷过来、重启代理就封着存了(狗自己解不开,站点收齐了解开)。" >&2
fi

# 对时(W09d):狗向站点主机对时(Orin 的钟不准、没有 NTP;钟不对连 mTLS 都过不了 —— 站点证书「还没生效」)。
# 配置按 /etc/nexwar/env 算(站点主机:NEXWAR_SITE_NTP,没有从 NEXWAR_SITE_MQTT 取);站点地址还没填就先不配,
# 填了再跑一遍这个脚本。用系统自带的 systemd-timesyncd,不另装包(狗上离线)。
TIMESYNC_CONF=/etc/systemd/timesyncd.conf.d/nexwar.conf
CHRONY_CONF=/etc/chrony/conf.d/nexwar.conf
# 厂家那份主配置:**只读不写**(看它在不在、读不读 conf.d)。
CHRONY_DROPIN_DIR=$(dirname "$CHRONY_CONF")
CHRONY_MAIN="$(dirname "$CHRONY_DROPIN_DIR")/chrony.conf"
# 只看单元在不在不够:C40221 上 systemd-timesyncd 的包卸了、单元 masked,`systemctl cat` 照样成功,
# 结果写了一份不生效的配置(2026-10-08)。单元要真能载入(LoadState=loaded)才算有。
if [[ "$(systemctl show -p LoadState --value systemd-timesyncd 2>/dev/null)" != loaded ]]; then
  # 装的是 chrony / ntp 的话 timedatectl 管的是它们,这份配置不生效(W09d 内审):说清楚,不假装配上了。
  rm -f "$TIMESYNC_CONF"
  if [[ -f "$CHRONY_MAIN" ]]; then
    # W70(用户 2026-11-03 同意「有站点时站点说了算」):厂家板(上装电脑)的钟不准 —— C40221 上慢 28 s,
    # chrony 跟它对、站点往回拨,一分钟拉锯一次。主配置里指向它的 server/pool/peer 行注释掉;第一次改之前
    # 原文件留一份,卸载时放回去。厂家板地址按 env 的 NEXWAR_VENDOR_TIME_HOSTS(逗号分隔),默认 .19。
    chrony_changed=0
    vendor_hosts=$(sed -n 's/^NEXWAR_VENDOR_TIME_HOSTS=//p' /etc/nexwar/env 2>/dev/null | tail -1)
    vendor_hosts=${vendor_hosts:-192.168.168.19}
    for vh in ${vendor_hosts//,/ }; do
      vre=${vh//./\\.}
      if grep -Eq "^[[:space:]]*(server|pool|peer)[[:space:]]+${vre}([[:space:]]|\$)" "$CHRONY_MAIN"; then
        [[ -e /etc/nexwar/chrony.conf.orig ]] || cp -p "$CHRONY_MAIN" /etc/nexwar/chrony.conf.orig
        sed -i -E "s/^([[:space:]]*(server|pool|peer)[[:space:]]+${vre}([[:space:]]|\$))/# nexwar W70 厂家板的钟不准,不跟它对时: \1/" "$CHRONY_MAIN"
        chrony_changed=1
        echo "  对时:机器人的 chrony 不再跟厂家板 $vh 对时(原文件在 /etc/nexwar/chrony.conf.orig)"
      fi
    done
    # 对时归厂家的 chrony 管(决策 61,2026-10-13,改掉决策 47 的「不动」):厂家配的源是 RTK 秒脉冲(没天线
    # 就没信号)和上装电脑(C40221 上慢 15 s;裸机上没有这台),狗的钟一直不准,站点按钟差拒发命令;整机
    # 断电再开是 1970 年。加**一个单独的片段**把站点主机设成首选、可信的源;厂家那份 chrony.conf 不改,
    # 删掉这个片段、重启 chrony 就回到厂家的配置。
    if ts_conf=$(bash "$PKG/deploy/nexwar_timesync_conf" /etc/nexwar/env); then
      ts_host=$(sed -n 's/^NTP=//p' <<<"$ts_conf")
      install -d -m 0755 "$CHRONY_DROPIN_DIR"
      {
        echo "# D1 Max(决策 61):狗向站点主机对时。删掉这个文件、重启 chrony 就回到厂家的配置。"
        echo "# prefer trust:别的源(上装电脑、没信号的秒脉冲)跟它对不上时听它的。"
        echo "server $ts_host iburst prefer trust"
        echo "# 差过 1 s 直接拨(头 3 次):断电再开是 1970 年,慢慢调要调几年。"
        echo "makestep 1.0 3"
      } > "$CHRONY_CONF.tmp"
      chmod 0644 "$CHRONY_CONF.tmp"
      mv -f "$CHRONY_CONF.tmp" "$CHRONY_CONF"
      # 主配置读不读我们的片段:**按整个参数认**(W35 外审 M1:原来只比路径前缀,
      # `confdir …/conf.d-disabled` 也算读了,片段没载入却说「向站点对时」)。认这两种写法:
      #   confdir <目录>…      其中一个参数正好是片段的目录(chrony 4 起才有 confdir)
      #   include <通配>       通配正好盖住我们这个文件(`…/conf.d/*.conf`、点名这个文件)
      chrony_loads_ours() {
        local kind arg rest
        while read -r kind rest; do
          case "$kind" in
            confdir)
              for arg in $rest; do
                [[ "${arg%/}" == "$CHRONY_DROPIN_DIR" ]] && return 0
              done ;;
            include)
              arg=${rest%%[[:space:]]*}
              # shellcheck disable=SC2053  # 右边故意不加引号:它是主配置里写的通配
              [[ "$(dirname "$arg")" == "$CHRONY_DROPIN_DIR" && "$CHRONY_CONF" == $arg ]] \
                && return 0 ;;
          esac
        done < "$CHRONY_MAIN"
        return 1
      }
      if chrony_loads_ours; then
        if systemctl restart chrony; then
          echo "  对时:chrony 的片段写好、主配置会读它、chrony 重启了,向 $ts_host 对时。"
          echo "       **有没有真对上要看** chronyc sources(站点那一行带 ^*);脚本没法替你确认。"
        else
          echo "  !! chrony 重启不了,对时没配上" >&2
        fi
      else
        [[ $chrony_changed == 1 ]] && { systemctl try-restart chrony >/dev/null 2>&1 || true; }
        echo "  !! 对时片段写好了($CHRONY_CONF),但厂家的 $CHRONY_MAIN 不读 conf.d:" >&2
        echo "     我们不替它加这一行。要生效,在它末尾加一行再 sudo systemctl restart chrony:" >&2
        echo "       chrony 4 及以上(chronyd --version 看):  confdir $CHRONY_DROPIN_DIR" >&2
        echo "       chrony 3.x(没有 confdir):              include $CHRONY_CONF" >&2
        echo "     这两行没在这只狗上验过(真机项 3f.20):加完看 chronyc sources 里有没有站点那一行。" >&2
      fi
    else
      rm -f "$CHRONY_CONF"
      systemctl try-restart chrony >/dev/null 2>&1 || true
      echo "  对时:先不配站点(原因见上一行)"
    fi
  else
    echo "  !! 这台机器的对时不归 systemd-timesyncd 管(没装或 masked),也没有 $CHRONY_MAIN," >&2
    echo "     我们不配。钟差站点会报(真机项 3d.28)" >&2
  fi
elif ts_conf=$(bash "$PKG/deploy/nexwar_timesync_conf" /etc/nexwar/env); then
  install -d -m 0755 "$(dirname "$TIMESYNC_CONF")"
  printf '%s\n' "$ts_conf" > "$TIMESYNC_CONF.tmp"
  chmod 0644 "$TIMESYNC_CONF.tmp"
  mv -f "$TIMESYNC_CONF.tmp" "$TIMESYNC_CONF"
  timedatectl set-ntp true || echo "  !! timedatectl set-ntp 没成,对时可能没开" >&2
  if systemctl restart systemd-timesyncd; then
    echo "  对时:向 $(sed -n 's/^NTP=//p' <<<"$ts_conf") 对时(timedatectl timesync-status 看)"
  else
    echo "  !! systemd-timesyncd 重启不了,对时没配上" >&2
  fi
else
  # 站点地址清空了、改得不干净:不留指着旧主机的配置(W09d 内审)
  rm -f "$TIMESYNC_CONF"
  systemctl try-restart systemd-timesyncd >/dev/null 2>&1 || true
  echo "  对时:先不配(原因见上一行)"
fi

say "7/7 切到刚装的这一版并起代理"
# **把 /etc/nexwar/env 里的 NEXWAR_SN 读出来(不 source)。** sudo 默认 env_reset,下面
# release activate 拿不到它;而这份文件是 systemd 的 EnvironmentFile= 在读,systemd 按
# 字面量解析(等号右边就是值,不分词、不做命令替换、分号不是分隔符)。`. /etc/nexwar/env`
# 让 bash 求值同一份文件,规矩不是一回事 —— `NEXWAR_SN=a;reboot` 这种人手敲得出来的行,
# bash 会以 root 把 reboot 跑了。所以只读要用的键,读法对着 systemd 的规矩:只认行首空白
# 之后紧跟 `键=`,同一个键写两遍取最后一次,值两边的空白剥掉。带引号的值不还原,被下面
# 的白名单挡掉,让人改干净再来。
read_env_value() {
  sed -n "s/^[[:space:]]*$1=//p" /etc/nexwar/env 2>/dev/null \
    | tail -n 1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
NEXWAR_SN=
if [[ -f /etc/nexwar/env ]]; then
  NEXWAR_SN=$(read_env_value NEXWAR_SN)
fi
# **SN 的字符白名单,不干净就停下来喊。** 带空格、引号、分号的值说明这一行是手敲歪的,
# systemd 和这里读出来的多半已经不是同一个东西。
if [[ -n "$NEXWAR_SN" && ! "$NEXWAR_SN" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "错误: /etc/nexwar/env 里的 NEXWAR_SN 不干净: [$NEXWAR_SN]" >&2
  echo "      只收字母、数字、点、下划线、减号 —— 空格、引号、分号、中文都不行。" >&2
  echo "      照着机身标签重新填一遍那一行(等号两边都不要加引号),再重跑这个脚本。" >&2
  echo "      **没有 stop/start**,现在跑着的还是上一版(如果有的话)。" >&2
  exit 3
fi
if [[ -z "${NEXWAR_SN//[[:space:]]/}" ]]; then
  echo "警告: /etc/nexwar/env 里的 NEXWAR_SN 还没填(照着机身标签填上,再重跑一次)。" >&2
fi
# **链不在就是空串**:readlink -f 在 current 还不存在时什么也不输出,外面套 basename 会得到
# 字面量 "current"。没有链就是空,跟任何 REL_NAME 都不相等,该 activate 就 activate。
CURRENT_REL=
if [[ -e "$ROOT/current" || -L "$ROOT/current" ]]; then
  CURRENT_REL=$(basename "$(readlink -f "$ROOT/current")")
fi
if [[ "$CURRENT_REL" == "$REL_NAME" ]]; then
  echo "  $REL_NAME 已经是在跑的那一版了,跳过切换。"
else
  # NEXWAR_SN 必须显式带过去 —— sudo env_reset 会把上面读出来的它扔掉。
  sudo -u "$RUN_USER" env NEXWAR_RELEASE_ROOT="$ROOT" NEXWAR_SN="${NEXWAR_SN:-}" \
    "$ROOT/bin/python" -m nexwar_patrol.cli release activate "$REL_NAME"
fi
# **先停代理,再搬数据,再起新版。** W01 之前数据落在槽里,新版读的是 /var/lib/nexwar。搬的时候
# 谁都不能正往槽里写,所以是 stop → migrate → start,**而且真没停掉就不搬**。可重跑:搬过的源
# 改名 *.migrated,第二次什么都不做。搬失败不拦装机 —— 数据还在槽里没丢,prune 见到槽里有数据
# 也不会删。stop 加 '|| true':第一次装机服务还没起过。
systemctl stop nexwar_agent.service || true
if systemctl is-active --quiet nexwar_agent.service; then
  echo "  !! 代理停不掉,跳过数据迁移(搬的前提是没人在往槽里写)。装完后手工:stop 代理,再跑 release migrate-data" >&2
else
  sudo -u "$RUN_USER" env NEXWAR_RELEASE_ROOT="$ROOT" NEXWAR_DATA_ROOT=/var/lib/nexwar \
    "$ROOT/bin/python" -m nexwar_patrol.cli release migrate-data \
    || echo "  !! 槽内数据迁移没成功。没搬动的那几样还在原槽里、原名不变,prune 不会删它们;已经改名成 *.migrated 的不受保护。先 ls /opt/nexwar/releases/*/ 看还有哪些原名的,再 stop 代理、手工跑 release migrate-data" >&2
fi
# 没有注册文件的话 systemd 按 ConditionPathExists 跳过,不算失败。**有注册文件、但站点地址、
# 地图、原点三样有一样空着就先不起**:启动脚本见到空值就退出,Restart=always + RestartSec=5 会把它
# 刷成每 5 秒一条日志的启动循环 —— 停下来说清楚要填什么,比留一堵日志墙好。
MISSING=
for key in NEXWAR_SITE_MQTT NEXWAR_MAP NEXWAR_HOME; do
  if [[ -z "$(read_env_value "$key")" ]]; then
    MISSING="$MISSING $key"
  fi
done
# 证书包要**代理的账号读得到**(代理以 $RUN_USER 跑,自己读私钥):root 拷进来的 0600 文件它读不了,
# 一样是每 5 秒崩一次(W00c5e 内部评审)。
for f in /etc/nexwar/registration.json /etc/nexwar/tls/ca.crt /etc/nexwar/tls/robot.crt \
         /etc/nexwar/tls/robot.key; do
  if [[ -f /etc/nexwar/registration.json ]] && ! sudo -u "$RUN_USER" test -r "$f"; then
    MISSING="$MISSING $f(不在,或者 $RUN_USER 读不了)"
  fi
done
if [[ -f /etc/nexwar/registration.json && -n "$MISSING" ]]; then
  echo "  代理先不起,这几样还不行:$MISSING" >&2
  # 外审 W55-2 C5:只把**文件**交给 $RUN_USER 读,目录(/etc/nexwar/tls)留 root —— 目录交出去,
  # 它就能在里面换目录项,root 的换证书会被它布下的链接带偏
  echo "  站点值填进 /etc/nexwar/env;证书包 sudo chown $RUN_USER:$RUN_USER /etc/nexwar/tls/* \\" >&2
  echo "  /etc/nexwar/registration.json(只改文件,目录别改)。好了之后:sudo systemctl start nexwar_agent。" >&2
else
  systemctl start nexwar_agent.service
fi
# 定位器(W09b)只装不 enable;有人手工起过(真机验收时)的话,换了版本要跟着重启,不然还跑着旧代码。
# try-restart:没在跑就什么也不做。
systemctl try-restart nexwar_localizer.service 2>/dev/null || true
systemctl try-restart nexwar_obstacles.service 2>/dev/null || true
systemctl try-restart nexwar_persons.service 2>/dev/null || true
systemctl try-restart nexwar_nav2.service 2>/dev/null || true
systemctl try-restart nexwar_lidar_merge.service 2>/dev/null || true
systemctl try-restart nexwar_zenohd.service 2>/dev/null || true
# env 里写了要用的(W70,用户 11-03 现场:W68 起定位器必用,重启后几个桥都没起、换图白等 120 s):
# 装机就设开机自启并起来;env 里没写的照旧只装不 enable(真机验过才起用,做法写在各自单元文件开头)。
for pair in NEXWAR_LOCALIZER=bridge:nexwar_localizer NEXWAR_OBSTACLES=bridge:nexwar_obstacles \
            NEXWAR_PERSONS=bridge:nexwar_persons NEXWAR_NAV=nav2:nexwar_nav2; do
  want=${pair%%:*}
  unit=${pair##*:}
  if grep -qE "^[[:space:]]*${want}[[:space:]]*$" /etc/nexwar/env 2>/dev/null \
      && [[ -f "/etc/systemd/system/$unit.service" ]]; then
    if systemctl enable --now "$unit.service"; then
      echo "  /etc/nexwar/env 里写了 $want:$unit 设了开机自启并已起来"
    else
      echo "  注意: /etc/nexwar/env 里写了 $want,但 $unit 设开机自启或起来没成:" \
           "journalctl -u $unit 看原因" >&2
    fi
  fi
done

# 前后雷达合并(W09i)只给 MOLA 用;W68 起定位器是 Super-LIO,没人订 /nexwar/merged_lidar。以前手工
# enable 过的,装机时关掉(W70,用户 11-03 现场:白占两核多)。env 里还有人订它就不动 ——
# NEXWAR_LOC_ARGS 里的 --lidar-topic 只有 --engine mola 才用(Super-LIO 只认前雷达,那一行是 MOLA 时代留下的)。
MERGE_USED=0
if grep -E '^[^#]*merged_lidar' /etc/nexwar/env 2>/dev/null \
    | grep -vqE '^[[:space:]]*NEXWAR_LOC_ARGS='; then MERGE_USED=1; fi
if grep -E '^[[:space:]]*NEXWAR_LOC_ARGS=.*merged_lidar' /etc/nexwar/env 2>/dev/null \
    | grep -qE -- '--engine[= ]mola'; then MERGE_USED=1; fi
if systemctl is-enabled --quiet nexwar_lidar_merge.service 2>/dev/null && [[ $MERGE_USED == 0 ]]; then
  systemctl disable --now nexwar_lidar_merge.service || true
  echo "  没人用前后雷达合并(定位器是 Super-LIO):nexwar_lidar_merge 关了,不再开机自启"
fi

if [[ -f "$PKG/deploy/nexwar_migrate" ]]; then
  sh "$PKG/deploy/nexwar_migrate" finish || true
fi

say "装完了。看一眼:"
echo "  systemctl status nexwar_agent"
echo "  journalctl -u nexwar_agent -f"
echo ""
echo "  连上站点之后,手机(站点模式)的狗列表里看得到这只狗;能力里报的是 NEXWAR_HAL 那个适配器。"
