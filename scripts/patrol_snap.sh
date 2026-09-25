#!/usr/bin/env bash
# =====================================================================
# patrol_snap.sh —— 巡检抓拍：抓前/后相机各一帧 + 当前地图坐标，落到
#   runs/inspection/<时间戳>/ 下（front.jpg / back.jpg / meta.json）。
#   分析交给 VLM（Claude）读这些帧出发现。配合 patrol_agent 定点巡检用。
#
#   用法: bash scripts/patrol_snap.sh [点位名]
#   前提: 已连狗热点(192.168.234.1)。当前坐标不读了（老看板 8095 随 W00c5e 退役），meta 里记空。
# =====================================================================
set -u
cd "$(dirname "$0")/.."
DOG=192.168.234.1
NAME="${1:-point}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="runs/inspection/${TS}_${NAME}"
mkdir -p "$OUT"

echo "[snap] 抓拍点 '$NAME' → $OUT"

# 1) 当前地图坐标：老看板退役后这里不再读，留空
POSE='{"ok":false}'

# 2) 抓前/后相机各一帧
grab(){ # $1=stream $2=outfile
  timeout 15 ffmpeg -y -rtsp_transport tcp -i "rtsp://$DOG:8554/$1" \
    -frames:v 1 -q:v 2 "$2" >/dev/null 2>&1 && echo "  ✓ $1 → $2" || echo "  ✗ $1 抓取失败"
}
grab front "$OUT/front.jpg"
grab back  "$OUT/back.jpg"

# 3) 元数据
cat > "$OUT/meta.json" <<JSON
{
  "point": "$NAME",
  "time_utc": "$TS",
  "map_frame": "loc_map",
  "pose": $POSE,
  "front": "front.jpg",
  "back": "back.jpg"
}
JSON

echo "[snap] 完成。分析: 让 Claude 读 $OUT/front.jpg 和 back.jpg"
echo "[snap] 坐标: $POSE"
ls -la "$OUT"
