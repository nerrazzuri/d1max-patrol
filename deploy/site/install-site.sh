#!/usr/bin/env bash
# D1 Max 站点主机安装(W00c1)。在**站点主机**(Ubuntu 22.04,x86 或 ARM)上以 root 跑;
# 不是狗上的 deploy/install.sh,两者互不调用。
#
#   sudo bash deploy/site/install-site.sh --site-id estate-1 --hostname site.local --hostname 192.168.1.10
#
# 做的事:装 mosquitto/openssl/python3-venv → 建系统用户 d1max-site → /opt/d1max-site/venv 装
# contract[mqtt] 与 site-node → 没 init 过就 `d1max-site init` → 装两个单元并启用。
# 之后:`d1max-site add-admin <名字>`、`d1max-site enroll <robot_id>`(见文末提示)。
set -euo pipefail

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_DIR=/var/lib/d1max-site
VENV=/opt/d1max-site/venv
UNIT_DIR=/etc/systemd/system
SITE_ID=""
HOSTS=()
BROKER_PORT=8883

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-id) SITE_ID="$2"; shift 2 ;;
    --hostname) HOSTS+=("$2"); shift 2 ;;
    --broker-port) BROKER_PORT="$2"; shift 2 ;;
    *) echo "不认识的参数: $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "要 root(sudo)" >&2; exit 1; }
[[ -n "$SITE_ID" && ${#HOSTS[@]} -gt 0 ]] || {
  echo "用法: install-site.sh --site-id <id> --hostname <名字或IP> [--hostname …]" >&2; exit 2; }

echo "[1/5] 系统包"
apt-get install -y mosquitto openssl python3-venv
# 发行版自带的 mosquitto 实例我们不用(配置冲突、多开一个端口),停掉;我们跑自己的单元。
systemctl disable --now mosquitto.service 2>/dev/null || true

echo "[2/5] 用户与目录"
id -u d1max-site >/dev/null 2>&1 || useradd --system --home-dir "$HOME_DIR" --shell /usr/sbin/nologin d1max-site
install -d -m 0750 -o d1max-site -g d1max-site "$HOME_DIR"

echo "[3/5] Python 环境"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade "$PKG/packages/contract[mqtt]" "$PKG/packages/site-node"

echo "[4/5] 站点目录"
if [[ ! -f "$HOME_DIR/site.json" ]]; then
  args=(--home "$HOME_DIR" init --site-id "$SITE_ID" --broker-port "$BROKER_PORT")
  for h in "${HOSTS[@]}"; do args+=(--hostname "$h"); done
  runuser -u d1max-site -- "$VENV/bin/d1max-site" "${args[@]}"
else
  echo "  已 init 过,跳过(不重建 CA:重建等于让所有狗失联)"
fi

echo "[5/5] systemd 单元"
install -m 0644 "$PKG/deploy/site/d1max-mosquitto.service" "$UNIT_DIR/"
install -m 0644 "$PKG/deploy/site/d1max-site.service" "$UNIT_DIR/"
systemctl daemon-reload
systemctl enable --now d1max-mosquitto.service d1max-site.service

cat <<TXT
装好了。接下来:
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR add-admin <名字>
  sudo -u d1max-site $VENV/bin/d1max-site --home $HOME_DIR enroll <robot_id>
  证书包在 $HOME_DIR/ca/issued/<robot_id>/,拷到狗上:
    ca.crt robot.crt robot.key → /etc/d1max/tls/   registration.json → /etc/d1max/
TXT
