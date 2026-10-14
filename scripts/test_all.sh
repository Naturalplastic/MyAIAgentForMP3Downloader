#!/usr/bin/env bash
# 全量回归。用法：
#   bash scripts/test_all.sh           # 含真实 API 冒烟（需 DEEPSEEK_API_KEY）
#   bash scripts/test_all.sh --no-real # 跳过真实 API（零成本、可离线）
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LLM_ARGS=""
[ "${1:-}" = "--no-real" ] && LLM_ARGS="" || LLM_ARGS="--real"

fail=0
total_pass=0
for t in tests/test_env.py tests/test_tools.py tests/test_loop.py; do
  echo "════════════════════════════════════════════════════════════"
  echo "  $t"
  echo "════════════════════════════════════════════════════════════"
  out=$(python3 "$t") || fail=1
  echo "$out" | grep -E "通过 |^  ✗|^  −" || true
  p=$(echo "$out" | grep -oP '通过 \K[0-9]+' | head -1)
  total_pass=$((total_pass + ${p:-0}))
  echo
done

echo "════════════════════════════════════════════════════════════"
echo "  tests/test_llm.py  ${LLM_ARGS:+(含真实 API 冒烟)}"
echo "════════════════════════════════════════════════════════════"
out=$(python3 tests/test_llm.py $LLM_ARGS) || fail=1
echo "$out" | grep -E "通过 |^  ✗|^  −" || true
p=$(echo "$out" | grep -oP '通过 \K[0-9]+' | head -1)
total_pass=$((total_pass + ${p:-0}))
echo

# 清理测试产生的临时媒体文件（它们只是异常路径的素材）
rm -f downloads/broken.mp4 downloads/noaudio.mp4

echo "════════════════════════════════════════════════════════════"
if [ "$fail" -eq 0 ]; then
  echo "✅ 全部测试通过（合计 $total_pass 项）"
else
  echo "❌ 有测试失败"
fi
echo "════════════════════════════════════════════════════════════"
exit "$fail"
