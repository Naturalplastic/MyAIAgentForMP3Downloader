"""ffmpeg / ffprobe 外部工具封装。

设计要点（这是"调用外部工具"作为 agent 实践的核心）：

  1. **能力探测（capability probing）**
     不假设 ffmpeg 存在。`probe_capabilities()` 返回结构化能力报告，
     agent 可以据此决定"能不能做 mp3"，而不是撞墙失败。

  2. **stderr 是一等公民**
     ffmpeg 把人类可读的原因全写在 stderr 里（编解码器缺失、流不存在、
     数据损坏…）。工具的职责是把这段噪声**压缩成模型能据以自救的结构化
     错误**（code + 相关行 + hint），而不是丢弃或原样倒给模型。

  3. **原子写入 + 产物验证**
     写到 .part 再改名；改名前用 ffprobe 验证 codec/时长。
     否则崩溃会留下"看起来成功但实际损坏"的 mp3 —— 对 agent 是最坏的失败。

  4. **不解析人类可读输出**
     `-version` / `-encoders` 解析只用于**探测**，核心流程只用返回码 + ffprobe JSON。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from . import config

# --------------------------------------------------------------------------
# 发现二进制
# --------------------------------------------------------------------------


def _candidates(name: str, env_var: str):
    """按优先级列出候选路径：环境变量 > 项目便携目录 > PATH。"""
    env = os.environ.get(env_var)
    if env:
        yield env
    bundled = os.path.join(config.BUNDLED_TOOLS_DIR, name)
    yield bundled
    if os.name == "nt":
        yield bundled + ".exe"
    which = shutil.which(name)
    if which:
        yield which


def find_binary(name: str) -> str | None:
    """定位可执行文件。返回首个存在且可执行的路径。"""
    env_var = config.ENV_FFMPEG if name == "ffmpeg" else config.ENV_FFPROBE
    for path in _candidates(name, env_var):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def ffmpeg_path() -> str | None:
    return find_binary("ffmpeg")


def ffprobe_path() -> str | None:
    return find_binary("ffprobe")


# --------------------------------------------------------------------------
# 能力探测
# --------------------------------------------------------------------------

_INT_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
# 注意：-encoders / -muxers 的输出**同时包含说明性头部**，例如：
#   -encoders: " A....D aac  ..."  （flags 6 字符）
#   -muxers:   "  E mp3  ..."      （flags 1 字符）
#              " D. = Demuxing supported"   ← 头部行，绝不能当条目
#              " .E = Muxing supported"     ← 头部行
# 因此必须：① 行内只有 flags + 名称两段；② 名称段不得以 '=' 开头。
_INT_ENC_RE = re.compile(r"^\s*[A-Z.]{2,8}\s+(\S+)\s+\S", re.M)
_INT_MUX_RE = re.compile(r"^\s{2,}[A-Z.]{1,8}\s+([^\s=]\S*)\s+\S", re.M)


def _run(args, timeout=20):
    """统一的外部进程调用。返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT after %ss" % timeout
    except FileNotFoundError as e:
        return -2, "", "FileNotFoundError: %s" % e
    except OSError as e:
        return -3, "", "OSError: %s" % e


_cap_cache = None


def probe_capabilities(refresh: bool = False) -> dict:
    """探测 ffmpeg/ffprobe 能力。结果缓存。

    返回：
      {ok, ffmpeg: {path, version}, ffprobe: {path, version},
       encoders: {mp3, aac}, muxers: {mp3, mp4}, can_encode_mp3, errors}
    """
    global _cap_cache
    if _cap_cache is not None and not refresh:
        return _cap_cache

    out = {
        "ok": False,
        "ffmpeg": None,
        "ffprobe": None,
        "encoders": {},
        "muxers": {},
        "can_encode_mp3": False,
        "can_extract_audio": False,
        "errors": [],
    }

    ff = ffmpeg_path()
    fp = ffprobe_path()

    if not ff:
        out["errors"].append(
            "未找到 ffmpeg。请安装（apt install ffmpeg）或将静态构建放入 "
            "%s/ ，或用环境变量 %s 指定路径。"
            % (config.BUNDLED_TOOLS_DIR, config.ENV_FFMPEG)
        )
    else:
        rc, stdout, stderr = _run([ff, "-hide_banner", "-version"])
        text = stdout + stderr
        m = _INT_VERSION_RE.search(text)
        out["ffmpeg"] = {"path": ff, "version": m.group(1) if m else "unknown"}

    if not fp:
        out["errors"].append(
            "未找到 ffprobe（用于产物校验）。缺失时仍可转码，但无法验证输出。"
        )
    else:
        rc, stdout, stderr = _run([fp, "-hide_banner", "-version"])
        # ffprobe 的自报名字是 "ffprobe version ..."，与 ffmpeg 不同
        m = re.search(r"ffprobe version (\S+)", stdout + stderr)
        out["ffprobe"] = {"path": fp, "version": m.group(1) if m else "unknown"}

    if ff:
        rc, stdout, stderr = _run([ff, "-hide_banner", "-encoders"])
        names = set(_INT_ENC_RE.findall(stdout))
        out["encoders"] = {
            "has_libmp3lame": "libmp3lame" in names,
            "has_native_mp3": "mp3" in names,
            "has_aac": "aac" in names,
        }
        rc, stdout, stderr = _run([ff, "-hide_banner", "-muxers"])
        mux = set(_INT_MUX_RE.findall(stdout))
        out["muxers"] = {"mp3": "mp3" in mux, "mp4": "mp4" in mux, "ipod": "ipod" in mux}

        out["can_encode_mp3"] = (
            out["muxers"].get("mp3", False)
            and (out["encoders"]["has_libmp3lame"] or out["encoders"]["has_native_mp3"])
        )
        out["can_extract_audio"] = out["muxers"].get("mp4", False)
        out["ok"] = True

    _cap_cache = out
    return out


# --------------------------------------------------------------------------
# 媒体探测（ffprobe）
# --------------------------------------------------------------------------

def probe_media(src: str, timeout: int = 60) -> dict:
    """用 ffprobe 读流信息。返回 {ok, format, streams, audio, video, duration}"""
    fp = ffprobe_path()
    if not fp:
        return {"ok": False, "error": "ffprobe_missing",
                "message": "未找到 ffprobe，无法探测媒体信息"}
    if not os.path.isfile(src):
        return {"ok": False, "error": "no_src", "message": "文件不存在: %s" % src}

    args = [
        fp, "-v", "error",
        "-show_entries", "format=format_name,duration,size,bit_rate",
        "-show_entries", "stream=index,codec_type,codec_name,channels,sample_rate,width,height,duration",
        "-of", "json", src,
    ]
    rc, stdout, stderr = _run(args, timeout=timeout)
    if rc != 0:
        err = {
            "ok": False,
            "error": "ffprobe_failed",
            "message": "ffprobe 退出码 %d" % rc,
            "stderr_tail": stderr.strip()[-300:],
        }
        # ffprobe 的 stderr 同样值得分类：这是模型能否自救的关键线索
        m = re.search(r"moov atom not found", stderr, re.I)
        if m:
            err["error"] = "incomplete_mp4"
            err["hint"] = ("mp4 缺少 moov box，说明文件不完整（下载中断或下到了"
                           "错误页）。应重新下载，而不是尝试修复。")
        elif re.search(r"Invalid data found", stderr, re.I):
            err["error"] = "corrupt_input"
            err["hint"] = "文件不是有效媒体（可能是 HTML 错误页或纯文本）。"
        return err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": "ffprobe_bad_json", "message": str(e),
                "stdout_snippet": stdout[:200]}

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]

    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "ok": True,
        "format": fmt.get("format_name"),
        "duration": duration,
        "size": int(fmt.get("size") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "streams": streams,
        "audio": audio,
        "video": video,
        "has_audio": bool(audio),
        "has_video": bool(video),
    }


# --------------------------------------------------------------------------
# ffmpeg stderr → 结构化错误
# --------------------------------------------------------------------------

# ffmpeg 的 stderr 噪声极大。这里用模式匹配把关键原因提炼成 code + hint。
_ERROR_PATTERNS = [
    (re.compile(r"Unknown encoder '([^']+)'", re.I), "encoder_missing",
     "该 ffmpeg 构建不含此编码器。若缺 libmp3lame，可改用内置 'mp3' 编码器。"),
    (re.compile(r"Unknown decoder '([^']+)'", re.I), "decoder_missing",
     "输入格式的解码器缺失。"),
    (re.compile(r"Stream map '([^']+)' matches no streams", re.I), "no_such_stream",
     "指定的音频轨不存在 —— 该文件可能根本没有音频流。"),
    (re.compile(r"Output file #?\d* does not contain any stream", re.I), "no_output_stream",
     "没有任何流被写入输出 —— 通常因为没有音频轨。"),
    (re.compile(r"Invalid data found when processing input", re.I), "corrupt_input",
     "输入文件损坏或不是媒体文件（可能下到了错误页）。"),
    (re.compile(r"Permission denied", re.I), "permission_denied",
     "无写入权限，请换输出目录。"),
    (re.compile(r"No space left on device", re.I), "no_space",
     "磁盘空间不足。"),
    (re.compile(r"moov atom not found", re.I), "incomplete_mp4",
     "mp4 缺少 moov（下载不完整）。应重新下载。"),
    (re.compile(r"Conversion failed", re.I), "conversion_failed", "转码失败。"),
]


def classify_ffmpeg_error(stderr: str, returncode: int) -> dict:
    """把 ffmpeg stderr 压缩成模型可用的结构化错误。

    返回 {error, message, hint, stderr_tail, matched_rule}
    关键点：**保留 stderr 尾部** —— 未匹配到已知模式时，这是模型唯一能
    据以自救的线索。
    """
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    tail = "\n".join(lines[-8:])[-600:]

    for pat, code, hint in _ERROR_PATTERNS:
        m = pat.search(stderr)
        if m:
            return {
                "error": code,
                "message": m.group(0)[:200],
                "hint": hint,
                "stderr_tail": tail,
                "matched_rule": code,
                "returncode": returncode,
            }

    return {
        "error": "ffmpeg_failed",
        "message": "ffmpeg 退出码 %d（未匹配已知错误模式）" % returncode,
        "hint": "请依据 stderr_tail 判断；若为未知输入格式，可先用 probe_media 检查文件。",
        "stderr_tail": tail,
        "matched_rule": None,
        "returncode": returncode,
    }


# --------------------------------------------------------------------------
# 音频抽取 / 转码
# --------------------------------------------------------------------------

def _safe_unlink(path):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def extract_audio(src: str, mode: str = "mp3", out_dir: str | None = None,
                  out_name: str | None = None, progress=None):
    """从媒体文件抽取/转码音频。

    mode:
      "mp3"  → libmp3lame VBR -q:a 2（实测 ~168 kbps）；缺 libmp3lame 时
               自动退回内置 mp3 编码器
      "copy" → 不重编码，直接复制音频流（aac → .m4a）。最快、无损

    返回 {ok, path, size, mode, codec, duration, elapsed, verified, error?}
    """
    if not os.path.isfile(src):
        return {"ok": False, "error": "no_src", "message": "源文件不存在: %s" % src}

    # 参数校验必须最先做 —— 否则会被"输入无音频流"之类的结果掩盖
    if mode not in ("mp3", "copy"):
        return {"ok": False, "error": "bad_mode",
                "message": "mode 必须是 mp3 或 copy", "got": mode,
                "hint": "mp3 = libmp3lame VBR 转码；copy = 无损复制音轨为 .m4a"}

    caps = probe_capabilities()
    if not caps["ok"]:
        return {
            "ok": False,
            "error": "ffmpeg_missing",
            "message": "未找到可用的 ffmpeg",
            "hint": " ".join(caps["errors"]) or
                    "请安装 ffmpeg，或用 %s 指定路径" % config.ENV_FFMPEG,
            "capabilities": caps,
        }

    ff = caps["ffmpeg"]["path"]

    # 先探测输入，尽早发现"根本没有音频轨"
    info = probe_media(src)
    if not info.get("ok"):
        # ffprobe 缺失不阻塞转码，但要记录下来
        if info.get("error") != "ffprobe_missing":
            return info
    elif not info["has_audio"]:
        return {
            "ok": False,
            "error": "no_audio_stream",
            "message": "源文件不含音频流",
            "hint": "该视频是纯画面（无音轨），无法抽取音频。",
            "probed": {k: info[k] for k in ("format", "has_audio", "has_video")},
        }

    out_dir = out_dir or os.path.dirname(os.path.abspath(src))
    os.makedirs(out_dir, exist_ok=True)
    stem = out_name or os.path.splitext(os.path.basename(src))[0]

    if mode == "mp3":
        ext, muxer = "mp3", "mp3"
    elif mode == "copy":
        ext, muxer = "m4a", "ipod"
    else:  # pragma: no cover - 已在函数开头拦截
        return {"ok": False, "error": "bad_mode",
                "message": "mode 必须是 mp3 或 copy", "got": mode}

    dst = os.path.join(out_dir, "%s.%s" % (stem, ext))
    tmp = dst + ".part"

    codec = ("libmp3lame" if caps["encoders"].get("has_libmp3lame") else "mp3") \
        if mode == "mp3" else "copy"

    args = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", src, "-vn", "-map", "0:a:0"]
    if mode == "mp3":
        args += ["-c:a", codec, "-q:a", config.MP3_QUALITY]
    else:
        args += ["-c:a", "copy"]
    args += ["-f", muxer, tmp]

    started = time.monotonic()
    rc, stdout, stderr = _run(args, timeout=config.FFMPEG_TIMEOUT)

    if rc != 0:
        _safe_unlink(tmp)
        err = classify_ffmpeg_error(stderr, rc)
        err["cmd"] = " ".join(args[:12]) + " ..."
        return {"ok": False, **err}

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _safe_unlink(tmp)
        return {"ok": False, "error": "empty_output",
                "message": "ffmpeg 退出码 0 但输出为空",
                "stderr_tail": stderr.strip()[-400:]}

    # 产物校验：只有验证通过才改名，避免留下"看似成功"的坏文件
    verified, verify_detail = _verify_output(tmp, info)
    if verified is False:
        size = os.path.getsize(tmp)
        _safe_unlink(tmp)
        return {"ok": False, "error": "verify_failed",
                "message": "产物校验未通过，已删除临时文件",
                "detail": verify_detail}
    os.replace(tmp, dst)

    return {
        "ok": True,
        "path": dst,
        "size": os.path.getsize(dst),
        "mode": mode,
        "codec": codec,
        "duration": verify_detail.get("duration"),
        "source": src,
        "verified": verified,
        "elapsed": round(time.monotonic() - started, 2),
        "ffmpeg_version": caps["ffmpeg"]["version"],
    }


def _verify_output(path: str, src_info: dict):
    """校验产物。返回 (True/False/None, detail)

    None 表示"无法校验"（缺 ffprobe）—— 不阻塞，但要如实上报。
    """
    if not ffprobe_path():
        return None, {"reason": "ffprobe_missing", "note": "未校验产物"}

    out = probe_media(path)
    if not out.get("ok"):
        return False, {"reason": "probe_failed", "detail": out}

    detail = {
        "format": out["format"],
        "duration": out["duration"],
        "size": out["size"],
    }
    if not out["has_audio"]:
        return False, {"reason": "no_audio_in_output", **detail}

    # 时长应与源一致（容差 2 秒）
    src_dur = src_info.get("duration") or 0
    if src_dur and out["duration"]:
        delta = abs(out["duration"] - src_dur)
        detail["duration_delta"] = round(delta, 2)
        if delta > 2.0:
            return False, {"reason": "duration_mismatch",
                           "src": src_dur, **detail}
    return True, detail
