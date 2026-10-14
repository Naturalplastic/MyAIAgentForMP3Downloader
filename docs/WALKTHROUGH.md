# 代码说明与接口参考

- 文档版本：1.0
- 适用代码版本：`biliagent` 1.0
- 标注约定：`file.py:NNN` 表示该文件第 NNN 行。行号随代码变动可能偏移，结构保持稳定

---

## 目录

1. [模块总览](#1-模块总览)
2. [config.py — 常量与护栏表](#2-configpy--常量与护栏表)
3. [env.py — 配置加载](#3-envpy--配置加载)
4. [bili.py — 会话层与 B 站工具](#4-bilipy--会话层与-b-站工具)
5. [media.py — 外部工具封装](#5-mediapy--外部工具封装)
6. [protocol.py — 协议层](#6-protocolpy--协议层)
7. [tools.py — 工具调度](#7-toolspy--工具调度)
8. [loop.py — agent 循环](#8-looppy--agent-循环)
9. [context.py — 上下文管理](#9-contextpy--上下文管理)
10. [llm.py — 模型适配](#10-llmpy--模型适配)
11. [cli.py — 命令行入口](#11-clipy--命令行入口)
12. [函数索引](#12-函数索引)

---

## 1. 模块总览

### 1.1 依赖关系

```
                    cli.py  (489 行)                L4 入口
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
    loop.py        tools.py       media.py          L3 / L2
    (324 行)       (255 行)       (449 行)
        │              │
   ┌────┴────┐         │
   ▼         ▼         │
protocol.py context.py │
 (295 行)   (360 行)   │
   │                   │
   └─────────┬─────────┘
             ▼
         bili.py (591 行)                          L1 + L2
             │
      ┌──────┼──────┬───────┐
      ▼      ▼      ▼       ▼
  llm.py  config.py env.py  (media.py)
  (260行)  (133行)  (188行)
```

依赖方向单向，无循环依赖。`bili.py` 仅被 `tools.py` 引用；
`media.py` 仅被 `tools.py` 与 `cli.py` 引用。

### 1.2 分层职责

| 层 | 模块 | 职责 | 禁止事项 |
|---|---|---|---|
| L4 | `cli.py` | 参数解析、输出格式化、日志落盘 | 业务判断、网络请求 |
| L3 | `loop.py` | 循环控制、护栏、状态管理 | 直接调用外部接口 |
| L3 | `protocol.py` | 输出解析、指令校验、Schema 定义 | 执行动作 |
| L3 | `tools.py` | action 路由、跨轮状态、观察压缩 | 直接构造 HTTP 请求 |
| L3 | `context.py` | token 估算、历史压缩、用量校准 | 修改消息语义 |
| L3 | `llm.py` | 模型 API 调用与错误整形 | 协议解析 |
| L2 | `bili.py` | B 站接口、结果清洗、下载 | 音频处理 |
| L2 | `media.py` | 外部工具探测、媒体处理 | B 站接口调用 |
| L1 | `BiliSession` | cookie、限流、重试、错误分级 | 业务逻辑 |
| L0 | 外部 | B 站 API/CDN、ffmpeg、DeepSeek | — |

### 1.3 阅读建议

按 L0 向上阅读：先看 `bili.py` 与 `media.py`（工具层），
再看 `protocol.py` 与 `tools.py`（协议与调度），最后看 `loop.py`（循环）。

---

## 2. `config.py` — 常量与护栏表

行数：133。集中存放路径常量、超时参数、护栏定义与偏好开关。

### 2.1 路径与超时常量

| 常量 | 行 | 值 | 说明 |
|---|---:|---|---|
| `ROOT` | 13 | 项目根目录 | 由 `__file__` 推导 |
| `BUNDLED_TOOLS_DIR` | 16 | `<ROOT>/.tools` | 便携外部工具目录 |
| `ENV_FFMPEG` / `ENV_FFPROBE` | 19–20 | `BILIAGENT_FFMPEG` / `BILIAGENT_FFPROBE` | 覆盖用环境变量名 |
| `DOWNLOAD_DIR` | 23 | `<ROOT>/downloads` | 默认输出目录 |
| `FFMPEG_TIMEOUT` | 26 | 600 | ffmpeg 单次执行超时（秒） |
| `MP3_QUALITY` | 29 | `"2"` | libmp3lame VBR 质量，实测约 168 kbps |
| `MP3_FALLBACK_CODEC` | 30 | `"mp3"` | 无 libmp3lame 时的回退编码器 |
| `MIN_REQUEST_INTERVAL` | 33 | 1.0 | 请求最小间隔（秒） |
| `MAX_RETRIES` | 34 | 2 | HTTP 重试上限 |

### 2.2 `KEEP_INTERMEDIATE_MP4`（L52）

```python
KEEP_INTERMEDIATE_MP4 = _env_bool("KEEP_INTERMEDIATE_MP4", False)
```

抽取音频后是否保留中间 mp4。取值来源于环境变量或 `.env`，
默认 `False`（删除）。

### 2.3 `GUARDRAILS`（L63）

护栏参数的集中定义。每一项包含四个字段：

| 字段 | 用途 |
|---|---|
| `value` | 实际取值，由 `loop.py` 与 `tools.py` 引用 |
| `enforced_by` | 强制执行的函数位置 |
| `on_exceed` | 越界后的行为契约 |
| `why` | 设置原因 |

```
GUARDRAILS = {
    "max_loop_rounds":     {"value": 10,    ...},
    "max_format_fixes":    {"value": 3,     ...},
    "repeat_action_limit": {"value": 3,     ...},
    "max_search_rounds":   {"value": 3,     ...},
    "max_context_tokens":  {"value": 20000, ...},
    "min_request_interval":{"value": 1.0,   ...},
    "max_http_retries":    {"value": 2,     ...},
    "ffmpeg_timeout":      {"value": 600,   ...},
}
```

引用方式（`loop.py:39-41`）：

```python
from .config import GUARDRAILS as _G
MAX_LOOP_ROUNDS     = _G["max_loop_rounds"]["value"]
MAX_FORMAT_FIXES    = _G["max_format_fixes"]["value"]
REPEAT_ACTION_LIMIT = _G["repeat_action_limit"]["value"]
```

引用方式（`tools.py:26`）：

```python
_MAX_SEARCH_ROUNDS = GUARDRAILS["max_search_rounds"]["value"]
```

集中定义的作用是消除"文档与代码取值不一致"的问题。

### 2.4 `PREFERENCES`（L117）

非护栏类的行为开关，目前包含 `KEEP_INTERMEDIATE_MP4`。

### 2.5 `guardrail_summary()`（L128）

将 `GUARDRAILS` 渲染为文本，供 `cli.py guardrails` 命令使用。

---

## 3. `env.py` — 配置加载

行数：188。从配置文件与环境变量读取配置项，提供脱敏输出。

### 3.1 加载优先级

```
真实环境变量  >  项目 .env  >  ~/.biliagent.env
```

路径常量：

| 常量 | 行 | 值 |
|---|---:|---|
| `PROJECT_ENV` | 35 | `<ROOT>/.env` |
| `USER_ENV` | 36 | `~/.biliagent.env` |
| `EXAMPLE_ENV` | 37 | `<ROOT>/.env.example` |

### 3.2 `MANAGED_KEYS`（L41）

受管键白名单。仅白名单内的键会从配置文件载入环境变量。

```python
MANAGED_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                "BILIAGENT_FFMPEG", "BILIAGENT_FFPROBE", "BILI_SESSDATA",
                "KEEP_INTERMEDIATE_MP4")
```

### 3.3 `env_bool(name, default=False)`（L52）

读取布尔型配置项。`true` / `1` / `yes` / `on`（不区分大小写）为真，
空值与未设置返回 `default`。

### 3.4 `parse_env_file(path)`（L66）

解析 `.env` 文件，返回 `{key: value}`。文件不存在时返回空字典。

语法支持：

| 形式 | 说明 |
|---|---|
| `KEY=value` | 基本形式 |
| `KEY="value"` / `KEY='value'` | 引号包裹，成对时去除 |
| `export KEY=value` | 带 `export` 前缀 |
| `# 注释` | 整行注释 |
| 空行 | 忽略 |

不支持多行值与变量插值。

两处边界处理：

- **行内注释仅识别 ` #`**（`env.py:103`）。若不加此前置空格判断，
  `https://example.com/v1#fragment` 中的片段标识符会被误删除。
- **引号成对才去除**（`env.py:97`）。若不成对，如 `ODD=he said "hi`，
  保留原样。

### 3.5 `load_env(override=False, verbose=False)`（L114）

将配置文件中的值写入 `os.environ`，返回 `{key: 来源文件路径}`。

参数：

| 参数 | 说明 |
|---|---|
| `override=False` | 不覆盖已存在的环境变量（默认为环境变量优先） |
| `override=True` | 文件值覆盖环境变量 |
| `verbose=True` | 返回来源信息 |

加载顺序（`env.py:127`）：

```python
for path in ((USER_ENV, PROJECT_ENV) if override else (PROJECT_ENV, USER_ENV)):
```

该顺序保证两种模式下的优先级均正确：`override=False` 时先加载者优先
（项目 `.env` 先加载）；`override=True` 时后加载者优先
（项目 `.env` 后加载）。

写入条件：

```python
if override or key not in os.environ or not os.environ.get(key):
```

`not os.environ.get(key)` 使空字符串被视为未设置，
因此 `export DEEPSEEK_API_KEY=` 之后仍可由 `.env` 补充。

**模块级缓存标志 `_loaded`**：加载过一次后再次调用直接返回。
该标志使 `load_env()` 非幂等，在测试中需显式重置：

```python
E._loaded = False
E.load_env()
```

调用位置：

| 位置 | 目的 |
|---|---|
| `cli.py:main()` 首行 | 使命令行参数默认值可读取 `.env` |
| `llm.py:20`（模块级） | 以库形式导入时亦可生效 |

### 3.6 `mask(value)`（L151）

密钥脱敏。保留前 7 与后 4 字符，中间以省略号替代，形如 `sk-49cb…84af`。
长度不超过 12 时仅保留前 2 字符。

### 3.7 `describe()`（L160）

返回当前配置状态，供 `cli.py cfg` 使用。密钥字段经 `mask()` 处理。

### 3.8 `missing_key_hint()`（L171）

密钥缺失时返回的配置指引，包含三种配置方式的完整命令与优先级说明。

---

## 4. `bili.py` — 会话层与 B 站工具

行数：591。包含会话管理（L73–234）与工具函数（L236–591）。

### 4.1 常量

| 常量 | 行 | 值 |
|---|---:|---|
| `UA` | 32 | Chrome 124 User-Agent |
| `HOME_URL` | 37 | `https://www.bilibili.com/` |
| `SEARCH_REFERER` | 38 | `https://search.bilibili.com/` |
| `API_SEARCH` | 39 | `x/web-interface/search/type` |
| `API_VIEW` | 40 | `x/web-interface/view` |
| `API_PLAYURL` | 41 | `x/player/playurl` |
| `MIN_INTERVAL` | 43 | 1.0 |
| `MAX_RETRIES` | 44 | 2 |
| `TIMEOUT` | 45 | 25 |
| `CHUNK` | 46 | 1 MiB |
| `FATAL_HTTP` | 49 | `{403, 412, 429}` |
| `RETRY_HTTP` | 50 | `{500, 502, 503, 504}` |

`FATAL_HTTP` 与 `RETRY_HTTP` 的划分依据：前者为风控与鉴权类响应，
重试无益且可能触发更严格的限制；后者为服务端临时故障，退避重试有效。

### 4.2 `_err(code, message, *, hint="", **extra)`（L65）

统一错误构造函数，返回：

```python
{"ok": False, "error": code, "message": message, "hint": hint, **extra}
```

`hint` 为 keyword-only 参数，强制调用方显式提供。该字段供模型读取，
用于自我修正。

### 4.3 `class BiliSession`（L77）

#### 构造函数（L85）

```python
BiliSession(sessdata: str | None = None, min_interval: float = 1.0)
```

创建 `CookieJar` 与 opener。`sessdata` 为 `None` 时不设置登录凭据，
即游客身份。

#### `_throttle()`（L107）

请求前强制等待，保证相邻请求间隔不小于 `min_interval`。使用
`time.monotonic()` 计时的原因是该值不受系统时钟调整影响。

#### `bootstrap()`（L113）

幂等。访问首页以获取游客 cookie（`buvid3`、`b_nut`）。
所有公开请求方法在发起前自动调用。

#### `_request(url, *, headers=None, want="json")`（L121）

底层请求，包含限流与退避重试。返回 `(status, headers, body_bytes)`，
不可重试的错误以 `BiliFatal` 抛出。

重试参数：最多 `MAX_RETRIES + 1` 次尝试，退避间隔 1 秒、2 秒。

#### `get_json(url, *, referer=None)`（L162）

发起请求并解析 JSON。仅负责获取与解析，不判断 `data.code`，
因为不同接口的错误码含义不同。

响应非合法 JSON 时返回 `not_json` 错误，附带响应摘要。

#### `open_stream(url, *, referer=None)`（L187）

返回流式响应对象，供大文件下载使用。`get_json()` 需读取完整响应体后
才能解析，不适用于媒体流。

该方法的存在避免了调用方访问 `_opener` / `_throttle` 等内部成员 ——
否则限流约束可被绕过，且内部实现变更会影响调用方。

### 4.4 `class BiliFatal`（L228）

不可重试错误的载体，携带结构化 `payload` 字段。

使用异常而非返回值的原因为：错误产生于 `_request()` 的重试循环内部，
返回值方式需逐层判断；异常可将错误直接传递至工具函数边界，
由 `tools.ToolBox.dispatch()` 统一转换为字典。

### 4.5 `_clean_text(s)`（L243）

去除 `<em>` 高亮标签并反转义 HTML 实体。B 站检索结果以
`<em class="keyword">` 标记匹配词。

### 4.6 `_clean_candidate(item, keywords=())`（L249）

将单条原始结果转换为稳定结构：

```python
{"bvid": str, "title": str, "author": str, "duration": str,
 "play": int, "favorites": int, "pubdate": int, "url": str,
 "relevance": float}
```

处理内容：

| 操作 | 说明 |
|---|---|
| 文本清洗 | 调用 `_clean_text()` |
| 类型转换 | `play`、`favorites`、`pubdate` 由字符串转为整数 |
| URL 补全 | 由 `bvid` 构造，避免模型自行拼接 |
| 相关性计算 | 在 `title + author + tag` 中匹配关键词 |

`relevance` 取值：`1.0` 全部关键词命中，`0.5` 部分命中，`0.0` 未命中。

### 4.7 `search_videos(session, keyword, page=1, limit=5)`（L281）

检索视频。

参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `session` | `BiliSession` | — | 会话实例 |
| `keyword` | str | — | 检索词，以空格分词 |
| `page` | int | 1 | 页码 |
| `limit` | int | 5 | 返回条数上限 |

返回：

```python
{"ok": True, "keyword": str, "page": int, "total": int,
 "matched": int, "items": [Candidate], "returned": int,
 "warning"?: str, "hint"?: str}
```

处理流程：

1. 参数校验（空关键词、`limit` 下限）
2. 调用接口，检查 `data.code`
3. 校验 `data.result` 类型
4. 逐条清洗与打分
5. 按 `(relevance, play)` 降序排序，截取前 `limit` 条
6. 统计相关性命中数；全部未命中时附加 `warning`

`total` 字段为接口声明的匹配总数，实测对无匹配关键词仍返回 1000，
不可用于判断检索有效性。有效判据为 `matched`。

错误码：`empty_keyword`、`bad_limit`、`api_error`、`unexpected_shape`、
`empty_result`、`not_json`、`http_403` / `http_412` / `http_429`。

### 4.8 `get_video_info(session, bvid)`（L371）

获取视频详情。

`bvid` 格式在本地以正则 `BV[0-9A-Za-z]{8,12}` 校验，不合法时返回
`bad_bvid`，不发起网络请求。

返回：

```python
{"ok": True, "bvid": str, "aid": int, "title": str, "owner": str,
 "duration": int, "cid": int, "pages": [{"page", "cid", "part", "duration"}],
 "url": str}
```

错误码：`empty_bvid`、`bad_bvid`、`api_error`（`-404` 表示视频不存在）。

### 4.9 `_playurl_html5(session, bvid, cid)`（L418）

获取 html5 变体的播放地址。请求参数：

```
playurl?bvid=<bvid>&cid=<cid>&platform=html5&high_quality=1
```

返回：

```python
{"ok": True, "url": str, "size": int, "length_ms": int,
 "quality": int, "format": str, "segments": int}
```

该变体返回单一完整 mp4 文件（音视频已封装），无需合并。
游客身份下 `quality` 为 16（360P）。

错误码：`api_error`、`no_stream`（内容可能需登录或属付费范围）。

### 4.10 `_safe_filename(name, maxlen=80)`（L459）

将标题转换为安全文件名。处理内容：

| 操作 | 说明 |
|---|---|
| 字符替换 | `/\:*?"<>|` 及控制字符（`\x00`–`\x1f`）替换为 `_` |
| 空白归一 | 连续空白压缩为单个空格 |
| 首尾处理 | 去除首尾空格与点号 |
| 长度限制 | 超出 `maxlen` 时截断 |

标题来自 B 站，模型提供的 URL 亦不可信，两者均需经此处理。

### 4.11 `download_html5_mp4(session, bvid, cid=None, out_dir="downloads", filename=None, progress=None)`（L469）

下载 html5 变体 mp4。

参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `session` | `BiliSession` | — | 会话实例 |
| `bvid` | str | — | 视频标识 |
| `cid` | int \| None | `None` | 未提供时自动获取 |
| `out_dir` | str | `"downloads"` | 输出目录 |
| `filename` | str \| None | `None` | 文件名主体，默认 `{bvid}_{标题}` |
| `progress` | Callable \| None | `None` | 进度回调 `(written, total)` |

校验点：

| 顺序 | 校验 | 失败错误码 |
|---:|---|---|
| 1 | `bvid` 非空 | `empty_bvid` |
| 2 | 可获取 `cid` | `no_cid` |
| 3 | 可获取播放地址 | `api_error` / `no_stream` |
| 4 | 磁盘可用空间 ≥ 文件大小 × 1.1 | `insufficient_space` |
| 5 | 响应首 64 字节含 `ftyp` | `not_mp4` |
| 6 | 写入字节数大于 0 | `empty_file` |

校验 4 的 1.1 系数为余量，避免下载过程中空间耗尽。
校验 5 用于识别 CDN 返回错误页的情况：错误页同样具有
`Content-Length`，流式读取不会报错，仅文件头可区分。

失败与异常路径均删除半成品文件。

返回：

```python
{"ok": True, "path": str, "bytes": int, "expected_bytes": int,
 "complete": bool, "quality": int, "elapsed": float,
 "url": str, "title": str}
```

### 4.12 `ffmpeg_available()`（L588）

向后兼容封装，等价于 `media.ffmpeg_path() is not None`。

---

## 5. `media.py` — 外部工具封装

行数：449。封装 ffmpeg 与 ffprobe 的探测与调用。

本模块不依赖 `bili.py`，可独立使用与测试。

### 5.1 二进制查找

| 函数 | 行 | 说明 |
|---|---:|---|
| `_candidates(name, env_var)` | 38 | 生成候选路径（生成器） |
| `find_binary(name)` | 52 | 返回首个存在且可执行的路径 |
| `ffmpeg_path()` | 61 | `find_binary("ffmpeg")` |
| `ffprobe_path()` | 65 | `find_binary("ffprobe")` |

查找顺序：环境变量 → 项目 `.tools/` → `PATH`。
判定条件为 `os.path.isfile()` 且 `os.access(path, os.X_OK)`。

### 5.2 `_run(args, timeout=20)`（L84）

统一的外部进程调用，返回 `(returncode, stdout, stderr)`。

返回码约定：

| 返回码 | 含义 |
|---:|---|
| `-1` | 超时 |
| `-2` | 未找到可执行文件 |
| `-3` | 操作系统错误 |

`subprocess.run` 使用 `errors="replace"`，避免编码异常导致失败。

### 5.3 `probe_capabilities(refresh=False)`（L101）

探测外部工具能力，结果缓存在模块级变量 `_cap_cache`。

返回：

```python
{"ok": bool,
 "ffmpeg":  {"path": str, "version": str} | None,
 "ffprobe": {"path": str, "version": str} | None,
 "encoders": {"has_libmp3lame": bool, "has_native_mp3": bool, "has_aac": bool},
 "muxers":   {"mp3": bool, "mp4": bool, "ipod": bool},
 "can_encode_mp3": bool,
 "can_extract_audio": bool,
 "errors": [str]}
```

`can_encode_mp3` 的判定为：mp3 容器可用，且存在 `libmp3lame` 或内置
mp3 编码器。

消费方：`cli.cmd_capabilities()`、`protocol.render_system_prompt()`、
`tools.ToolBox.__init__()`。

**输出解析注意事项**：`-encoders` 与 `-muxers` 的输出包含说明性头部：

```
 A....D libmp3lame      ← 条目（flags 6 字符）
  E mp3                 ← 条目（flags 1 字符）
 D. = Demuxing supported   ← 头部，非条目
 .E = Muxing supported     ← 头部，非条目
```

对应的正则（`media.py:80-81`）排除含 `=` 的字段：

```python
_INT_ENC_RE = re.compile(r"^\s*[A-Z.]{2,8}\s+(\S+)\s+\S", re.M)
_INT_MUX_RE = re.compile(r"^\s{2,}[A-Z.]{1,8}\s+([^\s=]\S*)\s+\S", re.M)
```

### 5.4 `probe_media(src, timeout=60)`（L175）

基于 `ffprobe -of json` 获取媒体信息，不解析人类可读文本。

返回：

```python
{"ok": True, "format": str, "duration": float, "size": int,
 "bit_rate": int, "streams": [dict], "audio": [dict], "video": [dict],
 "has_audio": bool, "has_video": bool}
```

错误码：

| 错误码 | 触发条件 |
|---|---|
| `ffprobe_missing` | 未找到 ffprobe |
| `no_src` | 文件不存在 |
| `incomplete_mp4` | stderr 含 `moov atom not found` |
| `corrupt_input` | stderr 含 `Invalid data found` |
| `ffprobe_failed` | 其他非零退出码 |

`incomplete_mp4` 与 `corrupt_input` 的分类依据来自 ffprobe 的 stderr 内容。

### 5.5 `classify_ffmpeg_error(stderr, returncode)`（L264）

将 stderr 归纳为结构化错误。

返回：

```python
{"error": str, "message": str, "hint": str, "stderr_tail": str,
 "matched_rule": str | None, "returncode": int}
```

识别规则（`_ERROR_PATTERNS`，L243）：

| 错误码 | 匹配模式 | `hint` 要点 |
|---|---|---|
| `encoder_missing` | `Unknown encoder '...'` | 该构建不含此编码器 |
| `decoder_missing` | `Unknown decoder '...'` | 输入格式解码器缺失 |
| `no_such_stream` | `Stream map '...' matches no streams` | 指定音频轨不存在 |
| `no_output_stream` | `Output file ... does not contain any stream` | 通常因无音频轨 |
| `corrupt_input` | `Invalid data found when processing input` | 输入损坏或非媒体文件 |
| `permission_denied` | `Permission denied` | 无写入权限 |
| `no_space` | `No space left on device` | 磁盘空间不足 |
| `incomplete_mp4` | `moov atom not found` | 文件不完整，应重新下载 |
| `conversion_failed` | `Conversion failed` | 转码失败 |
| `ffmpeg_failed` | 未匹配 | 依据 `stderr_tail` 判断 |

`stderr_tail` 保留最后 8 行（上限 600 字符）。未匹配到已知规则时，
该字段是模型唯一的诊断依据。

### 5.6 `_safe_unlink(path)`（L300）

忽略异常的文件删除。用于失败路径清理。

### 5.7 `extract_audio(src, mode="mp3", out_dir=None, out_name=None, progress=None)`（L308）

从媒体文件抽取或转换音频。

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `src` | str | 源文件路径 |
| `mode` | str | `"mp3"`（转码）或 `"copy"`（无损复制） |
| `out_dir` | str \| None | 输出目录，默认与源文件同目录 |
| `out_name` | str \| None | 输出文件名主体 |
| `progress` | Callable \| None | 预留参数 |

`mode` 取值：

| 值 | 编码 | 容器 | 说明 |
|---|---|---|---|
| `mp3` | `libmp3lame`，`-q:a 2` | mp3 | 有损转码，实测约 168 kbps |
| `copy` | 不重编码 | m4a（`ipod`） | 无损复制音轨 |

执行顺序：

| 步骤 | 操作 | 说明 |
|---:|---|---|
| 1 | 校验 `mode` | 必须最先执行，否则会被后续检查结果掩盖 |
| 2 | `probe_capabilities()` | 检查 ffmpeg 可用性与 mp3 编码能力 |
| 3 | `probe_media(src)` | 检查输入是否含音频轨 |
| 4 | 执行 ffmpeg | 输出至 `.part` 临时文件 |
| 5 | `_verify_output()` | 校验产物 |
| 6 | `os.replace()` | 原子改名 |

ffmpeg 命令：

```
<ffmpeg> -hide_banner -loglevel error -nostdin -y
         -i <src> -vn -map 0:a:0
         [-c:a libmp3lame -q:a 2 | -c:a copy]
         -f <mp3|ipod> <dst>.part
```

参数说明：

| 参数 | 作用 |
|---|---|
| `-nostdin` | 禁止读取标准输入，防止后台环境下挂起 |
| `-loglevel error` | 仅输出错误，避免噪声干扰错误分类 |
| `-vn` | 丢弃视频流 |
| `-map 0:a:0` | 仅取第一条音频轨 |
| `-f ipod` | `copy` 模式下明确容器类型 |

返回：

```python
{"ok": True, "path": str, "size": int, "mode": str, "codec": str,
 "duration": float, "source": str, "verified": bool | None,
 "elapsed": float, "ffmpeg_version": str}
```

`verified` 为三态：`True` 校验通过，`False` 校验失败，`None` 无法校验
（缺 ffprobe）。

错误码：`no_src`、`bad_mode`、`ffmpeg_missing`、`no_audio_stream`、
`ffmpeg_failed` 及其细化类别、`empty_output`、`verify_failed`。

### 5.8 `_verify_output(path, src_info)`（L421）

独立的产物校验，返回 `(True | False | None, detail)`。

校验项：

| 项 | 判据 |
|---|---|
| 容器格式 | `probe_media()` 成功 |
| 含音频流 | `has_audio` 为真 |
| 时长一致性 | 与源文件差值不超过 2 秒 |

2 秒容差的依据：MP3 编码会引入短暂的静音填充，实测差值约 0.03 秒。

---

## 6. `protocol.py` — 协议层

行数：295。定义模型的输出格式、解析规则、工具 Schema 与系统提示词生成。

### 6.1 标记常量

| 常量 | 行 | 值 |
|---|---:|---|
| `TOOL_CALL_OPEN` / `TOOL_CALL_CLOSE` | 31–32 | `<tool_call>` / `</tool_call>` |
| `FINAL_OPEN` / `FINAL_CLOSE` | 33–34 | `<final_answer>` / `</final_answer>` |

匹配正则（L36–37）：

```python
_TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FINAL_RE = re.compile(r"<final_answer>\s*(.*?)\s*</final_answer>", re.DOTALL)
```

`\s*` 容许标记内的换行与空白，`re.DOTALL` 使 `.` 匹配换行，
非贪婪量词避免跨块匹配。

### 6.2 解析状态

| 常量 | 行 | 值 | 含义 |
|---|---:|---|---|
| `OK_TOOL_CALL` | 40 | `"tool_call"` | 合法工具调用 |
| `OK_FINAL` | 41 | `"final_answer"` | 合法最终答复 |
| `ERR_NO_MARKER` | 42 | `"no_marker"` | 无标记 |
| `ERR_BOTH_MARKERS` | 43 | `"both_markers"` | 两种标记并存 |
| `ERR_MULTIPLE` | 44 | `"multiple_calls"` | 多个 `<tool_call>` |
| `ERR_BAD_JSON` | 45 | `"bad_json"` | 标记内非合法 JSON |
| `ERR_NOT_OBJECT` | 46 | `"not_object"` | JSON 合法但非对象 |
| `ERR_EMPTY_FINAL` | 47 | `"empty_final"` | 最终答复为空 |

各状态区分的原因：不同状态对应的修正指示（`fix`）不同。
若仅区分成功与失败，循环只能给出笼统的"格式错误"提示。

### 6.3 `parse_response(raw)`（L54）

参数：模型本轮输出的原始文本。

返回：

```python
{"status": str, "directive"?: dict, "text"?: str, "raw": str, "fix"?: str}
```

`status` 为 `OK_*` 时解析成功；为 `ERR_*` 时包含 `fix` 字段，
内容为面向模型的具体修正指引。

判定顺序：先检查两种标记是否并存，再检查 `<tool_call>` 数量，
然后依次处理工具调用与最终答复。

该方法不抛出异常：格式违规属预期情况。

### 6.4 `TOOL_SPECS`（L139）

工具定义的唯一来源。

```python
TOOL_SPECS = {
    "search":        {"desc": ..., "required": ["keyword"],
                      "schema": {"keyword": None, "limit": [1, 2, 3, 4, 5]}},
    "get_info":      {"desc": ..., "required": ["bvid"],     "schema": {"bvid": None}},
    "download":      {"desc": ..., "required": ["bvid"],     "schema": {"bvid": None}},
    "extract_audio": {"desc": ..., "required": ["mode"],
                      "schema": {"mode": ["mp3", "copy"]}},
    "ask_user":      {"desc": ..., "required": ["question"], "schema": {"question": None}},
    "finish":        {"desc": ..., "required": ["summary"],  "schema": {"summary": None}},
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `desc` | str | 工具描述，写入系统提示词 |
| `required` | list | 必填参数名 |
| `schema` | dict | 参数名 → 允许取值列表；`None` 表示任意取值 |

`ACTION_NAMES`（L172）为 `TOOL_SPECS` 的键集合。

该结构驱动三处逻辑：指令校验、提示词生成、action 路由。

### 6.5 `validate_directive(directive)`（L175）

参数：已解析的指令字典。

返回：

```python
{"ok": True, "action": str, "args": dict}
{"ok": False, "error": str, "fix": str, ...}
```

校验层次：

| 层 | 校验项 | 错误码 |
|---:|---|---|
| 1 | `action` 存在且属于 `TOOL_SPECS` | `missing_action` / `unknown_action` |
| 2 | `args` 为字典 | `args_not_object` |
| 3 | 必填参数齐全 | `missing_args` |
| 4 | 枚举参数取值合法 | `bad_arg_value` |
| 5 | 无多余参数 | `unknown_args` |

处理约定：

- `args` 为 `None` 时视为空字典（`{"action":"finish"}` 形式合法）
- 多余的顶层字段被忽略，不视为错误
- 必填参数的判据为键存在且值不为 `None` 或空字符串

### 6.6 `render_system_prompt(capabilities=None, extra_rules=())`（L238）

由 `TOOL_SPECS` 与能力探测结果生成系统提示词。

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `capabilities` | dict \| None | `probe_capabilities()` 的返回值 |
| `extra_rules` | tuple | 附加规则文本 |

生成内容：

1. 角色说明
2. 输出格式要求与规则列表
3. 工具清单（遍历 `TOOL_SPECS` 生成）
4. 本机能力说明（依据 `capabilities`）
5. 附加规则

能力说明的分支：

| 条件 | 提示词内容 |
|---|---|
| ffmpeg 可用且支持 mp3 | 声明可产出 mp3 |
| ffmpeg 可用但不支持 mp3 | 说明 `extract_audio` 仅可使用 `mode=copy` |
| ffmpeg 不可用 | 说明无法抽取音频，要求交付 mp4 或询问用户，并明确"不要反复尝试" |

---

## 7. `tools.py` — 工具调度

行数：255。

### 7.1 `class ToolBox`（L29）

单次会话的工具状态容器。

#### 构造函数（L32）

```python
ToolBox(session=None, out_dir="downloads", keep_mp4=None)
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `session` | `BiliSession` \| None | 为 `None` 时创建新实例 |
| `out_dir` | str | 输出目录 |
| `keep_mp4` | bool \| None | `None` 时取 `config.KEEP_INTERMEDIATE_MP4` |

`keep_mp4` 使用 `None` 作为"未指定"的标记，以区分"用户未指定"与
"用户指定保留"。

实例状态：

| 属性 | 说明 |
|---|---|
| `capabilities` | `probe_capabilities()` 结果，构造时探测一次 |
| `last_query` / `last_search_rounds` | 检索轮次统计 |
| `last_candidates` | 最近一次检索结果 |
| `selected_bvid` | 最近一次操作涉及的 `bvid` |
| `mp4_path` / `audio_path` | 产物路径 |
| `removed_intermediate` | 被清理的中间文件路径 |

使用实例而非模块级全局变量的原因：状态显式、可测试、支持多会话并行。

#### 终局 action

| 方法 | 行 | 返回 |
|---|---:|---|
| `_finish(args, state)` | 51 | `{"ok": True, "terminal": True, "kind": "finish", "summary": str}` |
| `_ask_user(args, state)` | 59 | `{"ok": True, "terminal": True, "kind": "ask_user", "question": str}` |

`terminal` 字段用于通知循环终止。

#### `_search(args, state)`（L68）

检索并在工具侧再次检查轮次上限。

同一关键词的计数逻辑：关键词相同则递增，否则重置为 1。
超过 `_MAX_SEARCH_ROUNDS`（3）时返回 `search_limit_reached`，
`hint` 提供两条出路（更换关键词或向用户提问）。

#### `_get_info(args, state)`（L92）

获取视频详情，成功后记录 `selected_bvid`。

#### `_download(args, state)`（L98）

下载 mp4，成功后记录 `selected_bvid` 与 `mp4_path`。

#### `_extract_audio(args, state)`（L106）

抽取音频。执行前有三项检查：

| 顺序 | 检查 | 失败错误码 | `hint` 要点 |
|---:|---|---|---|
| 1 | ffmpeg 可用 | `ffmpeg_missing` | 不要重试；交付 mp4 或询问用户 |
| 2 | 支持 mp3 编码（`mode=mp3` 时） | `mp3_unsupported` | 改用 `mode=copy` |
| 3 | 存在源文件（`mp4_path`） | `no_source` | 应先调用 `download` |

三项检查均在调用 `media.extract_audio()` 之前完成，避免必然失败的调用。

成功后按 `keep_mp4` 决定是否清理中间文件：

```python
if not res.get("ok"):
    return res                      # 失败时保留中间文件
self.audio_path = res["path"]
if not self.keep_mp4 and self.mp4_path and os.path.isfile(self.mp4_path):
    if os.path.getsize(res["path"]) > 0:
        try:
            freed = os.path.getsize(self.mp4_path)
            os.unlink(self.mp4_path)
            self.removed_intermediate = self.mp4_path
            res["intermediate_removed"] = True
            res["freed_mb"] = round(freed / 1048576, 2)
            self.mp4_path = None
        except OSError as e:
            res["intermediate_removed"] = False
            res["intermediate_error"] = str(e)
```

三项约束：

| 约束 | 理由 |
|---|---|
| 失败时不删除 | 中间文件是唯一的重试依据 |
| 产物非空才删除 | 防止"产物为空但源文件已删" |
| 删除失败不影响成功状态 | 音频已生成，主任务已完成 |

`mp4_path` 置为 `None` 的目的：避免后续调用指向已删除的文件。

#### `dispatch(action, args, state)`（L162）

action 路由，通过命名约定映射至 `_<action>` 方法。

异常处理：

```python
try:
    return handler(args, state)
except BiliFatal as e:
    return e.payload
except Exception as e:
    return {"ok": False, "error": "tool_crashed",
            "message": "%s: %s" % (type(e).__name__, e),
            "hint": "工具内部异常。请检查参数是否合理，或换一种方式达成目标。"}
```

工具层不向上抛出异常，以保证模型接收到的是结构化信息而非 Python 堆栈。

#### `make_observation(action, result, max_items=5)`（L188）

将工具结果转换为观察对象。

处理规则：

| action | 结果状态 | 处理 |
|---|---|---|
| `search` | 成功 | 截取前 `max_items` 条；候选仅保留 7 个字段；透传 `warning` 与 `hint` |
| `download` | 成功 | 保留 `path`、`size_mb`、`quality`、`complete`、`title` |
| `extract_audio` | 成功 | 保留 `path`、`codec`、`size_mb`、`duration`、`verified`；附加中间文件清理说明 |
| 任意 | 失败 | 保留 `error`、`message`、`hint` 及诊断字段 |
| 其他 | 成功 | 剔除 `terminal` 后原样返回 |

截断字段的原因：原始检索结果包含 30 个以上字段（`tag`、`pic`、
`senddate` 等），完整保留会使 token 消耗增加约 3 倍。

失败时完整保留错误信息的原因为：模型依赖该信息进行修正。

### 7.2 观察中的控制字段

| 字段 | 出现条件 | 含义 |
|---|---|---|
| `_action` | 始终 | 产生该观察的 action 名 |
| `_note` | 重复调用时 | 提示该动作此前已以相同参数执行过（含首次轮次） |
| `warning` | 检索结果全不相关 | `no_relevant_result` |
| `note` | 中间文件被清理 | 清理结果与释放空间 |

`_note` 为提示而非阻断：重复调用存在合法场景（如重新获取过期的签名 URL），
该字段将判断依据提供给模型，不代替模型决策。

---

## 8. `loop.py` — agent 循环

行数：324。

### 8.1 常量

| 常量 | 行 | 来源 |
|---|---:|---|
| `MAX_LOOP_ROUNDS` | 39 | `GUARDRAILS["max_loop_rounds"]["value"]` |
| `MAX_FORMAT_FIXES` | 40 | `GUARDRAILS["max_format_fixes"]["value"]` |
| `REPEAT_ACTION_LIMIT` | 41 | `GUARDRAILS["repeat_action_limit"]["value"]` |

### 8.2 `class AgentState`（L44）

单次任务的状态。

属性：

| 属性 | 类型 | 说明 |
|---|---|---|
| `user_text` | str | 原始输入 |
| `round` | int | 当前轮次 |
| `format_fixes` | int | **连续**格式违规计数，成功动作后归零 |
| `total_format_fixes` | int | 累计格式违规计数，用于诊断 |
| `compressions` | int | 上下文压缩次数 |
| `messages` | list[dict] | 发送给模型的消息数组 |
| `trace` | list[dict] | 每轮的结构化记录 |
| `outcome` | str \| None | 结束原因 |
| `report` | str \| None | 最终汇报文本 |
| `mp4_path` / `audio_path` | str \| None | 产物路径 |
| `started` | float | 起始时间戳 |

`format_fixes` 与 `total_format_fixes` 分离的原因：前者用于判断
（连续语义，避免惩罚"偶发错误但整体推进"的执行），后者用于诊断。

#### `to_dict()`（L61）

序列化为可 JSON 落盘的结构，包含 `trace` 与 `messages` 完整内容。

### 8.3 `class AgentLoop`（L78）

#### 构造函数（L87）

```python
AgentLoop(chat_fn, toolbox=None, max_rounds=MAX_LOOP_ROUNDS,
          max_format_fixes=MAX_FORMAT_FIXES, context_policy=None,
          verbose=False, logger=None)
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `chat_fn` | Callable | `(messages) -> dict`，模型调用的唯一接口 |
| `toolbox` | `ToolBox` \| None | 为 `None` 时创建新实例 |
| `max_rounds` | int | 轮数上限 |
| `max_format_fixes` | int | 连续格式违规上限 |
| `context_policy` | `ContextPolicy` \| None | 为 `None` 时使用默认值 |
| `verbose` | bool | 输出简化日志 |
| `logger` | Callable \| None | `(event, payload)` 形式的日志回调 |

`chat_fn` 的返回值约定：

```python
{"text": str, "usage"?: dict, "reasoning_len"?: int, ...}   # 成功
{"error": str, "message"?: str, "hint"?: str, ...}          # 失败
```

该参数是循环与模型的唯一耦合点。`ScriptedChat`（测试）与
`DeepSeekClient`（生产）在循环视角下无区别。

#### `_log(state, event, payload)`（L101）

调用 `logger` 并在 `verbose` 模式下输出。

#### `run(user_text)`（L108）

主循环。参数为原始用户输入，返回 `AgentState`。

初始化（L109–120）：

```python
state.messages.append({"role": "system",
                       "content": P.render_system_prompt(self.box.capabilities)})
state.messages.append({"role": "user", "content": user_text})
last_sig = None
repeat = 0
executed = {}     # (action, args) 签名 → 首次执行轮次
```

循环步骤：

| 步骤 | 行 | 内容 |
|---:|---:|---|
| 0 | 127 | `ctx.maybe_compress()`，在调用模型前执行 |
| 1 | 133 | 调用模型；记录真实 `usage` 用于校准 |
| 2 | 157 | `parse_response()`；格式违规时回喂 `fix` |
| 3 | 182 | `final_answer` 时终止 |
| 4 | 192 | `validate_directive()`；非法时回喂 `fix` |
| 5 | 215 | 重复动作检测（基于 `last_sig` 与 `repeat`） |
| 6 | 233 | 执行工具，捕获 `BiliFatal` 与 `Exception` |
| 7 | 246 | 处理 `terminal` 结果 |
| 8 | 265 | 生成观察、附加重复提示、回喂；成功后重置连续违规计数 |

模型调用失败的处理（L134–145）：汇报文本由 `error`、`message`、`hint`
三部分拼接而成，避免仅输出错误码。

重复动作检测（L215–226）：

```python
sig = (action, json.dumps(args, sort_keys=True, ensure_ascii=False))
if sig == last_sig:
    repeat += 1
else:
    repeat = 0
    last_sig = sig
if repeat >= REPEAT_ACTION_LIMIT:
    state.outcome = "stuck"
    break
```

`sort_keys=True` 使参数顺序不同的相同调用被视为同一签名。

重复调用提示（L269–281）：当签名曾执行过时，在观察中追加 `_note`
字段，但不阻断执行。

连续违规计数重置（L284）：

```python
if obs.get("ok"):
    state.format_fixes = 0
```

### 8.4 `class ScriptedChat`（L307）

按预设序列返回输出的替代实现，用于离线验证循环逻辑。

| 方法 | 行 | 说明 |
|---|---:|---|
| `__init__(script, name="scripted")` | 314 | 保存输出序列 |
| `__call__(messages)` | 319 | 依次返回；序列用尽时返回合法的终止输出 |

序列用尽时返回 `<final_answer>脚本已用尽</final_answer>`，
避免测试挂起。

---

## 9. `context.py` — 上下文管理

行数：360。

### 9.1 token 估算

常量（L62–66）：

```python
CJK_ANCHOR = 1.044      # 纯中文 token/字符，实测
OTHER_ANCHOR = 0.322    # 纯英文 token/字符，实测
```

| 函数 | 行 | 说明 |
|---|---:|---|
| `estimate_tokens(text)` | 69 | 单段文本估算 |
| `estimate_messages_tokens(messages)` | 85 | 整段对话估算，每条附加 4 token 结构开销 |

估算公式：

```python
n = len(text)
r = 汉字数量 / n
return int(n * (CJK_ANCHOR * r + OTHER_ANCHOR * (1 - r))) + 1
```

标定过程：

| 方案 | 结果 |
|---|---|
| 中文/非中文两分类 | 一般样本误差最大 1.9 倍 |
| 汉字/字母/数字/符号四分类 | 无法泛化。同为中文，重复串实测 0.546，单字串 1.044 |
| 单一系数 | 中文与英文相差 3.3 倍 |
| 双锚点插值 | 平均比值 1.03，范围 0.67–1.30 |

该函数仅用于判断是否需要压缩，不用于计费。计费依据为 API 返回的
`usage.prompt_tokens`。

### 9.2 摘要生成

| 函数 | 行 | 说明 |
|---|---:|---|
| `_summarize_observation(content, max_len=120)` | 113 | 将观察压缩为单行 |
| `_summarize_assistant(content, max_len=80)` | 145 | 提取 action 名 |

压缩后的条目带 `[已压缩]` 前缀（`_MARK`，L98）。

### 9.3 压缩产物约束

| 常量 | 行 | 值 |
|---|---:|---|
| `SUMMARY_TAG` | 109 | `<history_summary>` |
| `SUMMARY_TAG_END` | 110 | `</history_summary>` |

#### `_cap_history(blocks, max_chars=1400)`（L160）

为压缩历史设置滑窗上限。超出时从旧到新整条丢弃，
并插入省略标记。

丢弃整条而非截断单条的原因：截断会产生不完整信息。

#### `_merge_summaries(pinned)`（L196）

将多条既有摘要合并为一条。若不做合并，摘要条数会随压缩次数线性增长。

### 9.4 `compress_messages(messages, keep_recent=3, max_history_chars=1400)`（L220）

返回 `(新消息数组, 被压缩条数)`。

保留策略：

| 内容 | 处理 |
|---|---|
| `system` 提示词 | 永久保留 |
| 第一条 `user`（原始任务） | 永久保留 |
| 既有摘要 | 合并为一条，不再压缩 |
| 最近 `keep_recent` 轮 | 完整保留 |
| 更早轮次 | 合并为摘要 |

三重约束对应三类已修复的问题：

| 约束 | 未处理时的现象 |
|---|---|
| 摘要不再压缩 | 摘要被反复压缩形成嵌套，信息层层丢失 |
| 多摘要合并 | 摘要条数随压缩次数增长，压缩机制自身成为增长源 |
| 摘要设滑窗上限 | 消息条数受控但摘要内容持续增长 |

滑窗生效后的实测数据（每 3 轮压缩一次）：

| 轮次 | 摘要条数 | 消息数 | 摘要字符 | 估算 token |
|---:|---:|---:|---:|---:|
| 15 | 1 | 7 | 897 | 1351 |
| 30 | 1 | 7 | 678 | 1261 |
| 45 | 1 | 7 | 477 | 1175 |
| 60 | 1 | 7 | 276 | 1088 |

### 9.5 `class ContextPolicy`（L291）

| 方法 | 行 | 说明 |
|---|---:|---|
| `__init__(max_tokens=20000, keep_recent=3, enabled=True)` | 303 | 初始化 |
| `maybe_compress(messages)` | 314 | 超预算时压缩；返回 `(messages, 是否压缩)` |
| `observe_actual(estimated, usage)` | 330 | 记录估算值与真实用量 |
| `calibration()` | 338 | 返回偏差统计 |
| `stats()` | 351 | 返回完整统计 |

`maybe_compress()` 在 `dropped == 0` 时不递增压缩计数，
以区分"尝试压缩但无内容可压"与"实际压缩了内容"。

#### 用量校准

`observe_actual()` 记录 `(估算值, 真实值)` 对。
`calibration()` 输出：

```python
{"samples": int, "mean_ratio": float, "min_ratio": float,
 "max_ratio": float, "note": str}
```

`ratio` 为估算值与真实值之比。大于 1 表示估算偏保守（安全方向）。

实测 `mean_ratio ≈ 1.05`。

---

## 10. `llm.py` — 模型适配

行数：260。

### 10.1 常量

| 常量 | 行 | 值 |
|---|---:|---|
| `DEFAULT_BASE` | 48 | `https://api.deepseek.com` |
| `DEFAULT_MODEL` | 49 | `deepseek-flash` |
| `ENV_KEY` / `ENV_BASE` / `ENV_MODEL` | 50–52 | 对应环境变量名 |
| `TIMEOUT` | 54 | 180 |
| `MAX_RETRIES` | 55 | 2 |
| `RETRY_HTTP` | 58 | `{500, 502, 503, 504, 429}` |

`RETRY_HTTP` 包含 429，与 `bili.py` 的处理相反。原因：
对 B 站而言 429 为风控信号，重试可能加剧限制；对模型 API 而言
429 为限流，退避重试是标准做法。

### 10.2 模块级初始化（L20）

```python
load_env()
```

模块导入时加载配置。置于模块级而非构造函数中的原因：
避免配置读取时机与导入顺序耦合。

### 10.3 `class LLMError`（L61）

携带结构化 `payload` 的异常。

### 10.4 `redact(text)`（L67）

移除文本中的 API key。有单元测试覆盖。

### 10.5 `_post(url, body, api_key, timeout)`（L75）

发送 POST 请求，返回 `(status, payload)`。
非 JSON 响应体封装为 `{"raw": ...}`。

### 10.6 `class DeepSeekClient`（L104）

#### 构造函数（L111）

```python
DeepSeekClient(api_key=None, model=None, base_url=None,
               effort=None, temperature=None)
```

参数为 `None` 时依次从环境变量与默认值取值。
缺少 API key 时抛出 `LLMError`，`hint` 字段包含完整配置指引。

统计属性：`calls`、`total_prompt_tokens`、`total_completion_tokens`、
`total_reasoning_tokens`。

#### `__call__(messages)`（L131）

`chat()` 的别名，使实例可直接作为 `AgentLoop` 的 `chat_fn`。

#### `chat(messages)`（L134）

请求体：

```python
{"model": str, "messages": list, "stream": False,
 "reasoning_effort"?: str, "temperature"?: float}
```

返回值见 `DESIGN.md` §9.2。

重试逻辑：`RETRY_HTTP` 中的状态码退避重试，退避间隔 1 秒、2 秒。

#### `_shape_ok(payload)`（L178）

整理成功响应。`reasoning_content` 仅记录长度不保留内容：
该字段若回传将持续占用上下文，且对结构化输出无贡献。

#### `_shape_err(status, msg)`（L205）

按状态码生成 `hint`：

| 状态码 | `hint` |
|---|---|
| 401 | 检查 API key 与环境变量 |
| 402 | 余额不足 |
| 429 | 触发限流，降低频率或稍后重试 |
| 5xx | 服务端错误，已重试仍失败 |
| 网络错误 | 检查网络与 `base_url` 配置 |

#### `stats()`（L225）

返回累计调用次数与 token 用量。

### 10.7 `chat_with_tools(client, messages, tools)`（L238）

基于 DeepSeek 原生 `tools` / `tool_calls` 的等价实现。
本项目未采用，保留作为对照。

### 10.8 `from_env(**kwargs)`（L258）

按环境变量构造客户端。

---

## 11. `cli.py` — 命令行入口

行数：489。

### 11.1 辅助函数

| 函数 | 行 | 说明 |
|---|---:|---|
| `_log(name, payload)` | 40 | 以 JSONL 追加写入 `logs/cli_YYYYMMDD.jsonl` |
| `_fmt_duration(sec)` | 52 | 时长格式化 |
| `_print_err(res)` | 61 | 统一错误输出，包含 `hint` |

`_log()` 在 `OSError` 时静默返回。日志为辅助功能，
不应因磁盘问题导致主任务失败。

### 11.2 子命令

| 函数 | 行 | 子命令 | 说明 |
|---|---:|---|---|
| `cmd_search` | 71 | `search` | 输出候选与相关性标记 |
| `cmd_info` | 94 | `info` | 输出视频详情 |
| `cmd_download` | 114 | `download` | 下载；可选抽取音频 |
| `cmd_audio` | 166 | `audio` | 从本地文件抽取音频 |
| `cmd_capabilities` | 194 | `caps` | 输出能力探测结果；`--probe` 可附带媒体探测 |
| `cmd_agent` | 233 | `agent` | 运行 agent 循环 |
| `cmd_cfg` | 361 | `cfg` | 输出配置来源（密钥脱敏） |
| `cmd_guardrails` | 383 | `guardrails` | 输出护栏表与合规约定 |

### 11.3 `cmd_agent(args, session)`（L233）

模型来源判定：

| 条件 | 来源 |
|---|---|
| 提供 `--script` | 命令行指定的输出序列 |
| 提供 `--fake` | 内置脚本 |
| 默认 | `DeepSeekClient` |

密钥缺失时的处理：输出 `missing_key_hint()` 的完整指引，
并提示可使用 `--fake`，返回退出码 2。

token 统计：当使用真实模型时，对 `chat` 方法做包装以记录
延迟、token 用量与思考长度。

### 11.4 落盘内容

`cmd_agent` 落盘的内容为 `state.to_dict()`，包含完整的
`messages` 与 `trace`，可用于回放。

### 11.5 `main(argv=None)`（L472）

首行调用 `load_env()`，使命令行参数默认值可读取配置文件。

---

## 12. 函数索引

### 12.1 按模块

| 模块 | 公开函数 / 类 |
|---|---|
| `config.py` | `guardrail_summary`、`GUARDRAILS`、`PREFERENCES` |
| `env.py` | `env_bool`、`parse_env_file`、`load_env`、`mask`、`describe`、`missing_key_hint` |
| `bili.py` | `BiliSession`、`BiliFatal`、`search_videos`、`get_video_info`、`download_html5_mp4`、`video_url`、`referer_for`、`ffmpeg_available` |
| `media.py` | `find_binary`、`ffmpeg_path`、`ffprobe_path`、`probe_capabilities`、`probe_media`、`classify_ffmpeg_error`、`extract_audio` |
| `protocol.py` | `parse_response`、`validate_directive`、`render_system_prompt`、`TOOL_SPECS` |
| `tools.py` | `ToolBox` |
| `loop.py` | `AgentLoop`、`AgentState`、`ScriptedChat` |
| `context.py` | `estimate_tokens`、`estimate_messages_tokens`、`compress_messages`、`ContextPolicy` |
| `llm.py` | `DeepSeekClient`、`LLMError`、`redact`、`chat_with_tools`、`from_env` |
| `cli.py` | `main`、各 `cmd_*` 函数 |

### 12.2 内部函数

以下划线开头，不属公开接口：

| 模块 | 函数 | 用途 |
|---|---|---|
| `bili.py` | `_err` | 错误构造 |
| `bili.py` | `_clean_text`、`_clean_candidate` | 结果清洗 |
| `bili.py` | `_playurl_html5` | 获取播放地址 |
| `bili.py` | `_safe_filename` | 文件名安全化 |
| `media.py` | `_candidates` | 候选路径生成 |
| `media.py` | `_run` | 外部进程调用 |
| `media.py` | `_safe_unlink` | 忽略异常的文件删除 |
| `media.py` | `_verify_output` | 产物校验 |
| `context.py` | `_summarize_observation`、`_summarize_assistant` | 摘要生成 |
| `context.py` | `_cap_history`、`_merge_summaries` | 压缩产物约束 |
| `tools.py` | `_finish`、`_ask_user`、`_search`、`_get_info`、`_download`、`_extract_audio` | action 处理器 |
| `llm.py` | `_post` | HTTP 请求 |
| `llm.py` | `_shape_ok`、`_shape_err` | 响应整理 |

### 12.3 变更指引

| 需求 | 修改位置 |
|---|---|
| 更换模型 | `llm.DeepSeekClient` 或实现 `chat_fn` 接口的类 |
| 新增工具 | `protocol.TOOL_SPECS` 增加定义，`tools.ToolBox` 增加 `_<name>` 方法 |
| 调整提示词 | `protocol.render_system_prompt` |
| 调整护栏取值 | `config.GUARDRAILS` |
| 调整上下文预算 | `ContextPolicy` 参数或命令行 `--max-context` |
| 调整压缩策略 | `context.compress_messages`、`_cap_history` |
| 调整候选排序 | `bili.search_videos` 的排序表达式 |
| 调整相关性判据 | `bili._clean_candidate` |
| 增加 ffmpeg 错误识别 | `media._ERROR_PATTERNS` |
| 调整抽音频参数 | `media.extract_audio` 的命令构造、`config.MP3_QUALITY` |
| 调整中间文件策略 | `config.KEEP_INTERMEDIATE_MP4`、`tools.ToolBox._extract_audio` |
| 新增命令行子命令 | `cli.py` 增加 `cmd_*` 函数并在 `build_parser` 注册 |

### 12.4 调试入口

```bash
# 查看某次运行的完整记录
tail -1 logs/cli_$(date +%Y%m%d).jsonl | python3 -m json.tool | head -80

# 查看运行骨架
python3 -c "
import json
d = json.loads(open('logs/cli_20261015.jsonl').readlines()[-1])
r = d['result']
print(r['user_text'], '|', r['outcome'], '|', r['round'])
for t in r['trace']:
    print(' ', t['round'], t['type'], t.get('action', ''))
"

# 单独验证协议解析
python3 -c "
from biliagent.protocol import parse_response
print(parse_response('任意文本'))
print(parse_response('说明文字<tool_call>{\"action\":\"search\"}</tool_call>'))
"

# 单独验证配置加载
python3 -c "
from biliagent.env import parse_env_file, describe
print(parse_env_file('.env.example'))
print(describe())
"

# 离线运行循环测试
python3 tests/test_loop.py
```
