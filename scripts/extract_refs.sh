#!/usr/bin/env bash
# 从厂商原始压缩包重建 refs/ 目录。
# 用法: scripts/extract_refs.sh <存放原始压缩包的目录>
set -euo pipefail

SRC="${1:?用法: scripts/extract_refs.sh <压缩包目录>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/refs"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$DEST/nav-api" "$DEST/robot-sdk" "$DEST/urdf"

# 压缩包实际是 PKZIP 格式(非 tar 归档),GNU tar 无法读取,故用 unzip 解包。
# lib/<arch>/librobot_sdk.so 是符号链接,跨平台解包会失败,本卷用不到,直接排除。
unzip -q "$SRC/3.自主导航二开资料.zip" -d "$TMP" \
    -x '*/lib/*/librobot_sdk.so'

find "$TMP" -name '自主导航_WEBSOCKET_API.md' -exec cp {} "$DEST/nav-api/" \;
find "$TMP" -maxdepth 4 -type d -name 'RobotSDK-*' -exec cp -r {} "$DEST/robot-sdk/" \;
find "$TMP" -maxdepth 4 -type d -name 'max_description' -exec cp -r {} "$DEST/urdf/" \;

echo "refs/ 已重建于 $DEST"
