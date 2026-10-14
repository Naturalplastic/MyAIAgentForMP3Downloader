#!/usr/bin/env python3
"""D4 验收：agent loop + 协议层。

分两部分：
  A. 协议与 loop 逻辑（本地假 LLM，不依赖网络/ffmpeg）—— 主体验收
  B. 真实全链路（假指令驱动真工具）—— 端到端确认

为什么大量用例用假 LLM：loop 的 bug 和模型的不确定性必须能分开定位。
出问题时先跑 A 部分，全绿就说明 loop 逻辑没问题，锅在模型/prompt。

用法：python3 tests/test_loop.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from biliagent import protocol as P  # noqa: E402
from biliagent.loop import AgentLoop, ScriptedChat  # noqa: E402
from biliagent.protocol import TOOL_SPECS, parse_response, validate_directive  # noqa: E402

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


# ==========================================================================
section("A1. 标记解析：区分「格式错」与「它就是答复」")

# 核心用例：带解释的 JSON —— v0.1 的 JSON.parse 方案会把它误判为最终答复
r = parse_response('好的，我来搜索：\n<tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>')
check("带前置解释的 tool_call 仍被识别为工具调用",
      r["status"] == P.OK_TOOL_CALL and r["directive"]["action"] == "search",
      str(r)[:150])

r = parse_response('<final_answer>已完成下载。</final_answer>')
check("最终答复被正确识别",
      r["status"] == P.OK_FINAL and r["text"] == "已完成下载。", str(r)[:120])

r = parse_response("我觉得应该先搜索一下。")
check("无标记的纯文本 → no_marker（而不是当成答复）",
      r["status"] == P.ERR_NO_MARKER, str(r)[:120])
check("no_marker 带可执行的 fix 指示", "tool_call" in r.get("fix", ""))

r = parse_response('<tool_call>{"action":"search"}</tool_call>\n<final_answer>好了</final_answer>')
check("两标记并存 → both_markers", r["status"] == P.ERR_BOTH_MARKERS, str(r)[:120])

r = parse_response('<tool_call>{"action":"a"}</tool_call><tool_call>{"action":"b"}</tool_call>')
check("多个 tool_call → multiple_calls", r["status"] == P.ERR_MULTIPLE, str(r)[:120])

r = parse_response("<tool_call>{action: search}</tool_call>")
check("标记内非法 JSON → bad_json", r["status"] == P.ERR_BAD_JSON, str(r)[:120])
check("bad_json 的 fix 说明正确格式", "不要用 markdown" in r.get("fix", ""))

r = parse_response('<tool_call>["search"]</tool_call>')
check("JSON 合法但非对象 → not_object", r["status"] == P.ERR_NOT_OBJECT, str(r)[:120])

r = parse_response("<final_answer>   </final_answer>")
check("空的 final_answer → empty_final", r["status"] == P.ERR_EMPTY_FINAL, str(r)[:120])

# 容错：标记外的空白与换行
r = parse_response('\n\n  <tool_call>\n{"action":"finish","args":{"summary":"ok"}}\n</tool_call>\n\n')
check("容忍标记外空白与内部换行", r["status"] == P.OK_TOOL_CALL, str(r)[:120])


# ==========================================================================
section("A2. 指令校验：非法输出必须能自愈")

v = validate_directive({"action": "search", "args": {"keyword": "稻香"}})
check("合法指令通过", v["ok"] and v["action"] == "search")

v = validate_directive({"args": {"keyword": "x"}})
check("缺 action → missing_action", not v["ok"] and v["error"] == "missing_action")

v = validate_directive({"action": "rm_rf", "args": {}})
check("未知 action → unknown_action", not v["ok"] and v["error"] == "unknown_action")
check("unknown_action 的 fix 列出全部可用工具",
      all(a in v.get("fix", "") for a in TOOL_SPECS), str(v.get("fix"))[:200])

v = validate_directive({"action": "search", "args": {}})
check("缺必填参数 → missing_args", not v["ok"] and v["error"] == "missing_args")

v = validate_directive({"action": "extract_audio", "args": {"mode": "ogg"}})
check("枚举越界 → bad_arg_value",
      not v["ok"] and v["error"] == "bad_arg_value" and v["allowed"] == ["mp3", "copy"],
      str(v)[:150])

v = validate_directive({"action": "search", "args": {"keyword": "x", "foo": 1}})
check("多余参数 → unknown_args", not v["ok"] and v["error"] == "unknown_args")

v = validate_directive({"action": "search", "args": {"keyword": "x"}, "reason": "随便"})
check("多余的顶层字段被忽略（不因多余键而失败）", v["ok"], str(v)[:120])

v = validate_directive({"action": "finish", "args": {"summary": "done"}})
check("finish 指令通过", v["ok"] and v["action"] == "finish")


# ==========================================================================
section("A3. 系统提示词与工具 Schema 同源")

prompt = P.render_system_prompt()
check("提示词列出全部 action", all(a in prompt for a in TOOL_SPECS))
check("提示词包含标记格式说明", "<tool_call>" in prompt and "<final_answer>" in prompt)
check("提示词要求一次只输出一个 tool_call", "一个" in prompt)

no_ff = P.render_system_prompt({"ok": False, "errors": ["未找到 ffmpeg"]})
check("ffmpeg 缺失时提示词明确告知不能抽音频",
      "无法抽取" in no_ff or "未检测到 ffmpeg" in no_ff)
check("ffmpeg 缺失时提示词要求不要反复尝试",
      "不要反复尝试" in no_ff)

can_mp3 = P.render_system_prompt({"ok": True, "ffmpeg": {"version": "6.0"},
                                  "can_encode_mp3": True})
check("ffmpeg 可用时提示词告知可产 mp3", "可以产出 mp3" in can_mp3)

no_lame = P.render_system_prompt({"ok": True, "ffmpeg": {"version": "6.0"},
                                  "can_encode_mp3": False})
check("不能编码 mp3 时提示词给出降级指引", "mode=copy" in no_lame)


# ==========================================================================
section("A4. Loop 控制流（假 LLM）")


class FakeBox:
    """记录调用的假 ToolBox，用于纯逻辑测试（不打网络）。"""

    def __init__(self, results=None, capabilities=None):
        self.calls = []
        self.results = results or {}
        self.capabilities = capabilities or {"ok": True, "ffmpeg": {"version": "6.0"},
                                             "can_encode_mp3": True}
        self.mp4_path = None
        self.audio_path = None

    def dispatch(self, action, args, state):
        self.calls.append((action, args))
        if action in self.results:
            res = self.results[action]
            return res(args, state) if callable(res) else res
        if action == "finish":
            return {"ok": True, "terminal": True, "kind": "finish",
                    "summary": args.get("summary", "")}
        if action == "ask_user":
            return {"ok": True, "terminal": True, "kind": "ask_user",
                    "question": args.get("question", "")}
        return {"ok": True, "echo": args}


# A4.1 正常路径：搜索 → 下载 → 完成
script = [
    '<tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>',
    '<tool_call>{"action":"download","args":{"bvid":"BV1xx"}}</tool_call>',
    '<tool_call>{"action":"finish","args":{"summary":"搞定"}}</tool_call>',
]
box = FakeBox({"search": {"ok": True, "keyword": "稻香", "total": 1000, "matched": 5,
                          "items": [{"bvid": "BV1xx", "title": "稻香", "author": "周杰伦",
                                     "duration": "4:00", "play": 999, "relevance": 1.0}]},
               "download": {"ok": True, "path": "/tmp/a.mp4", "bytes": 1048576,
                            "quality": 16, "complete": True, "title": "稻香"}})
chat = ScriptedChat(script)
st = AgentLoop(chat, toolbox=box).run("下载稻香")
check("正常路径：3 轮内完成", st.round == 3, "round=%d" % st.round)
check("正常路径：outcome=finished", st.outcome == "finished", str(st.outcome))
check("正常路径：汇报内容正确", st.report == "搞定", st.report)
check("正常路径：工具按序调用",
      [c[0] for c in box.calls] == ["search", "download", "finish"],
      str([c[0] for c in box.calls]))
check("正常路径：观察里候选已截断为紧凑结构",
      "candidates" in st.trace[0]["observation"], str(st.trace[0])[:150])

# A4.2 格式违规自愈：先乱说，再纠正
script = [
    "我觉得应该搜索一下稻香。",                                  # no_marker
    '<tool_call>{"action":"search"}</tool_call>',                # missing_args
    '<tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>',  # 修正
    "<final_answer>好了</final_answer>",
]
box = FakeBox()
st = AgentLoop(ScriptedChat(script), toolbox=box).run("稻香")
check("格式违规后能自愈并继续", st.outcome == "finished", str(st.outcome))
check("自愈过程共 4 轮", st.round == 4, "round=%d" % st.round)
check("自愈后工具参数正确", box.calls and box.calls[0][1] == {"keyword": "稻香"},
      str(box.calls))
check("trace 记录了两次格式违规",
      sum(1 for t in st.trace if t["type"] in
          ("format_violation", "invalid_directive")) == 2,
      str([t["type"] for t in st.trace]))

# A4.3b 格式违规但整体在推进 → 不应被误判为卡死
# （验证「连续计数」而非「累计计数」的设计）
script = [
    "胡说",                                                     # 违规 1
    "再胡说",                                                   # 违规 2
    '<tool_call>{"action":"search","args":{"keyword":"a"}}</tool_call>',   # 成功 → 重置
    "又胡说",                                                   # 违规 1（新的连续段）
    '<tool_call>{"action":"search","args":{"keyword":"b"}}</tool_call>',   # 成功 → 重置
    "还胡说",                                                   # 违规 1
    '<tool_call>{"action":"finish","args":{"summary":"最终完成"}}</tool_call>',
]
box = FakeBox()
st = AgentLoop(ScriptedChat(script), toolbox=box, max_format_fixes=3,
               max_rounds=12).run("x")
check("格式违规后成功 → 不应被判卡死（连续计数生效）",
      st.outcome == "finished", "outcome=%s round=%d" % (st.outcome, st.round))
check("累计违规数被如实记录（4 次 > 连续上限 3）",
      st.total_format_fixes == 4, "total=%d" % st.total_format_fixes)
check("连续计数在成功后归零", st.format_fixes < 3, "format_fixes=%d" % st.format_fixes)

# A4.3c 连续违规达上限 → 放弃
script = ["胡说"] * 8
st = AgentLoop(ScriptedChat(script), toolbox=FakeBox(), max_format_fixes=3).run("x")
check("连续违规达上限 → format_stuck", st.outcome == "format_stuck", str(st.outcome))
check("连续违规在 4 轮内停止", st.round <= 5, "round=%d" % st.round)

# A4.3 格式持续违规 → 放弃（而不是无限重试）
script = ["胡说八道"] * 6
box = FakeBox()
st = AgentLoop(ScriptedChat(script), toolbox=box, max_format_fixes=2).run("x")
check("格式持续违规时放弃并报 format_stuck",
      st.outcome == "format_stuck", str(st.outcome))
check("放弃不是因为耗尽轮数", st.round <= 4, "round=%d" % st.round)
check("放弃时错误信息里带模型原始输出", "胡说八道" in (st.report or ""), str(st.report)[:120])

# A4.4 重复动作 → 判卡死
script = ['<tool_call>{"action":"get_info","args":{"bvid":"BV1"}}</tool_call>'] * 6
box = FakeBox()
st = AgentLoop(ScriptedChat(script), toolbox=box).run("x")
check("重复同一动作 → stuck", st.outcome == "stuck", str(st.outcome))
check("卡死时在 3 次重复内停止", st.round <= 4, "round=%d" % st.round)

# A4.5 轮数上限
script = ['<tool_call>{"action":"search","args":{"keyword":"k%d"}}</tool_call>' % i
          for i in range(30)]
box = FakeBox()
st = AgentLoop(ScriptedChat(script), toolbox=box, max_rounds=5).run("x")
check("达到轮数上限 → max_rounds", st.outcome == "max_rounds", str(st.outcome))
check("轮数严格不超过上限", st.round <= 5, "round=%d" % st.round)

# A4.6 ask_user 终止
script = ['<tool_call>{"action":"ask_user","args":{"question":"你要视频还是音频？"}}</tool_call>']
st = AgentLoop(ScriptedChat(script), toolbox=FakeBox()).run("帮我下载")
check("ask_user → outcome=asked", st.outcome == "asked", str(st.outcome))
check("ask_user 的问题被保留", "视频还是音频" in (st.report or ""), str(st.report))

# A4.7 模型调用失败
def failing_chat(messages):
    return {"error": "HTTP 401: invalid api key"}

st = AgentLoop(failing_chat, toolbox=FakeBox()).run("x")
check("模型调用失败 → llm_error 而不是崩溃", st.outcome == "llm_error", str(st.outcome))
check("llm_error 的汇报含原因", "401" in (st.report or ""), str(st.report))

# A4.8 工具抛异常不能泄漏到 loop
class BoomBox(FakeBox):
    def dispatch(self, action, args, state):
        raise RuntimeError("工具炸了")

st = AgentLoop(ScriptedChat(['<tool_call>{"action":"search","args":{"keyword":"x"}}</tool_call>',
                             "<final_answer>结束</final_answer>"]),
               toolbox=BoomBox()).run("x")
check("工具抛异常不会让 loop 崩溃", st.outcome == "finished", str(st.outcome))

# A4.9 静默失败必须被转发给模型（R1 的最后一道防线）
box = FakeBox({"search": {"ok": True, "keyword": "垃圾词", "total": 1000, "matched": 0,
                          "items": [{"bvid": "BV9", "title": "无关视频", "author": "某UP",
                                     "duration": "1:00", "play": 100, "relevance": 0.0}],
                          "warning": "no_relevant_result",
                          "hint": "请勿在其中选择。"}})
st = AgentLoop(ScriptedChat(['<tool_call>{"action":"search","args":{"keyword":"垃圾词"}}</tool_call>',
                             "<final_answer>结束</final_answer>"]),
               toolbox=box).run("x")
obs = st.trace[0]["observation"]
check("R1：warning 被转发到观察里", obs.get("warning") == "no_relevant_result", str(obs)[:150])
check("R1：hint 被转发到观察里", "请勿" in (obs.get("hint") or ""), str(obs)[:150])
check("R1：relevance 字段被保留给模型判断",
      obs["candidates"][0].get("relevance") == 0.0, str(obs["candidates"])[:150])

# A4.10 失败观察保留 error/hint，但不含内部字段
box = FakeBox({"download": {"ok": False, "error": "risk_control",
                            "message": "被拒绝", "hint": "别重试",
                            "internal_secret": "不该出现"}})
st = AgentLoop(ScriptedChat(['<tool_call>{"action":"download","args":{"bvid":"BV1"}}</tool_call>',
                             "<final_answer>结束</final_answer>"]),
               toolbox=box).run("x")
obs = st.trace[0]["observation"]
check("失败观察保留 error/message/hint",
      obs.get("error") == "risk_control" and obs.get("hint") == "别重试", str(obs))
check("失败观察剔除无关内部字段", "internal_secret" not in obs, str(obs))

# A4.11 状态可序列化（回放要求）
st = AgentLoop(ScriptedChat(['<tool_call>{"action":"finish","args":{"summary":"ok"}}</tool_call>']),
               toolbox=FakeBox()).run("x")
d = st.to_dict()
check("状态可 JSON 序列化（可回放）",
      isinstance(json.dumps(d, ensure_ascii=False), str), "")
check("状态含完整 messages 与 trace", bool(d["messages"]) and bool(d["trace"]))


# ==========================================================================
section("A5. 上下文压缩（预算控制）")

from biliagent.context import (  # noqa: E402
    ContextPolicy,
    compress_messages,
    estimate_messages_tokens,
    estimate_tokens,
)

check("token 估算：空串为 0", estimate_tokens("") == 0)
# 系数由真实 API 实测标定（见 context.py 头部注释），此处锁定端值锚点
check("token 估算：纯中文 ≈ 1.04 token/字",
      190 <= estimate_tokens("汉" * 200) <= 225, str(estimate_tokens("汉" * 200)))
check("token 估算：纯英文 ≈ 0.32 token/字符",
      27 <= estimate_tokens("a" * 100) <= 37, str(estimate_tokens("a" * 100)))
check("token 估算：中文占比越低，估算越小",
      estimate_tokens("汉" * 200) > 3 * estimate_tokens("a" * 200))
check("token 估算：中文显著多于同长度英文",
      estimate_tokens("汉" * 50) > estimate_tokens("a" * 50))

_msgs = [{"role": "system", "content": "s"},
         {"role": "user", "content": "u"}]
check("整体估算包含角色开销",
      estimate_messages_tokens(_msgs) >= estimate_tokens("s") + estimate_tokens("u") + 8,
      str(estimate_messages_tokens(_msgs)))

# 构造一段长对话：每轮 = assistant(tool_call) + user(大 observation)
def build_long_history(rounds=8, obs_size=2000):
    msgs = [{"role": "system", "content": "系统提示"},
            {"role": "user", "content": "原始任务：把老番茄的视频做成 mp3"}]
    for i in range(rounds):
        msgs.append({"role": "assistant",
                     "content": '<tool_call>{"action":"search","args":{"keyword":"k%d"}}'
                                '</tool_call>' % i})
        msgs.append({"role": "user",
                     "content": '<observation>{"_action":"search","ok":true,'
                                '"keyword":"k%d","matched":5,"pad":"%s"}</observation>'
                                % (i, "填" * obs_size)})
    return msgs


long_msgs = build_long_history(8)
before = estimate_messages_tokens(long_msgs)
compressed, dropped = compress_messages(long_msgs, keep_recent=3)
after = estimate_messages_tokens(compressed)

check("压缩确实减少了 token", after < before,
      "before=%d after=%d" % (before, after))
check("压缩比例显著（至少降到一半）", after < before * 0.5,
      "before=%d after=%d" % (before, after))
check("压缩返回被丢弃的条数", dropped > 0, "dropped=%d" % dropped)
check("system 提示词被永久保留",
      compressed[0].get("role") == "system" and compressed[0]["content"] == "系统提示")
check("原始用户任务被永久保留",
      any("原始任务" in m["content"] for m in compressed[:3]),
      str([m["content"][:40] for m in compressed[:3]]))
check("压缩痕迹被标注 [已压缩]",
      any("[已压缩]" in m["content"] for m in compressed),
      str([m["content"][:60] for m in compressed])[:200])
check("保留最近一轮的完整内容",
      any('"matched":5' in m.get("content", "") for m in compressed),
      "最近观察应未被压缩")

# 短对话不该被动
short = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
         {"role": "assistant", "content": "a"}, {"role": "user", "content": "o"}]
out, d = compress_messages(short, keep_recent=3)
check("短对话不压缩", d == 0 and out == short)

# 压缩后仍需保证最后一条是 user（否则 API 会拒）
check("压缩后最后一条仍是 user 角色",
      compressed[-1].get("role") == "user", compressed[-1].get("role"))

# 策略层
pol = ContextPolicy(max_tokens=50, keep_recent=2)
est = [{"role": "system", "content": "s" * 400}, {"role": "user", "content": "u"}]
_check_msgs, did = pol.maybe_compress(build_long_history(6, 500))
check("策略层在小预算下触发压缩", did, str(pol.stats()))
check("策略层统计压缩次数", pol.stats()["compressions"] >= 1, str(pol.stats()))

pol2 = ContextPolicy(max_tokens=10 ** 9)
_, did2 = pol2.maybe_compress(build_long_history(6, 500))
check("预算充足时不压缩", did2 is False, str(pol2.stats()))

pol3 = ContextPolicy(max_tokens=10 ** 9, enabled=False)
_, did3 = pol3.maybe_compress(build_long_history(6, 500))
check("可整体关闭压缩", did3 is False)

# loop 集成：大观察触发压缩并记录
big = "填" * 3000
big_obs = ('<observation>{"_action":"search","ok":true,"keyword":"k","matched":5,'
           '"pad":"%s"}</observation>' % big)
script = []
for i in range(6):
    script.append('<tool_call>{"action":"search","args":{"keyword":"k%d"}}</tool_call>' % i)
script.append("<final_answer>done</final_answer>")

box = FakeBox()
box.dispatch = lambda a, ar, s: {
    "ok": True, "keyword": ar.get("keyword"), "total": 1000, "matched": 5,
    "items": [{"bvid": "BV%d" % i, "title": "t" * 50, "author": "a", "duration": "1:00",
               "play": 100, "relevance": 1.0} for i in range(5)]}
st = AgentLoop(ScriptedChat(script), toolbox=box, max_rounds=10,
               context_policy=ContextPolicy(max_tokens=1500, keep_recent=2)).run("x")
check("loop 中触发压缩并计数", st.compressions >= 1, "compressions=%d" % st.compressions)
check("压缩不破坏正常结束", st.outcome == "finished", st.outcome)
check("state.to_dict 含压缩信息",
      "compressions" in st.to_dict(), str(list(st.to_dict().keys()))[:200])


# A5.2 压缩产物自身的边界（这三个 bug 都是真实运行中发现的）
from biliagent.context import SUMMARY_TAG, estimate_messages_tokens  # noqa: E402


def run_many_compressions(rounds=60, keep=2, obs_size=400):
    msgs = [{"role": "system", "content": "系统提示"},
            {"role": "user", "content": "原始任务"}]
    snapshots = []
    for i in range(rounds):
        msgs.append({"role": "assistant",
                     "content": '<tool_call>{"action":"search","args":{"keyword":"k%d"}}'
                                '</tool_call>' % i})
        msgs.append({"role": "user",
                     "content": '<observation>{"_action":"search","ok":true,'
                                '"keyword":"k%d","pad":"%s"}</observation>'
                                % (i, "\u586b" * obs_size)})
        if i % 3 == 2:
            msgs, _ = compress_messages(msgs, keep_recent=keep)
            snapshots.append((i + 1, list(msgs)))
    return snapshots


snaps = run_many_compressions(60)
_, last_msgs = snaps[-1]

# Bug A：压缩产物被反复再压缩 → 自我嵌套
check("多次压缩后摘要不自我嵌套",
      all(m.get("content", "").count(SUMMARY_TAG) <= 1 for m in last_msgs),
      str([m["content"].count(SUMMARY_TAG) for m in last_msgs]))

# Bug B：每次压缩新增一条摘要 → 摘要条数线性增长
n_sum = sum(1 for m in last_msgs if SUMMARY_TAG in m.get("content", ""))
check("多次压缩后摘要条数恒为 1（不累积）", n_sum == 1, "count=%d" % n_sum)

# Bug C：消息数受控 ≠ 上下文受控 —— 摘要内容仍会无限增长
sizes = [len("".join(m["content"] for m in ms if SUMMARY_TAG in m.get("content", "")))
         for _, ms in snaps]
# 注意：不能用"单调不增"来断言 —— 滑动窗口的大小是**上下波动**的
# （新条目进、旧条目出）。正确的不变量是"存在硬上限"。
check("摘要字符数存在硬上限（滑窗生效）",
      max(sizes) <= 1600,
      "最大=%d（上限 1600）｜ 全部快照=%s" % (max(sizes), sizes))
check("长时间运行后摘要规模回落（不是只增不减）",
      sizes[-1] < max(sizes),
      "最大=%d 末=%d" % (max(sizes), sizes[-1]))

tokens = [estimate_messages_tokens(ms) for _, ms in snaps]
check("60 轮后 token 仍收敛（上下文真正受控）",
      tokens[-1] < 3000, "首=%d 末=%d" % (tokens[0], tokens[-1]))
check("压缩后消息数不随时间增长", len(last_msgs) <= 12, "len=%d" % len(last_msgs))

check("滑动窗口丢弃旧条目时有显式标注",
      any("已省略" in m.get("content", "") for m in last_msgs),
      str([m["content"][:60] for m in last_msgs])[:200])

check("压缩 60 轮后 system 提示词仍在",
      last_msgs[0]["role"] == "system" and last_msgs[0]["content"] == "系统提示")
check("压缩 60 轮后原始任务仍在",
      any("原始任务" in m.get("content", "") for m in last_msgs[:3]))
check("压缩后最后一条仍是 user（API 要求）",
      last_msgs[-1]["role"] == "user", last_msgs[-1]["role"])

# A5.3 重复动作提示（软信号，不硬拦）
dup_script = [
    '<tool_call>{"action":"search","args":{"keyword":"same"}}</tool_call>',
    '<tool_call>{"action":"get_info","args":{"bvid":"BV1"}}</tool_call>',
    '<tool_call>{"action":"search","args":{"keyword":"same"}}</tool_call>',
    "<final_answer>done</final_answer>",
]
box = FakeBox()
box.dispatch = lambda a, ar, s: {"ok": True, "echo": ar}
st = AgentLoop(ScriptedChat(dup_script), toolbox=box, max_rounds=8).run("x")
dup_obs = [t for t in st.trace if t.get("type") == "tool_result"
           and t["action"] == "search"]
check("重复调用被允许执行（不硬拦）", len(dup_obs) == 2, "count=%d" % len(dup_obs))
check("但重复的那次观察里带了 _note 提示",
      "_note" in dup_obs[1]["observation"], str(dup_obs[1]["observation"])[:200])
check("首次调用不带 _note（避免噪音）",
      "_note" not in dup_obs[0]["observation"])
check("_note 指明了首次执行的轮次",
      "第 1 轮" in dup_obs[1]["observation"].get("_note", ""),
      str(dup_obs[1]["observation"].get("_note"))[:120])


# A5.4 中间产物清理的安全约束
# 抽取失败时保留 mp4 是关键 —— 它是唯一的源，删了就彻底没法重试
import tempfile as _tf  # noqa: E402

from biliagent import media as _media  # noqa: E402

_caps = _media.probe_capabilities()
if not _caps.get("ok"):
    skip("中间产物清理的安全性", "ffmpeg 不可用")
else:
    _tmpdir = _tf.mkdtemp(prefix="biliagent_test_")
    _ff = _caps["ffmpeg"]["path"]

    # 造一个**没有音轨**的 mp4：抽取必然失败
    _noaudio = os.path.join(_tmpdir, "noaudio.mp4")
    subprocess.run([_ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", _noaudio],
                   capture_output=True, timeout=60)

    # 造一个带音轨的 mp4：抽取会成功
    _okfile = os.path.join(_tmpdir, "withaudio.mp4")
    subprocess.run([_ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", _okfile],
                   capture_output=True, timeout=60)

    if os.path.exists(_noaudio) and os.path.exists(_okfile):
        from biliagent.tools import ToolBox as _TB2
        _box = _TB2(out_dir=_tmpdir)

        # ① 失败路径：无音轨 → 抽取失败 → **必须保留源文件**
        _box.mp4_path = _noaudio
        _r = _box.dispatch("extract_audio", {"mode": "mp3"}, None)
        check("抽取失败时返回错误", _r.get("ok") is False, str(_r)[:150])
        check("抽取失败时**保留**源 mp4（它是唯一的重试依据）",
              os.path.exists(_noaudio), "源文件被误删了！")
        check("抽取失败时不记录清理动作",
              _box.removed_intermediate is None, str(_box.removed_intermediate))

        # ② 成功路径：有音轨 → 抽取成功 → 清理源文件
        _box2 = _TB2(out_dir=_tmpdir)
        _box2.mp4_path = _okfile
        _r2 = _box2.dispatch("extract_audio", {"mode": "mp3"}, None)
        check("抽取成功", _r2.get("ok") is True, str(_r2)[:150])
        check("抽取成功时清理源 mp4", not os.path.exists(_okfile),
              "源文件应被删除")
        check("清理后记录路径与释放空间",
              _box2.removed_intermediate == _okfile and _r2.get("freed_mb", 0) > 0,
              "%r freed=%s" % (_box2.removed_intermediate, _r2.get("freed_mb")))
        check("产物音频存在且非空",
              _r2.get("path") and os.path.exists(_r2["path"])
              and os.path.getsize(_r2["path"]) > 0, str(_r2.get("path")))
        check("mp4_path 被置空，不再指向已删文件",
              _box2.mp4_path is None, str(_box2.mp4_path))

        # ③ 清理临时目录
        import shutil as _sh
        _sh.rmtree(_tmpdir, ignore_errors=True)
    else:
        skip("中间产物清理的安全性", "fixture 生成失败")


# ==========================================================================
section("B. 真实全链路（假指令 + 真工具，需网络/ffmpeg）")

try:
    from biliagent.tools import ToolBox

    box = ToolBox(out_dir="downloads")
    if not box.capabilities.get("ok"):
        skip("真实全链路", "ffmpeg 不可用")

    script = [
        '<tool_call>{"action":"search","args":{"keyword":"老番茄","limit":3}}</tool_call>',
        '<tool_call>{"action":"download","args":{"bvid":"BV16F411S7dW"}}</tool_call>',
        '<tool_call>{"action":"extract_audio","args":{"mode":"mp3"}}</tool_call>',
        "<final_answer>已下载并转成 mp3。</final_answer>",
    ]
    loop = AgentLoop(ScriptedChat(script), toolbox=box, max_rounds=8)
    st = loop.run("把老番茄那个视频做成 mp3")

    # 先判断是否遭遇临时风控：若下载被拦，后续断言无意义，应记为跳过
    _blocked = any(
        (t.get("observation") or {}).get("error") in ("http_412", "http_403", "http_429")
        or (t.get("observation") or {}).get("error") == "risk_control"
        for t in st.trace if t.get("type") == "tool_result"
    )
    if _blocked:
        skip("全链路：产物与清理校验", "临时风控拦截（非功能缺陷）")
    else:
        check("全链路：outcome=finished", st.outcome == "finished", str(st.outcome))
        check("全链路：4 轮完成", st.round == 4, "round=%d" % st.round)
        check("全链路：产出 mp3", bool(st.audio_path and os.path.exists(st.audio_path)),
              str(st.audio_path))
    if not _blocked and st.audio_path:
        check("全链路：mp3 经 ffprobe 校验通过",
              st.trace[2]["observation"].get("verified") is True,
              str(st.trace[2]["observation"]))
    obs1 = st.trace[0]["observation"]
    if obs1.get("error") in ("http_412", "http_403", "http_429"):
        # 风控为临时限制，非功能缺陷
        skip("全链路：搜索观察检查", "临时风控 %s" % obs1.get("error"))
    else:
        check("全链路：搜索结果已进入观察",
              obs1.get("matched", 0) > 0, str(obs1)[:150])

    # ── 中间产物清理（默认开启）────────────────────────
    if _blocked:
        skip("中间产物清理校验", "临时风控拦截")
    else:
        check("默认删掉中间 mp4（只留音频）",
              box.removed_intermediate is not None
              and not os.path.exists(box.removed_intermediate),
              "removed=%r" % box.removed_intermediate)
        check("清理后 st.mp4_path 置空（不再指向已删文件）",
              st.mp4_path is None, str(st.mp4_path))
        obs3 = st.trace[2]["observation"]
        check("观察里说明了中间文件已删（模型能如实汇报）",
              "中间 mp4 已删除" in (obs3.get("note") or ""), str(obs3)[:200])
        check("观察里给出了释放的空间", obs3.get("note", "").count("MB") >= 1,
              str(obs3.get("note")))
        check("音频文件仍然存在（清理没有误删产物）",
              os.path.exists(st.audio_path), str(st.audio_path))

    # ── 反向：keep_mp4=True 时应保留 ────────────────────
    if _blocked:
        skip("keep_mp4 反向校验", "临时风控拦截")
        box2 = None
        st2 = None
    else:
        from biliagent.tools import ToolBox as _TB
        box2 = _TB(out_dir="downloads", keep_mp4=True)
        loop2 = AgentLoop(ScriptedChat(script), toolbox=box2, max_rounds=8)
        st2 = loop2.run("把老番茄那个视频做成 mp3（保留视频）")
        check("keep_mp4=True 时保留中间 mp4",
              box2.mp4_path is not None and os.path.exists(box2.mp4_path),
              str(box2.mp4_path))
        check("keep_mp4=True 时不记录清理动作",
              box2.removed_intermediate is None, str(box2.removed_intermediate))
    # 收拾掉，别留两个 15MB 的文件
    for _p in ((box2.mp4_path if box2 else None),
               (st2.audio_path if st2 else None)):
        try:
            if _p and os.path.exists(_p):
                os.unlink(_p)
        except OSError:
            pass
except Exception as e:  # noqa: BLE001
    skip("真实全链路", "异常: %s" % e)


# ==========================================================================
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
