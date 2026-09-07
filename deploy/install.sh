#!/usr/bin/env bash
# D1 Max 巡检:装机。**可以重跑** —— 装到一半断了、参数填错了、换台机器,
# 直接再跑一遍就是。每一步都在屏幕上说人话,因为跑它的人多半不写代码。
set -euo pipefail

# **写死,不给环境变量覆盖。** systemd 单元里 /opt/d1max 是逐字写死的
# (WorkingDirectory=、Environment=D1MAX_RELEASE_ROOT=、两条 Exec* 的解释器
# 路径),app/server.py 里 AppContext.release_root 的默认值也是它。允许这里
# 被 D1MAX_ROOT 改而单元不跟着改,装出来的是一台脚本和服务各说各话的机器:
# 包落在一个根下,服务读的是另一个根。**要换根目录,这里和单元一起改。**
根=/opt/d1max
# 单元里的 User=robot 同样是写死的。留这个覆盖是给"客户那边账号不叫 robot"
# 用的,但**用了就得同时改单元里的 User=**,否则服务起来的身份跟目录属主对不上。
用户=${D1MAX_USER:-robot}
包=${1:-}

说() { printf '\n>>> %s\n' "$*"; }

# pip 会往它装的那份源目录里写东西(见 2/7 的注释),所以每次都从临时副本装。
# 两个临时目录统一在这儿清 —— 中途 set -e 退出、被 Ctrl+C 打断也清得掉。
临时包=
临时槽包=
清理() {
  [[ -n "$临时包" ]] && rm -rf "$临时包"
  [[ -n "$临时槽包" ]] && rm -rf "$临时槽包"
  return 0
}
trap 清理 EXIT

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
# **从一份临时副本装,绝不直接 pip install "$包"。** pyproject.toml 用的是
# setuptools.build_meta,pip 对本地目录做的是就地构建:会往源目录里写
# *.egg-info/ 和 __pycache__/。而下一步 release install 要对 "$包" 算一遍
# tree_sha256 跟包里 release.json 记的哈希对账 —— 源目录被写脏了,哈希当场
# 对不上,verify_package 报错,set -e 把装机整个中止在这一步。
临时包=$(mktemp -d)
cp -a "$包/." "$临时包/"
"$根/bin/python" -m pip install --quiet "$临时包"

说 "3/7 把包落进槽里"
sudo -u "$用户" env D1MAX_RELEASE_ROOT="$根" \
  "$根/bin/python" -m d1max_patrol.cli release install "$包"

说 "4/7 给这一版建自己的 venv"
# **每版一个 venv,不是服务共用根下那个解释器。** 服务单元的 ExecStart
# 指着 <槽>/venv/bin/python,这个解释器只能在这儿建 —— stage() 只是
# shutil.copytree,不会替我们建。选每版一个 venv 而不是共用根解释器的
# 理由:双槽的意义之一就是回滚要把依赖也一并退回去,依赖装在共用解释器
# 里的话,回滚只退代码不退依赖,退了也是白退。
# **装的是 "$包" 的一份临时副本,不是 "$槽" 本身。** pip 就地构建会往它装的
# 那份源目录里写 *.egg-info/ 和 __pycache__/;直接装 "$槽" 的话,落槽那一刻
# 算过的 tree_sha256 就跟盘上的字节对不上了,以后任何一次重新校验都会判这
# 一版坏掉。装出来的东西跟装 "$槽" 一模一样(两者是同一份内容),而 "$槽"
# 保持跟落槽那一刻逐字节一致。
名字=$(basename "$包")
槽="$根/releases/$名字"
if [[ ! -x "$槽/venv/bin/python" ]]; then
  临时槽包=$(mktemp -d)
  cp -a "$包/." "$临时槽包/"
  # pip 跑在 "$用户" 身份下,临时目录默认是 root 的 0700,不改属主它读不进去。
  chown -R "$用户":"$用户" "$临时槽包"
  sudo -u "$用户" python3 -m venv "$槽/venv"
  sudo -u "$用户" "$槽/venv/bin/python" -m pip install --quiet --upgrade pip
  sudo -u "$用户" "$槽/venv/bin/python" -m pip install --quiet "$临时槽包"
fi

说 "5/7 装 systemd 单元与环境文件"
install -m 0644 "$(dirname "$0")/d1max-patrol.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/d1max-bootguard.service" /etc/systemd/system/
# D1MAX_SN 的唯一来源。**已经存在就绝不覆盖** —— 现场填过的值不能被
# 重跑抹掉。两个 systemd 单元都用 EnvironmentFile=-/etc/d1max/env 读它,
# 7/7 步里这个脚本自己也 source 同一份文件再把 D1MAX_SN 显式传给
# release activate,两条路必须是同一个来源,否则重启后自检第四项
# (identity)会假失败,把一版好的自动回滚掉。
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

# 手机 app 连这台机器要输的设备 PIN。装机脚本第一次写这份文件时**随机生成
# 一个填在下面,不留空**:服务单元的 ExecStart 带 --host 0.0.0.0(手机 app
# 连的是 192.168.168.100:8095,只听 127.0.0.1 的话它根本连不上),而
# check_exposure() 见到非本机地址又没有 PIN 会直接 SystemExit 拒绝启动 ——
# 配上 Restart=always + RestartSec=5,那就是每 5 秒刷一条日志的启动循环。
# **这一行和单元里那个 --host 0.0.0.0 是一对,拆不开。**
# 想换成好记的:直接改下面这一行,再 sudo systemctl restart d1max-patrol。
环境模板
  # 用根下那个跟版本无关的解释器生成,不依赖机器上有没有别的 python。
  设备PIN=$("$根/bin/python" -c 'import secrets; print(f"{secrets.randbelow(10**6):06d}")')
  printf 'D1MAX_PIN=%s\n' "$设备PIN" >> /etc/d1max/env
  说 "这台机器的设备 PIN 是 $设备PIN"
  echo "  手机 app 第一次连这台机器要输它。**记进现场登记表**,屏幕关了就找不回来了"
  echo "  (还能在机器上看:sudo grep D1MAX_PIN /etc/d1max/env)。"
  echo "  想换成好记的:编辑 /etc/d1max/env 里的 D1MAX_PIN= 那一行,再"
  echo "  sudo systemctl restart d1max-patrol.service。"
fi
systemctl daemon-reload
systemctl enable d1max-bootguard.service d1max-patrol.service

说 "6/7 记下这台有没有装上装"
cat <<'提示'
  这一步没法替你做,因为只有站在机器旁边的人看得见(§7.1)。
  装完之后在手机 app 或者用 curl 记一次:

    curl -X PUT http://<机器 IP>:8095/api/identity/payload \
      -H 'Content-Type: application/json' \
      -d '{"has_payload": false, "by": "你的名字",
           "confirm": "我知道这会改掉升级方式"}'

  没记过之前,这台机器**不许升级** —— 因为升级流程不知道该怎么重启它。
提示

说 "7/7 切到刚装的这一版并起服务"
# **把 /etc/d1max/env 读进这个脚本自己的环境。** sudo 默认 env_reset,
# D1MAX_SN 不在 env_keep 里,而 root 自己的环境里本来也没有它 —— 不 source
# 的话,下面 release activate 拿到的是空值,cli.py 的 resolve() 会落到设备树
# /MAC 兜底,而服务侧经 EnvironmentFile= 拿到的是人填的真值。两个值一不一样,
# 重启后自检第四项(identity)就恒红,postcheck_verdict 判 ROLLBACK,一版好的
# 被退回去。**docs/装机清单.md 里"不填 SN 会假失败"那段话,正是因为这里
# source 了这份文件、并在下面把变量显式传了下去,才是成立的。**
set -a
[[ -f /etc/d1max/env ]] && . /etc/d1max/env
set +a

# **PIN 空着就停在这儿,不 restart。** 跟下面 SN 那条警告不是一回事:
# SN 空只是"可能"被误判回滚,PIN 空是必定起不来。单元的 ExecStart 带
# --host 0.0.0.0,check_exposure() 见到非本机地址又没 PIN 会 SystemExit;
# 配上 Restart=always + RestartSec=5,机器会变成每 5 秒刷一条日志的启动
# 循环,现场看到的是一堵日志墙,比停下来喊一声糟得多。
if ! grep -qE '^D1MAX_PIN=.+' /etc/d1max/env 2>/dev/null; then
  echo "错误: /etc/d1max/env 里的 D1MAX_PIN 是空的,服务起不来。" >&2
  echo "      单元的 ExecStart 带 --host 0.0.0.0(手机 app 要连它)," >&2
  echo "      而没有 PIN 的话服务会拒绝启动;Restart=always + RestartSec=5" >&2
  echo "      会把它刷成每 5 秒一条日志的启动循环。" >&2
  echo "      先填好 D1MAX_PIN=(4 位以上,建议 6 位数字),再重跑这个脚本。" >&2
  echo "      **没有 restart**,现在跑着的还是上一版(如果有的话)。" >&2
  exit 3
fi

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
  # D1MAX_SN 必须显式带过去 —— sudo env_reset 会把上面 source 进来的它扔掉。
  sudo -u "$用户" env D1MAX_RELEASE_ROOT="$根" D1MAX_SN="${D1MAX_SN:-}" \
    "$根/bin/python" -m d1max_patrol.cli release activate "$名字"
fi
systemctl restart d1max-patrol.service

说 "装完了。看一眼:"
echo "  systemctl status d1max-patrol"
echo "  curl http://127.0.0.1:8095/api/release"
echo "  curl http://127.0.0.1:8095/api/selfcheck"
echo "  curl http://127.0.0.1:8095/api/identity"
