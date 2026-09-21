#!/usr/bin/env bash
# D1 Max 独立录包(含 IMU)—— 标定包 & 覆盖包都用这个。
# 被动订阅,不碰控制权。Ctrl-C 或 SIGTERM 优雅停止(mcap 索引写完)。
# 在 Orin 本机跑最稳;笔记本跑需先配好 zenoh(见 config/zenoh_router.json5)。
set -uo pipefail

NAME="${1:-d1max}"                      # 包名前缀,例 calib / coverage
OUT_ROOT="${OUT_ROOT:-$HOME/d1max-bags}"
# ★ 关键:必须含 /front_lidar/imu(app 自带录包漏了它 → 标定包无法用 app 录)
TOPICS=(/front_lidar /front_lidar/imu /tf /tf_static)
# 备(以后双雷达):/rear_lidar /rear_lidar/imu

set +u; source /opt/ros/humble/setup.bash 2>/dev/null || { echo "无 /opt/ros/humble"; exit 1; }; set -u
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_zenoh_cpp}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-24}"
# 默认指向仓库里的 zenoh 路由配置(笔记本连狗用);已在环境里设了就用你的
if [ -z "${ZENOH_ROUTER_CONFIG_URI:-}" ]; then
  for c in "$PWD/config/zenoh_router.json5" "$(dirname "$0")/../../config/zenoh_router.json5"; do
    [ -f "$c" ] && export ZENOH_ROUTER_CONFIG_URI="$c" && break
  done
fi
echo "ZENOH_ROUTER_CONFIG_URI=${ZENOH_ROUTER_CONFIG_URI:-<未设,Orin本机跑可忽略>}"

mkdir -p "$OUT_ROOT"
# 磁盘检查(前雷达 ~20MB/s,留足)
FREE=$(df -Pm "$OUT_ROOT" | awk 'END{print $4}')
echo "可用磁盘: ${FREE} MB"; [ "$FREE" -lt 5000 ] && echo "⚠️ 磁盘 <5GB,注意!"

echo "检测 /front_lidar ..."
timeout 10 ros2 topic echo /front_lidar --once >/dev/null 2>&1 || { echo "!! 收不到 /front_lidar(确认狗开机 + zenoh)"; exit 2; }
echo "检测 /front_lidar/imu ..."
timeout 5 ros2 topic echo /front_lidar/imu --once >/dev/null 2>&1 || echo "⚠️ 收不到 IMU —— 标定包无效!覆盖包可继续"

BAG="$OUT_ROOT/${NAME}-$(date +%Y%m%dT%H%M%S)"
echo "=== 录制到 $BAG ==="
echo "话题: ${TOPICS[*]}"
echo ">>> 开始遥控/驱动。录完按 Ctrl-C 停(会等 mcap 写完索引)<<<"
exec ros2 bag record -s mcap -o "$BAG" "${TOPICS[@]}"
