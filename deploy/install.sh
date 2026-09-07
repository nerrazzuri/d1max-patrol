#!/usr/bin/env bash
# D1 Max 巡检:装机。**可以重跑** —— 装到一半断了、参数填错了、换台机器,
# 直接再跑一遍就是。每一步都在屏幕上说人话,因为跑它的人多半不写代码。
set -euo pipefail

根=${D1MAX_ROOT:-/opt/d1max}
用户=${D1MAX_USER:-robot}
包=${1:-}

说() { printf '\n>>> %s\n' "$*"; }

if [[ -z "$包" ]]; then
  echo "用法: sudo bash install.sh <包目录>" >&2
  echo "例:   sudo bash install.sh /home/robot/d1max-2026-09-20-77b2de" >&2
  exit 2
fi

说 "1/7 建目录 $根"
mkdir -p "$根/releases" "$根/bin"
chown -R "$用户":"$用户" "$根"

说 "2/7 装一个跟版本无关的解释器到 $根/bin"
# 守卫要用它。**它不能在任何一版目录里** —— 版本坏了,救生索还得在。
if [[ ! -x "$根/bin/python" ]]; then
  python3 -m venv "$根/bin-venv"
  ln -sfn "$根/bin-venv/bin/python" "$根/bin/python"
fi
"$根/bin/python" -m pip install --quiet --upgrade pip
"$根/bin/python" -m pip install --quiet "$包"

说 "3/7 把包落进槽里"
sudo -u "$用户" env D1MAX_RELEASE_ROOT="$根" \
  "$根/bin/python" -m d1max_patrol.cli release install "$包"

说 "4/7 给这一版建自己的 venv"
# **每版一个 venv,不是服务共用根下那个解释器。** 服务单元的 ExecStart
# 指着 <槽>/venv/bin/python,这个解释器只能在这儿建 —— stage() 只是
# shutil.copytree,不会替我们建。选每版一个 venv 而不是共用根解释器的
# 理由:双槽的意义之一就是回滚要把依赖也一并退回去,依赖装在共用解释器
# 里的话,回滚只退代码不退依赖,退了也是白退。这不会打破任何既有校验——
# verify_package 的树哈希只在落槽那一刻算一次,建 venv 不碰包里那些文件。
名字=$(basename "$包")
槽="$根/releases/$名字"
if [[ ! -x "$槽/venv/bin/python" ]]; then
  sudo -u "$用户" python3 -m venv "$槽/venv"
  sudo -u "$用户" "$槽/venv/bin/python" -m pip install --quiet --upgrade pip
  sudo -u "$用户" "$槽/venv/bin/python" -m pip install --quiet "$槽"
fi

说 "5/7 装 systemd 单元与环境文件"
install -m 0644 "$(dirname "$0")/d1max-patrol.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/d1max-bootguard.service" /etc/systemd/system/
# D1MAX_SN 的唯一来源。**已经存在就绝不覆盖** —— 现场填过的值不能被
# 重跑抹掉。两个 systemd 单元都用 EnvironmentFile=-/etc/d1max/env 读它,
# 命令行 release activate 也读同一个变量,两条路必须是同一个来源,
# 否则重启后自检第四项(identity)会假失败,把一版好的自动回滚掉。
mkdir -p /etc/d1max
if [[ ! -e /etc/d1max/env ]]; then
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
环境模板
fi
systemctl daemon-reload
systemctl enable d1max-bootguard.service d1max-patrol.service

说 "6/7 记下这台有没有装上装"
cat <<'提示'
  这一步没法替你做,因为只有站在机器旁边的人看得见(§7.1)。
  装完之后在手机 app 或者用 curl 记一次:

    curl -X PUT http://<机器 IP>:10010/api/identity/payload \
      -H 'Content-Type: application/json' \
      -d '{"has_payload": false, "by": "你的名字",
           "confirm": "我知道这会改掉升级方式"}'

  没记过之前,这台机器**不许升级** —— 因为升级流程不知道该怎么重启它。
提示

说 "7/7 切到刚装的这一版并起服务"
# SN 没填不致命(不 exit),但要喊出来 —— 打到 stderr,不吞在一堆 echo 里。
# 不加交互确认(read -p 之类):install.sh 必须能在无人值守的 provisioning
# 脚本里跑,一个会阻塞等输入的装机脚本比它要防的问题更麻烦,跟"安全网
# 不该比它防的问题更危险"是同一条理由。
if ! grep -qE '^D1MAX_SN=.+' /etc/d1max/env 2>/dev/null; then
  echo "警告: /etc/d1max/env 里的 D1MAX_SN 还没填。" >&2
  echo "      不填的话自检的 identity 那一项会拿兜底的 MAC 值去比对," >&2
  echo "      可能把这一版判成回滚。现场先填好再继续。" >&2
fi
# release activate 撞见"已经是在跑的这一版"会报错退出(这是对 HTTP 那条
# 路正确的行为,不改它 —— 见 engine/release.py 的 activate())。但重跑这个
# 脚本正是现场"填完 SN 让它生效"的路子 —— 跳过切换不能连 restart 也跳过,
# **restart 必须无条件执行**。
当前=$(basename "$(readlink -f "$根/current" 2>/dev/null || true)")
if [[ "$当前" == "$名字" ]]; then
  echo "  $名字 已经是在跑的那一版了,跳过切换。"
else
  sudo -u "$用户" env D1MAX_RELEASE_ROOT="$根" \
    "$根/bin/python" -m d1max_patrol.cli release activate "$名字"
fi
systemctl restart d1max-patrol.service

说 "装完了。看一眼:"
echo "  systemctl status d1max-patrol"
echo "  curl http://127.0.0.1:10010/api/release"
echo "  curl http://127.0.0.1:10010/api/selfcheck"
