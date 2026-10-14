"""上下文管理：估算 + 压缩。

为什么必须有这一层：
  每一轮的观察都会完整进入 messages。搜索 5 条候选 ≈ 800 tokens，
  一次长任务几轮下来 Context 就膨胀到几万 tokens。后果有三个：
    ① 迟早撞上下文上限，任务直接失败
    ② 成本无上限（每轮都要重发全部历史）
    ③ 信号被噪声淹没（模型被旧观察干扰）

压缩策略（刻意简单、确定性、不依赖模型）：
  ┌─ 永久保留 ────────────────────────────────┐
  │ system 提示词      ← 丢了模型就忘了规则      │
  │ 原始用户任务        ← 丢了就目标漂移          │
  ├─ 完整保留 ────────────────────────────────┤
  │ 最近 K 轮（默认 3）                         │
  ├─ 压缩 ────────────────────────────────────┤
  │ 更早轮次：把 assistant 的原始输出 + 观察       │
  │ 合成一条简短摘要，标注 [已压缩]               │
  └───────────────────────────────────────────┘

为什么不调用模型做摘要：那样每压缩一次就多一次 API 调用，成本与延迟都翻倍，
而且引入了不确定性。对这种结构化很强的对话，确定性压缩足够用。
（等真遇到"压缩丢关键信息"的问题，再上模型摘要也不迟 —— 到那时是有依据的决定。）
"""

from __future__ import annotations

import json
import re

# 粗略 token 估算 —— 标定过程与最终取舍（2026-10-14，deepseek-flash）
#
# 三次尝试的记录（这段试错本身就是一条工程教训）：
#
#   ① 按"中文/非中文"两类系数 → 通用长样本误差最大 1.9 倍
#   ② 按"汉字/字母/数字/符号"四类标定 → 看似精确但**不能泛化**：
#      同样"纯中文"，重复串实测 0.546 token/字，而单字重复串 1.044，差 1.9 倍。
#      原因：BPE tokenizer 对重复内容压缩率极高，字符统计抓不住这个本质。
#   ③ 用单一系数 → 中文和英文相差 3.3 倍（1.044 vs 0.322），
#      任何单值都会在某一侧错得离谱（实测误差到 2.9 倍）。
#
#   最终方案：**按中文占比在双锚点间线性插值**。
#   两个锚点是实测的端值，插值对中间构成合理，且对我们真实的中文为主内容准确。
#
# 本项目真实内容的实测表现（tok/字符）：
#   系统提示词 0.52 ｜ 搜索观察 0.50 ｜ 下载观察 0.65 ｜ tool_call 0.58 ｜ 最终答复 0.79
#
# 最终实测精度（对真实 API 的 usage.prompt_tokens 逐个核对）：
#   │ 样本              真实  ｜ 估算 ｜ 估算/真实
#   │ 纯中文（真实句）    247  ｜  322 ｜ 1.30
#   │ 中英混合           522  ｜  671 ｜ 1.29
#   │ JSON 观察          343  ｜  259 ｜ 0.76
#   │ 代码/符号          782  ｜  851 ｜ 1.09
#   │ 系统提示词          496  ｜  544 ｜ 1.10
#   │ 真实最终答复         84  ｜   56 ｜ 0.67
#   → 平均比值 1.03，范围 0.67~1.30
#
# ⚠️ 明确的能力边界：这仍是**估算**，某些内容会低估到 0.67。
#    所以：① 预算应留余量（ContextPolicy 默认 20000 而非贴着上限）
#         ② loop 会把**真实 usage**记下来与估算对照（observe_actual），
#            让"估算偏保守还是偏激进"是有数据的问题，而不是凭感觉。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                  r"\u3040-\u30ff\uac00-\ud7af]")

CJK_ANCHOR = 1.044     # 实测端值：纯中文 token/字符
OTHER_ANCHOR = 0.322   # 实测端值：纯英文 token/字符


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    ⚠️ 近似值，不是精确 tokenizer（DeepSeek 未提供可本地调用的 tokenizer）。
    用途：判断"该不该压缩上下文"。**不要用它计费** —— 计费请读 API 的 usage。
    """
    if not text:
        return 0
    text = str(text)
    n = len(text)
    if not n:
        return 1
    r = len(_CJK.findall(text)) / n
    return int(n * (CJK_ANCHOR * r + OTHER_ANCHOR * (1 - r))) + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算整段对话的 token 数（含少量结构开销）。"""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", ""))
        total += 4          # role / 分隔符等开销
    return total


# --------------------------------------------------------------------------
# 压缩
# --------------------------------------------------------------------------

_MARK = "[已压缩]"

# 压缩产物的标记。**已被压缩的内容绝不再次压缩。**
#
# 为什么需要这个保护（由一次真实运行的日志发现）：
#   压缩不是一次性的。循环继续推进时会不断追加新消息，于是下一轮又会超预算、
#   再次压缩。如果不对压缩产物做保护，已经压成摘要的内容会被**反复再压缩**，
#   形成自我嵌套：
#       <history_summary><history>[已压缩] ｜ [已压缩] <history_summary> …
#   实测一次运行里嵌套到 3 层，每层还各自截断一次 —— 信息在层层压缩中丢失。
#   这是"压缩把要保的东西压没了"，比不压缩更糟。
SUMMARY_TAG = "<history_summary>"
SUMMARY_TAG_END = "</history_summary>"


def _summarize_observation(content: str, max_len: int = 120) -> str:
    """把一条 <observation> 压成一行。"""
    raw = content.strip()
    m = re.match(r"<observation>(.*)</observation>\s*$", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    try:
        obj = json.loads(raw)
    except Exception:
        return "%s %s" % (_MARK, raw[:max_len])

    action = obj.get("_action") or obj.get("action") or "?"
    if obj.get("ok") is False:
        detail = obj.get("error", "error")
        hint = obj.get("hint")
        text = "失败(%s)" % detail
        if hint:
            text += "；" + str(hint)[:60]
    else:
        bits = []
        for k in ("path", "keyword", "matched", "size_mb", "codec", "title"):
            if k in obj and obj[k] not in (None, ""):
                v = obj[k]
                if k == "title":
                    v = str(v)[:24]
                bits.append("%s=%s" % (k, v))
        text = "成功" + ("（" + "，".join(bits) + "）" if bits else "")
        if obj.get("warning"):
            text += "；⚠" + str(obj.get("warning"))
    return "%s %s: %s" % (_MARK, action, text[:max_len])


def _summarize_assistant(content: str, max_len: int = 80) -> str:
    """把一条 assistant 的原始输出压成一行（只留 action 名）。"""
    raw = (content or "").strip()
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            return '%s 调用 %s' % (_MARK, obj.get("action", "?"))
        except Exception:
            pass
    if "<final_answer>" in raw:
        return "%s 给出最终答复" % _MARK
    return "%s 模型输出（%d 字符）" % (_MARK, len(raw))


def _cap_history(blocks: list[str], max_chars: int = 1400) -> list[str]:
    """给压缩历史加"滑动窗口"上限。

    为什么必须加：一开始以为"摘要只有一条"就够了 —— 但实测发现
    消息条数恒定为 1，**摘要内容却仍在无限增长**（每次压缩往里追加）。
    30 轮后摘要 1902 字符且持续增加。

    结论：**"消息条数受控"不等于"上下文受控"**。压缩产物自身也必须
    有大小上限，否则压缩只是把增长从"多条消息"搬到了"一条消息"里。

    策略：保留最近的条目，超出上限的旧条目**整行丢弃**
    （而不是截断某一行 —— 截断会产生半截信息，比丢掉更糟）。
    """
    if max_chars <= 0:
        return blocks
    total = sum(len(b) for b in blocks)
    if total <= max_chars:
        return blocks

    # 从新往旧保留
    kept: list[str] = []
    used = 0
    for b in reversed(blocks):
        if used + len(b) > max_chars and kept:
            break
        kept.append(b)
        used += len(b)
    kept.reverse()

    omitted = len(blocks) - len(kept)
    if omitted > 0:
        kept.insert(0, "<history>…（更早的 %d 条已省略以控制上下文）</history>"
                    % omitted)
    return kept


def _merge_summaries(pinned: list[dict]) -> list[dict]:
    """把多条历史摘要合并成一条，防止它们随压缩次数线性堆积。

    为什么需要：压缩不是一次性的。若每次都新增一条摘要并"永久保留"，
    摘要条数就随压缩次数增长（实测一次运行累积 6 条），
    等于"压缩机制自己变成了上下文增长源"。
    合并后：摘要在整段对话里恒定只有一条。
    """
    if not pinned:
        return []
    if len(pinned) == 1:
        return pinned

    bodies = []
    for m in pinned:
        c = m.get("content", "")
        c = c.replace(SUMMARY_TAG, "").replace(SUMMARY_TAG_END, "").strip()
        if c:
            bodies.append(c)
    merged = "\n".join(bodies)
    return [{"role": "user",
             "content": "%s\n%s\n%s" % (SUMMARY_TAG, merged, SUMMARY_TAG_END)}]


def compress_messages(messages: list[dict], keep_recent: int = 3,
                      max_history_chars: int = 1400) -> tuple[list, int]:
    """压缩对话历史。返回 (新 messages, 被压缩的条数)。

    规则：
      * messages[0]（system）与第一条 user（原始任务）永久保留
      * **已有摘要合并为一条、绝不再次压缩**（防自我嵌套、防摘要堆积）
      * 最近 keep_recent 轮完整保留
      * 更早的 assistant + 其后的 observation 合并为一条摘要
    """
    if len(messages) <= 2 + keep_recent * 2:
        return messages, 0

    head = messages[:2]                     # system + 原始任务
    pinned = [m for m in messages[2:] if SUMMARY_TAG in m.get("content", "")]
    body = [m for m in messages[2:] if SUMMARY_TAG not in m.get("content", "")]

    cut = len(body) - keep_recent * 2
    if cut <= 0:
        # 没有可压的老消息 —— 原样返回（保持顺序不动）
        return messages, 0

    old, recent = body[:cut], body[cut:]

    # 把老的成对消息合并
    merged: list[dict] = []
    pending_assistant = None
    dropped = 0
    for m in old:
        role = m.get("role")
        if role == "assistant":
            pending_assistant = m
        elif role == "user":
            # assistant 缺失时不产生"（无对应动作）"这类空壳条目 —— 只会污染摘要
            parts = []
            if pending_assistant is not None:
                parts.append(_summarize_assistant(pending_assistant["content"]))
            o = _summarize_observation(m.get("content", ""))
            if o:
                parts.append(o)
            if not parts:
                continue
            merged.append("<history>%s</history>" % " ｜ ".join(parts))
            dropped += 1
            pending_assistant = None
        else:
            merged.append("<history>%s</history>" % m.get("content", "")[:120])

    # 结构：head + [合并后的摘要（旧的在前，新的在后）] + recent
    blocks = []
    prev = _merge_summaries(pinned)
    if prev:
        blocks.append(prev[0]["content"]
                      .replace(SUMMARY_TAG, "").replace(SUMMARY_TAG_END, "").strip())
    # 只取每个 <history> 块作为可滑动窗口的单元
    if merged:
        blocks.extend(merged)

    blocks = _cap_history(blocks, max_chars=max_history_chars)

    if blocks:
        summary = {"role": "user",
                   "content": "%s\n%s\n%s" % (SUMMARY_TAG, "\n".join(blocks),
                                              SUMMARY_TAG_END)}
        out = head + [summary] + recent
    else:
        out = head + recent

    return out, dropped


class ContextPolicy:
    """上下文预算策略。

    除了"该不该压缩"，它还承担一件容易被忽略的事：
    **记录估算值与真实 usage 的偏差**。

    因为 token 估算不可能精确，唯一可靠的 ground truth 是 API 返回的
    usage.prompt_tokens。把它跟估算值一起记下来，就能回答：
      "我们的预算判断是偏保守还是偏激进？偏差多少？"
    而不是永远凭感觉调参数。
    """

    def __init__(self, max_tokens: int = 20000, keep_recent: int = 3,
                 enabled: bool = True):
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self.enabled = enabled
        self.compressions = 0
        self.total_dropped = 0
        self.peak_tokens = 0
        # 估算 vs 真实
        self.observed = []          # [(estimated, actual), ...]

    def maybe_compress(self, messages: list[dict]) -> tuple[list[dict], bool]:
        """超预算就压缩。返回 (messages, 是否压缩过)。"""
        n = estimate_messages_tokens(messages)
        self.peak_tokens = max(self.peak_tokens, n)
        if not self.enabled or n <= self.max_tokens:
            return messages, False

        out, dropped = compress_messages(messages, keep_recent=self.keep_recent)
        if dropped == 0:
            # 已经压无可压 —— 只能继续，但要如实记录（而不是假装没事）
            return out, False

        self.compressions += 1
        self.total_dropped += dropped
        return out, True

    def observe_actual(self, estimated: int, usage: dict | None):
        """把本轮的真实 usage 记下来，用于事后校准估算偏差。"""
        if not usage:
            return
        actual = usage.get("prompt_tokens")
        if isinstance(actual, int) and actual > 0:
            self.observed.append((estimated, actual))

    def calibration(self) -> dict:
        """估算偏差统计。ratio ~1.0 表示估算准确；>1 表示偏保守。"""
        if not self.observed:
            return {"samples": 0}
        ratios = [e / a for e, a in self.observed if a]
        return {
            "samples": len(ratios),
            "mean_ratio": round(sum(ratios) / len(ratios), 3),
            "min_ratio": round(min(ratios), 3),
            "max_ratio": round(max(ratios), 3),
            "note": "ratio = 估算/真实；>1 偏保守（安全），<1 偏激进（危险）",
        }

    def stats(self) -> dict:
        return {
            "max_tokens": self.max_tokens,
            "keep_recent": self.keep_recent,
            "enabled": self.enabled,
            "compressions": self.compressions,
            "dropped_messages": self.total_dropped,
            "peak_tokens": self.peak_tokens,
            "calibration": self.calibration(),
        }
