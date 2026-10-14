"""B 站媒体工具层（阶段 1 / 阶段 2 的工具实现）。

设计约束（全部来自实测，见 docs/FACTS.md）：
  * 仅标准库，零第三方依赖 —— 保证 D1/D2 在干净环境可直接跑通
  * 只走游客身份：不登录、不带任何人 Cookie
  * 每个请求之间强制间隔 >= MIN_INTERVAL，失败指数退避且重试 <= MAX_RETRIES
  * 所有函数返回结构化 dict（ok / error / hint），绝不抛裸异常给上层
    —— 这是 Agent 工具的基本契约：错误也要是可被理解的观察

接口选择说明：
  搜索用 x/web-interface/search/type（扁平数组），不用 search/all/v2
  （后者是 12 个分区块的嵌套结构，且不含 favorites 字段，清洗更麻烦）。
  下载用 playurl?platform=html5&high_quality=1，返回单一完整 mp4，无需 ffmpeg 合并。
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HOME_URL = "https://www.bilibili.com/"
SEARCH_REFERER = "https://search.bilibili.com/"
API_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

MIN_INTERVAL = 1.0  # 秒；合规边界 §8.4
MAX_RETRIES = 2  # 合规边界 §8.4
TIMEOUT = 25
CHUNK = 1 << 20  # 1 MiB

# B 站风控返回码 → 不重试，直接停下并如实汇报（合规边界 §8.5）
FATAL_HTTP = {403, 412, 429}
RETRY_HTTP = {500, 502, 503, 504}


def video_url(bvid: str) -> str:
    return "https://www.bilibili.com/video/%s" % bvid


def referer_for(bvid: str) -> str:
    return video_url(bvid)


# --------------------------------------------------------------------------
# 错误构造
# --------------------------------------------------------------------------

# HTTP 状态码对应的处理建议。412 与 403 在 B 站表现为风控，
# 提示应说明"这是临时限制"并给出等待建议 —— 否则用户会误认为是程序缺陷。
_HTTP_HINTS = {
    403: "请求被拒绝（防盗链或权限不足）。程序不进行重试。若持续出现，"
         "请稍后重试或更换网络环境。",
    412: "请求被风控拦截。该状态为临时性的，通常等待数十秒后恢复。"
         "程序按合规要求不再重试；请稍后重新执行。",
    429: "请求过于频繁，已触发限流。请降低调用频率，等待一段时间后重试。",
}


def _http_hint(code: int) -> str:
    return _HTTP_HINTS.get(code, "")


def _err(code: str, message: str, *, hint: str = "", **extra):
    d = {"ok": False, "error": code, "message": message}
    if hint:
        d["hint"] = hint
    d.update(extra)
    return d


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------

class BiliSession:
    """游客会话。

    必须在第一次 API 调用前访问一次首页以取得 buvid3 cookie；
    无此 cookie 时接口会概率性返回 HTTP 412（实测出现过）。
    只接受显式传入的 sessdata；None 表示纯游客。
    """

    def __init__(self, sessdata: str | None = None, min_interval: float = MIN_INTERVAL):
        self._min_interval = min_interval
        self._last = 0.0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._opener.addheaders = [("User-Agent", UA)]
        self.request_count = 0
        self._bootstrapped = False
        if sessdata:
            self._jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0, name="SESSDATA", value=sessdata, port=None,
                    port_specified=False, domain=".bilibili.com", domain_specified=True,
                    domain_initial_dot=True, path="/", path_specified=True,
                    secure=True, expires=None, discard=False, comment=None,
                    comment_url=None, rest={}, rfc2109=False,
                )
            )

    # -- 节流 --------------------------------------------------------------
    def _throttle(self):
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def bootstrap(self):
        """访问首页取游客 cookie。幂等。"""
        if self._bootstrapped:
            return
        self._request(HOME_URL, headers={"Referer": HOME_URL}, want="bytes")
        self._bootstrapped = True

    # -- 底层请求 ----------------------------------------------------------
    def _request(self, url, *, headers=None, want="json"):
        """带节流+退避重试的 HTTP 请求。返回 (status, headers, body_bytes)。"""
        hdrs = dict(headers or {})
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                backoff = 2 ** (attempt - 1)  # 1s, 2s
                time.sleep(backoff)
            self._throttle()
            self.request_count += 1
            req = urllib.request.Request(url, headers=hdrs)
            try:
                with self._opener.open(req, timeout=TIMEOUT) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                err = _err(
                    "http_%d" % e.code,
                    "服务端返回 HTTP %d" % e.code,
                    hint=_http_hint(e.code),
                    attempt=attempt,
                    body_snippet=body[:200].decode("utf-8", "ignore"),
                )
                if e.code in FATAL_HTTP:
                    # 风控 —— 立即停止，不做任何反反爬尝试
                    raise BiliFatal(err) from None
                if e.code in RETRY_HTTP and attempt < MAX_RETRIES:
                    last_exc = err
                    continue
                raise BiliFatal(err) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                err = _err("network_error", str(e), attempt=attempt)
                if attempt < MAX_RETRIES:
                    last_exc = err
                    continue
                raise BiliFatal(err) from None
        raise BiliFatal(last_exc or _err("unknown", "请求失败"))

    def get_json(self, url, *, referer=None):
        """发起请求并解析 JSON。

        ⚠️ 关键：B 站 API 的错误语义在 JSON 的 data.code 里，
        HTTP 状态码与 Content-Type 永远是正常的 200 / application/json。
        所以绝不能靠 HTTP 状态或 content-type 判断成败。
        """
        self.bootstrap()
        headers = {"Referer": referer or HOME_URL}
        status, resp_headers, body = self._request(url, headers=headers)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            ct = resp_headers.get("Content-Type", "")
            return _err(
                "not_json",
                "响应不是合法 JSON: %s" % e,
                hint="接口可能已变更或返回了风控页面",
                http_status=status,
                content_type=ct,
                raw_snippet=body[:300].decode("utf-8", "ignore"),
            )
        return payload

    # -- 流式请求 ----------------------------------------------------------
    def open_stream(self, url, *, referer=None):
        """打开一个流式响应（CDN 下载用）。

        为什么需要这个公开方法：`get_json()` 是"读完整个 body 再解析"，
        不适合几 MB~几百 MB 的媒体流。而下载方（L2 工具层）如果自己去碰
        `_opener` / `_throttle`，就是**分层泄漏** —— 节流与 cookie 的合规
        约束会被绕过，且私有成员一改就到处崩。

        所以把"节流 + 计数 + cookie + 错误语义"全部收在 L1，
        对外只暴露一个待关闭的响应对象。调用方只需 `with` + `read(n)`。
        """
        self.bootstrap()
        headers = {"Referer": referer or HOME_URL}
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                time.sleep(2 ** (attempt - 1))
            self._throttle()
            self.request_count += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                return self._opener.open(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                err = _err("http_%d" % e.code, "下载被拒绝 HTTP %d" % e.code,
                           attempt=attempt)
                if e.code in FATAL_HTTP:
                    # 风控 / 防盗链 —— 立即停止，绝不重试
                    raise BiliFatal(err) from None
                if e.code in RETRY_HTTP and attempt < MAX_RETRIES:
                    last_exc = err
                    continue
                raise BiliFatal(err) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                err = _err("network_error", str(e), attempt=attempt)
                if attempt < MAX_RETRIES:
                    last_exc = err
                    continue
                raise BiliFatal(err) from None
        raise BiliFatal(last_exc or _err("unknown", "流式请求失败"))


class BiliFatal(Exception):
    """不可重试的错误（风控/网络耗尽）。携带结构化 payload。"""

    def __init__(self, payload):
        super().__init__(payload.get("message", "fatal"))
        self.payload = payload


# --------------------------------------------------------------------------
# 工具 1：搜索
# --------------------------------------------------------------------------

_EM_RE = re.compile(r"</?em[^>]*>")


def _clean_text(s) -> str:
    if not isinstance(s, str):
        return ""
    return html.unescape(_EM_RE.sub("", s)).strip()


def _clean_candidate(item: dict, keywords=()) -> dict:
    """把一条原始搜索结果清洗成给 LLM 看的稳定结构。

    keywords: 用于打相关性标记。这不是"锦上添花"——B 站对任意垃圾关键词
    都会返回一页量级的结果（实测 numResults=1000），所以"是否为空"根本
    无法判断搜索是否成功，必须做逐条相关性检查，否则 LLM 会在一堆无关
    视频里"自信地"选一个。
    """
    bvid = item.get("bvid") or ""
    title = _clean_text(item.get("title"))
    author = _clean_text(item.get("author"))
    tag = _clean_text(item.get("tag"))
    haystack = (title + " " + author + " " + tag).lower()

    matched = [k for k in keywords if k and k.lower() in haystack]
    rel = 1.0 if keywords and len(matched) == len(keywords) else (
        0.5 if matched else 0.0
    )

    return {
        "bvid": bvid,
        "title": title,
        "author": author,
        "duration": item.get("duration") or "",
        "play": int(item.get("play") or 0),
        "favorites": int(item.get("favorites") or 0),
        "pubdate": int(item.get("pubdate") or 0),
        "url": video_url(bvid) if bvid else "",
        "relevance": rel,
    }


def search_videos(session: BiliSession, keyword: str, page: int = 1, limit: int = 5):
    """按关键词搜索视频。

    返回 {ok, keyword, total, items: [Candidate], error?, hint?}
    limit 用于控制回喂给 LLM 的上下文长度（默认 5 条）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return _err("empty_keyword", "搜索关键词为空", hint="请让用户澄清想找什么")
    if limit < 1:
        return _err("bad_limit", "limit 必须 >= 1")

    qs = urllib.parse.urlencode(
        {"search_type": "video", "keyword": keyword, "page": page}
    )
    url = "%s?%s" % (API_SEARCH, qs)

    try:
        payload = session.get_json(url, referer=SEARCH_REFERER)
    except BiliFatal as e:
        return e.payload

    if payload.get("ok") is False:  # get_json 自己产生的错误
        return payload

    code = payload.get("code")
    if code != 0:
        # 实测：纯空格关键词 → code=-1200；这类要靠 code 而不是 HTTP 状态发现
        return _err(
            "api_error",
            "搜索接口返回 code=%s: %s" % (code, payload.get("message")),
            hint="code=-1200 通常表示关键词非法或为空",
            api_code=code,
        )

    data = payload.get("data") or {}
    raw = data.get("result")
    if not isinstance(raw, list):
        return _err(
            "unexpected_shape",
            "data.result 不是列表（接口结构可能已变更）",
            hint="若接口返回空数组且 code=0，优先怀疑用了 wbi/search 端点",
            got_type=type(raw).__name__,
        )

    # 过滤广告位：无 bvid 的条目不是可下载视频
    kws = [t for t in re.split(r"[\s,，、]+", keyword) if t]
    items = [
        _clean_candidate(x, kws)
        for x in raw
        if isinstance(x, dict) and x.get("bvid")
    ]
    if not items:
        # 静默失败检测：code=0 但空数组是最危险的形态
        return _err(
            "empty_result",
            "搜索返回 0 条可用视频",
            hint="换关键词重试；若反复为空请检查是否误用了需 WBI 签名的端点",
            keyword=keyword,
            page=page,
        )

    items.sort(key=lambda x: (x["relevance"], x["play"]), reverse=True)
    kept = items[:limit]

    # ⚠️ 相关性守门：B 站对垃圾关键词也返回满页结果，所以必须自己判断
    matched_n = sum(1 for x in kept if x["relevance"] > 0)
    res = {
        "ok": True,
        "keyword": keyword,
        "page": page,
        "total": data.get("numResults"),
        "items": kept,
        "returned": len(kept),
        "matched": matched_n,
    }
    if matched_n == 0:
        # 不当作失败返回，而是带上明确的怀疑信号 —— 让 LLM 知道"这批结果不可信"
        res["warning"] = "no_relevant_result"
        res["hint"] = (
            "本次返回的条目均未包含关键词「%s」，B 站对无匹配关键词也会返回"
            "满页模糊结果。请勿在其中选择，应换关键词重搜或向用户澄清。" % keyword
        )
    return res


# --------------------------------------------------------------------------
# 工具 2：视频信息
# --------------------------------------------------------------------------

def get_video_info(session: BiliSession, bvid: str):
    """取视频元信息。返回 {ok, bvid, aid, title, owner, duration, cid, pages}"""
    bvid = (bvid or "").strip()
    if not bvid:
        return _err("empty_bvid", "bvid 为空")
    if not re.fullmatch(r"BV[0-9A-Za-z]{8,12}", bvid):
        return _err("bad_bvid", "bvid 格式不合法: %r" % bvid, hint="应形如 BV1xx411c7mD")

    url = "%s?%s" % (API_VIEW, urllib.parse.urlencode({"bvid": bvid}))
    try:
        payload = session.get_json(url, referer=video_url(bvid))
    except BiliFatal as e:
        return e.payload
    if payload.get("ok") is False:
        return payload

    if payload.get("code") != 0:
        return _err(
            "api_error",
            "view 接口返回 code=%s: %s" % (payload.get("code"), payload.get("message")),
            hint="code=-404 表示视频不存在或已失效",
            api_code=payload.get("code"),
        )

    d = payload.get("data") or {}
    pages = [
        {"page": p.get("page"), "cid": p.get("cid"), "part": _clean_text(p.get("part")),
         "duration": p.get("duration")}
        for p in (d.get("pages") or [])
    ]
    return {
        "ok": True,
        "bvid": d.get("bvid") or bvid,
        "aid": d.get("aid"),
        "title": _clean_text(d.get("title")),
        "owner": _clean_text((d.get("owner") or {}).get("name")),
        "duration": d.get("duration"),
        "cid": d.get("cid"),
        "pages": pages,
        "url": video_url(d.get("bvid") or bvid),
    }


# --------------------------------------------------------------------------
# 工具 3：下载（html5 单一 mp4，无需 ffmpeg 合并）
# --------------------------------------------------------------------------

def _playurl_html5(session: BiliSession, bvid: str, cid: int):
    qs = urllib.parse.urlencode(
        {"bvid": bvid, "cid": cid, "platform": "html5", "high_quality": 1}
    )
    url = "%s?%s" % (API_PLAYURL, qs)
    try:
        payload = session.get_json(url, referer=referer_for(bvid))
    except BiliFatal as e:
        return e.payload
    if payload.get("ok") is False:
        return payload
    if payload.get("code") != 0:
        return _err(
            "api_error",
            "playurl 返回 code=%s: %s" % (payload.get("code"), payload.get("message")),
            hint="游客仅能取到低清流；付费/会员内容必然失败",
            api_code=payload.get("code"),
        )

    d = payload.get("data") or {}
    durl = d.get("durl") or []
    if not durl:
        return _err(
            "no_stream",
            "playurl 未返回可下载的直链（durl 为空）",
            hint="该内容可能需要登录或属于付费/充电专属",
            quality=d.get("quality"),
            fmt=d.get("format"),
        )
    seg = durl[0]
    return {
        "ok": True,
        "url": seg.get("url"),
        "size": int(seg.get("size") or 0),
        "length_ms": int(seg.get("length") or 0),
        "quality": d.get("quality"),
        "format": d.get("format"),
        "segments": len(durl),
    }


def _safe_filename(name: str, maxlen: int = 80) -> str:
    """把标题变成安全文件名：过滤路径分隔符与保留字符，防路径穿越。"""
    name = _clean_text(name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name or "untitled"


def download_html5_mp4(session: BiliSession, bvid: str, cid: int | None = None,
                       out_dir: str = "downloads", filename: str | None = None,
                       progress=None):
    """下载 platform=html5 的单一 mp4（音视频已封装，无需 ffmpeg）。

    返回 {ok, path, size, bytes, quality, elapsed, error?}
    校验：文件以 'ftyp' box 开头（确认下到的是 mp4 而不是错误页）。
    """
    if not bvid:
        return _err("empty_bvid", "bvid 为空")

    # 需要 cid
    if cid is None:
        info = get_video_info(session, bvid)
        if not info.get("ok"):
            return info
        cid = info.get("cid")
        title = info.get("title") or bvid
    else:
        info = get_video_info(session, bvid)
        title = (info.get("title") if info.get("ok") else None) or bvid

    if not cid:
        return _err("no_cid", "无法确定 cid", hint="视频可能已失效")

    stream = _playurl_html5(session, bvid, cid)
    if not stream.get("ok"):
        return stream

    total = stream["size"]
    os.makedirs(out_dir, exist_ok=True)

    # 磁盘空间守门：宁可提前失败，也不要写满磁盘
    try:
        st = os.statvfs(out_dir)
        free = st.f_bavail * st.f_frsize
        if total and free < total * 1.1:
            return _err(
                "insufficient_space",
                "磁盘空间不足：需要约 %.1f MB，可用 %.1f MB" % (total / 1048576, free / 1048576),
            )
    except OSError:
        pass

    stem = filename or ("%s_%s" % (bvid, _safe_filename(title)))
    path = os.path.join(out_dir, "%s.mp4" % stem)

    started = time.monotonic()
    written = 0
    try:
        # 走 L1 的公开流式接口 —— 节流 / cookie / 重试 / 错误语义都归会话层
        with session.open_stream(stream["url"], referer=referer_for(bvid)) as resp, \
                open(path, "wb") as fh:
            header = resp.read(4096)
            if b"ftyp" not in header[:64]:
                fh.close()
                os.unlink(path)
                return _err(
                    "not_mp4",
                    "下载内容不是 mp4（未见 ftyp box）",
                    hint="CDN 可能返回了错误页；检查 playurl 是否过期",
                    head_hex=header[:32].hex(),
                )
            fh.write(header)
            written = len(header)
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                if progress:
                    progress(written, total)
    except BiliFatal as e:
        if os.path.exists(path):
            os.unlink(path)
        payload = dict(e.payload)
        code = payload.get("error", "")
        if code in ("http_403", "http_412", "http_429"):
            # 防盗链 / 风控 —— 结构性失败，重试无意义
            payload["error"] = "risk_control"
            payload["message"] = "下载被拒绝（HTTP %s，疑似防盗链或风控）" % code.split("_")[1]
            payload["hint"] = ("已停止，不做重试。可重新取流"
                               "（playurl 签名 URL 会过期），或改用 DASH 方案")
        return payload
    except Exception as e:
        if os.path.exists(path):
            os.unlink(path)
        return _err("download_failed", str(e))

    if written == 0:
        os.path.exists(path) and os.unlink(path)
        return _err("empty_file", "下载得到 0 字节")

    return {
        "ok": True,
        "path": path,
        "bytes": written,
        "expected_bytes": total,
        "complete": (not total) or written >= total,
        "quality": stream.get("quality"),
        "elapsed": round(time.monotonic() - started, 2),
        "url": video_url(bvid),
        "title": title,
    }


# --------------------------------------------------------------------------
# 工具 4：音频抽取 —— 已迁至 media.py
# --------------------------------------------------------------------------
# 说明：v0.3 起 ffmpeg 调用不再内联在下载模块里，而是抽成独立的 media.py，
# 原因（这是"调用外部工具"作为 agent 实践的一部分）：
#   1. 能力探测（probe_capabilities）需要独立于下载链路存在
#   2. ffmpeg 的 stderr 需要被分类成模型可自救的结构化错误
#   3. 产物需要原子写入 + ffprobe 校验，否则会留下"看似成功"的坏文件
# 保持向后兼容的转发：
from .media import extract_audio, ffmpeg_path, probe_capabilities, probe_media  # noqa: E402,F401


def ffmpeg_available() -> bool:
    """向后兼容的薄封装。推荐直接用 media.probe_capabilities()。"""
    return ffmpeg_path() is not None

