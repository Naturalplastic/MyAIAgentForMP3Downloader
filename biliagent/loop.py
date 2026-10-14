"""Agent 循环（手写 harness 的核心）。

结构：
    LLM 输出 ──► protocol.parse_response ──► 标记解析
                     │
                     ├─ 格式违规 ──► 把 fix 拼进观察，要模型重发（≤2 次）
                     ├─ <final_answer> ──► 结束
                     └─ <tool_call> ──► protocol.validate_directive
                                              │
                                              ├─ 校验失败 ──► 把 fix 回喂（≤2 次）
                                              └─ 通过 ──► TOOLBOX.dispatch
                                                              │
                                                              └─ 观察回喂 ──► 下一轮

护栏（全在 harness，不交给 LLM 决定）：
  - loop_rounds    ≤ 10    总轮数上限
  - format_fixes   ≤ 2     格式违规重发次数
  - 重复动作检测            同一 (action, args) 连续出现 → 判定卡死
  - 强制收尾                达到轮数上限时要求模型总结并退出
"""

from __future__ import annotations

import json
import time

from . import protocol as P
from .bili import BiliFatal
from .context import ContextPolicy
from .protocol import TOOL_SPECS, validate_directive
from .tools import ToolBox

# --------------------------------------------------------------------------
# 默认上限（集中在此，便于调参与测试）
# --------------------------------------------------------------------------
# 默认值统一从 config.GUARDRAILS 取 —— 避免"文档写 10、代码写 8"的漂移
from .config import GUARDRAILS as _G

MAX_LOOP_ROUNDS = _G["max_loop_rounds"]["value"]
MAX_FORMAT_FIXES = _G["max_format_fixes"]["value"]   # **连续**计数，成功后归零
REPEAT_ACTION_LIMIT = _G["repeat_action_limit"]["value"]


class AgentState:
    """一次任务的全部状态。可序列化 → 可回放（对应 §11 验收项 2）。"""

    def __init__(self, user_text: str):
        self.user_text = user_text
        self.round = 0
        self.format_fixes = 0          # 连续格式违规计数
        self.total_format_fixes = 0    # 累计（仅供诊断/回放）
        self.compressions = 0          # 上下文压缩次数
        self.messages: list[dict] = []
        self.trace: list[dict] = []        # 每轮的完整记录
        self.outcome: str | None = None    # finished | asked | max_rounds | stuck | format_stuck
        self.report: str | None = None
        self.mp4_path: str | None = None
        self.audio_path: str | None = None
        self.started = time.time()

    def to_dict(self):
        return {
            "user_text": self.user_text,
            "round": self.round,
            "format_fixes": self.format_fixes,
            "total_format_fixes": self.total_format_fixes,
            "compressions": self.compressions,
            "outcome": self.outcome,
            "report": self.report,
            "mp4_path": self.mp4_path,
            "audio_path": self.audio_path,
            "elapsed": round(time.time() - self.started, 2),
            "trace": self.trace,
            "messages": self.messages,
        }


class AgentLoop:
    """手写 agent loop。

    chat_fn(messages) -> dict 是唯一的模型依赖点，形如：
        {"text": str, "usage": {...}}   或   {"error": "..."}
    把它做成可注入的函数，就能用假 LLM 驱动整个循环（D4），
    也能在 D5 换成真 DeepSeek 而不动 loop 一行代码。
    """

    def __init__(self, chat_fn, toolbox: ToolBox | None = None,
                 max_rounds: int = MAX_LOOP_ROUNDS,
                 max_format_fixes: int = MAX_FORMAT_FIXES,
                 context_policy: ContextPolicy | None = None,
                 verbose: bool = False, logger=None):
        self.chat = chat_fn
        self.box = toolbox or ToolBox()
        self.max_rounds = max_rounds
        self.max_format_fixes = max_format_fixes
        self.ctx = context_policy or ContextPolicy()
        self.verbose = verbose
        self.logger = logger

    # -- 日志 --------------------------------------------------------------
    def _log(self, state: AgentState, event: str, payload: dict):
        if self.logger:
            self.logger(event, payload)
        if self.verbose:
            print("  [%s] %s" % (event, json.dumps(payload, ensure_ascii=False)[:300]))

    # -- 主循环 ------------------------------------------------------------
    def run(self, user_text: str) -> AgentState:
        state = AgentState(user_text)

        # 系统提示词由 TOOL_SPECS + 实测能力生成 —— 模型看到的工具说明
        # 与 harness 实际校验的规则同源，不会漂移
        state.messages.append({
            "role": "system",
            "content": P.render_system_prompt(self.box.capabilities),
        })
        state.messages.append({"role": "user", "content": user_text})

        last_sig = None
        repeat = 0
        executed = {}   # (action,args签名) -> 首次执行轮次；用于重复调用提示

        while state.round < self.max_rounds:
            state.round += 1

            # ---- 0. 上下文预算检查（在花钱之前做）----
            # 放在问模型之前：压缩的目的是让这一次调用不发爆
            state.messages, compressed = self.ctx.maybe_compress(state.messages)
            if compressed:
                state.compressions += 1
                self._log(state, "context_compressed", self.ctx.stats())

            # ---- 1. 问模型 ----
            resp = self.chat(state.messages)
            if resp.get("error"):
                # ⚠️ 不能只写 error 代码（形如 http_401）—— 那对用户毫无信息量。
                # 必须把 message 与 hint 一并带出，否则用户不知道该改什么。
                state.outcome = "llm_error"
                parts = ["模型调用失败：%s" % resp.get("error")]
                if resp.get("message"):
                    parts.append(str(resp["message"]))
                if resp.get("hint"):
                    parts.append("建议：%s" % resp["hint"])
                state.report = "\n".join(parts)
                self._log(state, "llm_error", resp)
                break

            # 用真实 usage 校准估算偏差（估算器不可能精确，这是唯一 ground truth）
            from .context import estimate_messages_tokens
            self.ctx.observe_actual(estimate_messages_tokens(state.messages),
                                    resp.get("usage"))

            raw = resp.get("text", "")
            state.messages.append({"role": "assistant", "content": raw})
            self._log(state, "llm_output", {"round": state.round, "text": raw[:500]})

            # ---- 2. 解析标记 ----
            parsed = P.parse_response(raw)

            if parsed["status"] != P.OK_TOOL_CALL and parsed["status"] != P.OK_FINAL:
                # 格式违规 —— 回喂 fix，要求重发（不计入答复）
                state.format_fixes += 1
                state.total_format_fixes += 1
                self._log(state, "format_violation",
                          {"status": parsed["status"], "fixes": state.format_fixes})

                if state.format_fixes > self.max_format_fixes:
                    state.outcome = "format_stuck"
                    state.report = ("模型连续 %d 次输出格式违规，已放弃。"
                                    "最后一次输出：%s"
                                    % (state.format_fixes, parsed.get("raw", "")[:200]))
                    break

                state.messages.append({
                    "role": "user",
                    "content": "<system>格式错误：%s</system>" % parsed["fix"],
                })
                state.trace.append({"round": state.round, "type": "format_violation",
                                    "status": parsed["status"]})
                continue

            # ---- 3. 终局：最终答复 ----
            if parsed["status"] == P.OK_FINAL:
                state.report = parsed["text"]
                state.outcome = "finished"
                state.trace.append({"round": state.round, "type": "final_answer",
                                    "text": parsed["text"]})
                self._log(state, "final_answer", {"text": parsed["text"][:300]})
                break

            # ---- 4. 工具调用：校验 ----
            directive = parsed["directive"]
            check = validate_directive(directive)

            if not check["ok"]:
                state.format_fixes += 1
                state.total_format_fixes += 1
                self._log(state, "invalid_directive",
                          {"error": check["error"], "got": directive})
                if state.format_fixes > self.max_format_fixes:
                    state.outcome = "format_stuck"
                    state.report = ("模型连续输出非法指令，已放弃。最后错误：%s"
                                    % check.get("fix", check["error"]))
                    break
                state.messages.append({
                    "role": "user",
                    "content": "<system>指令非法：%s</system>" % check["fix"],
                })
                state.trace.append({"round": state.round, "type": "invalid_directive",
                                    "error": check["error"]})
                continue

            action, args = check["action"], check["args"]

            # ---- 5. 重复动作检测（防模型卡死在同一个调用上）----
            sig = (action, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if sig == last_sig:
                repeat += 1
            else:
                repeat = 0
                last_sig = sig
            if repeat >= REPEAT_ACTION_LIMIT:
                state.outcome = "stuck"
                state.report = ("检测到重复动作 %s %s 连续 %d 次，判定卡死并停止。"
                                % (action, args, repeat + 1))
                self._log(state, "stuck", {"action": action, "args": args})
                break

            # ---- 6. 执行 ----
            # ⚠️ 这里必须自己兜异常，不能假设 ToolBox 会吞掉。
            # 否则任何工具实现的一处疏漏都会让整个循环崩掉，
            # 而模型看到的是 Python 堆栈 —— 它完全无法据此自救。
            self._log(state, "tool_call", {"action": action, "args": args})
            try:
                result = self.box.dispatch(action, args, state)
            except BiliFatal as e:
                result = e.payload
            except Exception as e:  # noqa: BLE001
                result = {
                    "ok": False,
                    "error": "tool_crashed",
                    "message": "%s: %s" % (type(e).__name__, e),
                    "hint": "工具内部异常。请检查参数是否合理，或改用其他方式达成目标。",
                }

            # ---- 7. 终局 action ----
            if result.get("terminal"):
                if result.get("kind") == "finish":
                    state.report = result.get("summary", "")
                    state.outcome = "finished"
                else:
                    state.report = result.get("question", "")
                    state.outcome = "asked"
                state.trace.append({"round": state.round, "type": result.get("kind"),
                                    "action": action, "text": state.report})
                break

            # ---- 8. 压缩观察并回喂 ----
            obs = ToolBox.make_observation(action, result)
            obs["_action"] = action

            # 重复调用提示（软信号，不是硬拦）。
            # 为什么不做硬拦：重复调用有时是正当的（比如重新取过期的签名 URL），
            # 硬拦会破坏合法场景。所以只把"这个动作之前已经做过"告诉模型，
            # 由它判断要不要继续 —— 判断权留给模型，信息由 harness 补齐。
            if obs.get("ok") and sig in executed:
                obs["_note"] = ("这个动作此前已用相同参数执行过（第 %d 轮），"
                                "这是重复调用。若非有意重试，请直接使用之前的结果 "
                                "推进任务，不要重复执行。" % executed[sig])
                self._log(state, "duplicate_action",
                          {"action": action, "args": args,
                           "first_at_round": executed[sig]})
            executed[sig] = state.round

            self._log(state, "observation", obs)
            state.trace.append({"round": state.round, "type": "tool_result",
                                "action": action, "args": args, "observation": obs})

            state.messages.append({
                "role": "user",
                "content": "<observation>%s</observation>"
                           % json.dumps(obs, ensure_ascii=False),
            })

            # 成功动作后重置「连续」违规计数 —— 累计值保留供诊断。
            # 为什么必须重置：模型偶尔格式错、但整体在推进，不应被误判为卡死。
            if obs.get("ok"):
                state.format_fixes = 0

        else:
            # while 正常退出 = 达到轮数上限
            state.outcome = "max_rounds"
            state.report = ("达到最大轮数 %d，任务未完成。最后一次工具结果：%s"
                            % (self.max_rounds,
                               json.dumps(state.trace[-1], ensure_ascii=False)[:300]
                               if state.trace else "无"))

        state.mp4_path = self.box.mp4_path
        state.audio_path = self.box.audio_path
        return state


# --------------------------------------------------------------------------
# Fake LLM：用预设脚本驱动整个循环（D4 的验收方式）
# --------------------------------------------------------------------------

class ScriptedChat:
    """按脚本返回固定输出的假模型。

    为什么先做这个：它让 loop 的逻辑可以**脱离 LLM 的不确定性**单独验证。
    出问题时你能确定是 loop 的 bug，而不是模型今天不听话。
    """

    def __init__(self, script: list[str], name: str = "scripted"):
        self.script = list(script)
        self.name = name
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        if not self.script:
            # 脚本用尽 —— 返回一个合法终止，避免测试挂死
            return {"text": "<final_answer>脚本已用尽（模型没有更多输出）。</final_answer>"}
        return {"text": self.script.pop(0)}
