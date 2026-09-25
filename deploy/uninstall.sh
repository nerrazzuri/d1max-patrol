#!/usr/bin/env bash
# =====================================================================
# uninstall.sh —— 一键卸载:把我们装上去的东西清掉,不留痕迹
#
#   用法:
#     sudo bash deploy/uninstall.sh                      # 默认:全部清掉
#     sudo bash deploy/uninstall.sh --keep-data          # 留现场配置和数据
#     sudo bash deploy/uninstall.sh --dry-run            # 只说要删什么,不动手
#     sudo bash deploy/uninstall.sh --agent-root <目录>   # 指定放 runs/ 的那个目录
#     sudo bash deploy/uninstall.sh --purge-agent-binary # 连编出来的二进制一起删
#
#   **默认就是彻底卸干净**:服务、单元、自启链、/opt/d1max 整个根、
#   /etc/d1max 整个目录(含站点证书包、注册文件、现场填的 env)、数据根,以及正在跑的
#   patrol_agent 和它留下的管道 / pid 文件 / 现场日志。
#   只想重装、不想重新向站点登记这只狗的,用 --keep-data。
#
#   **没有二次确认。** 要的是"一键"。想先看一眼的人走 --dry-run ——
#   脚本一上来就把要删的东西和不可逆的后果整块打在屏幕上,然后才动手。
#   不做交互式确认还有第二条理由,跟 install.sh 第 216-219 行一样:
#   这脚本要能在无人值守的 provisioning 里跑,一个会阻塞等输入的脚本
#   比它要防的问题更麻烦。
#
#   这台机器上没有的那一半会自动跳过:Orin 上没有 runs/d1max-agent.fifo,
#   agent 那一段就是个空操作;笔记本上没有 /opt/d1max,盘上那一段同理。
#   什么都没装过的机器上跑一遍,干干净净退 0。
#
#   **变量名一律用 ASCII。** bash 的标识符只认 ASCII 字母和下划线:
#   `根=/opt/d1max` 这种写法不是赋值,而是一条"找不到的命令",配上
#   set -euo pipefail 会让脚本停在那一行。注释和屏幕上的话照旧写中文。
# =====================================================================
set -euo pipefail

# ---------------------------------------------------------- 对账声明块
#
# **下面这几行是给机器读的,不是装饰。** tests/test_deploy_coexist.py 把
# deploy/install.sh 里的 @写盘 / @也删 跟这里的 @删除 / @保留 对起来:
#
#   * install.sh 新加一处写盘而这里没跟上 —— 那条测试当场红;
#   * 这里多删了一处 install.sh 根本没建过的东西 —— 同样红。
#
# 这条对账是"卸载卸得干净、又只卸我们自己的"这句话唯一不靠人记性的凭据。
#
# @删除 /opt/d1max                                                       整个根
# @删除 /opt/d1max/bin                                                   跟版本无关的解释器
# @删除 /opt/d1max/bin-venv                                              上面那个解释器的 venv
# @删除 /opt/d1max/releases                                              版本槽(代码和 venv)
# @删除 /opt/d1max/current                                               版本链
# @删除 /opt/d1max/pending.json                                          在途升级标记
# @删除 /opt/d1max/rolled_back.json                                      开机守卫退回上一版时留的条子
# @删除 /var/lib/d1max                                                   数据根(发件箱、事件簿、幂等记录)
# @删除 /etc/d1max                                                       整个配置目录
# @删除 /etc/d1max/env                                                   现场值(站点地址、地图、原点、适配器、SN)
# @删除 /etc/systemd/system/d1max-agent.service                           代理单元
# @删除 /etc/systemd/system/multi-user.target.wants/d1max-agent.service  代理的开机自启链
# @删除 /etc/systemd/system/d1max-bootguard.service                      老的守卫单元(多数机器上没有)
# @删除 /etc/systemd/system/d1max-patrol.service                         老服务(W00c5e 退役;没重跑过装机脚本的老机器上还在)
# @删除 /etc/systemd/system/multi-user.target.wants/d1max-patrol.service 老服务的自启链
# @删除 /usr/local/sbin/d1max-privileged                                 老的特权助手
# @删除 /etc/sudoers.d/d1max                                             老助手的 sudo 白名单
# @删除 /usr/local/sbin/d1max-restart-now                                老服务重启的内部入口
# @删除 /opt/d1max/bundles                                               老服务的任务包目录
#
# 带 --keep-data 时这几处留着,每一处的理由都要能说出口:
#
# @保留 /etc/d1max          配置目录留着:站点签发的证书包、注册文件、env 都在它底下
# @保留 /etc/d1max/env      现场填过的值(站点地址、地图、原点、SN),重装不用再填
# @保留 /opt/d1max          根目录本身留着(只清里面的解释器、版本槽、链)
# @保留 /var/lib/d1max      数据根:发件箱里还没传到站点的东西在这儿

# ------------------------------------------------------------ 常量
# **写死,不给环境变量覆盖**,跟 install.sh 第 11 行同一个口径。下面那些
# rm 的范围锁用的也是这几个字面量而不是这几个变量 —— 哪怕有人把变量改了,
# 锁还在原地。
ROOT=/opt/d1max
ETC_DIR=/etc/d1max
UNIT_DIR=/etc/systemd/system
MAIN_UNIT=d1max-agent.service
OLD_UNIT=d1max-bootguard.service
LEGACY_UNIT=d1max-patrol.service
WANTS_LINK=/etc/systemd/system/multi-user.target.wants/d1max-agent.service
LEGACY_WANTS_LINK=/etc/systemd/system/multi-user.target.wants/d1max-patrol.service

say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

usage() {
  warn "用法:"
  warn "  sudo bash $0                      # 默认:全部清掉,不留痕迹"
  warn "  sudo bash $0 --keep-data          # 只卸服务和代码,留现场配置和数据"
  warn "  sudo bash $0 --dry-run            # 只说要删什么,不动手"
  warn "  sudo bash $0 --agent-root <目录>   # patrol_agent 的 runs/ 在哪个目录下"
  warn "  sudo bash $0 --purge-agent-binary # 连编出来的 motion/patrol_agent 一起删"
}

# ------------------------------------------------------------ 参数
KEEP_DATA=0
DO_IT=1
PURGE_BIN=0
AGENT_ROOT=
# 没删掉的处数。**一条 rm 失败不能让 set -e 把后面几处和 5/5 回执一起吞掉。**
# 盘上有一处删不掉(权限、只读挂载、文件正被占着),现场最需要的恰恰是
# "剩下几处怎么样了"和那张回执 —— 死在第一条上等于既没卸干净、又不告诉人。
RM_FAILED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-data)          KEEP_DATA=1 ;;
    --dry-run)            DO_IT=0 ;;
    --purge-agent-binary) PURGE_BIN=1 ;;
    --agent-root)
      if [ $# -lt 2 ]; then
        warn "!! --agent-root 后面要跟一个目录"
        exit 2
      fi
      AGENT_ROOT=$2
      shift
      ;;
    --agent-root=*) AGENT_ROOT=${1#--agent-root=} ;;
    -h|--help) usage; exit 0 ;;
    *)
      warn "!! 不认识的参数:$1"
      usage
      exit 2
      ;;
  esac
  shift
done

# **真删必须是 root,而且这道闸要排在 reap_agent 前面。**
# 这脚本第一个不可逆的动作是收掉 patrol_agent,而那一步**恰恰不需要 root**
# (agent 是登录用户用 field-agent.sh 起的,同一个用户 kill -TERM 就够)。
# SDK 会话一丢,不重启 RK3588 就再也抢不回来(真机待验证清单 #46/#47)。
# 真正需要 root 的是后面 rm /opt/d1max 那几处。没有这道闸的话,一次忘了 sudo
# 的运行会:先把控制权永久交还给上装 -> 再在第一条 rm 上 EACCES -> set -e
# 当场打死 -> 盘上一个字节没卸、5/5 回执也打不出来。也就是最不可逆的那一步
# 照做了,该做的一步都没做,现场还看不到回执。
# **--dry-run 和 -h 不需要 root**:它们什么都不动,闸放在参数解析之后正是为此。
if [ "$DO_IT" = 1 ] && [ "$(id -u)" != 0 ]; then
  warn "!! 真删要用 root 跑: sudo bash $0 ..."
  warn "   现在就停下 —— 盘上一个字节都没动,patrol_agent 也还活着。"
  warn "   (想先看要删什么,不需要 root: bash $0 --dry-run)"
  exit 2
fi

# agent 那一套东西默认按"这个脚本在仓库里的位置"去找:field-agent.sh 开头
# 会 cd 到仓库根,runs/ 就在那儿。脚本被单独拷到别处时用 --agent-root 指。
if [ -z "$AGENT_ROOT" ]; then
  AGENT_ROOT=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || AGENT_ROOT=$PWD
fi
AGENT_FIFO="$AGENT_ROOT/runs/d1max-agent.fifo"
AGENT_PIDFILE="$AGENT_ROOT/runs/d1max-agent.pid"
AGENT_LOGDIR="$AGENT_ROOT/runs/field-logs"
AGENT_BIN="$AGENT_ROOT/motion/patrol_agent"

# --------------------------------------------------------- 删除的两把锁
#
# 第一把:目标不许为空。第二把:目标必须长成下面 case 里那几个形状之一。
# **两把锁都在 rm 之前,而且 rm 吃的是加了引号的、刚被这两把锁验过的变量。**
# 这几条写不对就是事故,不是 bug。

# 盘上属于我们的那几处。范围里**没有**任何一条能匹配到别家的目录、
# 别家的 ROS、或者任何不叫 d1max-* 的 systemd 单元。
rm_sys() {
  local target=${1:-} why=${2:-}
  if [ -z "$target" ]; then
    warn "  内部错误:删除目标是空的,拒绝执行。"
    return 0
  fi
  case "$target" in
    /opt/d1max|/opt/d1max/*) ;;
    /etc/d1max|/etc/d1max/*) ;;
    /var/lib/d1max|/var/lib/d1max/*) ;;
    /etc/systemd/system/d1max-*.service) ;;
    /etc/systemd/system/*.target.wants/d1max-*.service) ;;
    /usr/local/sbin/d1max-privileged) ;;
    /usr/local/sbin/d1max-restart-now) ;;
    /etc/sudoers.d/d1max) ;;
    *)
      warn "  拒绝删除 $target —— 不在允许的范围里。这是护栏,不是错误。"
      return 0
      ;;
  esac
  if [ ! -e "$target" ] && [ ! -L "$target" ]; then
    return 0
  fi
  if [ "$DO_IT" != 1 ]; then
    say "  [dry-run] 会删:$target${why:+  ($why)}"
    return 0
  fi
  # **删不掉只记一笔,不许把整个脚本带走。** 这一处 EACCES / 只读挂载,后面
  # 还有五六处该删的和那张 5/5 回执 —— set -e 死在这儿,现场既没卸干净,
  # 也不知道卸到哪儿了。rm 吃的仍然是上面两把锁刚验过的那个加引号的变量。
  if rm -rf -- "$target"; then
    say "  删了:$target${why:+  ($why)}"
  else
    warn "  !! 没删掉:$target —— 记下来,人工确认。"
    RM_FAILED=$((RM_FAILED + 1))
  fi
}

# patrol_agent 留在工作目录里的痕迹。它们不在 /opt 也不在 /etc,所以单独
# 一把锁:只认这几个固定的名字,而且必须长在 runs/ 或 motion/ 底下。
rm_agent_trace() {
  local target=${1:-} why=${2:-}
  if [ -z "$target" ]; then
    warn "  内部错误:删除目标是空的,拒绝执行。"
    return 0
  fi
  case "$target" in
    */runs/d1max-agent.fifo|*/runs/d1max-agent.pid|*/runs/field-logs) ;;
    */motion/patrol_agent) ;;
    *)
      warn "  拒绝删除 $target —— 不在允许的范围里。这是护栏,不是错误。"
      return 0
      ;;
  esac
  if [ ! -e "$target" ] && [ ! -L "$target" ]; then
    return 0
  fi
  if [ "$DO_IT" != 1 ]; then
    say "  [dry-run] 会删:$target${why:+  ($why)}"
    return 0
  fi
  # 同 rm_sys:删不掉只记一笔。理由见那边。
  if rm -rf -- "$target"; then
    say "  删了:$target${why:+  ($why)}"
  else
    warn "  !! 没删掉:$target —— 记下来,人工确认。"
    RM_FAILED=$((RM_FAILED + 1))
  fi
}

# ------------------------------------------------------- patrol_agent
# 从 pidfile 认领一个还活着的 agent。跟 field-agent.sh 的 read_pidfile 同一
# 条讲究:pid 会被系统回收,只看 kill -0 可能认领到一个毫不相干的进程。
read_pidfile() {
  local pid
  [ -f "$AGENT_PIDFILE" ] || return 1
  pid=$(cat "$AGENT_PIDFILE" 2>/dev/null) || return 1
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

# 等一个 pid 走掉。上限由参数给,**不许无限等**。
wait_gone() {
  local pid=$1 limit=$2 waited=0
  while [ "$waited" -lt "$limit" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  return 0
}

# 找出所有在跑的 agent:pidfile 里那个 + pgrep 兜底。
#
# **两处不许出错的地方:**
#   1. pgrep 的模式要收紧到"可执行文件本身叫 patrol_agent",否则
#      `grep patrol_agent` 这种无辜进程也会被匹进来;
#   2. 匹出来的 pid 里必须排掉自己($$)和自己的父进程($PPID) ——
#      不排的话脚本会把自己(或者调它的那个 shell)杀掉。
agent_pids() {
  local pid found raw= p out=
  pid=$(read_pidfile) || pid=
  if [ -n "$pid" ]; then
    raw="$pid"
  fi
  found=$(pgrep -f '(^|/)patrol_agent([[:space:]]|$)' 2>/dev/null) || found=
  raw="$raw $found"
  for p in $raw; do
    case "$p" in ''|*[!0-9]*) continue ;; esac
    if [ "$p" = "$$" ] || [ "$p" = "$PPID" ]; then
      continue
    fi
    case " $out " in *" $p "*) continue ;; esac
    out="$out $p"
  done
  printf '%s\n' "$out"
}

reap_agent() {
  local pid pids p
  say ""
  say "2/5 收掉 patrol_agent,再清掉它留下的痕迹"
  say "    (找的是 $AGENT_ROOT 底下的 runs/;不对的话用 --agent-root 指)"

  # 第一级:走它自己认的优雅退出 —— 往 FIFO 里写一条 shutdown。
  if [ -p "$AGENT_FIFO" ]; then
    if [ "$DO_IT" = 1 ]; then
      say "  往 $AGENT_FIFO 写一条 shutdown(field-agent.sh 里写明的那条优雅退出)"
      # **一定要带超时。** FIFO 没有读端的时候写端会一直阻塞 —— agent 已经
      # 死了而管道还留在盘上,正是最常见的那一种情形,不掐就是挂在这儿。
      if timeout 5 bash -c 'printf "%s\n" shutdown > "$1"' _ "$AGENT_FIFO" 2>/dev/null; then
        say "  写进去了。"
      else
        say "  (5 秒没写进去:多半没人在读这个管道 —— 往下走)"
      fi
    else
      say "  [dry-run] 会往 $AGENT_FIFO 写一条 shutdown"
    fi
  else
    say "  (没有 $AGENT_FIFO,跳过 FIFO 这一级)"
  fi

  # 第二级:有 pidfile 就等它自己退,上限 15 秒。
  pid=$(read_pidfile) || pid=
  if [ -n "$pid" ]; then
    if [ "$DO_IT" = 1 ]; then
      say "  pidfile 里是 $pid,等它自己退(最多 15 秒)"
      if wait_gone "$pid" 15; then
        say "  它自己退干净了 —— 不用发信号。"
      fi
    else
      say "  [dry-run] pidfile 里是 $pid,会等它最多 15 秒"
    fi
  fi

  # 第三、四级:还在就 TERM,再不走就 KILL。兜底那一路(pgrep)走同样两步。
  pids=$(agent_pids)
  if [ -z "$pids" ]; then
    say "  现在没有在跑的 patrol_agent。"
  else
    for p in $pids; do
      if [ "$DO_IT" != 1 ]; then
        say "  [dry-run] 会对 pid $p 先发 TERM(等 5 秒),还在就发 KILL"
        continue
      fi
      say "  pid $p 还在 —— 发 TERM,等 5 秒"
      kill -TERM "$p" 2>/dev/null || true
      if wait_gone "$p" 5; then
        say "    走了。"
        continue
      fi
      say "    还在 —— 发 KILL"
      kill -KILL "$p" 2>/dev/null || true
      if wait_gone "$p" 3; then
        say "    走了。"
      else
        warn "    !! pid $p 连 KILL 都没弄走(多半是权限不够,没用 sudo),手工看一眼"
      fi
    done
  fi

  rm_agent_trace "$AGENT_FIFO" "命令管道"
  rm_agent_trace "$AGENT_PIDFILE" "pid 文件"
  rm_agent_trace "$AGENT_LOGDIR" "现场日志"
  if [ "$PURGE_BIN" = 1 ]; then
    rm_agent_trace "$AGENT_BIN" "编出来的二进制(--purge-agent-binary)"
  else
    say "  留着 $AGENT_BIN —— 它是源码目录里编出来的产物,不是装机脚本装的。"
    say "    默认删掉它,下次开工的人会莫名其妙。真要删加 --purge-agent-binary。"
  fi
}

# ------------------------------------------------------------ systemd
stop_service() {
  local u
  say ""
  say "3/5 停服务、关自启、清掉我们的 systemd 单元"
  if ! command -v systemctl >/dev/null 2>&1; then
    say "  (这台机器上没有 systemctl,整段跳过)"
    return 0
  fi
  for u in "$MAIN_UNIT" "$LEGACY_UNIT" "$OLD_UNIT"; do
    if [ "$DO_IT" != 1 ]; then
      say "  [dry-run] 会 stop + disable $u"
      continue
    fi
    # 没装过这个单元的机器上这两条都会失败 —— 那不是错误,是"本来就没有"。
    systemctl stop "$u" >/dev/null 2>&1 || true
    systemctl disable "$u" >/dev/null 2>&1 || true
    say "  停了并关掉自启:$u(本来就没有的话,这一步什么也没发生)"
  done
  rm_sys "$UNIT_DIR/$MAIN_UNIT" "代理单元"
  rm_sys "$WANTS_LINK" "代理的开机自启链"
  rm_sys "$UNIT_DIR/$OLD_UNIT" "老的守卫单元,多数机器上本来就没有"
  rm_sys "$UNIT_DIR/$LEGACY_UNIT" "老服务(W00c5e 退役),重跑过装机脚本的机器上已经没有"
  rm_sys "$LEGACY_WANTS_LINK" "老服务的自启链"
  # 先删 sudoers 再删助手 —— 反过来的话中间有一瞬白名单指着一个不存在
  # 的路径,谁在那一刻建出同名文件谁就是 root。
  rm_sys "/etc/sudoers.d/d1max" "老助手的 sudo 白名单"
  rm_sys "/usr/local/sbin/d1max-privileged" "老的特权助手"
  rm_sys "/usr/local/sbin/d1max-restart-now" "老服务重启的内部入口"
  if [ "$DO_IT" = 1 ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl reset-failed "$MAIN_UNIT" >/dev/null 2>&1 || true
    say "  daemon-reload 和 reset-failed 都跑过了。"
  else
    say "  [dry-run] 会跑 daemon-reload 和 reset-failed $MAIN_UNIT"
  fi
}

# -------------------------------------------------------------- 盘上
wipe_disk() {
  local real=
  say ""
  say "4/5 清盘上的东西"
  # **删链之前先把它指的真实目录打出来。** 链一删,数据还在盘上,但没人
  # 知道在哪个槽里了 —— 这一行就是那张字条。
  real=$(readlink -f "$ROOT/current" 2>/dev/null) || real=
  if [ -n "$real" ] && [ "$real" != "$ROOT/current" ]; then
    say "  current 这条链现在指着:$real"
    say "  数据在 /var/lib/d1max(发件箱里可能还有没传到站点的东西)。"
  fi

  if [ "$KEEP_DATA" = 1 ]; then
    say "  --keep-data:只卸配置和服务。下面这几处留着,每一处都有理由。"
    rm_sys "$ROOT/bin" "跟版本无关的解释器"
    rm_sys "$ROOT/bin-venv" "上面那个解释器的 venv"
    rm_sys "$ROOT/current" "版本链"
    rm_sys "$ROOT/pending.json" "在途升级标记"
    rm_sys "$ROOT/rolled_back.json" "守卫的条子"
    rm_sys "$ROOT/bundles" "老服务的任务包目录"
    # **releases 里可能还留着搬迁时因重名没搬进数据根的 *.migrated 副本。**
    # 那份数据只有这一处备份,--keep-data 图的就是不丢数据 —— 这时候把
    # releases 整个删掉,数据就真的没了。先看一眼槽里(每个槽目录底下)
    # 有没有 *.migrated,有就留着让人自己核对完再删,没有才真删。
    if find "$ROOT/releases" -mindepth 2 -maxdepth 2 -name '*.migrated' \
        -print -quit 2>/dev/null | grep -q .; then
      say "  留着:$ROOT/releases —— 里面还有搬迁时因重名没搬进数据根的 *.migrated 副本,自己核对完再删。"
    else
      rm_sys "$ROOT/releases" "版本槽;数据不在里面了(W01 之后在 /var/lib/d1max)"
    fi
    say "  留着:$ETC_DIR/env —— 现场填过的站点地址、地图、原点、SN,重装不用再填。"
    say "  留着:$ETC_DIR —— 站点签发的证书包、注册文件、env 都在它底下;删了要重新向站点登记。"
    say "  留着:/var/lib/d1max —— 数据根:发件箱里还没传到站点的东西在这儿。"
    say "  留着:$ROOT —— 根目录本身。"
  else
    say "  默认模式:全部清掉,不留痕迹。"
    rm_sys "$ROOT/current" "版本链"
    rm_sys "$ROOT/pending.json" "在途升级标记"
    rm_sys "$ROOT/rolled_back.json" "守卫的条子"
    rm_sys "$ROOT/bin" "跟版本无关的解释器"
    rm_sys "$ROOT/bin-venv" "上面那个解释器的 venv"
    rm_sys "$ROOT/releases" "版本槽"
    rm_sys "$ROOT/bundles" "老服务的任务包目录"
    rm_sys "/var/lib/d1max" "数据根"
    rm_sys "$ROOT" "整个根"
    rm_sys "$ETC_DIR/env" "现场值"
    rm_sys "$ETC_DIR" "整个配置目录(连同站点证书包、注册文件)"
  fi
}

# ------------------------------------------------------------ 说在前面
announce() {
  say "================================================================"
  if [ "$DO_IT" != 1 ]; then
    say " D1 Max 巡检 · 卸载 —— **这是 dry-run,什么都不会动**"
  elif [ "$KEEP_DATA" = 1 ]; then
    say " D1 Max 巡检 · 卸载(--keep-data:留现场配置和数据)"
  else
    say " D1 Max 巡检 · 卸载(默认:全部清掉,不留痕迹)"
  fi
  say "================================================================"
  say ""
  say "接下来这个脚本会停掉 patrol_agent 并**释放 SDK 会话**。"
  say "这一步不可逆:"
  say ""
  say "  上装会立刻把控制权收回去,这台狗在重启 RK3588 之前"
  say "  再也抢不到控制权了(厂家已确认,见真机验证清单 #46/#47)。"
  say "  要重新接管:先重启运控主机,再跑 field-agent.sh 抢开机窗口。"
  say ""
  if [ "$KEEP_DATA" != 1 ]; then
    say "同时会删掉(默认模式):"
    say "  $ROOT 整个根 —— 代码、版本槽"
    say "  /var/lib/d1max —— 数据根:**发件箱里还没传到站点的东西**也在这儿"
    say "  $ETC_DIR 整个目录 —— 包括站点签发的证书包与注册文件"
    say "  $UNIT_DIR 底下我们的 d1max-* 单元和开机自启链"
    say "  patrol_agent 的命令管道、pid 文件、现场日志"
    say ""
    say "只想重装、不想重新向站点登记这只狗的,按 Ctrl+C 停下来,"
    say "改用:sudo bash $0 --keep-data"
  else
    say "$ROOT/releases 会删掉,除非里面还有搬迁时没搬完的 *.migrated 副本 —— 有就留着,自己核对完再删。"
  fi
  say ""
  say "**不动的东西**:别家的栈、ROS、任何不叫 d1max-* 的 systemd 单元,"
  say "一个字节都不碰 —— 这不是靠自觉,是 rm 前那两把锁挡着的。"
}

# -------------------------------------------------------------- 回执
receipt() {
  say ""
  say "5/5 回执"
  if [ "$DO_IT" != 1 ]; then
    say "  这是 dry-run —— 上面每一条都没有真的执行。"
    say "  真要动手:把 --dry-run 去掉重跑。"
  else
    say "  卸完了。"
    # **有删不掉的就得当着人的面说出来。** 5/5 回执是现场判断"卸干净没有"的
    # 唯一凭据,漏掉这句话的回执是一张假回执。
    if [ "$RM_FAILED" != 0 ]; then
      say "  **但有 $RM_FAILED 处没删掉**(上面打了 !! 的那几条)。"
      say "  这台机器没卸干净 —— 逐条抄下来,人工确认掉了再说撤场。"
    fi
    say "  已经停掉 patrol_agent 并释放了 SDK 会话 —— 上装已经把控制权收回去,"
    say "  这台狗在重启 RK3588 之前抢不到控制权了。要重新接管:先重启运控主机,"
    say "  再跑 field-agent.sh 抢开机窗口。"
  fi
  say ""
  say "  **这几样这个脚本清不掉,要的话得人去做:**"
  say "    1. systemd 的历史日志(journalctl -u d1max-agent 还查得到)。"
  say "       要清得用 journalctl --vacuum-time= 之类,那会连别人的日志一起清,"
  say "       所以这个脚本不替你做这个决定。"
  say "    2. 装机时 pip 在 ~/.cache/pip 里留下的轮子缓存。"
  say "    3. 跑过的人在 shell history 里留下的那些命令。"
  say "    4. 笔记本上 NetworkManager 记住的那条狗热点连接配置。"
  say "    5. 这个脚本和 footprint.sh 自己写在 runs/ 底下的快照文件。"
  if [ "$PURGE_BIN" != 1 ]; then
    say "    6. 源码目录里编出来的 motion/patrol_agent(加 --purge-agent-binary 才删)。"
  fi
}

# -------------------------------------------------------------- 主流程
say ""
say "1/5 先说清楚要干什么"
announce
# 上面那段里写着"按 Ctrl+C 停下来,改用 --keep-data"。**不给窗口的话那句话是空的**
# —— announce 打完屏,下一行 reap_agent 就开始收 patrol_agent,而 SDK 会话一丢
# 就是不可逆的那一半(#46/#47),等人读完那段字早就没了。
#
# 所以人坐在终端前的时候给 5 秒。**没有 TTY 就不等** —— 头部注释里说明了这脚本
# 要能在无人值守的 provisioning 里跑,那种场合没人会按 Ctrl+C,白等 5 秒而已;
# 但那也正是"不做交互式确认"的理由,这里不能因为加窗口就把它改成阻塞等输入。
# dry-run 更不用等:它什么都不动。
if [ "$DO_IT" = 1 ] && [ -t 0 ]; then
  say ""
  say "  5 秒后开始。要停就现在按 Ctrl+C。"
  sleep 5
fi
reap_agent
stop_service
wipe_disk
receipt
exit 0
