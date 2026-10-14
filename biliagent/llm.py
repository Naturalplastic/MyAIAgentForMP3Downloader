"""DeepSeek API 适配器。

这是 loop 与真实模型的**唯一接触点**。AgentLoop 只依赖一个
`chat_fn(messages) -> {"text": ...}`，所以换模型 / 换供应商只需换这个文件，
loop 一行都不用改。

实测事实（2026-10-14，见 docs/FACTS.md §8）：
  * endpoint     https://api.deepseek.com/chat/completions（OpenAI 兼容）
  * 可用模型     deepseek-flash、deepseek-v4-pro（均 1M 上下文 / 393K 输出上限）
  * 旧名映射     deepseek-chat / deepseek-reasoner 仍可用，但都路由到 deepseek-flash
  * reasoning    v4 系列返回 reasoning_content 字段（思考过程）
  * json mode    response_format={"type":"json_object"} 支持
  * 函数调用     tools / tool_choice 支持（作为本项目的对照方案，见下方说明）
  * reasoning 回传  带 reasoning_content 回传不报错；剥掉也正常

## 关于"为什么不用 response_format 也不用 function calling"

本项目的学习目标是**手写工具调用协议**。而：
  - `response_format=json_object` 要求模型只输出纯 JSON，
    与我们的 `<tool_call>…</tool_call>` 标记包裹**结构上冲突**，不能同用。
  - 原生 `tools`/`tool_calls` 是更稳的工业方案，但用了它，
    protocol.py 里那套解析/校验/自愈逻辑就全无用武之地 —— 学习目标就没了。

所以这里刻意**两者都不用**，只用纯文本 + prompt 约束 + 本地解析兜底。
这个选择是有意的，代价是格式稳定性略差，收益是真正理解协议层在解决什么问题。
（`tools` 的对照实现留在 `chat_with_tools()` 里，供对比实验。）

## 安全

API key **只从环境变量读取，绝不写入代码或日志**。
`_redact()` 会确保任何要落盘的文本里都不会出现 key。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .env import load_env, missing_key_hint

# 模块级加载一次 —— 这样即使不走 CLI（比如在别的 Python 代码里 import 本模块），
# .env 里的 key 也能生效。重复调用是幂等的。
load_env()

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
ENV_KEY = "DEEPSEEK_API_KEY"
ENV_BASE = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"

TIMEOUT = 180          # 推理模型可能想很久
MAX_RETRIES = 2

# 可重试的 HTTP 状态（服务端抖动）
RETRY_HTTP = {500, 502, 503, 504, 429}


class LLMError(Exception):
    def __init__(self, payload):
        super().__init__(payload.get("message", "llm error"))
        self.payload = payload


def redact(text: str) -> str:
    """从任意文本中抹掉 API key，防止落盘泄漏。"""
    key = os.environ.get(ENV_KEY)
    if key and len(key) > 8 and key in (text or ""):
        return text.replace(key, key[:7] + "***REDACTED***")
    return text


def _post(url: str, body: dict, api_key: str, timeout: int = TIMEOUT):
    """POST JSON。返回 (status, payload_dict)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        try:
            parsed = json.loads(body_text)
        except Exception:
            parsed = {"raw": body_text[:500]}
        return e.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return -1, {"error": {"message": str(e), "type": "network_error"}}


class DeepSeekClient:
    """DeepSeek chat 客户端。

    只做三件事：发请求、重试、把响应整理成 loop 需要的形状。
    不做任何协议解析 —— 那是 protocol.py 的职责。
    """

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, effort: str | None = None,
                 temperature: float | None = None):
        self.api_key = api_key or os.environ.get(ENV_KEY)
        if not self.api_key:
            raise LLMError({
                "error": "missing_api_key",
                "message": "未找到 %s（环境变量与 .env 文件里都没有）" % ENV_KEY,
                "hint": missing_key_hint(),
            })
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get(ENV_BASE) or DEFAULT_BASE).rstrip("/")
        self.effort = effort            # None / low / high / max
        self.temperature = temperature
        self.calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_reasoning_tokens = 0

    # -- 供 loop 使用的 chat_fn -------------------------------------------
    def __call__(self, messages):
        return self.chat(messages)

    def chat(self, messages: list[dict]) -> dict:
        """发一次对话。

        返回 loop 期望的形状：
          {"text": str, "usage": {...}, "reasoning_len": int, "model": str}
        出错：
          {"error": str, "message": str, "hint": str, "http_status": int}
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        if self.temperature is not None:
            body["temperature"] = self.temperature

        url = "%s/chat/completions" % self.base_url
        last = None

        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                time.sleep(2 ** (attempt - 1))
            status, payload = _post(url, body, self.api_key)
            self.calls += 1

            if status == 200 and "choices" in payload:
                return self._shape_ok(payload)

            # 整理错误
            err = payload.get("error") or {}
            msg = err.get("message") or payload.get("raw") or "未知错误"
            msg = redact(str(msg))

            if status in RETRY_HTTP and attempt < MAX_RETRIES:
                last = {"status": status, "msg": msg}
                continue

            return self._shape_err(status, msg)

        return self._shape_err(last["status"], "重试 %d 次后仍失败：%s"
                              % (MAX_RETRIES, last["msg"]))

    def _shape_ok(self, payload: dict) -> dict:
        choice = (payload.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""

        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        self.total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
        self.total_completion_tokens += usage.get("completion_tokens", 0) or 0
        self.total_reasoning_tokens += details.get("reasoning_tokens", 0) or 0

        return {
            "text": text,
            # 思考过程只记长度不保留内容：
            #  ① 回传会持续吃掉上下文（1M 也经不起几轮）
            #  ② 我们只关心最终的结构化输出
            "reasoning_len": len(reasoning),
            "finish_reason": choice.get("finish_reason"),
            "model": payload.get("model"),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": details.get("reasoning_tokens"),
            },
        }

    def _shape_err(self, status: int, msg: str) -> dict:
        hint = ""
        if status == 401:
            hint = "API key 无效或已失效。请在 DeepSeek 控制台确认，并检查环境变量。"
        elif status == 402:
            hint = "余额不足。请充值后重试。"
        elif status == 429:
            hint = "触发限流。降低请求频率或稍后重试。"
        elif status in RETRY_HTTP:
            hint = "服务端错误，已重试仍失败。稍后再试。"
        elif status == -1:
            hint = "网络不可达。检查网络或 base_url 配置。"
        return {
            "error": "http_%s" % status if status > 0 else "network_error",
            "message": msg,
            "hint": hint,
            "http_status": status,
            "model": self.model,
        }

    def stats(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "reasoning_tokens": self.total_reasoning_tokens,
        }


# --------------------------------------------------------------------------
# 对照实现：原生 function calling（本项目刻意不用，仅供对比实验）
# --------------------------------------------------------------------------

def chat_with_tools(client: DeepSeekClient, messages: list[dict],
                    tools: list[dict]) -> dict:
    """用 DeepSeek 原生 tools/tool_calls 完成同样的任务。

    **本项目不使用它** —— 保留它是为了做一个诚实的对照：
    用原生方案，protocol.py 里那 295 行解析/校验/自愈逻辑全部消失，
    可靠性立刻提升（结构化输出由 API 保证）。
    这就是"为什么工业界都用 function calling"的第一手答案。
    """
    body = {"model": client.model, "messages": messages,
            "tools": tools, "tool_choice": "auto", "stream": False}
    url = "%s/chat/completions" % client.base_url
    status, payload = _post(url, body, client.api_key)
    if status != 200:
        return client._shape_err(status, str(payload.get("error", payload))[:300])
    return client._shape_ok(payload) | {
        "tool_calls": (payload["choices"][0]["message"] or {}).get("tool_calls"),
    }


def from_env(**kwargs) -> DeepSeekClient:
    """按环境变量构造客户端（CLI 用）。"""
    return DeepSeekClient(**kwargs)
