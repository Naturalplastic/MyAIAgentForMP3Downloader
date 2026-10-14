#!/usr/bin/env python3
"""命令行入口 —— D1 / D2 的验收工具。

用法：
    python -m biliagent.cli search "老番茄"
    python -m biliagent.cli info BV16F411S7dW
    python -m biliagent.cli download BV16F411S7dW
    python -m biliagent.cli audio downloads/xxx.mp4 --mode mp3

设计说明：本文件刻意做成"纯工具 CLI"，不含任何 LLM 调用。
D1/D2 的目标是先让工具层单独可测，D4 才是 agent loop。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from biliagent.env import load_env, missing_key_hint  # noqa: E402
from biliagent.bili import (  # noqa: E402
    BiliSession,
    download_html5_mp4,
    get_video_info,
    search_videos,
)
from biliagent.media import (  # noqa: E402
    extract_audio,
    probe_capabilities,
    probe_media,
)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _log(name: str, payload: dict):
    """把每次工具调用落盘，供回放（对应设计稿 §11 验收项 2）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "cli_%s.jsonl" % time.strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "tool": name, "result": payload},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志失败不应影响主流程


def _fmt_duration(sec) -> str:
    if not sec:
        return "?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _print_err(res: dict):
    print("\n  [失败] %s" % res.get("error"), file=sys.stderr)
    print("    %s" % res.get("message", ""), file=sys.stderr)
    if res.get("hint"):
        print("    ↳ 建议: %s" % res["hint"], file=sys.stderr)
    for k, v in res.items():
        if k not in ("ok", "error", "message", "hint"):
            print("      %s = %s" % (k, str(v)[:200]), file=sys.stderr)


def cmd_search(args, session):
    res = search_videos(session, args.keyword, page=args.page, limit=args.limit)
    _log("search_videos", res)
    if not res.get("ok"):
        _print_err(res)
        return 1
    print("\n关键词「%s」— 该词总匹配 %s 条，按相关性+播放量排序后取前 %d 条：\n"
          % (res["keyword"], res.get("total"), res["returned"]))
    if res.get("warning") == "no_relevant_result":
        print("  [警告] 返回的条目均不含关键词。B 站对无匹配词也会返回整页模糊结果。")
        print("     %s\n" % res.get("hint", ""))
    for i, it in enumerate(res["items"], 1):
        mark = {1.0: "hit", 0.5: "part", 0.0: "miss"}[it.get("relevance", 0.0)]
        print("  %d. [%s] %s" % (i, mark, it["title"]))
        print("     UP: %s ｜ 时长: %s ｜ 播放: %s ｜ 收藏: %s"
              % (it["author"], it["duration"], f"{it['play']:,}", f"{it['favorites']:,}"))
        print("     %s  (%s)" % (it["url"], it["bvid"]))
        print()
    print("  相关性命中 %d/%d 条 ｜ 本次会话累计 HTTP 请求: %d"
          % (res.get("matched", 0), res["returned"], session.request_count))
    return 0


def cmd_info(args, session):
    res = get_video_info(session, args.bvid)
    _log("get_video_info", res)
    if not res.get("ok"):
        _print_err(res)
        return 1
    print("\n标题 : %s" % res["title"])
    print("UP   : %s" % res["owner"])
    print("时长 : %s 秒" % _fmt_duration(res["duration"]))
    print("bvid : %s   aid: %s   cid: %s" % (res["bvid"], res["aid"], res["cid"]))
    print("链接 : %s" % res["url"])
    if len(res["pages"]) > 1:
        print("分P  : %d 个" % len(res["pages"]))
        for p in res["pages"][:8]:
            print("   P%s cid=%s %s (%ss)"
                  % (p["page"], p["cid"], p["part"][:40], p["duration"]))
    print("\n  本次会话累计 HTTP 请求: %d" % session.request_count)
    return 0


def cmd_download(args, session):
    last = [0.0]

    def progress(done, total):
        now = time.monotonic()
        if now - last[0] < 0.4 and (not total or done < total):
            return
        last[0] = now
        if total:
            pct = done * 100.0 / total
            bar = "█" * int(pct / 4) + "·" * (25 - int(pct / 4))
            sys.stdout.write("\r  [%s] %5.1f%%  %.1f/%.1f MB"
                             % (bar, pct, done / 1048576, total / 1048576))
        else:
            sys.stdout.write("\r  已下载 %.1f MB" % (done / 1048576))
        sys.stdout.flush()

    print("\n开始下载 %s ..." % args.bvid)
    res = download_html5_mp4(session, args.bvid, out_dir=args.out_dir)
    sys.stdout.write("\n")
    _log("download_html5_mp4", res)
    if not res.get("ok"):
        _print_err(res)
        return 1

    print("\n  下载完成")
    print("    标题  : %s" % res["title"])
    print("    路径  : %s" % res["path"])
    print("    大小  : %.2f MB (%d 字节)" % (res["bytes"] / 1048576, res["bytes"]))
    print("    完整  : %s" % ("是" if res["complete"] else "否（可能与预期大小不符）"))
    print("    画质  : quality=%s" % res["quality"])
    print("    耗时  : %.2fs" % res["elapsed"])
    print("  本次会话累计 HTTP 请求: %d" % session.request_count)

    if args.audio:
        keep = getattr(args, "keep_mp4", False)
        rc = cmd_audio(argparse.Namespace(src=res["path"], mode=args.audio,
                                          out_dir=None), session)
        # 抽完音频后清理中间 mp4（仅在抽取成功时）
        if rc == 0 and not keep and os.path.isfile(res["path"]):
            freed = os.path.getsize(res["path"]) / 1048576
            try:
                os.unlink(res["path"])
                print("    （已删除中间 mp4，释放 %.1f MB；"
                      "加 --keep-mp4 可保留）" % freed)
            except OSError as e:
                print("    中间 mp4 删除失败：%s" % e, file=sys.stderr)
        elif rc == 0 and keep:
            print("    （按 --keep-mp4 保留中间 mp4：%s）" % res["path"])
    return 0


def cmd_audio(args, session):
    print("\n抽取音频: %s (mode=%s) ..." % (args.src, args.mode))
    caps = probe_capabilities()
    if not caps["ok"]:
        print("  未检测到可用的 ffmpeg，走降级路径")
    else:
        print("  使用 ffmpeg %s (%s)" % (caps["ffmpeg"]["version"], caps["ffmpeg"]["path"]))

    res = extract_audio(args.src, mode=args.mode, out_dir=args.out_dir)
    _log("extract_audio", res)
    if not res.get("ok"):
        _print_err(res)
        print("\n  ↳ 视频文件仍可用: %s" % args.src, file=sys.stderr)
        return 1
    print("\n  音频已生成")
    print("    路径    : %s" % res["path"])
    print("    大小    : %.2f MB" % (res["size"] / 1048576))
    print("    编码器  : %s" % res["codec"])
    if res.get("duration"):
        print("    时长    : %s" % _fmt_duration(res["duration"]))
    verified = res.get("verified")
    print("    产物校验: %s" % ("通过" if verified is True
                              else ("— 跳过（无 ffprobe）" if verified is None
                                    else "未通过")))
    print("    耗时    : %.2fs" % res.get("elapsed", 0))
    return 0


def cmd_capabilities(args, session):
    """能力探测 —— agent 决定"我能做什么"的依据。"""
    caps = probe_capabilities()
    _log("probe_capabilities", caps)
    print("\n=== 外部工具能力探测 ===")
    print("  ffmpeg  : %s" % (("v%s @ %s" % (caps["ffmpeg"]["version"], caps["ffmpeg"]["path"]))
                              if caps["ffmpeg"] else "未找到"))
    print("  ffprobe : %s" % (("v%s @ %s" % (caps["ffprobe"]["version"], caps["ffprobe"]["path"]))
                              if caps["ffprobe"] else "未找到（无法校验产物）"))
    print("  编码器  : %s" % caps["encoders"])
    print("  容器    : %s" % caps["muxers"])
    print()
    print("  → 能产出 .mp3      : %s" % ("是" if caps["can_encode_mp3"] else "否"))
    print("  → 能抽取音频流     : %s" % ("是" if caps["can_extract_audio"] else "否"))
    for e in caps["errors"]:
        print("  [警告] %s" % e)

    if args.probe:
        print("\n=== 媒体探测: %s ===" % args.probe)
        info = probe_media(args.probe)
        if not info.get("ok"):
            _print_err(info)
            return 1
        print("  容器   : %s" % info["format"])
        print("  时长   : %s" % _fmt_duration(info["duration"]))
        print("  大小   : %.2f MB" % (info["size"] / 1048576))
        print("  有音频 : %s ｜ 有视频: %s" % (info["has_audio"], info["has_video"]))
        for s in info["streams"]:
            if s.get("codec_type") == "audio":
                print("    [audio #%s] %s %sHz %sch"
                      % (s.get("index"), s.get("codec_name"),
                         s.get("sample_rate"), s.get("channels")))
            else:
                print("    [video #%s] %s %sx%s"
                      % (s.get("index"), s.get("codec_name"),
                         s.get("width"), s.get("height")))
    return 0


def cmd_agent(args, session):
    """跑一次 agent loop。

    两种驱动方式：
      --script ...   假模型脚本（D4 用，验证 loop 本身）
      无 --script    真 DeepSeek（D5；需环境变量 DEEPSEEK_API_KEY）
    """
    from biliagent.loop import AgentLoop, ScriptedChat
    from biliagent.tools import ToolBox

    box = ToolBox(session=session, out_dir=args.out_dir,
                  keep_mp4=True if args.keep_mp4 else None)

    # ---- 决定模型来源 ----
    llm = None
    if args.script:
        script, script_src = args.script, "命令行 --script（假模型）"
    elif args.fake:
        script, script_src = [
            '<tool_call>{"action":"search","args":{"keyword":"老番茄","limit":3}}</tool_call>',
            '<tool_call>{"action":"download","args":{"bvid":"BV16F411S7dW"}}</tool_call>',
            '<tool_call>{"action":"extract_audio","args":{"mode":"mp3"}}</tool_call>',
            "<final_answer>已完成。</final_answer>",
        ], "内置 demo 脚本（假模型）"
    else:
        try:
            from biliagent.llm import DeepSeekClient
            llm = DeepSeekClient(model=args.model, effort=args.effort)
            script, script_src = None, "DeepSeek %s" % llm.model
        except Exception as e:  # noqa: BLE001
            print("\n无法初始化 DeepSeek 客户端：%s" % e, file=sys.stderr)
            if hasattr(e, "payload") and e.payload.get("error") == "missing_api_key":
                print("\n%s" % missing_key_hint(), file=sys.stderr)
            elif hasattr(e, "payload"):
                print("  %s" % e.payload.get("hint", ""), file=sys.stderr)
            print("\n  或者：用 --fake 跑内置脚本（完全不需要 API key）。", file=sys.stderr)
            return 2

    print("\n=== 启动 agent loop ===")
    print("  用户输入  : %s" % args.task)
    print("  模型来源  : %s" % script_src)
    if llm and args.effort:
        print("  effort    : %s" % args.effort)
    print("  轮数上限  : %d" % args.max_rounds)
    caps = box.capabilities
    print("  ffmpeg    : %s" % ("可用 v%s" % caps["ffmpeg"]["version"]
                                if caps.get("ffmpeg") else "不可用"))
    print()

    def trace_logger(event, payload):
        if args.quiet and event in ("llm_output",):
            return
        icons = {
            "llm_output": "[模型]", "tool_call": "[工具]",
            "observation": "[观察]", "format_violation": "[格式违规]",
            "invalid_directive": "[指令非法]", "final_answer": "[答复]",
            "stuck": "[卡死]", "llm_error": "[模型失败]",
        }
        summary = json.dumps(payload, ensure_ascii=False)
        limit = 900 if event == "llm_output" else 400
        print("%s  %s" % (icons.get(event, "·"), event))
        print("   %s" % (summary[:limit] + ("…" if len(summary) > limit else "")))

    if llm is not None:
        # 把客户端的调用也记进 trace（token 消耗对 agent 很重要）
        raw_chat = llm.chat

        def logged_chat(messages):
            t0 = time.monotonic()
            res = raw_chat(messages)
            dt = time.monotonic() - t0
            if res.get("error"):
                print("[模型调用失败] (%.1fs): %s" % (dt, res.get("message"))[:200])
                if res.get("hint"):
                    print("   ↳ %s" % res["hint"])
            else:
                print("   [模型] %s | %.1fs | tokens in/out=%s/%s | 思考 %s 字"
                      % (res.get("model"), dt,
                         (res.get("usage") or {}).get("prompt_tokens"),
                         (res.get("usage") or {}).get("completion_tokens"),
                         res.get("reasoning_len")))
            return res

        chat_fn = logged_chat
    else:
        chat_fn = ScriptedChat(script)

    from biliagent.context import ContextPolicy
    policy = ContextPolicy(max_tokens=args.max_context,
                           keep_recent=args.keep_recent)
    loop = AgentLoop(chat_fn, toolbox=box, max_rounds=args.max_rounds,
                     context_policy=policy, logger=trace_logger)
    state = loop.run(args.task)

    print("\n=== 结果 ===")
    print("  结束原因: %s" % state.outcome)
    print("  总轮数  : %d" % state.round)
    print("  格式纠正: %d 次（连续计数 %d）"
          % (state.total_format_fixes, state.format_fixes))
    if loop.ctx.compressions:
        print("  上下文  : 压缩 %d 次，丢弃 %d 条旧消息"
              % (loop.ctx.compressions, loop.ctx.total_dropped))
    else:
        print("  上下文  : 峰值估算 %d tokens，未触发压缩"
              % loop.ctx.peak_tokens)
    print("  耗时    : %.2fs" % (time.time() - state.started))
    if state.mp4_path:
        print("  mp4     : %s" % state.mp4_path)
    if state.audio_path:
        print("  音频    : %s" % state.audio_path)
    if box.removed_intermediate:
        print("  已清理  : 中间 mp4 %s" % box.removed_intermediate)
    if llm is not None:
        s = llm.stats()
        print("  token   : in=%s out=%s（其中思考 %s）｜ 调用 %s 次"
              % (s["prompt_tokens"], s["completion_tokens"],
                 s["reasoning_tokens"], s["calls"]))
        cal = loop.ctx.calibration()
        if cal.get("samples"):
            print("  估算校准: 估算/真实 平均 %.2f（>1 偏保守）｜ %d 个样本"
                  % (cal["mean_ratio"], cal["samples"]))
    print("  汇报    : %s" % (state.report or "(无)")[:400])

    _log("agent_run", state.to_dict())
    print("\n  完整 trace 已写入 logs/cli_%s.jsonl" % time.strftime("%Y%m%d"))
    return 0 if state.outcome in ("finished", "asked") else 1


def cmd_cfg(args, session):
    """显示当前配置状态（key 会脱敏）。"""
    from biliagent.env import PROJECT_ENV, USER_ENV, describe

    info = describe()
    print("\n=== 当前配置 ===")
    for k, v in info.items():
        if k == "_files":
            continue
        print("  %-20s = %s" % (k, v))
    print()
    print("=== 配置文件 ===")
    for label, path in (("项目 .env", PROJECT_ENV), ("~/.biliagent.env", USER_ENV)):
        exists = os.path.isfile(path)
        print("  %-18s %-10s %s" % (label, "存在" if exists else "未创建", path))
    print()
    print("  优先级：真实环境变量 > 项目 .env > ~/.biliagent.env")
    print("  注意：.env 为明文密钥，不应提交到仓库（.gitignore 已排除）")
    _log("cfg", info)
    return 0


def cmd_guardrails(args, session):
    """打印全部护栏。既给人看，也是"我受什么约束"的可查询答案（R12）。"""
    from biliagent.config import GUARDRAILS

    print("\n=== Agent 护栏清单 ===")
    print("（这些约束全部由 harness 强制，模型无权决定）\n")
    for name, spec in GUARDRAILS.items():
        print("  %s" % name)
        print("      值        : %s" % spec["value"])
        print("      强制执行者: %s" % spec["enforced_by"])
        print("      越界行为  : %s" % spec["on_exceed"])
        print("      存在理由  : %s" % spec["why"])
        print()
    print("=== 合规边界（硬性）===")
    for line in [
        "仅个人学习；工具不分发、不部署为公共服务、不商用",
        "只调用公开接口；不登录、不带他人 Cookie、不绕过会员鉴权",
        "请求间隔 ≥1s；失败重试 ≤2 次且指数退避",
        "遇 403/412/429 立即停止并如实汇报，不做反反爬",
        "产出文件不出本地、不上传、不分享",
    ]:
        print("  · %s" % line)
    _log("guardrails", GUARDRAILS)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="biliagent",
        description="B 站媒体工具层 CLI（阶段 1/2，纯工具，不含 LLM）",
    )
    p.add_argument("--sessdata", default=os.environ.get("BILI_SESSDATA") or None,
                   help="可选。不提供则为纯游客身份（默认）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="按关键词搜索视频")
    s.add_argument("keyword")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--limit", type=int, default=5)

    i = sub.add_parser("info", help="查看视频元信息")
    i.add_argument("bvid")

    d = sub.add_parser("download", help="下载 html5 单一 mp4（无需 ffmpeg）")
    d.add_argument("bvid")
    d.add_argument("--out-dir", default="downloads")
    d.add_argument("--audio", choices=["mp3", "copy"], default=None,
                   help="下载完顺便抽取音频：mp3=转码，copy=无损复制音轨")
    d.add_argument("--keep-mp4", action="store_true",
                   help="用 --audio 时保留中间 mp4（默认删除）")

    a = sub.add_parser("audio", help="从本地文件抽取音频（需 ffmpeg）")
    a.add_argument("src")
    a.add_argument("--mode", choices=["mp3", "copy"], default="mp3",
                   help="mp3=libmp3lame VBR 转码；copy=无损复制音轨为 .m4a")
    a.add_argument("--out-dir", default=None)

    c = sub.add_parser("caps", help="探测 ffmpeg/ffprobe 能力")
    c.add_argument("--probe", default=None, metavar="FILE",
                   help="同时用 ffprobe 探测一个媒体文件")

    sub.add_parser("guardrails", help="打印全部护栏与合规边界")
    sub.add_parser("cfg", help="显示当前配置来源（key 脱敏）")

    g = sub.add_parser("agent", help="跑一次 agent loop（默认接真 DeepSeek）")
    g.add_argument("task", nargs="?", default="把老番茄的视频做成 mp3",
                   help="自然语言任务描述")
    g.add_argument("--script", nargs="*", default=None,
                   help="假模型的输出序列（验证 loop 本身，不需要 API key）")
    g.add_argument("--fake", action="store_true",
                   help="用内置 demo 脚本（不需要 API key）")
    g.add_argument("--model", default=None,
                   help="模型 id，如 deepseek-flash / deepseek-v4-pro")
    g.add_argument("--effort", default=None, choices=["low", "high", "max"],
                   help="推理强度（越大越慢越贵）")
    g.add_argument("--out-dir", default="downloads")
    g.add_argument("--max-rounds", type=int, default=10)
    g.add_argument("--keep-mp4", action="store_true",
                   help="抽完音频后保留中间 mp4（默认删除，只留音频）")
    g.add_argument("--quiet", action="store_true",
                   help="不打印模型原始输出（只留工具调用与观察）")
    g.add_argument("--max-context", type=int, default=20000,
                   help="上下文预算（估算 tokens），超出则压缩历史")
    g.add_argument("--keep-recent", type=int, default=3,
                   help="压缩时保留最近几轮完整对话")

    return p


def main(argv=None):
    # 最先加载配置文件 —— 所有后续逻辑（含参数默认值）都能看到 .env 里的值
    load_env()
    args = build_parser().parse_args(argv)
    session = BiliSession(sessdata=args.sessdata)
    handlers = {"search": cmd_search, "info": cmd_info,
                "download": cmd_download, "audio": cmd_audio,
                "caps": cmd_capabilities, "agent": cmd_agent,
                "guardrails": cmd_guardrails, "cfg": cmd_cfg}
    try:
        return handlers[args.cmd](args, session)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
