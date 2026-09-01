#!/usr/bin/env bash
# =====================================================================
# field-agent.sh —— 一键：保持连狗热点 + 抢连握住控制权【不放】 + 跨重启自动重抢
#
#   干的三件事：
#     1. 一直把 WiFi 连在狗热点上（掉了自动重连）。
#     2. 起 motion/patrol_agent —— 它抢开机窗口连上、TakeControl、握住不放、
#        心跳续控、上装松手就自动补控；**故意不释放**（连 SIGINT 都不放）。
#     3. 监督：机器一旦重启（变不可达），当前会话必然断——脚本停掉旧 agent，
#        等机器重新上线，**重新抢一次开机窗口**。如此循环，直到你 Ctrl+C。
#
#   用法：
#     bash field-agent.sh        # 先跑它，再给 RK3588 上电/重启
#
#   Ctrl+C：只停进程、**不释放控制权**（要真交还得发 shutdown 命令，见下）。
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
cleanup(){
  echo
  echo "[field-agent] 退出：给 patrol_agent 发 SIGTERM（它收到只退进程、不释放控制权）"
  [ -n "$AGENT_PID" ] && kill -TERM "$AGENT_PID" 2>/dev/null
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
echo " Ctrl+C 退出（只停进程、不释放控制权）"
echo "======================================================"

round=0
while true; do
  round=$((round+1))

  # 1) 一直把 WiFi 连上狗热点
  until connect_wifi; do
    echo "[field-agent] 连热点 $SSID 中...（狗关着/信号弱都会到这）"
    sleep 2
  done

  # 2) 起 patrol_agent（它自己抢连+TakeControl+握住不放，日志进文件）
  LOG="runs/field-logs/agent-$(date -u +%Y%m%dT%H%M%SZ).log"
  echo "[field-agent] 第 ${round} 轮：启动 patrol_agent，抢连 $DOG:$PORT （日志 $LOG）"
  "$BIN" "$DOG" "$PORT" --listen "$LISTEN" --fifo "$FIFO" >>"$LOG" 2>&1 &
  AGENT_PID=$!

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
        kill -TERM "$AGENT_PID" 2>/dev/null; wait "$AGENT_PID" 2>/dev/null
        AGENT_PID=""
        break
      fi
    fi
    sleep 3
  done

  # patrol_agent 自己退了（比如收到 shutdown）也会走到这
  if [ -n "$AGENT_PID" ] && ! kill -0 "$AGENT_PID" 2>/dev/null; then
    echo "[field-agent] patrol_agent 已退出（可能是 shutdown）。2s 后重新抢——若想彻底停请 Ctrl+C。"
    AGENT_PID=""
    sleep 2
  fi
done
