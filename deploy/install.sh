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
# @写盘 /opt/d1max                                                       根目录(下面那条 mkdir -p 建的)
# @写盘 /opt/d1max/releases                                              版本槽(只有代码和 venv;巡检数据在 /var/lib/d1max)
# @写盘 /opt/d1max/bin                                                   跟版本无关的解释器 wrapper
# @写盘 /opt/d1max/bin-venv                                              上面那个解释器的 venv
# @写盘 /opt/d1max/current                                               release activate 摆的版本链
# @写盘 /opt/d1max/pending.json                                          在途升级标记,代理切版本时落在根下
# @写盘 /opt/d1max/rolled_back.json                                      开机守卫退回上一版时留的条子,代理报给站点后删
# @写盘 /var/lib/d1max                                                   数据根(代理的发件箱、事件簿、幂等记录),升级回滚都不碰它
# @写盘 /etc/d1max                                                       配置目录(站点签发的证书包、注册文件也放这儿,由人拷来)
# @写盘 /etc/d1max/env                                                   现场值:站点地址、地图、原点、适配器、SN
# @写盘 /etc/systemd/system/d1max-agent.service                           代理单元(狗上只有它一个服务,W00c5e)
# @写盘 /etc/systemd/system/multi-user.target.wants/d1max-agent.service  systemctl enable 生成的自启链
#
# 这份脚本不写、但 uninstall.sh 要负责收掉的(老机器上可能还在;这里只 rm/disable 它们,从来不写):
#
# @也删 /etc/systemd/system/d1max-bootguard.service 老的守卫单元
# @也删 /etc/systemd/system/d1max-patrol.service 老服务(W00c5e 退役)
# @也删 /etc/systemd/system/multi-user.target.wants/d1max-patrol.service 老服务的自启链
# @也删 /usr/local/sbin/d1max-privileged 特权助手(W00c5e 退役:代理运行时不需要 root)
# @也删 /etc/sudoers.d/d1max 助手的 sudo 白名单
# @也删 /usr/local/sbin/d1max-restart-now 老服务重启的内部入口
# @也删 /opt/d1max/bundles 老服务的任务包目录(W00c5e:任务随命令从站点来,狗上不落任务包)
#
# **盘上只有以上这些。** 所有 pip install 都落在 /opt/d1max 底下的 venv 里,
# python3 -m venv 只读系统 python3,不往系统 site-packages 里写东西。

# **写死,不给环境变量覆盖。** systemd 单元里 /opt/d1max 是逐字写死的
# (WorkingDirectory=、Environment=D1MAX_RELEASE_ROOT=、两条 Exec* 的路径)。允许这里
# 被 D1MAX_ROOT 改而单元不跟着改,装出来的是一台脚本和服务各说各话的机器:
# 包落在一个根下,服务读的是另一个根。**要换根目录,这里和单元一起改。**
ROOT=/opt/d1max
# 单元里的 User=robot 同样是写死的。留这个覆盖是给"客户那边账号不叫 robot"
# 用的,但**用了就得同时改单元里的 User=**,否则服务起来的身份跟目录属主对不上。
RUN_USER=${D1MAX_USER:-robot}
# **只收目录,不收归档。** 下面 cp -a "$PKG/." 、basename "$PKG" 、以及
# release install "$PKG" 要算的那一遍 tree_sha256,三处都只认目录;给一个
# .tar.gz 会在第一处就炸。docs/装机清单.md 一节写的也是"只承诺目录",
# **两份必须一起改** —— 哪天真要支持归档,是先解包再走同一条路,不是
# 把这里放宽。
PKG=${1:-}
# 离线现场的透传口子。Orin NX 出不了外网时,把 aarch64 轮子拷到 U 盘上,
# 然后这样跑:
#   sudo D1MAX_PIP_ARGS='--no-index --find-links=/media/<U盘>/wheels' \
#     bash install.sh <包目录>
# 下面每一处 pip install 都带上它 —— 只带一半的话,离线现场会在没带的
# 那一处卡住,而现场看到的是"装到一半不动了",最难查的那种。
# **展开时故意不加引号**:这个变量装的是好几个参数,加引号会被当成一个
# 带空格的长参数传给 pip。它只从运维手里来,不从网上来。
PIP_ARGS=${D1MAX_PIP_ARGS:-}

# **包目录名不是随便起的。** engine/release.py 的 _NAME_RE 只认
# <日期>-<6 到 12 位十六进制>,verify_package() 还要求这个目录名跟包里
# release.json 的 name 一字不差 —— 那是包校验的第二关(第一关是整棵树的
# tree_sha256)。所以例子里**不能**带 d1max- 之类的前缀:带了的话 3/7 的
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
# 一路带到 2/7 末尾的 cp -a "$PKG/." 才炸 —— 而那时候 /opt/d1max 已经建出来了、
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
# /opt/d1max 已经建出来之后中止,留半个脚印给人收,报错还是 chown 的行话。
# 这个检查排在 mkdir 之前,盘上一个字节都不会动。
# **包目录名当场核一遍,别拖到 3/7 才拒。** engine/release.py 的 _NAME_RE 只认
# <日期>-<6 到 12 位十六进制>,verify_package() 还要求这个名字跟包里 release.json
# 的 name 一字不差。名字不对的话 3/7 的 release install 必拒 —— 而那时候
# /opt/d1max 已经建出来、根下那个跟版本无关的解释器也装完了(离线现场还为它
# 插过一次 U 盘),现场要回头收拾半个脚印。跟上面两条前置检查同一条理由:
# 失败要早、要便宜、要说人话。
# **这个正则要跟 _NAME_RE 一字不差**,放宽它不是这里的事 —— 目录名跟 manifest
# 对齐是故意的,它同时也是防路径穿越的那道闸。
# REL_NAME 在这里就算出来:2/7 的哨兵(见下面)和 4/7 的槽名都要用它。
REL_NAME=$(basename "$PKG")
if [[ ! "$REL_NAME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{6,12}$ ]]; then
  echo "错误: 包目录名不对: $REL_NAME" >&2
  echo "      必须长成 <日期>-<6 到 12 位十六进制>,例如 2026-09-20-77b2de。" >&2
  echo "      **不能带 d1max- 之类的前缀** —— 目录名要跟包里 release.json 的" >&2
  echo "      name 一字不差,那是包校验的第二关(第一关是整棵树的 tree_sha256)。" >&2
  echo "      包是在笔记本上用 release pack 打的,默认就是这个格式;名字不对" >&2
  echo "      多半是拷贝的时候被改过,或者手工建了个目录。" >&2
  exit 2
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  echo "错误: 这台机器上没有账号 $RUN_USER,装不下去。" >&2
  echo "      systemd 单元里的 User= 也是写死 robot 的。客户那边账号不叫 robot" >&2
  echo "      的话:先把账号建出来,或者 sudo D1MAX_USER=<账号名> bash install.sh," >&2
  echo "      **并且同时改单元里的 User=** —— 只改一边,服务的身份跟目录属主对不上。" >&2
  exit 2
fi

say "1/7 建目录 $ROOT 和数据根 /var/lib/d1max"
mkdir -p "$ROOT/releases" "$ROOT/bin"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT"
# **巡检数据根,在版本槽外面。** 服务单元的 Environment=D1MAX_DATA_ROOT 指着它,
# uninstall.sh 默认删、--keep-data 留。路径写死,理由跟 $ROOT 一样:单元里是字面量。
# **chown 只在目录是这一趟新建的时候才做。** 重跑是这份脚本的日常路径(填完
# SN 让它生效、换一版重装),这时候 /var/lib/d1max 早就在,而且里面正躺着
# 服务自己写出来的 runs/、queue.jsonl —— 服务已经把它们的属主设成了
# $RUN_USER;这里要是无条件 chown -R 一遍,每次重跑都要把这一整棵越长越大
# 的数据树递归 chown 一遍,纯粹的浪费,而且万一现场手工改过属主(比如临时
# 用 root 抢救过一次数据)这一行会把那次手工操作悄悄抹掉。只有目录还不在、
# 也就是真的第一次建它的时候,才需要把它的属主设成服务要用的那个用户。
if [ ! -d /var/lib/d1max ]; then
  mkdir -p "/var/lib/d1max"
  chown "$RUN_USER":"$RUN_USER" "/var/lib/d1max"
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
if [[ ! -x "$ROOT/bin/python" ]]; then
  python3 -m venv "$ROOT/bin-venv"
  cat > "$ROOT/bin/python" <<解释器包装
#!/bin/sh
# 由 deploy/install.sh 生成。**不要改成软链** —— 理由见 install.sh 的 2/7 步。
exec "$ROOT/bin-venv/bin/python" "\$@"
解释器包装
  chmod 0755 "$ROOT/bin/python"
  # **升 pip 收在这个 if 里面,而且失败不致命。** 原来它在 if 外面,于是
  # 每一次重跑都强制联网一次 —— 而重跑正是"填完 SN 让它生效"的路子(见
  # 7/7),离线现场照着清单跑第二趟就会卡死在这一步,set -e 直接中止在
  # 2/7,连服务都不会被 restart。升不上去本来也不是中止装机的理由:venv
  # 自带的那个 pip 装得动我们的包。
  "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS --upgrade pip \
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
# 忘了带 D1MAX_PIP_ARGS=,就会挂死在 2/7(pip 默认 5 次重试、每次 15 秒),
# 现场看到的是"装到一半不动了",set -e 中止,连 stop/start 都不跑,SN 永远不生效。
# 所以照 4/7 那个哨兵的形状给根下的 venv 也加一个,讲究完全一样:
# **哨兵在最后一行才落**,它在就等于这个包的依赖在根 venv 里装齐了。
# 哨兵里存的是版本名:换一版,内容对不上,照样重装 —— 升级路径不受影响。
ROOT_SENTINEL="$ROOT/bin-venv/.deps-ok"
ROOT_SENTINEL_WANT="d1max_patrol $REL_NAME"
if [[ "$(cat "$ROOT_SENTINEL" 2>/dev/null)" != "$ROOT_SENTINEL_WANT" ]]; then
  TMP_PKG=$(mktemp -d)
  cp -a "$PKG/." "$TMP_PKG/"
  "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS "$TMP_PKG"
  # W00b 起引擎在 robot-agent 里(W00c4 删了根包的别名壳),根包 import 引擎就要那三个包。boot-guard、
  # bundle guard、3/7 的 release install、7/7 的 activate/migrate-data 全从这个解释器跑,
  # 所以这里也装(顺序按依赖)。老包没带 packages/ 就跳过 —— 那一版的根包里 engine 还是真身。
  if [[ -d "$TMP_PKG/packages/robot-agent" ]]; then
    "$ROOT/bin/python" -m pip install --quiet $PIP_ARGS \
      "$TMP_PKG/packages/contract[mqtt]" \
      "$TMP_PKG/packages/adapter-sim" \
      "$TMP_PKG/packages/adapter-d1max" \
      "$TMP_PKG/packages/robot-agent"
  fi
  printf '%s\n' "$ROOT_SENTINEL_WANT" > "$ROOT_SENTINEL"
else
  echo "  根下这一版的依赖已经装齐了($REL_NAME),跳过 —— 离线重跑不需要网。"
fi

say "3/7 把包落进槽里"
sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" \
  "$ROOT/bin/python" -m d1max_patrol.cli release install "$PKG"

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
  printf 'd1max_patrol %s\n' "$REL_NAME" > "$SENTINEL"
  chown "$RUN_USER":"$RUN_USER" "$SENTINEL"
fi

say "5/7 装代理单元与环境文件;清掉老服务"
# **狗上只有 d1max-agent.service 一个服务**(W00c5e:老的 HTTP 服务、手机直连、特权助手退役)。
# 单元优先取包里的 deploy/,老包没带就退回这个脚本自己所在的目录。单元装一次不随版本换:
# 启动参数在每一版带的 deploy/d1max-agent-start 里(W00c5d),退回上一版时参数跟着回来。
AGENT_UNIT_SRC="$PKG/deploy/d1max-agent.service"
[[ -f "$AGENT_UNIT_SRC" ]] || AGENT_UNIT_SRC="$(dirname "$0")/d1max-agent.service"
install -m 0644 "$AGENT_UNIT_SRC" /etc/systemd/system/
# **老服务清掉。** 两个进程抢同一个旁路进程的控制权会出事,两个服务共用在途标记还会互相数
# 开机次数、替对方提交。失败不致命:新机器上本来就没有这些。
# 顺序:先停老服务、再删 sudoers、再删助手 —— 反过来的话中间有一瞬白名单指着一个不存在
# 的路径,谁在那一刻建出同名文件谁就是 root。
systemctl disable --now d1max-patrol.service 2>/dev/null || true
rm -f /etc/systemd/system/d1max-patrol.service
systemctl disable --now d1max-bootguard.service 2>/dev/null || true
rm -f /etc/systemd/system/d1max-bootguard.service
rm -f /etc/sudoers.d/d1max
rm -f /usr/local/sbin/d1max-privileged /usr/local/sbin/d1max-restart-now
# 老服务的任务包目录(狗上不再落任务包,任务随命令从站点来)。
rm -rf "$ROOT/bundles"
# 现场值的唯一来源。**已经存在就绝不覆盖** —— 现场填过的值不能被重跑抹掉。
mkdir -p /etc/d1max
if [[ ! -e /etc/d1max/env ]]; then
  # **先按 0600 建出空文件,再往里写。** `cat >` 在 root 的 umask 022 下建出来是 0644,
  # 同机别的账号都读得到。systemd 的 EnvironmentFile= 由 PID 1(root)读,不需要放宽。
  install -m 0600 /dev/null /etc/d1max/env
  cat > /etc/d1max/env <<'环境模板'
# D1 Max 运行环境(代理 d1max-agent 读它)。装机脚本只在这个文件不存在时才写一次,
# 以后重跑 install.sh 不会碰它 —— 现场填过的值放心填。

# 这只狗的机身序列号(机身标签上抄)。只收字母、数字、点、下划线、减号。
D1MAX_SN=

# 版本根目录,一般不用改。
D1MAX_RELEASE_ROOT=/opt/d1max

# ---------------------------------------------------------------- 站点
# 站点 broker 的地址,站点装好之后填:
#   mqtts://<站点主机>:8883
# 证书包(ca.crt、robot.crt、robot.key)拷到 /etc/d1max/tls/,注册文件拷成
# /etc/d1max/registration.json。**没有注册文件代理就不起**(单元的 ConditionPathExists)。
D1MAX_SITE_MQTT=
# 狗上现在载着的地图 <map_id>:<version>,与这张图上的原点 x,y,yaw。站点下发过地图之后,
# 代理以站点下发的那张为准,这两行只是第一次起来时的缺省。
D1MAX_MAP=
D1MAX_HOME=

# ---------------------------------------------------------------- 适配器
# sim = 仿真(动不了真狗)。**W00d 真机验收(庄园场景待真机测试 §3b)过了,才改成 d1max**,
# 同时在 D1MAX_AGENT_ARGS 里带上实测过的换算参数,例如:
#   D1MAX_AGENT_ARGS=--sidecar 127.0.0.1:8090 --mps-per-unit 0.4 --radps-per-unit 1.0
D1MAX_HAL=sim
# 其余参数(按空白拆开接在代理参数后面),例如建图:--mapping
D1MAX_AGENT_ARGS=

# 发件箱(断网暂存):缺省在 /var/lib/d1max/outbox。临时硬盘到了改到它上面,并写挂载点
# (没挂上代理就不起,不会悄悄写到系统盘上):
#   D1MAX_OUTBOX=/mnt/<临时盘>/outbox
#   D1MAX_OUTBOX_MOUNT=/mnt/<临时盘>
环境模板
fi
chmod 0600 /etc/d1max/env
# 老机器的 env 里可能还有老服务用的几行(设备 PIN、回传地址与密钥):**不动它们**(不改人手写的
# 配置文件),只说一声它们已经没人读了。
if grep -qE '^[[:space:]]*D1MAX_(PIN|CONSOLE_URL|CONSOLE_TOKEN)=' /etc/d1max/env 2>/dev/null; then
  echo "  提示: /etc/d1max/env 里的 D1MAX_PIN / D1MAX_CONSOLE_URL / D1MAX_CONSOLE_TOKEN"
  echo "        是老服务用的,已经没人读了,可以删掉(手机只连站点,W00c5e)。"
fi
systemctl daemon-reload
systemctl enable d1max-agent.service

say "6/7 看一眼站点签发的证书包与注册文件"
if [[ -f /etc/d1max/registration.json && -f /etc/d1max/tls/ca.crt \
      && -f /etc/d1max/tls/robot.crt && -f /etc/d1max/tls/robot.key ]]; then
  echo "  都在:/etc/d1max/registration.json、/etc/d1max/tls/{ca.crt,robot.crt,robot.key}。"
else
  cat <<'提示'
  还没有(没有注册文件代理就不起,不会刷日志)。先在站点主机上给这只狗登记(enroll,
  见装机清单一),把给出的证书包拷到这台狗的 /etc/d1max/tls/,注册文件拷成 /etc/d1max/registration.json,
  在 /etc/d1max/env 里填好 D1MAX_SITE_MQTT、D1MAX_MAP、D1MAX_HOME,再:
    sudo systemctl start d1max-agent
提示
fi

say "7/7 切到刚装的这一版并起代理"
# **把 /etc/d1max/env 里的 D1MAX_SN 读出来(不 source)。** sudo 默认 env_reset,下面
# release activate 拿不到它;而这份文件是 systemd 的 EnvironmentFile= 在读,systemd 按
# 字面量解析(等号右边就是值,不分词、不做命令替换、分号不是分隔符)。`. /etc/d1max/env`
# 让 bash 求值同一份文件,规矩不是一回事 —— `D1MAX_SN=a;reboot` 这种人手敲得出来的行,
# bash 会以 root 把 reboot 跑了。所以只读要用的键,读法对着 systemd 的规矩:只认行首空白
# 之后紧跟 `键=`,同一个键写两遍取最后一次,值两边的空白剥掉。带引号的值不还原,被下面
# 的白名单挡掉,让人改干净再来。
read_env_value() {
  sed -n "s/^[[:space:]]*$1=//p" /etc/d1max/env 2>/dev/null \
    | tail -n 1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
D1MAX_SN=
if [[ -f /etc/d1max/env ]]; then
  D1MAX_SN=$(read_env_value D1MAX_SN)
fi
# **SN 的字符白名单,不干净就停下来喊。** 带空格、引号、分号的值说明这一行是手敲歪的,
# systemd 和这里读出来的多半已经不是同一个东西。
if [[ -n "$D1MAX_SN" && ! "$D1MAX_SN" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "错误: /etc/d1max/env 里的 D1MAX_SN 不干净: [$D1MAX_SN]" >&2
  echo "      只收字母、数字、点、下划线、减号 —— 空格、引号、分号、中文都不行。" >&2
  echo "      照着机身标签重新填一遍那一行(等号两边都不要加引号),再重跑这个脚本。" >&2
  echo "      **没有 stop/start**,现在跑着的还是上一版(如果有的话)。" >&2
  exit 3
fi
if [[ -z "${D1MAX_SN//[[:space:]]/}" ]]; then
  echo "警告: /etc/d1max/env 里的 D1MAX_SN 还没填(照着机身标签填上,再重跑一次)。" >&2
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
  # D1MAX_SN 必须显式带过去 —— sudo env_reset 会把上面读出来的它扔掉。
  sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" D1MAX_SN="${D1MAX_SN:-}" \
    "$ROOT/bin/python" -m d1max_patrol.cli release activate "$REL_NAME"
fi
# **先停代理,再搬数据,再起新版。** W01 之前数据落在槽里,新版读的是 /var/lib/d1max。搬的时候
# 谁都不能正往槽里写,所以是 stop → migrate → start,**而且真没停掉就不搬**。可重跑:搬过的源
# 改名 *.migrated,第二次什么都不做。搬失败不拦装机 —— 数据还在槽里没丢,prune 见到槽里有数据
# 也不会删。stop 加 '|| true':第一次装机服务还没起过。
systemctl stop d1max-agent.service || true
if systemctl is-active --quiet d1max-agent.service; then
  echo "  !! 代理停不掉,跳过数据迁移(搬的前提是没人在往槽里写)。装完后手工:stop 代理,再跑 release migrate-data" >&2
else
  sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" D1MAX_DATA_ROOT=/var/lib/d1max \
    "$ROOT/bin/python" -m d1max_patrol.cli release migrate-data \
    || echo "  !! 槽内数据迁移没成功。没搬动的那几样还在原槽里、原名不变,prune 不会删它们;已经改名成 *.migrated 的不受保护。先 ls /opt/d1max/releases/*/ 看还有哪些原名的,再 stop 代理、手工跑 release migrate-data" >&2
fi
# 没有注册文件的话 systemd 按 ConditionPathExists 跳过,不算失败。**有注册文件、但站点地址、
# 地图、原点三样有一样空着就先不起**:启动脚本见到空值就退出,Restart=always + RestartSec=5 会把它
# 刷成每 5 秒一条日志的启动循环 —— 停下来说清楚要填什么,比留一堵日志墙好。
MISSING=
for key in D1MAX_SITE_MQTT D1MAX_MAP D1MAX_HOME; do
  if [[ -z "$(read_env_value "$key")" ]]; then
    MISSING="$MISSING $key"
  fi
done
if [[ -f /etc/d1max/registration.json && -n "$MISSING" ]]; then
  echo "  代理先不起:/etc/d1max/env 里这几样还空着:$MISSING" >&2
  echo "  填好之后:sudo systemctl start d1max-agent(不用重跑这个脚本)。" >&2
else
  systemctl start d1max-agent.service
fi

say "装完了。看一眼:"
echo "  systemctl status d1max-agent"
echo "  journalctl -u d1max-agent -f"
echo ""
echo "  连上站点之后,手机(站点模式)的狗列表里看得到这只狗;能力里报的是 D1MAX_HAL 那个适配器。"
