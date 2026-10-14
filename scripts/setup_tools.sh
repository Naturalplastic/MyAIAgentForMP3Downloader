#!/usr/bin/env bash
# 获取便携版 ffmpeg / ffprobe（无需 root）。
#
# 背景：本机 `sudo apt-get` 被容器的 "no new privileges" 标志阻止，
#       提权也无法解决，因此改用静态构建 + 项目内 .tools/ 目录。
#       这反而更好：不污染系统、版本可控、可用环境变量覆盖。
#
# 用法：bash scripts/setup_tools.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools"
MIRROR="https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0"

mkdir -p "$TOOLS"

fetch() {
  local name="$1" url="$2" gz="${3:-}"
  if [ -x "$TOOLS/$name" ]; then
    echo "  ✓ $name 已存在，跳过"
    return
  fi
  echo "  ↓ 下载 $name ..."
  if [ -n "$gz" ]; then
    curl -sL --max-time 600 "$url" | gunzip > "$TOOLS/$name"
  else
    curl -sL --max-time 600 -o "$TOOLS/$name" "$url"
  fi
  chmod +x "$TOOLS/$name"
  echo "  ✓ $name ($(du -h "$TOOLS/$name" | cut -f1))"
}

echo "=== 获取便携 ffmpeg 工具链 ==="
fetch ffmpeg  "$MIRROR/ffmpeg-linux-x64"      ""
fetch ffprobe "$MIRROR/ffprobe-linux-x64.gz" "gz"

echo
echo "=== 验证 ==="
"$TOOLS/ffmpeg"  -hide_banner -version | head -1
"$TOOLS/ffprobe" -hide_banner -version | head -1

echo
echo "=== 能力自检 ==="
cd "$ROOT" && python3 -m biliagent.cli caps
