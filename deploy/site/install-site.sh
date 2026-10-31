#!/usr/bin/env bash
# D1 Max 站点主机安装(W00c1)。在**站点主机**(Ubuntu 22.04,x86 或 ARM)上以 root 跑;
# 不是狗上的 deploy/install.sh,两者互不调用。
#
#   sudo bash deploy/site/install-site.sh --site-id estate-1 --hostname site.local --hostname 192.168.1.10
#
# 做的事:装 mosquitto/openssl/python3-venv/ffmpeg → 建系统用户 d1max-site → /opt/d1max-site/venv 装
# contract[mqtt] 与 site-node → 没 init 过就 `d1max-site init` → 装两个单元并启用。
# 之后:`d1max-site add-admin <名字>`、`d1max-site enroll <robot_id>`(见文末提示)。
#
# **只升级软件**(W55;远程运维的「升级站点」由特权执行器这样调,也可以手工跑):
#   sudo bash deploy/site/install-site.sh --upgrade
# 不装系统包(站点可能不能上外网)、不 init、不动密钥;装新的 Python 包、服务单元、特权执行器,再起服务。
#
# **恢复到新主机**(A2,docs/备份恢复与灾备演练.md):
#   sudo bash deploy/site/install-site.sh --recover
# 只装软件、用户、目录、服务单元:**不生成密钥**(三把要先从离线另存的那份放回 /etc/d1max-site/,
# 没放回就停下)、**不 init**(不建新的站点身份)、**不起服务**。之后 `d1max-site restore`,再起服务。
set -euo pipefail

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_DIR=/var/lib/d1max-site
VENV=/opt/d1max-site/venv
UNIT_DIR=/etc/systemd/system
SITE_ID=""
HOSTS=()
BROKER_PORT=8883
RECOVER=0
UPGRADE=0
NO_VPN=0
KEYS_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-id) SITE_ID="$2"; shift 2 ;;
    --hostname) HOSTS+=("$2"); shift 2 ;;
    --broker-port) BROKER_PORT="$2"; shift 2 ;;
    --recover) RECOVER=1; shift ;;
    --upgrade) UPGRADE=1; shift ;;
    --no-vpn) NO_VPN=1; shift ;;
    --keys) KEYS_FILE="$2"; shift 2 ;;
    *) echo "不认识的参数: $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "要 root(sudo)" >&2; exit 1; }
if [[ $UPGRADE -eq 1 ]]; then
  [[ -f "$HOME_DIR/site.json" ]] || { echo "--upgrade 只用在已经装好的站点上($HOME_DIR 没有站点)" >&2; exit 2; }
elif [[ $RECOVER -eq 1 && -n "$KEYS_FILE" ]]; then
  [[ -f "$KEYS_FILE" ]] || { echo "托管包 $KEYS_FILE 不在" >&2; exit 2; }
  echo "  恢复模式:密钥从托管包放回(装好软件以后,会问托管包口令)"
elif [[ $RECOVER -eq 1 ]]; then
  # 恢复:三把密钥必须先放回(换了钥匙,备份解不开、库里的口令解不开、封着的证据解不开)
  for k in secrets backup evidence; do
    [[ -f /etc/d1max-site/$k.key ]] || {
      echo "恢复要先把离线另存的 /etc/d1max-site/$k.key 放回来(不会生成新的)" >&2; exit 2; }
  done
  [[ ! -f "$HOME_DIR/site.json" ]] || {
    echo "$HOME_DIR 已经有站点了:恢复只往空的站点目录里放" >&2; exit 2; }
else
  [[ -n "$SITE_ID" && ${#HOSTS[@]} -gt 0 ]] || {
    echo "用法: install-site.sh --site-id <id> --hostname <名字或IP> [--hostname …]" >&2
    echo "  恢复到新主机: install-site.sh --recover" >&2; exit 2; }
fi

echo "[1/5] 系统包"
if [[ $UPGRADE -eq 1 ]]; then
  echo "  只升级软件:不装系统包"
else
  apt-get update
  apt-get install -y mosquitto openssl python3-venv ffmpeg chrony   # ffmpeg:视频经站点(W00c5b);chrony:给狗对时(W09d)
  # W55:站点 VPN 的工具(工程师远程进来、4G 上的机器人连回站点);客户要求只在局域网的加 --no-vpn
  if [[ $NO_VPN -eq 0 ]]; then
    apt-get install -y wireguard-tools || echo "  !! 没装上 wireguard-tools:VPN 用不了(局域网照常)" >&2
  fi
fi
# 发行版自带的 mosquitto 实例我们不用(配置冲突、多开一个端口),停掉;我们跑自己的单元。
systemctl disable --now mosquitto.service 2>/dev/null || true

# 站点主机当狗的时间服务器(W09d):只给私网发;自己连不上外网也照样发它的钟(狗跟站点对上最要紧)。
install -d -m 0755 /etc/chrony/conf.d
cat > /etc/chrony/conf.d/d1max-site.conf <<'对时'
# D1 Max 站点(W09d):给现场局域网里的机器人发时间
allow 10.0.0.0/8
allow 172.16.0.0/12
allow 192.168.0.0/16
local stratum 10
对时
systemctl restart chrony

echo "[2/5] 用户与目录"
id -u d1max-site >/dev/null 2>&1 || useradd --system --home-dir "$HOME_DIR" --shell /usr/sbin/nologin d1max-site
# A1:诊断包要带站点自己的日志,站点用户得在 systemd-journal 组里才读得到;每次都补(老站点升级也补上)。
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -aG systemd-journal d1max-site
fi
install -d -m 0750 -o d1max-site -g d1max-site "$HOME_DIR"

# W30(决策 43):口令密钥、备份密钥 —— 不跟库放一起(站点服务对 /etc 只读),只有站点用户能读。
# **已经有了绝不重新生成**:换了钥匙,库里加密的口令、以前的加密备份就都打不开了。
install -d -m 0750 -o root -g d1max-site /etc/d1max-site
# W30b(决策 51):证据私钥(X25519,32 字节随机数):狗用它的公钥封照片、录像,站点收齐了解开。
# 恢复模式(A2、A3)**绝不生成**:密钥要么先放回了,要么下一步从托管包放回 —— 这里生成一把新的,
# 托管包就导不回来(不一样的不盖),旧备份也解不开。
for k in secrets backup evidence; do
  if [[ ! -f /etc/d1max-site/$k.key ]]; then
    [[ $RECOVER -eq 1 ]] && continue
    ( umask 077; head -c 32 /dev/urandom > /etc/d1max-site/$k.key )
    echo "  生成了 /etc/d1max-site/$k.key"
  fi
  chown root:d1max-site /etc/d1max-site/$k.key
  chmod 0440 /etc/d1max-site/$k.key
done
# 发行公钥:仓库里有就装(登记发布包时先验签名;狗上也会验)
if [[ -f "$PKG/deploy/release-pub.pem" ]]; then
  install -m 0644 "$PKG/deploy/release-pub.pem" /etc/d1max-site/release-pub.pem
fi
# 换钥匙(A3):另外也认的发行公钥,跟包里的一样(包里删掉的这里也删)
if [[ -d "$PKG/deploy/release-pub.d" ]]; then
  install -d -m 0755 /etc/d1max-site/release-pub.d
  find /etc/d1max-site/release-pub.d -maxdepth 1 -type f -name '*.pem' -delete
  for k in "$PKG"/deploy/release-pub.d/*.pem; do
    [[ -f "$k" ]] && install -m 0644 "$k" /etc/d1max-site/release-pub.d/
  done
fi

# W55 特权执行器:站点服务(NoNewPrivileges)要 root 才能做的几件事(重启自己、恢复备份、升级、装 VPN 配置)
# 交给它。执行器和验签用的两个小模块拷到 root 属主的 /usr/local/lib/d1max/,不从站点可写的地方跑代码。
install -d -m 0755 -o root -g root /usr/local/lib/d1max /usr/local/lib/d1max/d1max_contract
install -m 0755 -o root -g root "$PKG/deploy/d1max-ops-runner" /usr/local/lib/d1max/ops-runner
install -m 0644 -o root -g root /dev/null /usr/local/lib/d1max/d1max_contract/__init__.py
for m in ed25519 relsign; do
  install -m 0644 -o root -g root "$PKG/packages/contract/src/d1max_contract/$m.py" /usr/local/lib/d1max/d1max_contract/
done
install -d -m 0755 -o root -g root /var/lib/d1max-ops
install -d -m 0700 -o d1max-site -g d1max-site /var/lib/d1max-ops/req
install -d -m 0750 -o root -g d1max-site /var/lib/d1max-ops/done
cat > /etc/d1max-ops.json <<OPS
{"profile": "site", "requester": "d1max-site", "spool": "/var/lib/d1max-ops",
 "site_home": "$HOME_DIR", "site_venv": "$VENV",
 "site_pubkey": "/etc/d1max-site/release-pub.pem"}
OPS
chmod 0644 /etc/d1max-ops.json

echo "[3/5] Python 环境"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade "$PKG/packages/contract[mqtt,planning]" "$PKG/packages/site-node"
if [[ $RECOVER -eq 1 && -n "$KEYS_FILE" ]]; then
  # A3:托管包里的密钥放回 /etc/d1max-site(root:d1max-site 0440);口令交互输入
  "$VENV/bin/d1max-site" --home "$HOME_DIR" keys-import "$KEYS_FILE"
  for k in secrets backup evidence; do
    [[ -f /etc/d1max-site/$k.key ]] || { echo "托管包里没有 $k.key" >&2; exit 2; }
  done
fi

echo "[4/5] 站点目录"
if [[ $RECOVER -eq 1 ]]; then
  echo "  恢复模式:不 init(站点身份从备份里恢复)"
elif [[ ! -f "$HOME_DIR/site.json" ]]; then
  args=(--home "$HOME_DIR" init --site-id "$SITE_ID" --broker-port "$BROKER_PORT")
  for h in "${HOSTS[@]}"; do args+=(--hostname "$h"); done
  runuser -u d1max-site -- "$VENV/bin/d1max-site" "${args[@]}"
else
  echo "  已 init 过,跳过(不重建 CA:重建等于让所有狗失联)"
fi

echo "[5/5] systemd 单元"
install -m 0644 "$PKG/deploy/site/d1max-mosquitto.service" "$UNIT_DIR/"
install -m 0644 "$PKG/deploy/site/d1max-site.service" "$UNIT_DIR/"
install -m 0644 "$PKG/deploy/d1max-ops.path" "$PKG/deploy/d1max-ops.service" "$UNIT_DIR/"
systemctl daemon-reload
systemctl enable --now d1max-ops.path                 # W55 特权执行器(只有请求来了才跑)
if [[ $RECOVER -eq 1 ]]; then
  systemctl enable d1max-mosquitto.service d1max-site.service      # 不起:先恢复
  cat <<TXT
软件装好了(恢复模式:没 init、没起服务)。接下来:
  接上备份盘,恢复:
    sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR restore <备份目录> --key /etc/d1max-site/backup.key
  恢复的结果 ok 为 true 了再起服务:
    sudo systemctl start d1max-mosquitto d1max-site
  自检,不能有 FAIL:
    sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR selfcheck
  跟换主机前记的那份清点比(站点证书指纹要一样):
    sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR inventory --compare <before.json>
步骤见 docs/备份恢复与灾备演练.md 第四节。
TXT
  exit 0
fi
systemctl enable --now d1max-mosquitto.service d1max-site.service
if [[ $UPGRADE -eq 1 ]]; then
  echo "升级好了(站点服务已起)。"
  exit 0
fi

cat <<TXT
装好了。接下来:
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR add-admin <名字>
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR selfcheck        # 不能有 FAIL
  开通一台狗(出一行 24 小时有效的开通码,狗上 provision --code 领证书包):
    sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR enroll <robot_id> --code

**三把密钥现在就离线另存一份**(U 盘、保险柜;三把都不在备份里):
  /etc/d1max-site/backup.key   备份是用它加密的:站点主机坏了、没有它,备份就解不开(d1max-site backup-open)
  /etc/d1max-site/secrets.key  库里的摄像头口令、事件源密钥是用它加密的:恢复到新主机时要放回原来这一把,
                               不然库能打开、口令解不开(摄像头连不上、事件源验签全拒)
  /etc/d1max-site/evidence.key 狗上的照片、录像是用它的公钥封的(W30b):丢了它,封着传上来、还没解开的
                               就再也解不开(d1max-site evidence-open 补解)
三把一起导成一个带口令的托管包更省事:
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR keys-export <U 盘上的文件>
恢复步骤见 docs/备份恢复与灾备演练.md 第四节。
TXT
if [[ $NO_VPN -eq 0 ]]; then
  cat <<TXT

工程师远程运维要站点 VPN(W55,决策 71;只放行工程师到 22 端口):初始化一次,填对外地址(域名或公网 IP + 端口,路由器上转发这个 UDP 端口):
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR vpn init --endpoint <域名或公网IP>          # UDP 端口默认 51820,--port 改
之后在网页「账号」给工程师开临时账号时勾选「同时发放 VPN 配置」。见 docs/远程运维手册.md。
TXT
fi
