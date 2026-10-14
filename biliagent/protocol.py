"""LLM 调用协议：标记解析 + action 校验 + 工具 Schema。

这一层解决三个问题（都是手写 harness 最容易崩的地方）：

  1. **不要把"解析失败"和"它不是工具调用"混为一谈**
     v0.1 用「整段 JSON.parse，失败即判为最终答复」。漏洞：模型输出
     `好的，我来搜索：\n{"action":...}` 时整段 parse 失败 → 被误判成答复
     直接返回给用户，而模型本意是调工具。
     对策：**标记包裹**。有 `<tool_call>` 就是指令（解析失败要它重发），
     有 `<final_answer>` 才是答复。两件事在结构上彻底分离。

  2. **非法输出要能自愈**
     `validate_directive()` 返回带 `fix` 字段的错误 —— 这个字段会被拼进
     下一轮的观察里，等于告诉模型"你哪里错了、怎么改"。

  3. **工具 Schema 是唯一的真相来源**
     系统提示词由 `render_system_prompt()` 从 `TOOL_SPECS` 生成。
     这样"模型看到的工具说明"和"harness 实际校验的规则"永远一致 ——
     手写两份必然漂移。
"""

from __future__ import annotations

import json
import re

# --------------------------------------------------------------------------
# 标记定义
# --------------------------------------------------------------------------

TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"
FINAL_OPEN = "<final_answer>"
FINAL_CLOSE = "</final_answer>"

_TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FINAL_RE = re.compile(r"<final_answer>\s*(.*?)\s*</final_answer>", re.DOTALL)

# 解析状态码（供 loop 决定下一步，而不是靠猜）
OK_TOOL_CALL = "tool_call"          # 合法的工具调用
OK_FINAL = "final_answer"           # 合法的最终答复
ERR_NO_MARKER = "no_marker"         # 两个标记都没有 ← 纯文本，最需要纠正
ERR_BOTH_MARKERS = "both_markers"   # 两个都出现 ← 必须二选一
ERR_MULTIPLE = "multiple_calls"     # 多个 tool_call ← 必须一次一个
ERR_BAD_JSON = "bad_json"           # 标记对了但内容不是合法 JSON
ERR_NOT_OBJECT = "not_object"       # JSON 合法但不是对象
ERR_EMPTY_FINAL = "empty_final"     # 最终答复为空


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def parse_response(raw: str) -> dict:
    """把模型的一次原始输出解析成结构化结果。

    返回：
      {status, directive?|text?, raw, fix?}

    `status` 是 OK_* 之一表示成功；是 ERR_* 表示格式违规。
    违规时 `fix` 是给模型看的具体纠正指示（会被拼进下一轮观察）。
    """
    if raw is None:
        raw = ""
    text = raw.strip()

    tool_hits = _TOOL_RE.findall(text)
    final_hits = _FINAL_RE.findall(text)

    # 两个标记同时出现 —— 违反"每轮只输出一种"
    if tool_hits and final_hits:
        return {
            "status": ERR_BOTH_MARKERS,
            "raw": raw,
            "fix": ("你在同一轮里同时输出了 <tool_call> 和 <final_answer>。"
                    "每轮只能输出其中一种。若任务已完成，只输出 <final_answer>；"
                    "否则只输出一个 <tool_call>。"),
        }

    # 多个工具调用 —— 必须一次一个（否则 harness 无法判断依赖顺序）
    if len(tool_hits) > 1:
        return {
            "status": ERR_MULTIPLE,
            "raw": raw,
            "fix": ("你输出了 %d 个 <tool_call>。每轮只能输出一个，"
                    "因为后一个调用可能依赖前一个的结果。请只保留当前最该执行的那一个。"
                    % len(tool_hits)),
        }

    if tool_hits:
        body = tool_hits[0]
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            return {
                "status": ERR_BAD_JSON,
                "raw": raw,
                "bad_body": body,
                "fix": ("<tool_call> 内的内容不是合法 JSON（%s）。"
                        "必须严格形如 <tool_call>{\"action\":\"...\",\"args\":{...}}</tool_call>，"
                        "不要用 markdown 代码块包裹，不要加注释或额外文字。" % e.msg),
            }
        if not isinstance(obj, dict):
            return {
                "status": ERR_NOT_OBJECT,
                "raw": raw,
                "fix": ("<tool_call> 内必须是 JSON 对象（形如 {\"action\":...,\"args\":{...}}），"
                        "而你给的是 %s。" % type(obj).__name__),
            }
        return {"status": OK_TOOL_CALL, "directive": obj, "raw": raw}

    if final_hits:
        answer = final_hits[0].strip()
        if not answer:
            return {
                "status": ERR_EMPTY_FINAL,
                "raw": raw,
                "fix": "你的 <final_answer> 是空的。请给出对用户的完整汇报。",
            }
        return {"status": OK_FINAL, "text": answer, "raw": raw}

    # 两个标记都没有 —— 这通常意味着模型直接说了句人话
    return {
        "status": ERR_NO_MARKER,
        "raw": raw,
        "fix": ("你的输出没有被任何标记包裹。必须严格二选一：\n"
                "  要调用工具：<tool_call>{\"action\":\"...\",\"args\":{...}}</tool_call>\n"
                "  要结束任务：<final_answer>你的汇报</final_answer>\n"
                "不要输出标记之外的任何文字。"),
    }


# --------------------------------------------------------------------------
# 工具 Schema —— 唯一真相来源
# --------------------------------------------------------------------------

# required: 必填参数名
# schema:   参数名 → 允许的取值列表（None 表示任意）
TOOL_SPECS = {
    "search": {
        "desc": "按关键词搜索 B 站视频，返回候选列表（含 bvid/标题/UP主/播放量/相关性）",
        "required": ["keyword"],
        "schema": {"keyword": None, "limit": [1, 2, 3, 4, 5]},
    },
    "get_info": {
        "desc": "查看指定 bvid 的视频详情（标题、UP主、时长、分P）",
        "required": ["bvid"],
        "schema": {"bvid": None},
    },
    "download": {
        "desc": "下载指定 bvid 的视频为 mp4（游客画质 360P）",
        "required": ["bvid"],
        "schema": {"bvid": None},
    },
    "extract_audio": {
        "desc": "从已下载的 mp4 中抽取音频：mp3=转码(约168kbps)，copy=无损复制(.m4a)",
        "required": ["mode"],
        "schema": {"mode": ["mp3", "copy"]},
    },
    "ask_user": {
        "desc": "向用户提问以澄清需求（当描述含糊、或需要确认选择时使用）",
        "required": ["question"],
        "schema": {"question": None},
    },
    "finish": {
        "desc": "任务完成，向用户汇报结果",
        "required": ["summary"],
        "schema": {"summary": None},
    },
}

ACTION_NAMES = set(TOOL_SPECS)


def validate_directive(directive: dict) -> dict:
    """校验一个已解析的 directive。

    返回 {ok: True, action, args} 或 {ok: False, error, fix, ...}
    `fix` 会被拼进下一轮观察，让模型自我修正 —— 这是"非法输出自愈"的实现。
    """
    action = directive.get("action")
    if not action:
        return {
            "ok": False, "error": "missing_action",
            "fix": "缺少 action 字段。必须是 %s 之一。" % sorted(ACTION_NAMES),
        }
    if not isinstance(action, str) or action not in TOOL_SPECS:
        return {
            "ok": False, "error": "unknown_action", "got": str(action)[:40],
            "fix": "action %r 不存在。可用 action：%s。不要发明新工具。"
                   % (str(action)[:40], sorted(ACTION_NAMES)),
        }

    spec = TOOL_SPECS[action]
    args = directive.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {
            "ok": False, "error": "args_not_object", "action": action,
            "fix": "args 必须是 JSON 对象（可为空 {}），你给的是 %s。"
                   % type(args).__name__,
        }

    missing = [k for k in spec["required"] if k not in args or args[k] in (None, "")]
    if missing:
        return {
            "ok": False, "error": "missing_args", "action": action, "missing": missing,
            "fix": "action %r 缺少必填参数 %s。预期参数：%s。"
                   % (action, missing, sorted(spec["schema"])),
        }

    # 枚举校验
    for key, allowed in spec["schema"].items():
        if allowed is not None and key in args and args[key] not in allowed:
            return {
                "ok": False, "error": "bad_arg_value", "action": action,
                "arg": key, "got": args[key], "allowed": allowed,
                "fix": "参数 %s 的值 %r 不合法，只能是 %s 之一。"
                       % (key, args[key], allowed),
            }

    unknown = [k for k in args if k not in spec["schema"]]
    if unknown:
        return {
            "ok": False, "error": "unknown_args", "action": action, "unknown": unknown,
            "fix": "action %r 不接受参数 %s。它只接受：%s。"
                   % (action, unknown, sorted(spec["schema"])),
        }

    return {"ok": True, "action": action, "args": args}


# --------------------------------------------------------------------------
# 系统提示词渲染
# --------------------------------------------------------------------------

def render_system_prompt(capabilities: dict | None = None, extra_rules=()) -> str:
    """从 TOOL_SPECS 生成系统提示词。

    这样工具说明与校验规则同源，不会漂移。
    capabilities: probe_capabilities() 的结果，用于告诉模型"本机能不能做 mp3"。
    """
    lines = [
        "你是一个 B 站媒体下载助手。你通过调用工具完成任务，不能直接访问网络。",
        "",
        "## 输出格式（强制）",
        "你每轮必须输出且只输出以下之一，用标记包裹，标记外不要有任何文字：",
        "",
        '  <tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>',
        "  <final_answer>任务完成，文件在 downloads/xxx.mp3</final_answer>",
        "",
        "规则：",
        "1. 每轮只能输出一个 <tool_call>，不能并列多个（后一个可能依赖前一个的结果）",
        "2. 不要用 markdown 代码块包裹 JSON，不要加注释",
        "3. 不确定用户意图时用 ask_user 提问，不要瞎猜",
        "4. 搜索最多重试 3 次（换关键词），之后应 ask_user",
        "5. 若观察里出现 warning 或 error 字段，先读懂它的 hint 再决定下一步",
        "6. 任务完成后必须用 finish 或 <final_answer> 汇报，不要静默结束",
        "",
        "## 可用工具",
    ]

    for name in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[name]
        params = []
        for p in sorted(spec["schema"]):
            mark = "必填" if p in spec["required"] else "可选"
            allowed = spec["schema"][p]
            if allowed is None:
                params.append("%s(%s)" % (p, mark))
            elif all(isinstance(x, int) for x in allowed):
                params.append("%s(%s, 取 %s)" % (p, mark, allowed))
            else:
                params.append("%s(%s, 取 %s)" % (p, mark, allowed))
        lines.append("  - %s: %s" % (name, spec["desc"]))
        lines.append("      参数: %s" % ("，".join(params) if params else "无"))

    # 能力感知 —— 让模型知道本机能做到哪一步
    if capabilities is not None:
        lines += ["", "## 本机能力"]
        if capabilities.get("ok") and capabilities.get("ffmpeg"):
            lines.append("  - ffmpeg 可用（v%s）：可以产出 mp3"
                         % capabilities["ffmpeg"].get("version"))
            if not capabilities.get("can_encode_mp3"):
                lines.append("  - ⚠️ 但该 ffmpeg 不能编码 mp3，extract_audio 只能用 mode=copy")
        else:
            lines.append("  - ❌ 未检测到 ffmpeg：**无法抽取或转码音频**。")
            lines.append("    若用户要音频，应说明只能交付 mp4，或询问是否接受 mp4。"
                         "不要反复尝试 extract_audio。")

    if extra_rules:
        lines += ["", "## 附加规则"] + ["  - %s" % r for r in extra_rules]

    return "\n".join(lines)
