"""工具分发层：把校验过的 action 绑到真实工具函数上。

职责（严格限定）：
  - action → 具体工具函数的路由
  - 维护跨轮次的工具状态（会话、上一次下载的文件）
  - 把工具结果原样（或加最少必要信息后）交给 loop

不负责：
  - 校验（在 protocol.py）
  - 循环控制 / 轮数上限（在 loop.py）
  - 提示词渲染（在 protocol.py）

设计要点：`ToolBox` 持有状态而不是用全局变量 —— 这样一次会话的状态是
显式的、可回放的、可并行多个的。全局状态是手写 harness 最常见的坑。
"""

from __future__ import annotations

import os

from . import config, media
from .bili import BiliFatal, BiliSession, download_html5_mp4, get_video_info, search_videos
from .config import GUARDRAILS
from .protocol import TOOL_SPECS

_MAX_SEARCH_ROUNDS = GUARDRAILS["max_search_rounds"]["value"]


class ToolBox:
    """一次 agent 会话的工具状态容器。"""

    def __init__(self, session: BiliSession | None = None, out_dir: str = "downloads",
                 keep_mp4: bool | None = None):
        self.session = session or BiliSession()
        self.out_dir = out_dir
        # None → 用 config 里的默认值；显式 True/False → 覆盖（CLI --keep-mp4）
        self.keep_mp4 = (config.KEEP_INTERMEDIATE_MP4 if keep_mp4 is None
                         else keep_mp4)
        self.capabilities = media.probe_capabilities()

        # 跨轮次状态
        self.last_query: str | None = None
        self.last_candidates: list[dict] = []
        self.last_search_rounds = 0
        self.selected_bvid: str | None = None
        self.mp4_path: str | None = None
        self.audio_path: str | None = None
        self.removed_intermediate: str | None = None   # 被清理的中间文件（供汇报）

    # -- 终局 action ------------------------------------------------------
    def _finish(self, args, state):
        return {
            "ok": True,
            "terminal": True,
            "kind": "finish",
            "summary": args.get("summary", ""),
        }

    def _ask_user(self, args, state):
        return {
            "ok": True,
            "terminal": True,
            "kind": "ask_user",
            "question": args.get("question", ""),
        }

    # -- 业务 action ------------------------------------------------------
    def _search(self, args, state):
        keyword = args["keyword"]
        limit = int(args.get("limit", 5))

        # 轮数护栏在工具侧也查一次：即使 loop 漏了，也不会无限重搜
        if self.last_query == keyword:
            self.last_search_rounds += 1
        else:
            self.last_search_rounds = 1
        self.last_query = keyword

        if self.last_search_rounds > _MAX_SEARCH_ROUNDS:
            return {
                "ok": False,
                "error": "search_limit_reached",
                "message": "同一关键词已搜索 %d 次，达到上限" % (self.last_search_rounds - 1),
                "hint": "不要再重搜。应换一个明显不同的关键词，或用 ask_user 让用户澄清。",
            }

        res = search_videos(self.session, keyword, limit=limit)
        if res.get("ok"):
            self.last_candidates = res["items"]
        return res

    def _get_info(self, args, state):
        res = get_video_info(self.session, args["bvid"])
        if res.get("ok"):
            self.selected_bvid = res["bvid"]
        return res

    def _download(self, args, state):
        bvid = args["bvid"]
        res = download_html5_mp4(self.session, bvid, out_dir=self.out_dir)
        if res.get("ok"):
            self.selected_bvid = bvid
            self.mp4_path = res["path"]
        return res

    def _extract_audio(self, args, state):
        mode = args["mode"]

        # 能力守门：ffmpeg 不可用时不要浪费一次失败尝试
        if not self.capabilities.get("ok"):
            return {
                "ok": False,
                "error": "ffmpeg_missing",
                "message": "本机没有可用的 ffmpeg，无法抽取或转码音频",
                "hint": ("不要重试 extract_audio。要么直接交付 mp4，"
                         "要么用 ask_user 说明情况并询问是否接受 mp4。"),
                "fallback_path": self.mp4_path,
            }
        if mode == "mp3" and not self.capabilities.get("can_encode_mp3"):
            return {
                "ok": False,
                "error": "mp3_unsupported",
                "message": "本机 ffmpeg 不能编码 mp3",
                "hint": "改用 mode=copy 得到无损 .m4a，或 ask_user 询问是否接受 m4a。",
            }

        if not self.mp4_path:
            return {
                "ok": False,
                "error": "no_source",
                "message": "还没有下载任何文件，无从抽取音频",
                "hint": "应先调用 download 拿到 mp4，再 extract_audio。",
            }

        res = media.extract_audio(self.mp4_path, mode=mode, out_dir=self.out_dir)
        if not res.get("ok"):
            # ⚠️ 失败时**绝不**删 mp4 —— 它是唯一的源文件，
            #    删了就彻底没法重试了（得重新下载一遍）。
            return res

        self.audio_path = res["path"]

        # 音频已就绪，按配置清理中间 mp4
        if not self.keep_mp4 and self.mp4_path and os.path.isfile(self.mp4_path):
            # 只有当音频文件确实存在且非空时才动手
            if os.path.getsize(res["path"]) > 0:
                try:
                    freed = os.path.getsize(self.mp4_path)
                    os.unlink(self.mp4_path)
                    self.removed_intermediate = self.mp4_path
                    res["intermediate_removed"] = True
                    res["freed_mb"] = round(freed / 1048576, 2)
                    self.mp4_path = None
                except OSError as e:
                    # 删不掉不算失败：音频已经拿到了，如实上报即可
                    res["intermediate_removed"] = False
                    res["intermediate_error"] = str(e)

        return res

    # -- 分发 --------------------------------------------------------------
    def dispatch(self, action: str, args: dict, state) -> dict:
        """执行一个已校验的 action。返回工具的结构化结果。"""
        handler = getattr(self, "_" + action, None)
        if handler is None:
            # 理论上 protocol.validate_directive 已拦下，这里是兜底
            return {
                "ok": False,
                "error": "unknown_action",
                "message": "没有名为 %r 的工具" % action,
                "hint": "可用工具：%s" % sorted(TOOL_SPECS),
            }
        try:
            return handler(args, state)
        except BiliFatal as e:
            return e.payload
        except Exception as e:  # noqa: BLE001
            # 工具绝不能把裸异常抛给 loop —— 那会让模型看到 Python 堆栈
            return {
                "ok": False,
                "error": "tool_crashed",
                "message": "%s: %s" % (type(e).__name__, e),
                "hint": "工具内部异常。请检查参数是否合理，或换一种方式达成目标。",
            }

    # -- 观察压缩 ----------------------------------------------------------
    @staticmethod
    def make_observation(action: str, result: dict, max_items: int = 5) -> dict:
        """把工具结果压成给模型看的观察。

        Agent 里"回喂什么"和"执行什么"同等重要：
          - 必须**保留错误/hint/warning**（模型靠它自救）
          - 必须**截断大数据**（否则上下文爆炸）
          - 必须**标注静默失败**（否则模型把"空"当"成功"）
        """
        if action == "search" and result.get("ok"):
            items = result.get("items", [])[:max_items]
            obs = {
                "ok": True,
                "keyword": result.get("keyword"),
                "total": result.get("total"),
                "matched": result.get("matched"),
                "candidates": [
                    {k: it[k] for k in
                     ("bvid", "title", "author", "duration", "play", "relevance", "url")
                     if k in it}
                    for it in items
                ],
            }
            # ⚠️ 静默失败必须显式标注 —— 这是头号风险 R1 的最后一道防线
            if result.get("warning"):
                obs["warning"] = result["warning"]
                obs["hint"] = result.get("hint")
            return obs

        if action == "download" and result.get("ok"):
            return {
                "ok": True,
                "path": result.get("path"),
                "size_mb": round(result.get("bytes", 0) / 1048576, 2),
                "quality": result.get("quality"),
                "complete": result.get("complete"),
                "title": result.get("title"),
            }

        if action == "extract_audio" and result.get("ok"):
            obs = {
                "ok": True,
                "path": result.get("path"),
                "codec": result.get("codec"),
                "size_mb": round(result.get("size", 0) / 1048576, 2),
                "duration": result.get("duration"),
                "verified": result.get("verified"),
            }
            # 中间 mp4 被清掉了 —— 告诉模型，这样汇报里能说清楚
            # "只留了 mp3"，用户不会以为视频还在
            if result.get("intermediate_removed"):
                obs["note"] = ("中间 mp4 已删除（释放 %.1f MB），"
                               "目录里只保留音频文件。"
                               % result.get("freed_mb", 0))
            elif result.get("intermediate_error"):
                obs["note"] = "音频已生成，但中间 mp4 删除失败：%s" % \
                              result["intermediate_error"]
            return obs

        # 失败：完整保留 error/message/hint + 少量诊断字段
        if not result.get("ok", True):
            keep = ("error", "message", "hint", "api_code", "http_status",
                    "stderr_tail", "fallback_path", "missing", "allowed", "got")
            obs = {k: result[k] for k in keep if k in result}
            obs["ok"] = False
            return obs

        # 其余成功结果：剔除内部字段后原样返回
        return {k: v for k, v in result.items() if k != "terminal"}
