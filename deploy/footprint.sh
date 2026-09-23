#!/usr/bin/env bash
# =====================================================================
# footprint.sh —— 装机前后的足迹对照:证明我们没有覆盖别人的文件
#
#   为什么要有它:静态审计(读脚本、读测试)只能证明**我们打算写哪儿**,
#   证明不了那台机器上实际发生了什么。这个脚本是"我们没覆盖别人的文件"
#   这句话在现场唯一的证据。
#
#   用法:
#     sudo bash deploy/footprint.sh snapshot <输出文件>   # 装机前跑
#     sudo bash deploy/footprint.sh diff <装机前的文件>    # 装机后跑
#
#   输出文件不给的话,默认落在 runs/footprint/ 下,文件名带 UTC 时间戳。
#   **这个文件要留着** —— 它是装机前那一刻的盘上状态,事后补不出来。
#
#   本脚本【只读】:被勘察的那几处目录,一个字节都不写。它唯一写盘的地方
#   是你用参数指定的那个输出文件(默认在当前目录的 runs/ 底下)。
#
#   故意不用 set -e:每一条采集都要兜底,一条采不到不能让整个脚本退出
#   (跟 scripts/orin-survey.sh 的 run() 同一条理由 —— 少采一条的代价是
#   再跑一趟,中途 abort 的代价是整份快照都没了)。
#
#   **变量名一律用 ASCII。** bash 的标识符只认 ASCII 字母和下划线:
#   `根=/opt/d1max` 这种写法不是赋值,而是一条"找不到的命令",配上
#   set -e 会让脚本停在那一行。注释和屏幕上的话照旧写中文。
# =====================================================================
set -u

# ------------------------------------------------------------ 勘察范围
# 格式是 "目录:深度"。**每一处都要有深度上限** —— 全盘 find / 在 Orin 上
# 要跑好几分钟,现场时间比结果贵;而我们要看的那几处都很浅。
#   /etc/systemd/system 深度 2:单元文件在第 1 层,*.wants/ 里的链在第 2 层。
#   /opt 深度 2:第 1 层是各家的根(我们的、别人的),第 2 层够看出哪一家动了。
#   /etc 深度 1:只看顶层多没多出来东西,底下各自的配置不归这个脚本管。
#   /usr/local/bin 深度 1、/usr/local/lib 深度 2(lib 底下还有一层 pythonX)。
SURVEY_DIRS=(
  "/etc/systemd/system:2"
  "/opt:2"
  # W01 之后巡检数据根在 /var/lib/d1max;装机会在这儿建目录,勘察范围要跟上,
  # 不然装机前后的 diff 证明不了"只动了声明过的那几处"。深度 1 只看到 d1max 这一层。
  "/var/lib:1"
  "/etc:1"
  "/usr/local/bin:1"
  "/usr/local/lib:2"
  # W01b:特权助手与 sudo 白名单各占一处,深度 1 就看得见文件本身。
  "/usr/local/sbin:1"
  "/etc/sudoers.d:1"
)

# 我们自己占的四个端口。装机**之前**它们就有人占着的话,那是装机前就该发现
# 的冲突,不是装完才发现 —— snapshot 会当场在屏幕上喊出来。
OUR_PORTS=(8090 8091 8092 8095)

# 想顺带盯住别人的端口就填这个变量,空格分开:
#   D1MAX_FOOTPRINT_PORTS='7447 8554' sudo bash deploy/footprint.sh snapshot x.txt
# **脚本里不写死任何一个别人的端口号。** 有 ss / netstat 的机器上整张监听表
# 本来就会被一行不落地记下来,别人的端口自己会出现在快照里;只有在连
# ss 和 netstat 都没有的机器上,才需要靠这个变量逐个去探。
EXTRA_PORTS=${D1MAX_FOOTPRINT_PORTS:-}

# 勘察范围也可以整个换掉(空格分开的 "目录:深度")。**这是给测试和特殊现场
# 留的口子**,正常装机不要用它 —— 换掉之后这份快照就不再是那五处的证据了。
if [ -n "${D1MAX_FOOTPRINT_DIRS:-}" ]; then
  # 故意不加引号:这个变量装的是好几个条目。
  # shellcheck disable=SC2206
  SURVEY_DIRS=(${D1MAX_FOOTPRINT_DIRS})
fi

DEFAULT_OUT_DIR=runs/footprint

# ------------------------------------------------------------ 兜底工具
say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

usage() {
  warn "用法:"
  warn "  sudo bash $0 snapshot [输出文件]   # 装机前跑,记下盘上现在是什么样"
  warn "  sudo bash $0 diff <装机前的文件>    # 装机后跑,跟那一刻比"
}

# GNU find 才有 -printf。busybox / BSD 的 find 没有,那时候退到 stat 那条路。
# **只探一次**,免得每处目录都探一遍。
if find /dev/null -maxdepth 0 -printf '' >/dev/null 2>&1; then
  HAS_PRINTF=1
else
  HAS_PRINTF=0
fi

# 采一处目录的文件清单。输出一行一个条目:
#   FILE|<路径>|<mtime 秒>|<字节数>|<类型 d/f/l>
# 用 '|' 分隔是为了路径里有空格也不会错位,同时 awk -F'|' 直接能吃。
scan_dir() {
  local root=$1 depth=$2 p m s t
  [ -e "$root" ] || return 0
  if [ "$HAS_PRINTF" = 1 ]; then
    # %T@ 带小数(纳秒),两次采集之间没有意义还碍眼,截掉小数部分。
    timeout 20 find "$root" -maxdepth "$depth" -printf 'FILE|%p|%T@|%s|%y\n' 2>/dev/null \
      | awk -F'|' -v OFS='|' '{ sub(/\.[0-9]*$/, "", $3); print }' 2>/dev/null
    return 0
  fi
  timeout 30 find "$root" -maxdepth "$depth" 2>/dev/null | while IFS= read -r p; do
    m=$(stat -c %Y "$p" 2>/dev/null) || m='?'
    s=$(stat -c %s "$p" 2>/dev/null) || s='?'
    if [ -L "$p" ]; then t=l; elif [ -d "$p" ]; then t=d; else t=f; fi
    printf 'FILE|%s|%s|%s|%s\n' "$p" "$m" "$s" "$t"
  done
}

# 现在有谁在听。**整张表都记**,不只是我们那几个 —— 别人的端口哪天从表上
# 消失了,diff 会当场看见,而我们不用在交付脚本里写死人家的端口号。
# 认得到就把端口号用空格打到 stdout 并返回 0;两条路都不通就返回 1。
listening_ports() {
  local raw=
  raw=$(ss -ltn 2>/dev/null) || raw=
  case "$raw" in *LISTEN*) ;; *) raw= ;; esac
  if [ -z "$raw" ]; then
    raw=$(netstat -ltn 2>/dev/null) || raw=
    # **必须见到 LISTEN 才当真。** 有的系统上 netstat 是个同名的别家程序,
    # 认不得这几个参数,吐一段用法出来 —— 拿它去抽端口号会抽出一堆假的。
    case "$raw" in *LISTEN*) ;; *) raw= ;; esac
  fi
  [ -n "$raw" ] || return 1
  printf '%s\n' "$raw" \
    | awk '{ n=split($4, a, ":"); if (a[n] ~ /^[0-9]+$/) print a[n] }' 2>/dev/null \
    | LC_ALL=C sort -un 2>/dev/null | tr '\n' ' '
  return 0
}

# 端口那一段。两条路都要有兜底:有 ss/netstat 就读整张表,都没有就用
# bash 自带的 /dev/tcp 逐个探我们关心的那几个。
scan_ports() {
  local heard= p
  if heard=$(listening_ports); then
    for p in $heard; do
      printf 'PORT|%s|listen\n' "$p"
    done
    for p in "${OUR_PORTS[@]}" $EXTRA_PORTS; do
      case " $heard " in
        *" $p "*) ;;
        *) printf 'PORT|%s|free\n' "$p" ;;
      esac
    done
  else
    warn "  (这台机器上 ss 和 netstat 都不认得,端口改用 /dev/tcp 逐个探 —— 只探得到点名的那几个)"
    for p in "${OUR_PORTS[@]}" $EXTRA_PORTS; do
      if timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$p" 2>/dev/null; then
        printf 'PORT|%s|listen\n' "$p"
      else
        printf 'PORT|%s|free\n' "$p"
      fi
    done
  fi
}

# 一次完整采集。**排序稳定** —— 两份快照可以直接拿 diff 比。
collect() {
  local item
  printf '# D1 Max 装机足迹快照\n'
  printf '# 采集时间(UTC): %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')"
  printf '# 主机: %s\n' "$(hostname 2>/dev/null || echo '?')"
  printf '# 一行一个条目, 已排序 —— 可以直接拿 diff 比\n'
  {
    for item in "${SURVEY_DIRS[@]}"; do
      scan_dir "${item%%:*}" "${item##*:}"
    done
    scan_ports
  } | LC_ALL=C sort -u
}

# ------------------------------------------------------------ snapshot
do_snapshot() {
  local out=${1:-} clash=0 p
  if [ -z "$out" ]; then
    out="$DEFAULT_OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo now)-footprint.txt"
  fi
  mkdir -p "$(dirname "$out")" 2>/dev/null || true
  collect > "$out" || { warn "!! 快照没写成:$out"; return 1; }

  say "快照写好了:$out"
  say "  条目数:$(grep -c '^FILE|' "$out" 2>/dev/null || echo '?')"
  say "  **这个文件要留着** —— 装完之后拿它跑 diff。"

  # 装机之前我们的端口就有人占着,那是现在就该处理的事。
  for p in "${OUR_PORTS[@]}"; do
    if grep -q "^PORT|$p|listen$" "$out" 2>/dev/null; then
      warn ""
      warn "!!!! 端口 $p 装机前就有人占着 !!!!"
      warn "     这是装机**前**就该发现的冲突。先弄清楚是谁在听(ss -ltnp | grep $p),"
      warn "     再决定是停掉它还是给我们换端口 —— 等装完才发现的话,现场看到的是"
      warn "     服务起不来,而日志里只有一行 address already in use。"
      clash=1
    fi
  done
  if [ "$clash" = 0 ]; then
    say "  我们那四个端口现在都空着,可以装。"
  fi
  return 0
}

# ------------------------------------------------------------ diff
do_diff() {
  local old=${1:-}
  if [ -z "$old" ] || [ ! -f "$old" ]; then
    warn "!! 要一个装机前的快照文件当参数,而 '$old' 不是。"
    usage
    return 2
  fi
  # awk 的标识符同样只能是 ASCII,所以这一段里的函数名和变量名是英文的;
  # 整段用单引号裹着,**里面一个单引号都不能出现**。
  collect | awk -F'|' '
function is_ours(p) {
  if (p ~ /^\/opt\/d1max($|\/)/) return 1
  if (p ~ /^\/etc\/d1max($|\/)/) return 1
  if (p ~ /^\/etc\/systemd\/system\/d1max-[^\/]*\.service$/) return 1
  if (p ~ /^\/etc\/systemd\/system\/[^\/]*\.target\.wants\/d1max-[^\/]*\.service$/) return 1
  return 0
}
# 被勘察的那几处目录**自己**。在它们底下加一个文件, 父目录的 mtime 必然跟着
# 变 —— 那不是"我们改了别人的文件", 那条变化的真正内容会以一条独立的
# "新增" 或 "消失" 条目出现在同一份报告里。所以这里单列一档, 不判红。
function is_parent(p) {
  if (p == "/etc" || p == "/opt") return 1
  if (p == "/etc/systemd/system") return 1
  if (p == "/usr/local/bin" || p == "/usr/local/lib") return 1
  if (p ~ /^\/etc\/systemd\/system\/[^\/]*\.target\.wants$/) return 1
  return 0
}
function verdict(p) {
  if (is_ours(p)) return "[ours]"
  if (is_parent(p)) return "[ours] (被勘察目录自己的 mtime, 真正的变化另有一条)"
  bad++
  return "[!! 不是我们的]"
}
function section(title, tab, n,   i) {
  print ""
  print "=== " title " (" n ") ==="
  if (n == 0) { print "  (没有)"; return }
  for (i = 1; i <= n; i++) print "  " verdict(tab[i]) " " tab[i]
}
NR == FNR {
  if ($1 == "FILE") { of[$2] = $3 "|" $4 }
  else if ($1 == "PORT") { op[$2] = $3 }
  next
}
{
  if ($1 == "FILE") {
    nf[$2] = $3 "|" $4
    if (!($2 in of)) add[++na] = $2
    else if (of[$2] != nf[$2]) chg[++nc] = $2
  } else if ($1 == "PORT") {
    np[$2] = $3
    if (!($2 in op)) padd[++npa] = $2
    else if (op[$2] != np[$2]) pchg[++npc] = $2
  }
}
END {
  for (p in of) if (!(p in nf)) gone[++ng] = p
  section("新增的", add, na)
  section("被改动的 (mtime 或 size 变了)", chg, nc)
  section("消失的", gone, ng)
  print ""
  print "=== 端口 ==="
  if (npa == 0 && npc == 0) {
    print "  (跟装机前一样)"
  } else {
    for (i = 1; i <= npa; i++) print "  新出现: 端口 " padd[i] " " np[padd[i]]
    for (i = 1; i <= npc; i++) {
      print "  变了: 端口 " pchg[i] " " op[pchg[i]] " -> " np[pchg[i]]
      if (op[pchg[i]] == "listen" && np[pchg[i]] != "listen") {
        print "        ^^ 装机前有人听、现在没人听了 —— 这一条要当回事"
        bad++
      }
    }
  }
  print ""
  print "================================================================"
  if (bad == 0) {
    print "结论: 盘上的变化全都落在我们自己那几处 —— 没有覆盖到别人的文件。"
    exit 0
  }
  print "结论: 有 " bad " 处变化不在我们的白名单里。上面打了 [!! 不是我们的]"
  print "      的每一条都要人去看一眼: 要么是我们写错了地方, 要么是这段时间里"
  print "      别人(别家的栈、apt、OTA)也动了盘 —— 两种都不能默认当成没事。"
  exit 1
}
' "$old" -
}

# ------------------------------------------------------------ 入口
SUBCMD=${1:-}
case "$SUBCMD" in
  snapshot) do_snapshot "${2:-}" ;;
  diff)     do_diff "${2:-}" ;;
  *)        usage; exit 2 ;;
esac
