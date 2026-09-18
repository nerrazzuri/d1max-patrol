#!/usr/bin/env bash
# One-click floor map from a ROS2 bag using MOLA LiDAR-Odometry.
# Output: trajectory (TUM), point cloud (PLY), clean wall-slab floor plan (PNG),
#         and a ROS map_server occupancy grid (PGM + YAML).
#
# Usage:
#   ./make_floor_map.sh <bag.mcap|bag_dir> [out_dir] [lidar_topic] [base_frame]
# Defaults: out_dir=./mola_out  lidar_topic=/front_lidar  base_frame=rslidar_head
#
# Prereqs: sudo apt install ros-humble-mola ros-humble-mola-lidar-odometry ros-humble-mola-input-rosbag2
set -euo pipefail
source /opt/ros/humble/setup.bash

BAG="${1:?need bag path}"
OUT="${2:-./mola_out}"
LIDAR="${3:-/front_lidar}"
BASE="${4:-rslidar_head}"
SHARE=/opt/ros/humble/share/mola_lidar_odometry
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
mkdir -p "$OUT"

echo "[1/4] MOLA LiDAR-odometry (LiDAR-only) ..."
export MOLA_LIDAR_TOPIC="$LIDAR" MOLA_TF_BASE_LINK="$BASE"
mola-lidar-odometry-cli \
  -c "$SHARE/pipelines/lidar3d-default.yaml" \
  --state-estimator-param-file "$SHARE/state-estimator-params/state-estimation-simple.yaml" \
  --input-rosbag2 "$BAG" \
  --lidar-sensor-label "$LIDAR" \
  --output-tum-path "$OUT/traj.tum" \
  --output-simplemap "$OUT/map.simplemap" \
  --progress-bar-period 10

echo "[2/4] simplemap -> metric map ..."
sm2mm -i "$OUT/map.simplemap" -o "$OUT/map.mm" --no-progress-bar

echo "[3/4] metric map -> PLY ..."
mm2ply -i "$OUT/map.mm" -o "$OUT/map.ply"   # writes <...>.ply_raw.ply

echo "[4/4] gravity-align -> floor plan PNG + ray-traced ROS PGM/YAML ..."
PLY="$OUT/map.ply_raw.ply"; [ -f "$PLY" ] || PLY="$OUT/map.ply"
python3 "$SCRIPT_DIR/render_floormap.py" "$PLY" "$OUT/floor" --traj "$OUT/traj.tum"

echo "DONE. Deliverables in $OUT:"
echo "  traj.tum  map.ply(_raw.ply)  floor_wallplan.png  floor.pgm  floor.yaml"
