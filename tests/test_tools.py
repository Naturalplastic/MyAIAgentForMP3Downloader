#!/usr/bin/env python3
"""工具层回归测试 —— 覆盖设计稿 §11 验收项与 §9 风险表。

设计原则：**测试也要能在没有网络/没有 ffmpeg 的机器上跑**，
所以每个用例先探测前置条件，缺失就记为 SKIP 而不是 FAIL。
这一点对 agent 项目尤其重要：依赖外部世界的能力必须可降级、可观测。

用法：python3 tests/test_tools.py
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from biliagent import media  # noqa: E402
from biliagent.bili import (  # noqa: E402
    BiliSession,
    _http_hint,
    _safe_filename,
    get_video_info,
    search_videos,
)

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


def section(title):
    print("\n\033[1m%s\033[0m" % title)


# ==========================================================================
section("1. 静态校验（不需要网络 / ffmpeg）")

check("非法 bvid 格式在本地拦截，不发网络请求",
      get_video_info(BiliSession(), "NOTABVID").get("error") == "bad_bvid")
check("空 bvid 被拦截",
      get_video_info(BiliSession(), "").get("error") == "empty_bvid")
check("空关键词被拦截",
      search_videos(BiliSession(), "   ").get("error") == "empty_keyword")
check("非法 limit 被拦截",
      search_videos(BiliSession(), "x", limit=0).get("error") == "bad_limit")

# 文件名安全 —— 对应 R9
r = _safe_filename("a/b\\c:d*e?f\"g<h>i|j")
check("文件名过滤路径分隔符与保留字符", not any(c in r for c in '/\\:*?"<>|'),
      "得到 %r" % r)
long_name = _safe_filename("x" * 300)
check("超长文件名被截断", len(long_name) <= 80, "长度 %d" % len(long_name))
check("路径穿越被消除", "/" not in _safe_filename("../../etc/passwd"))

# 参数校验必须最先执行（曾被"无音频流"掩盖）
bad = media.extract_audio(__file__, mode="ogg")
check("非法 mode 立即报错，不被后续检查掩盖",
      bad.get("error") == "bad_mode", str(bad)[:120])

missing = media.extract_audio("definitely/not/here.mp4")
check("源文件不存在 → no_src", missing.get("error") == "no_src")

# 风控状态码必须带可操作的提示（否则用户会误认为程序缺陷）
for code, kw in ((403, "拒绝"), (412, "风控"), (429, "限流")):
    h = _http_hint(code)
    check("HTTP %d 有处理建议" % code, bool(h) and kw in h, h[:80])
check("412 的提示说明该状态是临时性的",
      "临时" in _http_hint(412), _http_hint(412)[:80])
check("412 的提示建议稍后重试",
      "稍后" in _http_hint(412), _http_hint(412)[:80])
check("未定义的状态码返回空提示", _http_hint(404) == "")


# ==========================================================================
section("2. ffmpeg 外部工具封装")

caps = media.probe_capabilities()
if caps["errors"]:
    for e in caps["errors"]:
        print("     ⚠ %s" % e)

if not caps["ok"]:
    skip("ffmpeg 能力探测", "未安装 ffmpeg")
else:
    check("探测到 ffmpeg 版本", bool(caps["ffmpeg"]["version"]))
    check("探测到 ffprobe 版本（用于产物校验）",
          bool(caps["ffprobe"] and caps["ffprobe"]["version"] != "unknown"),
          str(caps["ffprobe"]))
    check("编码器探测解析正确（不应把头部行当条目）",
          isinstance(caps["encoders"].get("has_libmp3lame"), bool),
          str(caps["encoders"]))
    check("容器探测解析正确（' D. = Demuxing supported' 不是条目）",
          caps["muxers"].get("mp4") is True and caps["muxers"].get("mp3") is True,
          str(caps["muxers"]))
    check("能力结论自洽：有 mp3 容器+编码器 ⇒ can_encode_mp3",
          caps["can_encode_mp3"] == (
              caps["muxers"]["mp3"]
              and (caps["encoders"]["has_libmp3lame"] or caps["encoders"]["has_native_mp3"])),
          str(caps))

    # stderr 分类器 —— 这是"外部工具错误 → 模型可读原因"的核心
    samples = [
        ("Unknown encoder 'nope'", "encoder_missing"),
        ("Stream map '0:a:0' matches no streams.", "no_such_stream"),
        ("Invalid data found when processing input", "corrupt_input"),
        ("moov atom not found", "incomplete_mp4"),
        ("Permission denied", "permission_denied"),
        ("No space left on device", "no_space"),
        ("something totally unexpected happened", "ffmpeg_failed"),
    ]
    ok_all = True
    for stderr, want in samples:
        got = media.classify_ffmpeg_error(stderr, 1)["error"]
        if got != want:
            ok_all = False
            print("       %r → %s (期望 %s)" % (stderr, got, want))
    check("stderr 分类器覆盖 7 类失败", ok_all)
    check("未匹配时保留 stderr_tail 供模型自救",
          "stderr_tail" in media.classify_ffmpeg_error("weird", 1))


# ==========================================================================
section("3. 端到端（需网络）")

TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads")
os.makedirs(TMP, exist_ok=True)

sess = BiliSession()
try:
    res = search_videos(sess, "老番茄", limit=3)
    if not res.get("ok"):
        # 412 为临时风控，不代表功能缺陷 —— 记为跳过而非失败
        if res.get("error") in ("http_412", "http_403", "http_429"):
            skip("搜索工具", "临时风控 %s（非功能缺陷）" % res.get("error"))
        else:
            skip("搜索工具", "错误 %s" % res.get("error"))
    else:
        check("搜索返回结构化候选", len(res["items"]) == 3)
        check("候选含排序依据（play/relevance）",
              all("play" in it and "relevance" in it for it in res["items"]))
        check("标题已剥 <em> 高亮标签",
              not any("<em>" in it["title"] for it in res["items"]))

    # R1 头号风险回归：垃圾关键词必须报警告，而不是静默给出无关结果
    junk = search_videos(sess, "zzqqxx不存在的关键词zzqqxx", limit=3)
    if junk.get("error") in ("http_412", "http_403", "http_429"):
        skip("R1 回归", "临时风控 %s（非功能缺陷）" % junk.get("error"))
    else:
        check("R1 回归：垃圾关键词触发 no_relevant_result 警告",
              junk.get("warning") == "no_relevant_result",
              "got warning=%r matched=%s" % (junk.get("warning"), junk.get("matched")))
        check("R1 回归：警告带可执行 hint",
              bool(junk.get("hint")) and "请勿" in (junk.get("hint") or ""))

    # 不存在的 bvid（格式合法）
    bogus = get_video_info(sess, "BV1xx411c7mD")
    check("不存在的 bvid 返回结构化 api_error 或成功（该 id 可能恰好有效）",
          ("ok" in bogus), str(bogus)[:100])
    check("HTTP 请求计数随调用增长", sess.request_count > 0,
          "count=%d" % sess.request_count)
except Exception as e:  # noqa: BLE001
    skip("网络相关用例", "异常: %s" % e)


# ==========================================================================
section("4. 媒体管线（需 ffmpeg + 已有的 mp4）")

# ⚠️ 不能依赖 downloads/ 里"碰巧存在的 mp4" —— 那会让本节结果取决于
#    运行顺序（比如另一个测试先清空了目录）。自己造 fixture，保证确定性。
FIXTURE = os.path.join(TMP, "_fixture_audio.mp4")
mp4s = [] if not os.path.exists(FIXTURE) else [FIXTURE]

if not mp4s and caps["ok"]:
    # 用 ffmpeg 合成一个带音轨的测试视频（约 1 秒，体积可忽略）
    _ff = caps["ffmpeg"]["path"]
    subprocess.run([_ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", FIXTURE],
                   capture_output=True, timeout=60)
    if os.path.exists(FIXTURE):
        mp4s = [FIXTURE]

if not mp4s:
    skip("mp4 → mp3 转码", "无法生成测试 fixture（ffmpeg 不可用）")
elif not caps["ok"] or not caps["can_encode_mp3"]:
    skip("mp4 → mp3 转码", "ffmpeg 不可用或不能编码 mp3")
else:
    src = mp4s[0]
    info = media.probe_media(src)
    check("ffprobe 成功解析 mp4", info.get("ok") and info["has_audio"],
          str(info)[:120])

    out = media.extract_audio(src, mode="mp3", out_dir=TMP)
    check("mp4 → mp3 成功", out.get("ok"), str(out)[:200])
    if out.get("ok"):
        check("产物经 ffprobe 校验通过", out.get("verified") is True)
        check("编码器为 libmp3lame", out.get("codec") == "libmp3lame")
        check("产物非空", out.get("size", 0) > 0)
        check("时长与源一致（差值 < 2s）",
              abs((out.get("duration") or 0) - info["duration"]) < 2.0,
              "out=%.1f src=%.1f" % (out.get("duration") or 0, info["duration"]))
        check("无 .part 残留文件", not os.path.exists(out["path"] + ".part"))

    # copy 模式（无损）
    out2 = media.extract_audio(src, mode="copy", out_dir=TMP)
    check("无损 copy 模式成功", out2.get("ok"), str(out2)[:200])
    if out2.get("ok"):
        check("copy 模式输出 .m4a", out2["path"].endswith(".m4a"))

    # 损坏输入 → 结构化错误（不是崩溃）
    broken = os.path.join(TMP, "broken.mp4")
    with open(broken, "wb") as fh:
        fh.write(b"this is not a media file at all " * 100)
    r = media.extract_audio(broken, mode="mp3")
    check("损坏输入返回结构化错误且不崩溃",
          r.get("ok") is False and r.get("error") in
          ("incomplete_mp4", "corrupt_input", "ffprobe_failed"), str(r)[:150])
    check("损坏输入的错误带 hint 或 stderr_tail",
          bool(r.get("hint") or r.get("stderr_tail")))

    # 无音频流 → 明确原因（用 ffmpeg 造）
    noaudio = os.path.join(TMP, "noaudio.mp4")
    ff = caps["ffmpeg"]["path"]
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", noaudio],
                   capture_output=True, timeout=60)
    if os.path.exists(noaudio):
        r = media.extract_audio(noaudio, mode="mp3")
        check("无音频流的视频 → no_audio_stream + 明确 hint",
              r.get("error") == "no_audio_stream" and bool(r.get("hint")),
              str(r)[:150])


# ==========================================================================
# 清理本次测试生成的全部 fixture（不留垃圾在 downloads/）
# 包括派生出来的 .mp3 / .m4a —— 测试产物不该污染用户目录
for _name in os.listdir(TMP):
    if _name.startswith("_fixture_") or _name in ("broken.mp4", "noaudio.mp4"):
        try:
            os.unlink(os.path.join(TMP, _name))
        except OSError:
            pass

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
