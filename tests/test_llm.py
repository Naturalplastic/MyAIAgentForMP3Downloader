#!/usr/bin/env python3
"""D5 验收：DeepSeek 适配器。

用 **注入式 mock** 替代真实 HTTP —— 不打网络、不花 token，
但能覆盖所有错误分支（401/429/5xx/超时/坏 JSON）。

真实 API 的行为由 `test_real_api()` 覆盖，仅在显式提供 key 时运行
（`--real` 参数），因为真调用有成本和不确定性，不适合放进常规回归。

用法：
    python3 tests/test_llm.py            # mock 测试（默认）
    python3 tests/test_llm.py --real     # 追加真实 API 冒烟测试
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from biliagent import llm as L  # noqa: E402
from biliagent.loop import AgentLoop  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  \033[32m✓\033[0m %s" % name)
    else:
        FAIL.append((name, detail))
        print("  \033[31m✗\033[0m %s  %s" % (name, detail))


def skip(name, why):
    SKIP.append((name, why))
    print("  \033[33m−\033[0m %s  (跳过: %s)" % (name, why))


def section(t):
    print("\n\033[1m%s\033[0m" % t)


# --------------------------------------------------------------------------
# mock 基础设施：替换 llm._post
# --------------------------------------------------------------------------
class MockPost:
    """记录调用并按脚本返回响应。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, body, api_key, timeout=180):
        self.calls.append({"url": url, "body": body, "key": api_key})
        if not self.script:
            return 500, {"error": {"message": "mock script exhausted"}}
        item = self.script.pop(0)
        return item


def ok_body(text="<final_answer>done</final_answer>", model="deepseek-flash",
            prompt=100, completion=20, reasoning=5):
    return 200, {
        "id": "x", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text,
                                 "reasoning_content": "r" * reasoning}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


_orig_post = L._post


def with_mock(script):
    mock = MockPost(script)
    L._post = mock
    return mock


def restore():
    L._post = _orig_post


# ==========================================================================
section("1. 客户端构造与企业级防护")

# 保住真实环境中可能存在的 key —— 测试绝不能污染它（否则 --real 会用到 mock key）
_real_key = os.environ.get(L.ENV_KEY)

# 缺 key 的行为
os.environ.pop(L.ENV_KEY, None)
try:
    L.DeepSeekClient()
    check("缺 API key 时抛 LLMError", False, "没有抛出")
except L.LLMError as e:
    check("缺 API key 时抛 LLMError", True)
    check("缺 key 的错误带明确 hint",
          "DEEPSEEK_API_KEY" in e.payload.get("hint", ""), str(e.payload)[:200])
    check("缺 key 的 hint 给出 .env 的做法",
          ".env" in e.payload.get("hint", ""), str(e.payload)[:200])
    check("缺 key 的 message 说明文件与环境变量都查过了",
          ".env" in e.payload.get("message", ""), str(e.payload.get("message"))[:150])
except Exception as e:  # noqa: BLE001
    check("缺 API key 时抛 LLMError", False, "抛了 %s" % type(e).__name__)

# 后续用例用 mock key
os.environ[L.ENV_KEY] = "sk-TESTONLY1234567890abcdef"
check("redact() 抹掉文本中的 key",
      "sk-TESTONLY1234567890abcdef" not in L.redact("用了 sk-TESTONLY1234567890abcdef 调用"),
      L.redact("用了 sk-TESTONLY1234567890abcdef 调用"))
check("redact() 对无 key 文本原样返回",
      L.redact("普通文本") == "普通文本")
check("redact() 对 None 安全", L.redact(None) is None)


# ==========================================================================
section("2. 正常路径")

mock = with_mock([ok_body("<tool_call>{}</tool_call>")])
try:
    c = L.DeepSeekClient(model="deepseek-flash")
    res = c.chat([{"role": "user", "content": "hi"}])
    check("返回 text 字段", res.get("text") == "<tool_call>{}</tool_call>", str(res)[:120])
    check("返回 usage 明细", res["usage"]["prompt_tokens"] == 100, str(res.get("usage")))
    check("记录 reasoning 长度（不保留内容）",
          res.get("reasoning_len") == 5, str(res.get("reasoning_len")))
    check("请求打到 /chat/completions",
          mock.calls[0]["url"].endswith("/chat/completions"), mock.calls[0]["url"])
    check("请求带 Bearer 鉴权头",
          mock.calls[0]["key"] == "sk-TESTONLY1234567890abcdef")
    check("默认 stream=False", mock.calls[0]["body"]["stream"] is False)
    check("默认不带 reasoning_effort", "reasoning_effort" not in mock.calls[0]["body"])
    check("token 统计累加", c.stats()["prompt_tokens"] == 100, str(c.stats()))
finally:
    restore()

# effort 透传
mock = with_mock([ok_body()])
try:
    c = L.DeepSeekClient(effort="high")
    c.chat([{"role": "user", "content": "x"}])
    check("effort 透传为 reasoning_effort",
          mock.calls[0]["body"].get("reasoning_effort") == "high",
          str(mock.calls[0]["body"].keys()))
finally:
    restore()

# reasoning 不回传给模型
mock = with_mock([ok_body()])
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("思考内容不混入 text（避免污染协议解析）",
          "r" * 5 not in (r.get("text") or ""), repr(r.get("text"))[:80])
finally:
    restore()


# ==========================================================================
section("3. 错误分支")

cases = [
    (401, {"error": {"message": "Authentication Fails"}}, "http_401", "API key"),
    (402, {"error": {"message": "Insufficient Balance"}}, "http_402", "余额"),
    (404, {"error": {"message": "model not found"}}, "http_404", ""),
]
for status, payload, want_code, want_hint_kw in cases:
    mock = with_mock([(status, payload)])
    try:
        c = L.DeepSeekClient()
        r = c.chat([{"role": "user", "content": "x"}])
        check("%d → %s" % (status, want_code), r.get("error") == want_code, str(r)[:120])
        if want_hint_kw:
            check("%d 的 hint 提示 %s" % (status, want_hint_kw),
                  want_hint_kw in r.get("hint", ""), str(r.get("hint"))[:120])
    finally:
        restore()

# 429 会重试
mock = with_mock([(429, {"error": {"message": "rate limited"}}),
                  ok_body()])
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("429 先重试再成功", "text" in r and len(mock.script) == 0, str(r)[:100])
    check("429 触发了 2 次请求", len(mock.calls) == 2, "calls=%d" % len(mock.calls))
finally:
    restore()

# 5xx 重试后仍失败
mock = with_mock([(500, {"error": {"message": "boom"}})] * 3)
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("5xx 重试耗尽后返回错误", r.get("error") == "http_500", str(r)[:120])
    check("5xx 恰好尝试 3 次（1+2 重试）", len(mock.calls) == 3, "calls=%d" % len(mock.calls))
finally:
    restore()

# 网络错误
mock = with_mock([(-1, {"error": {"message": "connection refused"}})] * 3)
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("网络错误 → network_error", r.get("error") == "network_error", str(r)[:120])
    check("网络错误带网络相关 hint", "网络" in r.get("hint", ""), str(r.get("hint")))
finally:
    restore()

# 非 JSON 响应体（网关返回 HTML 之类）
mock = with_mock([(502, {"raw": "<html>Bad Gateway</html>"})] * 3)
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("坏响应体不崩溃", r.get("error", "").startswith("http_"), str(r)[:140])
finally:
    restore()

# 错误消息里的 key 必须被抹掉
mock = with_mock([(401, {"error": {"message":
                "invalid key sk-TESTONLY1234567890abcdef used"}})])
try:
    c = L.DeepSeekClient()
    r = c.chat([{"role": "user", "content": "x"}])
    check("错误消息中的 key 被 redact",
          "sk-TESTONLY1234567890abcdef" not in r.get("message", ""),
          str(r.get("message"))[:150])
finally:
    restore()


# ==========================================================================
section("4. 与 loop 集成")

# 模型失败 → loop 报 llm_error 而不是崩溃
mock = with_mock([(401, {"error": {"message": "bad key"}})] * 3)
try:
    from biliagent.tools import ToolBox
    box = ToolBox.__new__(ToolBox)          # 不触发能力探测/网络
    box.capabilities = {"ok": True, "ffmpeg": {"version": "6.0"}, "can_encode_mp3": True}
    box.mp4_path = box.audio_path = None
    c = L.DeepSeekClient()
    st = AgentLoop(c, toolbox=box).run("x")
    check("适配器报错 → loop 报 llm_error",
          st.outcome == "llm_error", str(st.outcome))
    check("llm_error 的汇报含 message 与 hint（不只 error 代码）",
          "bad key" in (st.report or "") and "建议" in (st.report or ""),
          str(st.report)[:200])
finally:
    restore()

# 适配器正常返回 → loop 正常解析
mock = with_mock([ok_body("<tool_call>{\"action\":\"finish\",\"args\":{\"summary\":\"ok\"}}</tool_call>")])
try:
    from biliagent.tools import ToolBox
    box = ToolBox.__new__(ToolBox)
    box.capabilities = {"ok": True, "ffmpeg": {"version": "6.0"}, "can_encode_mp3": True}
    box.mp4_path = box.audio_path = None
    box.dispatch = lambda a, ar, s: {"ok": True, "terminal": True,
                                     "kind": "finish", "summary": ar.get("summary", "")}
    c = L.DeepSeekClient()
    st = AgentLoop(c, toolbox=box).run("x")
    check("适配器正常 → loop 完成", st.outcome == "finished", str(st.outcome))
    check("loop 解析出 finish 的 summary", st.report == "ok", str(st.report))
finally:
    restore()

# 关键点：模型原始输出必须原样交给 protocol，不做预处理
mock = with_mock([ok_body("前置废话 <tool_call>{\"action\":\"search\",\"args\":{\"keyword\":\"k\"}}</tool_call> 后置废话")])
try:
    from biliagent.tools import ToolBox
    box = ToolBox.__new__(ToolBox)
    box.capabilities = {"ok": True, "ffmpeg": {"version": "6.0"}, "can_encode_mp3": True}
    box.mp4_path = box.audio_path = None
    seen = {}

    def fake_dispatch(a, ar, s):
        seen["action"] = a
        seen["args"] = ar
        return {"ok": True, "terminal": True, "kind": "finish", "summary": "end"}

    box.dispatch = fake_dispatch
    c = L.DeepSeekClient()
    AgentLoop(c, toolbox=box).run("x")
    check("带前后杂质的输出仍被正确解析（适配器不做预处理）",
          seen.get("action") == "search" and seen.get("args") == {"keyword": "k"},
          str(seen))
finally:
    restore()


# ==========================================================================
section("5. 真实 API 冒烟测试（--real）")

if "--real" not in sys.argv:
    skip("真实 API 调用", "未加 --real 参数（避免常规回归产生费用）")
elif not _real_key:
    skip("真实 API 调用", "未设置 %s" % L.ENV_KEY)
else:
    # 关键：必须用**真实** key，而不是上面 mock 用的那个
    os.environ[L.ENV_KEY] = _real_key
    try:
        c = L.DeepSeekClient()
        r = c.chat([{"role": "system", "content":
                     "只输出 <final_answer>OK</final_answer>，不要其他文字。"},
                    {"role": "user", "content": "确认"}])
        check("真实 API 可调用", "text" in r, str(r)[:200])
        if "text" in r:
            check("真实模型遵循标记协议", "<final_answer>" in r["text"],
                  repr(r["text"])[:150])
            check("真实调用返回 token 统计",
                  (r.get("usage") or {}).get("prompt_tokens", 0) > 0,
                  str(r.get("usage")))
            check("真实调用记录了思考长度",
                  isinstance(r.get("reasoning_len"), int), str(r.get("reasoning_len")))
    except Exception as e:  # noqa: BLE001
        check("真实 API 可调用", False, str(e)[:200])


# ==========================================================================
# 恢复环境到测试前的状态（不能留下 mock key）
if _real_key:
    os.environ[L.ENV_KEY] = _real_key
else:
    os.environ.pop(L.ENV_KEY, None)

print("\n" + "=" * 62)
print("通过 %d ｜ 失败 %d ｜ 跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("\n失败项:")
    for n, d in FAIL:
        print("  ✗ %s\n      %s" % (n, d))
if SKIP:
    print("\n跳过项:")
    for n, w in SKIP:
        print("  − %s (%s)" % (n, w))
print("=" * 62)
sys.exit(1 if FAIL else 0)
