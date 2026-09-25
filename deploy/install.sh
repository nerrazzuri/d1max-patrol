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
# @写盘 /opt/d1max/bundles                                               任务包目录,服务跑起来后落在根下
# @写盘 /opt/d1max/pending.json                                          在途升级标记,同上
# @写盘 /var/lib/d1max                                                   巡检数据根(runs、上传队列、基线、导出),升级回滚都不碰它
# @写盘 /etc/d1max                                                       配置目录
# @写盘 /etc/d1max/env                                                   设备 PIN 在这个文件里
# @写盘 /etc/systemd/system/d1max-patrol.service                         install -m 0644 摆进去的单元
# @写盘 /etc/systemd/system/multi-user.target.wants/d1max-patrol.service systemctl enable 生成的自启链
# @写盘 /etc/systemd/system/d1max-agent.service                           W00b:robot-agent 的单元,装而不 enable(W00c 有站点 broker 了再起用)
# @写盘 /usr/local/sbin/d1max-privileged                                 W01b:robot 通过 sudo 白名单能跑的唯一 root 命令(装单元、restart、reboot)
# @写盘 /etc/sudoers.d/d1max                                             W01b:那条白名单,只放行上面那个脚本
# @写盘 /usr/local/sbin/d1max-restart-now                                W01b:OTA 重启的内部入口(stop→搬数据→start),0700,不在白名单里,只由 systemd-run 调
#
# 这份脚本不写、但 uninstall.sh 要负责收掉的:
#
# @也删 /etc/systemd/system/d1max-bootguard.service 老的守卫单元;这里只 rm -f 它,从来不写它
#
# **盘上只有以上这些。** 所有 pip install 都落在 /opt/d1max 底下的 venv 里,
# python3 -m venv 只读系统 python3,不往系统 site-packages 里写东西。

# **写死,不给环境变量覆盖。** systemd 单元里 /opt/d1max 是逐字写死的
# (WorkingDirectory=、Environment=D1MAX_RELEASE_ROOT=、两条 Exec* 的解释器
# 路径),app/server.py 里 AppContext.release_root 的默认值也是它。允许这里
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

say "5/7 装 systemd 单元、特权助手与环境文件"
# **单元、助手、sudoers 三样优先取包里的 deploy/**(W01b 起 release pack 带它),
# 这样 install.sh 装的和 OTA 以后 release activate 装的是同一份来源。老包没带
# deploy/ 就退回这个脚本自己所在的目录 —— 装机清单教现场 scp 的就是那份。
UNIT_SRC="$PKG/deploy/d1max-patrol.service"
[[ -f "$UNIT_SRC" ]] || UNIT_SRC="$(dirname "$0")/d1max-patrol.service"
install -m 0644 "$UNIT_SRC" /etc/systemd/system/
# W00b:robot-agent 的单元**装而不 enable**。它要连站点的 broker,那东西 W00c 才有;
# 现在 enable 只会得到一个每 5 秒重启一次、连不上任何东西的进程。老包没带就跳过。
AGENT_UNIT_SRC="$PKG/deploy/d1max-agent.service"
[[ -f "$AGENT_UNIT_SRC" ]] || AGENT_UNIT_SRC="$(dirname "$0")/d1max-agent.service"
if [[ -f "$AGENT_UNIT_SRC" ]]; then
  install -m 0644 "$AGENT_UNIT_SRC" /etc/systemd/system/
fi
# 特权助手:robot 通过 sudo 白名单能跑的唯一 root 命令(OTA 装单元、restart、
# reboot)。**必须装在 root 拥有的目录里**:/opt/d1max 整棵归 robot,sudo 白名单
# 指着 robot 可写的脚本等于把 root 送给 robot。它不随 OTA 更新,改它重跑本脚本。
HELPER_SRC="$PKG/deploy/d1max-privileged"
[[ -f "$HELPER_SRC" ]] || HELPER_SRC="$(dirname "$0")/d1max-privileged"
install -m 0755 -o root -g root "$HELPER_SRC" /usr/local/sbin/d1max-privileged
# 重启的内部入口:stop → 搬槽内数据 → start。**不进 sudoers**,0700 只有 root 能读
# 能跑;助手的 restart 用 systemd-run 把它起在服务 cgroup 外面。
RESTART_NOW_SRC="$PKG/deploy/d1max-restart-now"
[[ -f "$RESTART_NOW_SRC" ]] || RESTART_NOW_SRC="$(dirname "$0")/d1max-restart-now"
install -m 0700 -o root -g root "$RESTART_NOW_SRC" /usr/local/sbin/d1max-restart-now
# sudoers:**先 visudo -cf 校验再落盘**。一份坏 sudoers 会锁死整机的 sudo,
# 现场就只剩重刷系统这一条路。
SUDOERS_SRC="$PKG/deploy/sudoers-d1max"
[[ -f "$SUDOERS_SRC" ]] || SUDOERS_SRC="$(dirname "$0")/sudoers-d1max"
visudo -cf "$SUDOERS_SRC" >/dev/null
install -m 0440 -o root -g root "$SUDOERS_SRC" /etc/sudoers.d/d1max
# **老机器上的 d1max-bootguard.service 要清掉。** 守卫已经挪成主单元的
# ExecStartPre=(理由见那个单元里那段注释:不带上装的机器升级走 systemctl
# restart,根本不开机,挂在开机上的守卫永远不会重跑)。两处都留着的话,
# 真开机时守卫会被数两次,pending.attempts 一次开机加二,第二次开机就把
# 一版本来健康的退掉 —— 比守卫根本没跑还危险。
# **失败不致命**:绝大多数机器上本来就没有这个单元,而这个脚本要能重跑。
systemctl disable --now d1max-bootguard.service 2>/dev/null || true
rm -f /etc/systemd/system/d1max-bootguard.service
# D1MAX_SN 的唯一来源。**已经存在就绝不覆盖** —— 现场填过的值不能被
# 重跑抹掉。服务单元用 EnvironmentFile=-/etc/d1max/env 读它,7/7 步里这个
# 脚本自己也按 systemd 的规矩解析同一份文件再把 D1MAX_SN 显式传给 release activate,
# 两条路必须是同一个来源,否则重启后自检第四项(identity)会假失败,
# 把一版好的自动回滚掉。
mkdir -p /etc/d1max
if [[ ! -e /etc/d1max/env ]]; then
  # **先把空文件按 0600 建出来,再往里写。** 这份文件里有设备 PIN 和回传密钥
  # (下面自己就写着"这一行是密钥,别抄进任何工单"),而 `cat >` 在 root 的
  # umask 022 下建出来是 0644 —— 这台狗上还跑着厂商的上装、系统 Python 是
  # 共用的,同机任何一个账号都读得到。
  # **0600 root:root 两个读它的人都够用**:服务那条路是 systemd 的
  # EnvironmentFile= 在读,由 PID 1(root)读完再降到 User=robot,不是 robot
  # 自己去开这个文件;7/7 步这个脚本也是 root。文档教现场看 PIN 用的也是
  # sudo grep。**别为了让 robot 读得到就放宽它** —— 没有哪一方需要。
  # 下面的 `cat >` 和 5/7 末尾的 `printf >>` 都是重定向,不改已有文件的 mode,
  # 所以这个 0600 保得住。
  install -m 0600 /dev/null /etc/d1max/env
  cat > /etc/d1max/env <<'环境模板'
# D1 Max 运行环境。装机脚本只在这个文件不存在时才写一次,以后重跑
# install.sh 不会碰它 —— 现场填过的值放心填。

# 这只狗的机身序列号。**厂商的 SDK 和导航 WebSocket 接口都不报这个
# 字段**,只能靠现场贴在机身上的标签手填。不填的话,身份解析会兜底用
# 网卡 MAC 凑一个 SN,重启后自检拿它跟 release activate 记的真 SN
# 一比对不上,可能把一版好的自动回滚掉 —— 装机时务必先填这一行,
# 再执行 release activate。
D1MAX_SN=

# 版本根目录,一般不用改。
D1MAX_RELEASE_ROOT=/opt/d1max

# 手机 app 连这台机器要输的设备 PIN。装机脚本第一次写这份文件时**随机生成
# 一个填在下面,不留空**:服务单元的 ExecStart 带 --host 0.0.0.0(手机 app
# 连的是 192.168.168.100:8095,只听 127.0.0.1 的话它根本连不上),而
# check_exposure() 见到非本机地址又没有 PIN 会直接 SystemExit 拒绝启动 ——
# 配上 Restart=always + RestartSec=5,那就是每 5 秒刷一条日志的启动循环。
# **这一行和单元里那个 --host 0.0.0.0 是一对,拆不开。**
# 想换成好记的:直接改下面这一行,再 sudo systemctl restart d1max-patrol。

# ---------------------------------------------------------------- 回传(可选)
# 买了服务器的才填这两行。**两行都留空 = 单机档**:证据全留在本机,
# 靠 U 盘导出(见《值守与告警》)。单机档是正常形态,不是装坏了。
#
# 怎么确认自己在哪一档:装完之后 journalctl -u d1max-patrol | grep 回传。
# 单机档会有一行"没配回传地址,这台狗按单机档跑";配上了就没有这一行。
# 值守屏上那一格显示的是「没装回传」,不是 0 —— **别把它当成"传了 0 条"**。
#
# 地址填服务器的 https://<你们的服务器>:8096,填错或服务器没起来不会
# 影响巡检 —— 上传线程永远不阻塞走路拍照,传不出去就一直在盘上排着。
D1MAX_CONSOLE_URL=

# 回传要带的钥匙,跟服务器上配的那把必须一模一样。填错的话上传会一直被
# 服务器拒,队列积压条数会从心跳里报上来,值守屏看得见。
# **这一行是密钥,别抄进任何工单、截图或聊天记录。**
D1MAX_CONSOLE_TOKEN=
环境模板
  # 用根下那个跟版本无关的解释器生成,不依赖机器上有没有别的 python。
  DEVICE_PIN=$("$ROOT/bin/python" -c 'import secrets; print(f"{secrets.randbelow(10**6):06d}")')
  printf 'D1MAX_PIN=%s\n' "$DEVICE_PIN" >> /etc/d1max/env
  say "这台机器的设备 PIN 是 $DEVICE_PIN"
  echo "  手机 app 第一次连这台机器要输它。**记进现场登记表**,屏幕关了就找不回来了"
  echo "  (还能在机器上看:sudo grep D1MAX_PIN /etc/d1max/env)。"
  echo "  想换成好记的:编辑 /etc/d1max/env 里的 D1MAX_PIN= 那一行,再"
  echo "  sudo systemctl restart d1max-patrol(只改 PIN 的话,不用重跑装机脚本)。"
fi
# **老机器重跑一次也要被收紧。** 上面那个 install -m 0600 只在文件不存在时走,
# 而 2026-09-13 之前装出来的机器盘上躺着的是 0644 的那一份,里面同样有 PIN 和
# 回传密钥。这一行放在 if 外面,重跑一次就把它们一起收掉。chmod 不动文件内容,
# 现场填过的值一个字都不会变。
chmod 0600 /etc/d1max/env
systemctl daemon-reload
systemctl enable d1max-patrol.service

say "6/7 记下这台有没有装上装"
cat <<'提示'
  这一步没法替你做,因为只有站在机器旁边的人看得见(§7.1)。
  装完之后在手机 app 里记一次;要用 curl 的话**先换 token** ——
  服务带 --host 0.0.0.0 起,所以从 127.0.0.1 发过去也一样要 token,
  没有"本机豁免"这回事。换 token 的那几行在 docs/装机清单.md 三节,
  照那儿抄,别把 PIN 敲进命令里。换到之后:

    curl -X PUT http://<机器 IP>:8095/api/identity/payload \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      -d '{"has_payload": false, "by": "你的名字",
           "confirm": "我知道这会改掉升级方式"}'

  没记过之前,这台机器**不许升级** —— 因为升级流程不知道该怎么重启它。
提示

say "7/7 切到刚装的这一版并起服务"
# **把 /etc/d1max/env 里要用的值读出来。** sudo 默认 env_reset,D1MAX_SN 不在
# env_keep 里,而 root 自己的环境里本来也没有它 —— 不读的话,下面 release
# activate 拿到的是空值,cli.py 的 resolve() 会落到设备树/MAC 兜底,而服务侧经
# EnvironmentFile= 拿到的是人填的真值。两个值一不一样,重启后自检第四项
# (identity)就恒红,postcheck_verdict 判 ROLLBACK,一版好的被退回去。
# **docs/装机清单.md 里"不填 SN 会假失败"那段话,正是因为这里把这份文件读了、
# 并在下面把变量显式传了下去,才是成立的。**
#
# **但绝不能 source 它。** 这份文件是 systemd 的 EnvironmentFile= 在读,systemd
# 按字面量解析:等号右边就是值,不分词、不做命令替换、分号不是分隔符。而
# `. /etc/d1max/env` 是让 bash 求值同一份文件,规矩根本不是一回事 —— 而这份
# 文件是现场拿 nano 一个字一个字敲出来的,下面三种都是人手敲得出来的:
#
#   * D1MAX_SN=C4 0221              systemd 认;bash 去执行 `0221`,set -e 当场 127,
#                                   装机死在 7/7,前六步全白跑
#   * D1MAX_CONSOLE_TOKEN=a;reboot  systemd 认;bash **以 root 把 reboot 跑了**
#   * D1MAX_PIN=$(id -u)            systemd 原样当字面量;bash 做命令替换
#
# 所以这里只把要用的两个键读出来,读法逐条对着 systemd 的解析(src/core/
# execute.c 走 load_env_file_push,键名过 env_name_is_valid)对齐:
#
#   * 只认 `键=` 开头,键前面允许有空白 —— systemd 也跳过行首空白;
#   * `#` 开头的注释行两边都不认(sed 那一条要求行首空白之后紧跟键名);
#   * `export D1MAX_SN=x` 两边都不认 —— systemd 解出来的键名是 `export D1MAX_SN`,
#     带空格,env_name_is_valid() 判非法,整行被忽略(只打一条警告)。这里跟着
#     不认,是为了两边读出同一个值,不是漏掉了;
#   * 同一个键写了两遍,**systemd 取最后一次**(basic/env-util.c 的
#     strv_env_replace:后面的赋值替换前面的),所以这里 tail -n 1;
#   * 值两边的空白 systemd 对不带引号的值会剥掉(\r 也算空白),这里跟着剥 ——
#     现场用 nano 敲完常常多一个尾随空格,不剥的话下面的字符白名单会把一个
#     systemd 明明认得的值判死,现场白挨一次 exit 3。
#
# 带引号的值(D1MAX_SN="C4 0221")这里**故意不还原**:引号会留在值里,被下面
# 的白名单挡掉,让人改干净再来。与其在 shell 里重写一遍 systemd 的引号和转义
# 规则(写歪一点就是两边各读各的值,而那正是这一步要防的事),不如停下来喊。
read_env_value() {
  sed -n "s/^[[:space:]]*$1=//p" /etc/d1max/env 2>/dev/null \
    | tail -n 1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
D1MAX_SN=
D1MAX_PIN_VALUE=
if [[ -f /etc/d1max/env ]]; then
  D1MAX_SN=$(read_env_value D1MAX_SN)
  D1MAX_PIN_VALUE=$(read_env_value D1MAX_PIN)
fi

# **SN 的字符白名单,不干净就停下来喊。** 这个值会被原样塞进回传请求的
# HTTP header(engine/http_sink.py 里 sn 不转义、原样进 header),填成中文的话
# 上传线程每一条都 UnicodeEncodeError;带空格、引号、分号的值则说明这一行是
# 手敲歪的,systemd 和这里读出来的多半已经不是同一个东西。"装机时挡住脏 sn"
# 本来就记在配置自检这一层(挂账 142),这里正是那一层。
# 白名单覆盖了本项目见过的所有真 SN(C40221、D1M-0007、D1MAX-TEST-01 之类),
# 它挡的是空格和标点,不挡任何一种真序列号的写法。
if [[ -n "$D1MAX_SN" && ! "$D1MAX_SN" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "错误: /etc/d1max/env 里的 D1MAX_SN 不干净: [$D1MAX_SN]" >&2
  echo "      只收字母、数字、点、下划线、减号 —— 空格、引号、分号、中文都不行。" >&2
  echo "      照着机身标签重新填一遍那一行(等号两边都不要加引号),再重跑这个脚本。" >&2
  echo "      **没有 stop/start**,现在跑着的还是上一版(如果有的话)。" >&2
  exit 3
fi

# **PIN 空着就停在这儿,不 stop/start。** 跟下面 SN 那条警告不是一回事:
# SN 空只是"可能"被误判回滚,PIN 空是必定起不来。单元的 ExecStart 带
# --host 0.0.0.0,check_exposure() 见到非本机地址又没 PIN 会 SystemExit;
# 配上 Restart=always + RestartSec=5,机器会变成每 5 秒刷一条日志的启动
# 循环,现场看到的是一堵日志墙,比停下来喊一声糟得多。
# **测的是解析出来的值,不是"这一行非空"。** 原来这里是
# grep -qE '^D1MAX_PIN=.+',它问的是"文件里有没有一行长这样",而
# `D1MAX_PIN=   `(尾随几个空格)、`D1MAX_PIN=` 写了两遍第二遍是空的,
# 这两种它都判"有 PIN",于是脚本照常往下 stop/start,机器照样进那个每 5 秒一条的
# 启动循环 —— 守卫在,墙也在。现在测的是上面按 systemd 的规矩读出来的那个值。
if [[ -z "${D1MAX_PIN_VALUE//[[:space:]]/}" ]]; then
  echo "错误: /etc/d1max/env 里的 D1MAX_PIN 是空的,服务起不来。" >&2
  echo "      单元的 ExecStart 带 --host 0.0.0.0(手机 app 要连它)," >&2
  echo "      而没有 PIN 的话服务会拒绝启动;Restart=always + RestartSec=5" >&2
  echo "      会把它刷成每 5 秒一条日志的启动循环。" >&2
  echo "      先填好 D1MAX_PIN=(4 位以上,建议 6 位数字),再重跑这个脚本。" >&2
  echo "      **没有 stop/start**,现在跑着的还是上一版(如果有的话)。" >&2
  # **本次 enable 过了,得撤掉再退。** 5/7 已经 systemctl enable 过了:不撤的话
  # 这次确实不 stop/start(上面那句话是真的),但**下一次开机** systemd 会照
  # multi-user.target.wants 那条自启链把它拉起来,check_exposure() 见到
  # --host 0.0.0.0 又没 PIN 就 SystemExit,Restart=always + RestartSec=5 +
  # StartLimitIntervalSec=0 —— 这段守卫要防的那堵日志墙原样回来,只是推迟到了
  # 现场没人看着的时候。填好 PIN 重跑一趟,5/7 会重新 enable 回去。
  # 失败不致命:单元还没装上、systemd 不在的机器上也得能干净地退出来。
  systemctl disable d1max-patrol.service >/dev/null 2>&1 || true
  exit 3
fi

# SN 没填不致命(不 exit),但要喊出来 —— 打到 stderr,不吞在一堆 echo 里。
# 不加交互确认(read -p 之类):install.sh 必须能在无人值守的 provisioning
# 脚本里跑,一个会阻塞等输入的装机脚本比它要防的问题更麻烦,跟"安全网
# 不该比它防的问题更危险"是同一条理由。
# 同上,测的是解析出来的值 —— `D1MAX_SN=   ` 这种填了个寂寞的写法,
# 原来那条 grep 判"填了",而 systemd 剥完空白读出来的是空串。
if [[ -z "${D1MAX_SN//[[:space:]]/}" ]]; then
  echo "警告: /etc/d1max/env 里的 D1MAX_SN 还没填。" >&2
  echo "      不填的话自检的 identity 那一项会拿兜底的 MAC 值去比对," >&2
  echo "      可能把这一版判成回滚。现场先填好再继续。" >&2
fi
# release activate 撞见"已经是在跑的这一版"会报错退出(这是对 HTTP 那条
# 路正确的行为,不改它 —— 见 engine/release.py 的 activate())。但重跑这个
# 脚本正是现场"填完 SN 让它生效"的路子 —— 跳过切换不能连 stop/start 也跳过,
# **stop 和 start 必须无条件执行**。
# **链不在就是空串,别拿 basename 的兜底值凑合。** readlink -f 在 current 这条
# 链还不存在时什么也不输出,外面套一层 basename 得到的是字面量 "current" ——
# 一个长得像版本名的假值。今天它碰巧咬不着人:_NAME_RE 不允许哪一版叫
# current,所以下面那个相等判断一定不成立。但那是靠**别处**的规矩兜着的,
# _NAME_RE 哪天松一点,这里就变成"第一次装机反而跳过了 activate"。
# 写成显式的:没有链就是空,空串跟任何 REL_NAME 都不相等,该 activate 就 activate。
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
# **先停服务,再搬数据,再起新版。** W01 之前 --runs-root 落在槽里;新版起来后
# 读的是 /var/lib/d1max,不搬的话历史全"消失"、待传队列停传。搬的时候老版本
# 必须已经停了 —— 它还活着就可能正往 queue.jsonl / runs/ 追加,搬到一半的文件
# 会被当成已完整拷走。所以不是 migrate → restart,是 stop → migrate → start,
# **而且真没停掉就不搬**(见下面 is-active 那道闸)。
# 可重跑:搬过的源会改名 *.migrated,第二次什么都不做。搬失败不拦装机 ——
# 数据还在槽里没丢,prune 见到槽里有数据也不会删(engine/release.py)。
# stop 加 '|| true':第一次装机服务还不存在,stop 会报错,不能让 set -e 死在这。
systemctl stop d1max-patrol.service || true
# **停不掉就别搬。** 搬的前提是没人在往槽里写;stop 超时/失败时老进程还活着。
# is-active 对"没这个单元"也回非零,所以第一次装机照样走搬迁分支。
if systemctl is-active --quiet d1max-patrol.service; then
  echo "  !! 服务停不掉,跳过数据迁移(搬的前提是没人在往槽里写)。装完后手工:stop 服务,再跑 release migrate-data" >&2
else
  sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" D1MAX_DATA_ROOT=/var/lib/d1max \
    "$ROOT/bin/python" -m d1max_patrol.cli release migrate-data \
    || echo "  !! 槽内数据迁移没成功。没搬动的那几样还在原槽里、原名不变,prune 不会删它们;已经改名成 *.migrated 的不受保护。先 ls /opt/d1max/releases/*/ 看还有哪些原名的,再 stop 服务、手工跑 release migrate-data" >&2
fi
systemctl start d1max-patrol.service

say "装完了。看一眼:"
echo "  systemctl status d1max-patrol"
echo ""
echo "  下面三条要看的是 /api/release、/api/selfcheck、/api/identity,"
echo "  但裸 curl 会全部返回 401 —— 服务带 --host 0.0.0.0 起,必须有 PIN,"
echo "  而且从 127.0.0.1 发过去也一样要 token。先照 docs/装机清单.md 三节"
echo "  换一个 TOKEN(那几行从 /etc/d1max/env 取 PIN,不用手敲),再:"
echo ""
echo "    curl -sS -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:8095/api/release"
echo "    curl -sS -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:8095/api/selfcheck"
echo "    curl -sS -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:8095/api/identity"
