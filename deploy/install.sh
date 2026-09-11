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
# @写盘 /opt/d1max/releases                                              版本槽,巡检数据也落在槽里
# @写盘 /opt/d1max/bin                                                   跟版本无关的解释器软链
# @写盘 /opt/d1max/bin-venv                                              上面那个解释器的 venv
# @写盘 /opt/d1max/current                                               release activate 摆的版本链
# @写盘 /opt/d1max/bundles                                               任务包目录,服务跑起来后落在根下
# @写盘 /opt/d1max/pending.json                                          在途升级标记,同上
# @写盘 /etc/d1max                                                       配置目录
# @写盘 /etc/d1max/env                                                   设备 PIN 在这个文件里
# @写盘 /etc/systemd/system/d1max-patrol.service                         install -m 0644 摆进去的单元
# @写盘 /etc/systemd/system/multi-user.target.wants/d1max-patrol.service systemctl enable 生成的自启链
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
  echo "用法: sudo bash install.sh <包目录>" >&2
  echo "例:   sudo bash install.sh /home/robot/d1max-2026-09-20-77b2de" >&2
  exit 2
fi

say "1/7 建目录 $ROOT"
mkdir -p "$ROOT/releases" "$ROOT/bin"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT"

say "2/7 装一个跟版本无关的解释器到 $ROOT/bin"
# 守卫要用它。**它不能在任何一版目录里** —— 版本坏了,救生索还得在。
if [[ ! -x "$ROOT/bin/python" ]]; then
  python3 -m venv "$ROOT/bin-venv"
  ln -sfn "$ROOT/bin-venv/bin/python" "$ROOT/bin/python"
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
TMP_PKG=$(mktemp -d)
cp -a "$PKG/." "$TMP_PKG/"
"$ROOT/bin/python" -m pip install --quiet $PIP_ARGS "$TMP_PKG"

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
REL_NAME=$(basename "$PKG")
SLOT="$ROOT/releases/$REL_NAME"
# **幂等的门看哨兵,不看解释器在不在。** python3 -m venv 一跑完,
# <槽>/venv/bin/python 就存在且可执行 —— 此后 pip 装到哪一步断掉(网断、
# 盘满、Ctrl+C),重跑都会判"已经建过了"把整段跳过,留下一个解释器在、
# 依赖不全的槽。脚本接着走到 7/7 restart,ExecStart 指的正是这个解释器,
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
  # **最后一行才落哨兵。** 上面任何一步断了,set -e 会在这之前就退出,
  # 哨兵不在,下一趟整段重来 —— 这就是"依赖装完没有"这个判据的全部。
  printf 'd1max_patrol %s\n' "$REL_NAME" > "$SENTINEL"
  chown "$RUN_USER":"$RUN_USER" "$SENTINEL"
fi

say "5/7 装 systemd 单元与环境文件"
install -m 0644 "$(dirname "$0")/d1max-patrol.service" /etc/systemd/system/
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
# 脚本自己也 source 同一份文件再把 D1MAX_SN 显式传给 release activate,
# 两条路必须是同一个来源,否则重启后自检第四项(identity)会假失败,
# 把一版好的自动回滚掉。
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
  DEVICE_PIN=$("$ROOT/bin/python" -c 'import secrets; print(f"{secrets.randbelow(10**6):06d}")')
  printf 'D1MAX_PIN=%s\n' "$DEVICE_PIN" >> /etc/d1max/env
  say "这台机器的设备 PIN 是 $DEVICE_PIN"
  echo "  手机 app 第一次连这台机器要输它。**记进现场登记表**,屏幕关了就找不回来了"
  echo "  (还能在机器上看:sudo grep D1MAX_PIN /etc/d1max/env)。"
  echo "  想换成好记的:编辑 /etc/d1max/env 里的 D1MAX_PIN= 那一行,再"
  echo "  sudo systemctl restart d1max-patrol.service。"
fi
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
CURRENT_REL=$(basename "$(readlink -f "$ROOT/current" 2>/dev/null || true)")
if [[ "$CURRENT_REL" == "$REL_NAME" ]]; then
  echo "  $REL_NAME 已经是在跑的那一版了,跳过切换。"
else
  # D1MAX_SN 必须显式带过去 —— sudo env_reset 会把上面 source 进来的它扔掉。
  sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" D1MAX_SN="${D1MAX_SN:-}" \
    "$ROOT/bin/python" -m d1max_patrol.cli release activate "$REL_NAME"
fi
systemctl restart d1max-patrol.service

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
