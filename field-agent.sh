#!/usr/bin/env bash
# =====================================================================
# field-agent.sh —— 一键：保持连狗热点 + 抢连握住控制权【不放】 + 跨重启自动重抢
#
#   干的三件事：
#     1. 一直把 WiFi 连在狗热点上（掉了自动重连）。
#     2. 起 motion/patrol_agent —— 它抢开机窗口连上、TakeControl、握住不放、
#        心跳续控、上装松手就自动补控；**故意不释放**。
#     3. 监督：机器一旦重启（变不可达），当前会话必然断——脚本停掉旧 agent，
#        等机器重新上线，**重新抢一次开机窗口**。如此循环，直到你 Ctrl+C。
#
#   用法：
#     bash field-agent.sh        # 先跑它，再给 RK3588 上电/重启
#
#   Ctrl+C：**只退监督脚本，patrol_agent 继续跑、控制权继续握着。**
#     厂家已确认控制权交不回来（清单 #46/#47），一旦松手就只能重启 RK3588
#     重抢开机窗口。所以 Ctrl+C 绝不能连带把会话丢掉。agent 跑在自己的
#     session 里，Ctrl+C 和关终端都带不走它。
#     退出后停的是「保活」：热点掉线不再自动重连、机器重启不再自动重抢。
#     想恢复监督，重跑本脚本即可 —— 它会认出还在跑的 agent 并直接接管。
#
#   发命令（另开终端）—— FIFO 是空格分隔（hold02 风格），一行一条：
#     echo 'stand'        > runs/d1max-agent.fifo
#     echo 'walk 1 0.4'   > runs/d1max-agent.fifo    # 走 1 秒、前进量 0.4
#     echo 'lie'          > runs/d1max-agent.fifo
#     echo 'estop'        > runs/d1max-agent.fifo     # 软急停开；clear 关
#     echo 'status'       > runs/d1max-agent.fifo
#     echo 'shutdown'     > runs/d1max-agent.fifo     # 真释放+退出(不可逆，之后要重启 RK3588)
#   （TCP 口 127.0.0.1:8090 才是 JSONL 协议，给 Python 客户端 SidecarDeviceBackend 用。）
#
#   前提：急停在手；动作前四周留足空地。
# =====================================================================
set -u
cd "$(dirname "$0")"
DOG=192.168.234.1; PORT=8082; SSID=XG2WIFI_C40221; PASS=12345678
BIN=./motion/patrol_agent
LISTEN=127.0.0.1:8090
FIFO=runs/d1max-agent.fifo
PIDFILE=runs/d1max-agent.pid
mkdir -p runs/field-logs

# 缺二进制就编（Makefile 已修好带空格路径的 rpath）
if [ ! -x "$BIN" ]; then
  echo "[field-agent] 未见 $BIN，编译中..."
  make -C motion patrol_agent || { echo "!! 编译失败"; exit 1; }
fi

# 命令管道（应急人工通道）
[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }

# 判定"机器重启了"要连续多少次 ping 不通(每次间隔 3s)。
#
# 这个数字的两边不对称,所以宁大勿小:
#   判早了 —— 一次 WiFi 抖动就把**活着的、握着控制权的** agent 杀掉。杀掉之后
#             机器根本没重启,也就没有新的开机窗口可抢,重连大概率撞上
#             "Controlled denial of service"(清单 #40),这台机器今天就废了。
#   判晚了 —— 只是晚几十秒去抢窗口。而 patrol_agent 自己会重试 2000 次,
#             真机重启到 SDK 口可用本来就要 40s 以上,晚一点根本追得上。
# 原来是 3(=9 秒),太灵敏了 —— 2.4G 热点上有人走过去就可能抖这么久。
DOWN_STRIKES=10   # 10 x 3s = 30s

AGENT_PID=""
LOG=""

# ---------------------------------------------------------------------
# patrol_agent 的生死
# ---------------------------------------------------------------------

# 从 pidfile 认领一个还活着的 agent，认出来就把 pid 打到 stdout。
#
# 为什么还要查 /proc/<pid>/cmdline: pidfile 可能是上次留下的陈迹，而 pid
# 是会被系统回收的。只看 kill -0 的话，我们可能"认领"了一个毫不相干的进程，
# 然后一整轮都不去起真正的 agent —— 现场表现为"脚本说好着呢，狗不动"。
read_pidfile(){
  local pid
  [ -f "$PIDFILE" ] || return 1
  pid=$(cat "$PIDFILE" 2>/dev/null)
  case "$pid" in ''|*[!0-9]*) return 1;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  grep -qa patrol_agent "/proc/$pid/cmdline" 2>/dev/null || return 1
  echo "$pid"
}

# 起 agent，**放进它自己的 session**。
#
# 这一步是"Ctrl+C 不杀 agent"的关键,而且光靠 trap 是做不到的: Ctrl+C 是
# 终端把 SIGINT 发给整个前台进程组,agent 在组里就会直接收到,轮不到脚本的
# trap 说话(patrol_agent 收到 SIGINT 就 _Exit)。setsid 把它挪出这个组、
# 同时脱离控制终端 —— 于是 Ctrl+C 和关终端(SIGHUP)都碰不到它。
#
# pid 由内层 shell 自己写: setsid 有可能 fork、也有可能不 fork($! 因此不可靠),
# 而内层 shell exec 之后的 pid 就是 agent 的 pid,这个是确定的。
start_agent(){
  local setsid_bin waited
  setsid_bin=$(command -v setsid || true)
  LOG="runs/field-logs/agent-$(date -u +%Y%m%dT%H%M%SZ).log"
  rm -f "$PIDFILE"
  echo "[field-agent] 第 ${round} 轮：启动 patrol_agent，抢连 $DOG:$PORT （日志 $LOG）"

  if [ -z "$setsid_bin" ]; then
    echo "[field-agent] !! 没有 setsid（util-linux）——agent 只能跟脚本同组，"
    echo "               这种情况下 Ctrl+C 会连它一起杀掉、控制权就丢了。"
    echo "               装一下：sudo apt install util-linux"
    "$BIN" "$DOG" "$PORT" --listen "$LISTEN" --fifo "$FIFO" >>"$LOG" 2>&1 &
    AGENT_PID=$!
    echo "$AGENT_PID" > "$PIDFILE"
    return 0
  fi

  "$setsid_bin" bash -c 'echo $$ > "$1"; shift; exec "$@"' _ \
      "$PIDFILE" "$BIN" "$DOG" "$PORT" --listen "$LISTEN" --fifo "$FIFO" \
      >>"$LOG" 2>&1 &

  waited=0
  while [ "$waited" -lt 50 ]; do
    if AGENT_PID=$(read_pidfile); then return 0; fi
    waited=$((waited+1)); sleep 0.1
  done
  AGENT_PID=""
  echo "!! patrol_agent 起来了但 5 秒内没写下 pid，日志见 $LOG"
  return 1
}

# 停 agent 并等它真的走掉。**只有确认机器重启了才该调这个** —— 别处调
# 就是白白丢掉一次抢不回来的控制权。
stop_agent(){
  local waited=0
  [ -n "$AGENT_PID" ] || return 0
  kill -TERM "$AGENT_PID" 2>/dev/null
  # 它多半已经不是本脚本的子进程了（setsid 之后），wait 不管用，只能轮询。
  while [ "$waited" -lt 50 ] && kill -0 "$AGENT_PID" 2>/dev/null; do
    waited=$((waited+1)); sleep 0.1
  done
  rm -f "$PIDFILE"
  AGENT_PID=""
}

cleanup(){
  echo
  if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    cat <<TIP
[field-agent] 退出监督脚本 —— **patrol_agent 继续跑（pid $AGENT_PID），控制权没松手。**
  它在自己的 session 里，关掉这个终端也带不走它。

  还能继续用：
    echo 'stand' > $FIFO          # FIFO 人工通道照旧
    TCP 127.0.0.1:8090            # Python 客户端照旧

  停掉的是「保活」这一层：热点掉了不再自动重连，机器重启也不再自动重抢窗口。
  想恢复：bash field-agent.sh     # 会认出它、直接接管，不会重复起第二个

  真要交还控制权（不可逆，之后必须重启 RK3588 才能再抢）：
    echo 'shutdown' > $FIFO
TIP
  else
    echo "[field-agent] 退出（当前没有在跑的 patrol_agent）。"
  fi
  exit 0
}
trap cleanup INT TERM

connect_wifi(){
  local cur
  cur=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2}')
  [ "$cur" = "$SSID" ] && return 0
  nmcli dev wifi rescan >/dev/null 2>&1
  nmcli dev wifi connect "$SSID" password "$PASS" >/dev/null 2>&1
  cur=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2}')
  [ "$cur" = "$SSID" ]
}

echo "======================================================"
echo " field-agent —— 抢连握住不放 + 跨重启自动重抢"
echo " 狗=$DOG:$PORT  热点=$SSID  命令口 TCP=$LISTEN  fifo=$FIFO"
echo " ★ 现在就可以给 RK3588 上电/重启，脚本会一直抢开机窗口。"
echo " Ctrl+C 只退本脚本——patrol_agent 继续跑，控制权不松手"
echo "======================================================"

round=0
while true; do
  round=$((round+1))

  # 1) 一直把 WiFi 连上狗热点
  until connect_wifi; do
    echo "[field-agent] 连热点 $SSID 中...（狗关着/信号弱都会到这）"
    sleep 2
  done

  # 2) 已经有 agent 在跑就直接接管。
  #
  #    Ctrl+C 之后重跑本脚本会走到这。**绝不能再起一个** —— 控制权是独占的，
  #    第二个 agent 抢不到，只会一边刷失败一边把日志搅乱，而真正握着控制权的
  #    那个还好端端在那儿。
  if AGENT_PID=$(read_pidfile); then
    LOG="（沿用已在跑的会话，日志见 runs/field-logs/ 里最新的一个）"
    echo "[field-agent] 发现 patrol_agent 已在跑（pid $AGENT_PID）——接管监督，不重复启动。"
  else
    start_agent || { sleep 2; continue; }
  fi

  # 3) 监督：保活 WiFi + 盯机器可达性；机器重启→停旧 agent→回到顶部重抢
  seen_up=0; fails=0
  while kill -0 "$AGENT_PID" 2>/dev/null; do
    connect_wifi || true
    if ping -c1 -W2 "$DOG" >/dev/null 2>&1; then
      fails=0
      if [ "$seen_up" = 0 ]; then
        seen_up=1
        echo "[field-agent] 机器可达；patrol_agent 持有中（详见 $LOG，找 ‘抢到窗口’ / ‘TakeControl’）"
      fi
    elif [ "$seen_up" = 1 ]; then
      fails=$((fails+1))
      echo "[field-agent] 机器不可达 #${fails}/${DOWN_STRIKES}（曾在线）—— 疑似重启中"
      if [ "$fails" -ge "$DOWN_STRIKES" ]; then
        echo "[field-agent] 判定机器重启：当前会话已断，停掉旧 agent，准备重抢新开机窗口"
        stop_agent
        break
      fi
    fi
    sleep 3
  done

  # patrol_agent 自己退了（比如收到 shutdown）也会走到这
  if [ -n "$AGENT_PID" ] && ! kill -0 "$AGENT_PID" 2>/dev/null; then
    echo "[field-agent] patrol_agent 已退出（可能是 shutdown）。2s 后重新抢——若想彻底停请 Ctrl+C。"
    rm -f "$PIDFILE"
    AGENT_PID=""
    sleep 2
  fi
done
