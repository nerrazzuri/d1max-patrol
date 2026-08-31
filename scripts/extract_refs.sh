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
find "$TMP" -type d -name 'max_description' -exec cp -r {} "$DEST/urdf/" \;

# RobotSDK 在 zip 里是 **tar 包**(RobotSDK-<ver>.tar.gz),不是目录。
# 早先这里写的是 `find -maxdepth 4 -type d -name 'RobotSDK-*'`,-type d 永远
# 匹配不到一个 .tar.gz,所以 refs/robot-sdk/ 一直是空的、而且不报错。
# 别改回 -type d。
find "$TMP" -type f -name 'RobotSDK-*.tar.gz' -print0     | while IFS= read -r -d '' tarball; do
        tar xzf "$tarball" -C "$DEST/robot-sdk/"
    done

# 解出来必须非空 —— 上一版的失败模式就是"静默产出空目录"。
if [ -z "$(ls -A "$DEST/robot-sdk")" ]; then
    echo "错误: refs/robot-sdk/ 解包后为空" >&2
    exit 1
fi

echo "refs/ 已重建于 $DEST"
