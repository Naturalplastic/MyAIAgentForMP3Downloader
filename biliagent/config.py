"""可配置项。

设计意图：把"外部工具在哪"从代码里赶出去，变成显式配置。
这对 agent 项目尤其重要 —— 模型/harness 需要能**查询**"我现在能用什么"，
而不是假设 ffmpeg 一定在 PATH 里。
"""

from __future__ import annotations

import os

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 便携工具目录（本项目实测可用的 ffmpeg/ffprobe 静态构建放在这里）
BUNDLED_TOOLS_DIR = os.path.join(ROOT, ".tools")

# 允许用环境变量覆盖，便于换机器/换版本
ENV_FFMPEG = "BILIAGENT_FFMPEG"
ENV_FFPROBE = "BILIAGENT_FFPROBE"

# 默认下载目录
DOWNLOAD_DIR = os.path.join(ROOT, "downloads")

# ffmpeg 单次执行超时（秒）。10 分钟足够转一部长视频
FFMPEG_TIMEOUT = 600

# 抽取/转码的默认参数集（实测基线，见 docs/FACTS.md）
MP3_QUALITY = "2"          # libmp3lame -q:a 2 → 实测 ~168 kbps VBR
MP3_FALLBACK_CODEC = "mp3"  # 若 libmp3lame 不可用，退回内置 mp3 编码器

# 请求节流与重试（合规边界 §8.4）
MIN_REQUEST_INTERVAL = 1.0
MAX_RETRIES = 2

# --------------------------------------------------------------------------
# 产物清理
# --------------------------------------------------------------------------
# 抽音频需要先有 mp4 作为源文件（mp3 是从 mp4 里解码出来的），所以 mp4 是
# 必要的中转。但中转用完是否保留应该可控 —— 用户说"要 mp3"时，
# 留下一个比 mp3 大 2~3 倍的 mp4 纯属浪费。
#
# True  → 音频抽取成功后删除中间 mp4（默认，省空间）
# False → 保留 mp4（想同时留一份视频时用）
#
# 安全约束：**只在音频抽取成功后才删**。失败时保留 mp4，
# 因为它是唯一的源，删了就没法重试了。
#
# 优先级：命令行 --keep-mp4  >  环境变量/.env  >  这里的默认值
from .env import env_bool as _env_bool  # noqa: E402

KEEP_INTERMEDIATE_MP4 = _env_bool("KEEP_INTERMEDIATE_MP4", False)


# --------------------------------------------------------------------------
# 护栏参数集中表
# --------------------------------------------------------------------------
# 为什么集中在一处：护栏散落在各模块里时，你无法回答"这个 agent 到底受哪些
# 约束"。集中成一张表之后，它既是配置入口，也是**自文档**——
# 看一遍就知道失控风险被哪些机制兜住了。
#
# 每项都标注：谁强制执行、越界时的行为。
GUARDRAILS = {
    "max_loop_rounds": {
        "value": 10,
        "enforced_by": "AgentLoop.run()",
        "on_exceed": "停止循环，outcome=max_rounds，强制汇报最后状态",
        "why": "防失控：模型可能一直不给出最终答复",
    },
    "max_format_fixes": {
        "value": 3,
        "enforced_by": "AgentLoop.run()",
        "on_exceed": "停止，outcome=format_stuck，汇报里附模型最后的原始输出",
        "why": "连续格式违规说明模型/prompt 有问题，重试无益。"
               "⚠️ 是**连续**计数，成功动作后归零",
    },
    "repeat_action_limit": {
        "value": 3,
        "enforced_by": "AgentLoop.run()",
        "on_exceed": "停止，outcome=stuck",
        "why": "同一 (action,args) 反复出现 = 卡死，不是重试能解决的",
    },
    "max_search_rounds": {
        "value": 3,
        "enforced_by": "ToolBox._search()",
        "on_exceed": "返回 search_limit_reached，hint 要求换词或 ask_user",
        "why": "防模型反复重搜同一关键词",
    },
    "max_context_tokens": {
        "value": 20000,
        "enforced_by": "ContextPolicy.maybe_compress()",
        "on_exceed": "压缩早期轮次为摘要（保留 system + 原始任务 + 最近 3 轮）",
        "why": "防上下文膨胀导致失败/成本失控",
    },
    "min_request_interval": {
        "value": MIN_REQUEST_INTERVAL,
        "enforced_by": "BiliSession._throttle()",
        "on_exceed": "—（主动限速）",
        "why": "合规边界 §8.4：对 B 站请求间隔 ≥1s",
    },
    "max_http_retries": {
        "value": MAX_RETRIES,
        "enforced_by": "BiliSession._request() / open_stream()",
        "on_exceed": "返回结构化错误",
        "why": "只重试 5xx/网络错误；403/412/429 立即停止（合规 §8.5）",
    },
    "ffmpeg_timeout": {
        "value": FFMPEG_TIMEOUT,
        "enforced_by": "media._run()",
        "on_exceed": "返回 ffmpeg 超时错误",
        "why": "防外部进程挂死",
    },
}


# 可调的行为开关（不是护栏，是偏好设置）
PREFERENCES = {
    "KEEP_INTERMEDIATE_MP4": {
        "value": KEEP_INTERMEDIATE_MP4,
        "what": "抽完音频后是否保留中间 mp4",
        "why": "mp4 是抽音频的必要中转，但通常比 mp3 大 2~3 倍；"
               "用户只要 mp3 时不该多占空间。失败时无论如何都保留。",
        "override_cli": "--keep-mp4",
    },
}


def guardrail_summary() -> str:
    """把护栏表渲染成可读文本（给 CLI 和文档用）。"""
    lines = []
    for name, spec in GUARDRAILS.items():
        lines.append("%-22s = %-8s  %s" % (name, spec["value"], spec["enforced_by"]))
    return "\n".join(lines)
