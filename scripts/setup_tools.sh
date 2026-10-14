#!/usr/bin/env bash
#
# 获取便携版 ffmpeg / ffprobe 到项目内的 .tools/ 目录。
#
# 用途
#   .tools/ 已在 .gitignore 中排除（约 150 MB），不随仓库分发。
#   在新的机器或新的克隆上，需执行本脚本一次以获取外部工具。
#
# 特性
#   - 无需 root 权限，不修改系统目录
#   - 自动识别操作系统与 CPU 架构
#   - 幂等：已存在且可用的文件会跳过
#   - 原子安装：先下载到临时文件并校验，通过后才替换正式文件
#     避免中断产生的不完整文件被后续判断误认为"已安装"
#
# 用法
#   bash scripts/setup_tools.sh            # 获取（已存在则跳过）
#   bash scripts/setup_tools.sh --check    # 仅检查是否就绪，不下载
#   bash scripts/setup_tools.sh --force    # 强制重新下载
#
# 环境变量
#   FFMPEG_STATIC_MIRROR   覆盖镜像地址（网络受限时使用）
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools"
MIRROR="${FFMPEG_STATIC_MIRROR:-https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0}"

MODE="install"
case "${1:-}" in
  --check) MODE="check" ;;
  --force) MODE="force" ;;
  "")      MODE="install" ;;
  *) echo "未知参数: $1（可用: --check / --force）" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------------------
# 平台识别
# ---------------------------------------------------------------------------
detect_platform() {
  local os arch
  case "$(uname -s)" in
    Linux)  os="linux" ;;
    Darwin) os="darwin" ;;
    MINGW*|MSYS*|CYGWIN*) os="win32" ;;
    FreeBSD) os="freebsd" ;;
    *) echo "不支持的操作系统: $(uname -s)" >&2; return 1 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
    armv7l|armv6l|arm) arch="arm" ;;
    i386|i686) arch="ia32" ;;
    *) echo "不支持的 CPU 架构: $(uname -m)" >&2; return 1 ;;
  esac
  # 镜像站提供的组合有限，逐项校验
  case "$os-$arch" in
    linux-x64|linux-arm64|linux-arm|linux-ia32|\
    darwin-x64|darwin-arm64|\
    win32-x64|win32-ia32|\
    freebsd-x64) echo "$os-$arch" ;;
    *) echo "镜像站未提供 $os-$arch 的构建" >&2; return 1 ;;
  esac
}

PLATFORM="$(detect_platform)" || {
  echo "" >&2
  echo "替代方案：" >&2
  echo "  1. 使用系统已安装的 ffmpeg（程序会自动查找 PATH）" >&2
  echo "  2. 自行下载静态构建放入 .tools/ 目录" >&2
  echo "  3. 用环境变量指定路径：" >&2
  echo "       export BILIAGENT_FFMPEG=/path/to/ffmpeg" >&2
  echo "       export BILIAGENT_FFPROBE=/path/to/ffprobe" >&2
  exit 1
}

EXT=""
[ "${PLATFORM%%-*}" = "win32" ] && EXT=".exe"

echo "=== 便携 ffmpeg 工具链 ==="
echo "  平台   : $PLATFORM"
echo "  目标   : $TOOLS"
echo "  镜像   : $MIRROR"
echo

# ---------------------------------------------------------------------------
# 就绪检查
# ---------------------------------------------------------------------------
# 判定"可用"的标准不只是文件存在，还要能实际执行并输出版本号。
# 只看存在性会让被截断的下载文件被误认为安装成功。
is_usable() {
  local bin="$1"
  [ -x "$bin" ] || return 1
  "$bin" -hide_banner -version 2>/dev/null | grep -q "^ff" || return 1
  return 0
}

# --check 模式：只报告状态
if [ "$MODE" = "check" ]; then
  rc=0
  for name in ffmpeg ffprobe; do
    bin="$TOOLS/$name$EXT"
    if is_usable "$bin"; then
      printf "  %-8s 就绪   %s\n" "$name" "$($bin -hide_banner -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
    else
      printf "  %-8s 缺失   需执行: bash scripts/setup_tools.sh\n" "$name"
      rc=1
    fi
  done
  exit $rc
fi

# 写入权限早检查 —— 只读文件系统上应在下载前失败，而非下载后
mkdir -p "$TOOLS" 2>/dev/null || true
if [ ! -w "$TOOLS" ]; then
  echo "错误: 目录不可写: $TOOLS" >&2
  echo "  若文件系统为只读，请改用系统 ffmpeg 或设置 BILIAGENT_FFMPEG" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 下载与原子安装
# ---------------------------------------------------------------------------
TMPFILES=()
cleanup() {
  for f in "${TMPFILES[@]:-}"; do
    [ -n "$f" ] && rm -f "$f" 2>/dev/null || true
  done
}
trap cleanup EXIT

download() {
  local name="$1" url="$2" use_gzip="$3"
  local target="$TOOLS/$name$EXT"
  local tmp="$TOOLS/.download.$$.$name"

  if [ "$MODE" != "force" ] && is_usable "$target"; then
    echo "  $name 已就绪，跳过"
    return 0
  fi

  echo "  下载 $name ..."
  TMPFILES+=("$tmp")
  rm -f "$tmp"

  if [ "$use_gzip" = "gz" ]; then
    if ! curl -fsSL --max-time 900 "$url" | gunzip > "$tmp"; then
      echo "    下载或解压失败: $url" >&2
      return 1
    fi
  else
    if ! curl -fsSL --max-time 900 -o "$tmp" "$url"; then
      echo "    下载失败: $url" >&2
      return 1
    fi
  fi

  # 校验下载结果，未通过则不落到正式路径
  if [ ! -s "$tmp" ]; then
    echo "    下载结果为空，已放弃" >&2
    return 1
  fi

  chmod +x "$tmp"
  if ! is_usable "$tmp"; then
    echo "    文件无法执行或不是 ffmpeg 构建，已放弃" >&2
    return 1
  fi

  # 校验通过后原子替换
  mv -f "$tmp" "$target"
  echo "    完成 ($(du -h "$target" | cut -f1))"
}

# 依次尝试候选来源，首个成功者生效。
# 采用"逐个尝试"而非"先探测再下载"的原因：探测需要额外网络往返，
# 且不同服务器对 HEAD 请求的处理不一致（部分服务器在响应头之后仍继续传输）。
# 逐个尝试的代价仅是一次失败请求，逻辑更简单且更可靠。
download_first_available() {
  local name="$1"; shift
  local spec url kind
  for spec in "$@"; do
    url="${spec%|*}"
    kind="${spec##*|}"
    if download "$name" "$url" "$kind"; then
      return 0
    fi
    echo "    尝试下一个来源 ..." >&2
  done
  echo "  错误: $name 的所有来源均不可用" >&2
  return 1
}

# gz 体积约为未压缩版的三分之一，故优先；文件不存在时自动回退
download_first_available ffmpeg \
  "$MIRROR/ffmpeg-$PLATFORM$EXT.gz|gz" \
  "$MIRROR/ffmpeg-$PLATFORM$EXT|"

download_first_available ffprobe \
  "$MIRROR/ffprobe-$PLATFORM$EXT.gz|gz" \
  "$MIRROR/ffprobe-$PLATFORM$EXT|"

# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------
echo
echo "=== 安装结果 ==="
for name in ffmpeg ffprobe; do
  bin="$TOOLS/$name$EXT"
  if is_usable "$bin"; then
    printf "  %-8s %s\n" "$name" "$($bin -hide_banner -version 2>/dev/null | head -1)"
  else
    printf "  %-8s 不可用\n" "$name"
    exit 1
  fi
done

echo
echo "=== 能力自检 ==="
cd "$ROOT" && python3 -m biliagent.cli caps
