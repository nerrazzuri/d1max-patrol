#!/usr/bin/env bash
# =====================================================================
# D1 Max 现场离线联调脚本  (走 WiFi / 单网卡)
# 断网也能独立跑完：C2 连机器狗热点  +  D2 conform 一致性巡检
#
# 用法（在一个【真实终端】里，不是在 Claude 里）：
#     cd "~/Projects/D1 Max/d1max-patrol"
#     bash field-connect.sh
# 会先问一次 sudo 密码（加路由要用）。全程只读，机器【不会动】。
# 跑完会自动帮你连回 Always Robots_5G（恢复上网），再把日志贴给 Claude。
# =====================================================================
set -u
cd "$(dirname "$0")"

DOG_SSID="XG2WIFI_C40221"
DOG_PASS="12345678"
HOME_SSID="Always Robots_5G"
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=~/d1max-field-$TS.log
exec > >(tee -a "$LOG") 2>&1

say(){ echo; echo "========== $* =========="; }

say "0. 预热 sudo（加路由要用，现在输一次密码，后面就不再问）"
sudo -v || { echo "!! sudo 认证失败，退出"; exit 1; }

say "1. 临时关掉 $HOME_SSID 的自动连接（防止没网时系统偷偷跳回去）"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect no 2>/dev/null && echo "已临时关闭（收尾会自动恢复）"

say "2. 连接机器狗热点 $DOG_SSID（AP 信号偏弱，最多扫 12 轮）"
CONNECTED=no
for i in $(seq 1 12); do
  nmcli dev wifi rescan 2>/dev/null; sleep 3
  if nmcli -f SSID dev wifi list 2>/dev/null | grep -q "$DOG_SSID"; then
    echo "第 $i 轮：看到热点，尝试连接..."
    if nmcli dev wifi connect "$DOG_SSID" password "$DOG_PASS" 2>&1; then
      CONNECTED=yes; break
    fi
  else
    echo "第 $i 轮：还没扫到 $DOG_SSID，重试..."
  fi
done
if [ "$CONNECTED" != yes ]; then
  echo "!! 连不上 $DOG_SSID。确认机器开着、人离机器近一点，再重跑本脚本。"
fi

say "3. 看拿到的 IP（预期是 192.168.234.x）"
sleep 3
ip -br addr show wlp59s0

say "4. ping RK3588 的 AP 地址 192.168.234.1"
ping -c 3 -W 2 192.168.234.1

say "5. 加路由：192.168.168.0/24 走 AP（这样才够得到导航主机 Orin NX）"
sudo ip route del 192.168.168.0/24 2>/dev/null
if sudo ip route add 192.168.168.0/24 via 192.168.234.1; then
  echo "路由已加"
else
  echo "!! 路由添加失败（AP 没连上时会这样）"
fi

say "6. ping 两台主机"
echo "-- RK3588  192.168.168.168 --"; ping -c 3 -W 2 192.168.168.168
echo "-- Orin NX 192.168.168.100 --"; ping -c 3 -W 2 192.168.168.100

say "7. 选导航地址（TCP 探 10010，两个候选都试）"
source .venv/bin/activate
export PYTHONPATH=
U=""
for cand in 192.168.168.100 192.168.144.100; do
  if timeout 4 bash -c "exec 3<>/dev/tcp/$cand/10010" 2>/dev/null; then
    U="ws://$cand:10010"; echo "  $cand:10010  通  → 用它跑 conform"; break
  else
    echo "  $cand:10010  不通"
  fi
done
if [ -z "$U" ]; then
  echo "  两个导航地址 TCP 都不通；仍用 168.100 让 conform 把'不通'如实记录下来。"
  U="ws://192.168.168.100:10010"
fi
echo "  U=$U"

say "8. 【今天的头号任务】conform 一致性巡检 —— 全程只读，机器不动"
python -m d1max_patrol.cli --url "$U" -v conform

say "跑完了"
echo "日志：     $LOG"
echo "原始帧：   runs/frames/   （最重要的产出，务必带回）"
echo "对拍报告： runs/conformance/<时间戳>/conformance-report.md"
echo
echo ">> 想留在机器狗网络里手动多敲几条只读命令，现在按 Ctrl-C。"
echo ">> 否则按回车，自动连回 $HOME_SSID 恢复上网，再把日志贴给 Claude。"
read -r _

say "9. 收尾：恢复上网"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect yes 2>/dev/null
sudo nmcli con up "$HOME_SSID" 2>&1
echo
echo "===================================================================="
echo " 上网恢复后，请把这个文件的【全部内容】整段贴回给 Claude："
echo "     $LOG"
echo " （或直接贴 runs/conformance/ 里那个 conformance-report.md 的正文）"
echo "===================================================================="
