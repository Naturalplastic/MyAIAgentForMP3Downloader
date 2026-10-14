# 设计文档

- 文档版本：1.0
- 对应代码：`biliagent/`（10 个模块，3,431 行）
- 状态：工具层与 agent 循环已实现，245 项测试通过

---

## 1. 概述

### 1.1 目标

实现一个能够将自然语言描述转换为本地媒体文件的 agent：

```
输入：我想要泠鸢的泼墨漓江，要 mp3 音频
输出：downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3
```

agent 需自主完成：需求理解、站内检索、结果筛选、视频下载、音频抽取、结果汇报。

### 1.2 设计约束

| 约束 | 说明 |
|---|---|
| 零第三方依赖 | 仅使用 Python 标准库；外部程序仅 ffmpeg/ffprobe |
| 模型接口为纯文本 | 使用标记包裹协议，不使用 `response_format`，不使用原生 function calling |
| 游客身份 | 不登录、不使用他人凭据、不绕过会员鉴权 |
| 不使用 agent 框架 | 循环、协议、工具调度、护栏均自行实现 |
| 不使用第三方 B 站库 | 相关社区项目已于 2026 年归档并清空代码（见 `FACTS.md`） |

### 1.3 非目标

- 不追求最高画质/音质（游客上限为 360P / 187 kbps，不为此引入登录）
- 不实现批量下载、频道订阅
- 不提供公共服务，不打包分发

---

## 2. 架构

### 2.1 分层

```
L4  入口层        cli.py
L3  循环层        loop.py  ──┬── protocol.py   协议解析与校验
    模型适配      llm.py     └── context.py    上下文预算管理
    工具调度      tools.py
L2  工具层        bili.py      B 站接口
                  media.py     本地媒体处理
L1  会话层        BiliSession  cookie / 限流 / 重试 / 错误语义
L0  外部依赖      B 站 API、B 站 CDN、ffmpeg、ffprobe、DeepSeek API
```

分层依据为变更频率：L1 几乎不变，L2 随外部接口变动，L3 随协议与策略调整，
L4 最易变更。依赖方向单向，无循环依赖（`bili.py` 仅被 `tools.py` 引用）。

### 2.2 模块清单

| 模块 | 行数 | 职责 |
|---|---:|---|
| `cli.py` | 489 | 命令行入口：参数解析、输出、日志落盘 |
| `loop.py` | 324 | agent 主循环、护栏、状态管理 |
| `protocol.py` | 295 | 输出标记解析、指令校验、工具 Schema、提示词生成 |
| `tools.py` | 255 | action 路由、跨轮状态、观察压缩 |
| `bili.py` | 591 | 会话管理、搜索、详情、下载 |
| `media.py` | 449 | 外部工具探测、媒体探测、错误分类、音频抽取 |
| `context.py` | 360 | token 估算、历史压缩、用量校准 |
| `llm.py` | 260 | DeepSeek API 适配 |
| `env.py` | 188 | 配置文件加载与脱敏 |
| `config.py` | 133 | 常量与护栏集中表 |

### 2.3 数据流

```
用户输入
  │
  ├─ cli.cmd_agent()            装配 ToolBox / DeepSeekClient / ContextPolicy
  │   └─ loop.AgentLoop.run()
  │       ├─ protocol.render_system_prompt()      生成系统提示词
  │       └─ 循环（上限 10 轮）
  │           ├─ context.maybe_compress()         预算检查
  │           ├─ llm.DeepSeekClient.chat()        调用模型
  │           ├─ protocol.parse_response()        解析标记
  │           ├─ protocol.validate_directive()    校验指令
  │           ├─ tools.ToolBox.dispatch()         路由执行
  │           │   └─ bili.* / media.*             实际工作
  │           └─ tools.ToolBox.make_observation() 压缩结果并回喂
  └─ cli._log()                 落盘 logs/cli_*.jsonl
```

---

## 3. 设计原则

### 3.1 harness 不信任工具层的契约

工具层约定"永不抛出异常，只返回结构化 dict"。但该约定是设计意图，
不是运行时保证。`AgentLoop.run()` 在执行工具时另行捕获 `BiliFatal` 与
`Exception`，转换为结构化错误。若缺少该层保护，工具实现的任意疏漏都会导致
循环中断，而模型将收到 Python 堆栈信息，无法据此恢复。

### 3.2 错误信息必须可行动

所有对外错误均包含三个字段：

| 字段 | 读者 | 内容 |
|---|---|---|
| `error` | 程序 | 机器可读的错误码 |
| `message` | 人 | 错误描述 |
| `hint` | 模型与用户 | 下一步应如何操作 |

`hint` 为必需项而非可选项。模型依赖 `hint` 完成自我修正。

### 3.3 不可靠性应被翻译而非传播

外部依赖存在三类失效模式：

| 来源 | 失效表现 | 处理方式 |
|---|---|---|
| B 站 API | HTTP 200，错误位于 `data.code` | 显式检查 `code != 0` |
| ffmpeg | 退出码非零，原因位于 stderr | `classify_ffmpeg_error()` 归类为 8 类 |
| 网络/CDN | 403/412/429 | 按状态码分级：致命类不重试 |

### 3.4 判断权与执行权分离

模型负责语义判断（需求理解、结果筛选、格式选择），代码负责执行与约束
（限流、重试、轮数上限、产物校验）。约束不得交由模型决定。

### 3.5 计数器的语义需明确

格式违规计数采用**连续计数**（成功动作后归零）而非累计计数。
累计计数会将"偶发格式错误但整体推进"误判为失败。

---

## 4. 接口设计

### 4.1 工具契约

所有工具函数遵循统一签名与返回约定。

```
签名：f(session_or_context, <业务参数>) -> dict
返回：{"ok": True,  <业务字段>}
      {"ok": False, "error": str, "message": str, "hint": str, [诊断字段]}
```

工具函数不抛出异常（`BiliFatal` 在 `ToolBox.dispatch()` 边界被转换）。

### 4.2 B 站工具（`bili.py`）

#### `search_videos`

```python
search_videos(session: BiliSession, keyword: str,
              page: int = 1, limit: int = 5) -> dict
```

检索视频并按相关性排序。

请求：
```
GET https://api.bilibili.com/x/web-interface/search/type
    ?search_type=video&keyword=<kw>&page=<n>
Referer: https://search.bilibili.com/
```

响应（成功）：
```json
{
  "ok": true,
  "keyword": "泠鸢 泼墨漓江",
  "page": 1,
  "total": 1000,
  "matched": 5,
  "items": [
    {
      "bvid": "BV1Ds411h7T1",
      "title": "【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】",
      "author": "泠鸢yousa",
      "duration": "5:6",
      "play": 1710053,
      "favorites": 91383,
      "pubdate": 1600000000,
      "url": "https://www.bilibili.com/video/BV1Ds411h7T1",
      "relevance": 1.0
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total` | int | 接口声明的匹配总数。**不可用于判断检索有效性**（见 4.2.1） |
| `matched` | int | 返回条目中关键词实际命中的数量 |
| `relevance` | float | `1.0` 全部关键词命中，`0.5` 部分命中，`0.0` 未命中 |
| `play` | int | 播放量。接口原始返回为字符串，已转换为整数 |

错误码：

| `error` | 触发条件 | `hint` 要点 |
|---|---|---|
| `empty_keyword` | 空或纯空白关键词 | 请求用户澄清 |
| `bad_limit` | `limit < 1` | 参数下限 |
| `api_error` | `data.code != 0` | `-1200` 表示关键词非法 |
| `unexpected_shape` | `data.result` 非列表 | 提示可能误用 WBI 端点 |
| `empty_result` | 无可用视频条目 | 更换关键词 |
| `not_json` | 响应非合法 JSON | 接口变更或返回风控页面 |
| `http_403/412/429` | 风控 | 立即停止，不重试 |

#### 4.2.1 相关性守门

B 站检索接口对无匹配的关键词同样返回整页结果。实测：

```
keyword = "zzqqxx不存在的关键词zzqqxx"
data.code = 0, data.numResults = 1000
返回内容为与关键词无关的视频
```

因此"结果是否为空"不能作为检索有效性的判据。实现采用逐条相关性标记：
在 `title + author + tag` 中做关键词子串匹配，按 `(relevance, play)`
降序排序。当返回条目全部 `relevance == 0` 时，在成功响应中附加：

```json
{"warning": "no_relevant_result",
 "hint": "本次返回的条目均未包含关键词「...」，请勿在其中选择，应换关键词重搜或向用户澄清。"}
```

该字段使模型能够区分"检索失败"与"结果不可信"。

#### `get_video_info`

```python
get_video_info(session: BiliSession, bvid: str) -> dict
```

请求：`GET https://api.bilibili.com/x/web-interface/view?bvid=<bvid>`

响应：
```json
{
  "ok": true,
  "bvid": "BV1Ds411h7T1",
  "aid": 275792276,
  "title": "【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】",
  "owner": "泠鸢yousa",
  "duration": 306,
  "cid": 1261421445,
  "pages": [{"page": 1, "cid": 1261421445, "part": "...", "duration": 306}],
  "url": "https://www.bilibili.com/video/BV1Ds411h7T1"
}
```

`bvid` 格式在本地以正则校验（`BV[0-9A-Za-z]{8,12}`），不合法时不发起网络请求。

#### `download_html5_mp4`

```python
download_html5_mp4(session: BiliSession, bvid: str, cid: int | None = None,
                   out_dir: str = "downloads", filename: str | None = None,
                   progress: Callable | None = None) -> dict
```

执行流程与校验点：

| 步骤 | 校验 | 失败错误码 |
|---|---|---|
| 1 | `bvid` 非空 | `empty_bvid` |
| 2 | 获取 `cid`（未提供时调用 `get_video_info`） | `no_cid` |
| 3 | 获取播放地址（`playurl?platform=html5&high_quality=1`） | `api_error` / `no_stream` |
| 4 | `os.statvfs()` 可用空间 ≥ 文件大小 × 1.1 | `insufficient_space` |
| 5 | 响应首 64 字节含 `ftyp` box | `not_mp4` |
| 6 | 写入字节数 > 0 | `empty_file` |

失败与异常路径均删除半成品文件。响应：

```json
{"ok": true, "path": "downloads/...mp4", "bytes": 16276355,
 "expected_bytes": 16276355, "complete": true, "quality": 16,
 "elapsed": 2.6, "url": "...", "title": "..."}
```

### 4.3 媒体工具（`media.py`）

#### `probe_capabilities`

```python
probe_capabilities(refresh: bool = False) -> dict
```

探测外部工具能力，结果缓存。查找顺序：环境变量 → 项目 `.tools/` 目录 → `PATH`。

```json
{
  "ok": true,
  "ffmpeg":  {"path": "...", "version": "6.0-static"},
  "ffprobe": {"path": "...", "version": "6.0-static"},
  "encoders": {"has_libmp3lame": true, "has_native_mp3": false, "has_aac": true},
  "muxers":   {"mp3": true, "mp4": true, "ipod": true},
  "can_encode_mp3": true,
  "can_extract_audio": true,
  "errors": []
}
```

该结果有三个消费方：CLI 输出、系统提示词生成、工具调用守门。

解析 ffmpeg `-encoders` / `-muxers` 输出时需排除说明性头部（形如
`D. = Demuxing supported`），否则会被误识别为条目。

#### `probe_media`

```python
probe_media(src: str, timeout: int = 60) -> dict
```

基于 `ffprobe -of json` 输出，不解析人类可读文本。响应包含 `format`、
`duration`、`size`、`streams`、`has_audio`、`has_video`。

错误码：`ffprobe_missing`、`no_src`、`incomplete_mp4`（缺 `moov`）、
`corrupt_input`、`ffprobe_failed`。

#### `extract_audio`

```python
extract_audio(src: str, mode: str = "mp3", out_dir: str | None = None,
              out_name: str | None = None, progress=None) -> dict
```

`mode` 取值：

| 值 | 编码 | 容器 | 说明 |
|---|---|---|---|
| `mp3` | `libmp3lame`，`-q:a 2` | mp3 | 有损转码，实测约 168 kbps |
| `copy` | 不重编码 | m4a (`ipod`) | 无损复制音轨 |

执行顺序（顺序本身是设计的一部分）：

1. 参数校验（`mode` 合法性）—— **必须最先**，否则会被后续检查的结果掩盖
2. 能力探测（ffmpeg 是否存在、是否支持 mp3）
3. 输入探测（`probe_media`，判断是否含音频流）
4. 构造并执行命令，输出至 `.part` 临时文件
5. `_verify_output()` 校验产物
6. `os.replace()` 原子改名

ffmpeg 命令：

```
<ffmpeg> -hide_banner -loglevel error -nostdin -y
         -i <src> -vn -map 0:a:0
         [-c:a libmp3lame -q:a 2 | -c:a copy]
         -f <mp3|ipod> <dst>.part
```

各参数必要性：

| 参数 | 作用 |
|---|---|
| `-nostdin` | 防止在后台或管道环境下挂起 |
| `-loglevel error` | 抑制冗余输出，避免错误分类被噪声干扰 |
| `-vn` | 丢弃视频流 |
| `-map 0:a:0` | 仅取第一条音频轨 |
| `-f ipod` | `copy` 模式下明确容器类型 |

产物校验（`_verify_output`）返回三态：

| 返回值 | 含义 | 后续动作 |
|---|---|---|
| `True` | 校验通过 | 原子改名 |
| `False` | 校验失败 | 删除临时文件并报错 |
| `None` | 无法校验（缺 ffprobe） | 保留文件，`verified` 字段记为 `null` |

校验项：容器格式、含音频流、时长与源文件差值 ≤ 2 秒。

#### `classify_ffmpeg_error`

```python
classify_ffmpeg_error(stderr: str, returncode: int) -> dict
```

将 stderr 归纳为结构化错误。识别类别：

`encoder_missing`、`decoder_missing`、`no_such_stream`、`no_output_stream`、
`corrupt_input`、`incomplete_mp4`、`permission_denied`、`no_space`、
`conversion_failed`；未匹配时返回 `ffmpeg_failed`。

响应保留 `stderr_tail`（末 8 行，上限 600 字符）与 `matched_rule`。

### 4.4 中间产物管理

音频抽取以 mp4 为源文件，因此 mp4 为必要中间产物。其体积通常为音频的
2~3 倍（实测 15.52 MB 对 5.82 MB）。清理策略：

| 条件 | 行为 |
|---|---|
| 抽取成功 且 `keep_mp4=False` | 删除中间 mp4，记录 `intermediate_removed` 与 `freed_mb` |
| 抽取成功 且 `keep_mp4=True` | 保留，不记录清理动作 |
| 抽取失败 | **保留**中间 mp4（唯一的重试依据） |
| 删除失败（`OSError`） | 不影响主流程成功状态，记录 `intermediate_error` |

清理后 `ToolBox.mp4_path` 置为 `None`，避免后续调用指向已删除文件。

开关优先级：命令行 `--keep-mp4` > 环境变量 `KEEP_INTERMEDIATE_MP4` >
`config.KEEP_INTERMEDIATE_MP4`。

---

## 5. 会话层

`BiliSession` 封装全部与 B 站通信的横切关注点。

### 5.1 接口

```python
class BiliSession:
    def __init__(self, sessdata: str | None = None,
                 min_interval: float = 1.0) -> None
    def bootstrap(self) -> None
    def get_json(self, url: str, *, referer: str | None = None) -> dict
    def open_stream(self, url: str, *, referer: str | None = None) -> HTTPResponse
```

`_request()` 与 `_throttle()` 为内部方法；所有外部调用通过上述三个公开方法，
以保证限流与 cookie 约束无法被绕过。

### 5.2 Cookie 管理

访问首页获取游客 cookie（`buvid3`、`b_nut`）后所有请求复用同一 `CookieJar`。
`bootstrap()` 幂等，在所有 API 调用前自动执行。

依据：无 cookie 请求曾有约 412 失败记录，条件未稳定复现，故采用固定成本
的预防措施。

### 5.3 限流与重试

| 参数 | 值 | 依据 |
|---|---|---|
| 最小请求间隔 | 1.0 s | 合规要求 |
| 最大重试次数 | 2 | 合规要求 |
| 退避策略 | 1 s, 2 s | 指数退避 |

HTTP 状态码分级：

| 状态码 | 分类 | 处理 |
|---|---|---|
| 403 / 412 / 429 | 致命 | 立即终止，转为 `BiliFatal` |
| 500 / 502 / 503 / 504 | 可重试 | 退避后重试 |
| 其他 4xx | 致命 | 立即终止 |

`open_stream()` 采用相同的分级策略。

### 5.4 错误语义

B 站 API 的错误信息位于响应体的 `data.code` 字段，HTTP 状态码与
`Content-Type` 恒为正常值。因此：

- 不得以 HTTP 状态码判断业务成败
- 不得以 `Content-Type` 判断响应是否为错误页
- 必须先检查 `code != 0`，再解析 `data`

---

## 6. agent 循环

### 6.1 组件

```python
AgentLoop(chat_fn, toolbox=None, max_rounds=10, max_format_fixes=3,
          context_policy=None, verbose=False, logger=None)
```

`chat_fn` 签名为 `(messages: list[dict]) -> dict`，返回 `{"text": str, ...}`
或 `{"error": str, ...}`。该参数是循环与模型的唯一耦合点：
`ScriptedChat` 用于离线测试，`DeepSeekClient` 用于生产，
两者对循环不可区分。

### 6.2 循环步骤

| 步骤 | 操作 | 代码位置 |
|---|---|---|
| 0 | 上下文预算检查与压缩 | `loop.py:127` |
| 1 | 调用模型；记录真实 usage 用于校准 | `loop.py:133` |
| 2 | 解析标记；格式违规时回喂修正指示 | `loop.py:157` |
| 3 | 若为 `<final_answer>` 则终止 | `loop.py:182` |
| 4 | 校验指令；非法时回喂修正指示 | `loop.py:192` |
| 5 | 重复动作检测 | `loop.py:215` |
| 6 | 执行工具（含异常兜底） | `loop.py:233` |
| 7 | 终局 action 处理（`finish` / `ask_user`） | `loop.py:246` |
| 8 | 压缩观察、重复调用提示、回喂 | `loop.py:265` |

### 6.3 终止条件

| `outcome` | 触发 | 汇报内容 |
|---|---|---|
| `finished` | 模型输出 `<final_answer>` 或调用 `finish` | 模型给出的总结 |
| `asked` | 模型调用 `ask_user` | 模型提出的问题 |
| `max_rounds` | 达到轮数上限 | 最后一轮的工具结果 |
| `format_stuck` | 连续格式违规达到上限 | 模型最后一次原始输出 |
| `stuck` | 同一动作重复达到上限 | 重复的动作与参数 |
| `llm_error` | 模型调用失败 | `error` + `message` + `hint` |

### 6.4 护栏

| 护栏 | 默认值 | 强制位置 | 越界行为 |
|---|---:|---|---|
| `max_loop_rounds` | 10 | `AgentLoop.run()` | 停止，`outcome=max_rounds` |
| `max_format_fixes` | 3 | `AgentLoop.run()` | 停止，附模型原始输出 |
| `repeat_action_limit` | 3 | `AgentLoop.run()` | 停止，`outcome=stuck` |
| `max_search_rounds` | 3 | `ToolBox._search()` | 返回 `search_limit_reached` |
| `max_context_tokens` | 20000 | `ContextPolicy.maybe_compress()` | 压缩历史 |
| `min_request_interval` | 1.0 s | `BiliSession._throttle()` | 主动等待 |
| `max_http_retries` | 2 | `BiliSession._request()` | 返回结构化错误 |
| `ffmpeg_timeout` | 600 s | `media._run()` | 返回超时错误 |

默认值集中定义于 `config.GUARDRAILS`，由 `loop.py` 与 `tools.py` 引用。

---

## 7. 模型协议

### 7.1 输出格式

模型每轮须输出且仅输出以下两种形式之一：

```
<tool_call>{"action": "...", "args": {...}}</tool_call>
<final_answer>...</final_answer>
```

采用标记包裹而非"尝试解析 JSON，失败即视为最终答复"的原因：模型输出
带前置说明的 JSON 时（如 `好的，我来搜索：<tool_call>{...}</tool_call>`），
后者会导致整段解析失败并被误判为最终答复。

### 7.2 解析状态

`parse_response()` 返回以下状态之一：

| 状态 | 含义 | 处置 |
|---|---|---|
| `tool_call` | 合法工具调用 | 进入校验 |
| `final_answer` | 合法最终答复 | 终止循环 |
| `no_marker` | 无标记 | 回喂修正指示 |
| `both_markers` | 两种标记并存 | 回喂修正指示 |
| `multiple_calls` | 多个 `tool_call` | 回喂修正指示 |
| `bad_json` | 标记内非合法 JSON | 回喂修正指示 |
| `not_object` | JSON 合法但非对象 | 回喂修正指示 |
| `empty_final` | `<final_answer>` 为空 | 回喂修正指示 |

格式违规响应包含 `fix` 字段，内容为具体的修正指引。该字段被原样注入
下一轮消息，构成自我修正闭环：

```
模型输出非法 → 校验失败 → 生成 fix → 注入对话 → 模型修正后重发
```

### 7.3 指令校验

`validate_directive()` 执行五层校验：

| 层 | 校验项 | 错误码 |
|---|---|---|
| 1 | `action` 存在且属于 `TOOL_SPECS` | `missing_action` / `unknown_action` |
| 2 | `args` 为对象 | `args_not_object` |
| 3 | 必填参数齐全 | `missing_args` |
| 4 | 枚举参数取值合法 | `bad_arg_value` |
| 5 | 无多余参数 | `unknown_args` |

多余的顶层字段被忽略（模型常附加解释性字段）。

### 7.4 工具 Schema

`TOOL_SPECS` 为工具定义的唯一来源，同时驱动：

1. `validate_directive()` 的校验规则
2. `render_system_prompt()` 生成的提示词
3. `ToolBox.dispatch()` 的 action 路由

| action | 必填参数 | 可选参数 | 说明 |
|---|---|---|---|
| `search` | `keyword` | `limit` ∈ [1,5] | 检索视频 |
| `get_info` | `bvid` | — | 视频详情 |
| `download` | `bvid` | — | 下载 mp4 |
| `extract_audio` | `mode` ∈ {mp3, copy} | — | 抽取音频 |
| `ask_user` | `question` | — | 向用户提问 |
| `finish` | `summary` | — | 汇报并结束 |

### 7.5 能力注入

`render_system_prompt(capabilities)` 将外部工具能力写入系统提示词：

| 条件 | 提示词内容 |
|---|---|
| ffmpeg 可用且支持 mp3 | 声明可产出 mp3 |
| ffmpeg 可用但不支持 mp3 | 提示 `extract_audio` 仅可使用 `mode=copy` |
| ffmpeg 不可用 | 声明无法抽取音频，要求交付 mp4 或询问用户，并明确"不要反复尝试" |

`ToolBox._extract_audio()` 在工具侧执行同样的检查，形成双重防护。

---

## 8. 上下文管理

### 8.1 必要性

每轮的观察结果均完整进入消息历史。单次检索的观察约 1,000 字符。
实测五次调用累计输入 5,533 token 而输出仅 268 token —— 输入为输出的 20 倍。
若无预算控制，长任务将同时面临上下文溢出与成本失控。

### 8.2 token 估算

DeepSeek 未提供可本地调用的 tokenizer，因此采用估算。标定过程：

| 方案 | 结果 |
|---|---|
| 按"中文/非中文"两分类 | 一般样本误差最大 1.9 倍 |
| 按汉字/字母/数字/符号四分类 | 无法泛化：同为中文，重复串实测 0.546，单字串 1.044，相差 1.9 倍 |
| 单一系数 | 中文与英文相差 3.3 倍，任一单值在另一侧严重偏差 |
| **按中文占比双锚点插值** | 平均比值 1.03，范围 0.67~1.30 |

最终实现：

```python
CJK_ANCHOR = 1.044     # 纯中文 token/字符（实测）
OTHER_ANCHOR = 0.322   # 纯英文 token/字符（实测）

def estimate_tokens(text: str) -> int:
    n = len(text)
    r = len(_CJK.findall(text)) / n
    return int(n * (CJK_ANCHOR * r + OTHER_ANCHOR * (1 - r))) + 1
```

该函数仅用于判断是否需要压缩，不得用于计费。计费依据为 API 返回的
`usage.prompt_tokens`。

### 8.3 用量校准

`ContextPolicy.observe_actual(estimated, usage)` 记录估算值与真实用量的比值。
`calibration()` 汇总偏差统计。实测 `mean_ratio ≈ 1.05`，即估算偏保守。

### 8.4 压缩策略

```
保留：system 提示词（永久）
保留：原始用户任务（永久）
保留：最近 keep_recent 轮（默认 3）
压缩：更早轮次 → 单行摘要，标注 [已压缩]
```

压缩产物受三重约束：

| 约束 | 实现 | 防止的问题 |
|---|---|---|
| 摘要不再被压缩 | `SUMMARY_TAG` 检测与摘出 | 自我嵌套导致信息层层丢失 |
| 多条摘要合并为一条 | `_merge_summaries()` | 摘要条数随压缩次数线性增长 |
| 摘要内容设滑窗上限 | `_cap_history(max_chars=1400)` | 压缩产物自身无限增长 |

滑窗溢出时整条丢弃（而非截断单条），并插入显式的省略标记。

实测（60 轮，每 3 轮压缩一次）：

| 轮次 | 摘要条数 | 消息数 | 摘要字符 | 估算 token |
|---:|---:|---:|---:|---:|
| 15 | 1 | 7 | 897 | 1351 |
| 30 | 1 | 7 | 678 | 1261 |
| 45 | 1 | 7 | 477 | 1175 |
| 60 | 1 | 7 | 276 | 1088 |

---

## 9. 模型适配

### 9.1 接口

```python
DeepSeekClient(api_key=None, model=None, base_url=None,
               effort=None, temperature=None)
```

`__call__(messages)` 实现为 `chat()` 的别名，使客户端实例可直接作为
`AgentLoop` 的 `chat_fn` 参数。

### 9.2 返回值

成功：

```json
{"text": "...", "reasoning_len": 108, "finish_reason": "stop",
 "model": "deepseek-flash",
 "usage": {"prompt_tokens": 519, "completion_tokens": 72,
           "reasoning_tokens": 60}}
```

失败：

```json
{"error": "http_401", "message": "Authentication Fails...",
 "hint": "API key 无效或已失效。请在 DeepSeek 控制台确认，并检查环境变量。",
 "http_status": 401, "model": "deepseek-flash"}
```

### 9.3 思考内容处理

DeepSeek v4 系列响应包含 `message.reasoning_content` 字段。本项目仅记录
其长度，不回传：回传将持续占用上下文，而该内容对结构化输出无贡献。

### 9.4 错误分类

| 状态码 | `hint` |
|---|---|
| 401 | 检查 API key 与环境变量 |
| 402 | 余额不足 |
| 429 | 触发限流，降低频率或稍后重试 |
| 5xx | 服务端错误，已重试仍失败 |
| 网络错误 | 检查网络与 `base_url` 配置 |

429 在本模块属可重试类别，与 `bili.py` 中的处理相反。同一状态码在
不同服务语义下处理方式不同。

### 9.5 密钥处理

- 密钥仅从配置文件与环境变量读取，模块内无默认值
- `redact()` 在返回错误前移除文本中的密钥，有单元测试覆盖
- 客户端不写入任何文件

### 9.6 对照实现

`chat_with_tools()` 实现基于 DeepSeek 原生 `tools` / `tool_calls` 的等价流程。
本项目未采用该方案（将取代 `protocol.py` 的解析与校验逻辑，与手写协议的
目标冲突），保留其作为对照。

---

## 10. 配置

### 10.1 加载优先级

```
真实环境变量 > 项目 .env > ~/.biliagent.env
```

`load_env()` 在 `cli.main()` 起始处与 `llm.py` 模块级各调用一次，后者保证
以库形式使用时可读取 `.env`。调用幂等。

### 10.2 受管键

仅以下键会从配置文件载入环境：`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、
`DEEPSEEK_BASE_URL`、`BILIAGENT_FFMPEG`、`BILIAGENT_FFPROBE`、
`BILI_SESSDATA`、`KEEP_INTERMEDIATE_MP4`。

### 10.3 键值解析

支持 `KEY=value`、引号包裹、`export` 前缀、`#` 注释。行内注释仅在
`#` 前有空格时生效，避免破坏含片段标识符的 URL。

### 10.4 脱敏

`mask()` 输出形如 `sk-49cb…84af`，保留前 7 与后 4 字符。`describe()`
用于 `cfg` 命令展示，不输出完整密钥。

### 10.5 安全约定

- `.env` 已在 `.gitignore` 中排除；`.env.example` 仅含占位符，可提交
- 建议权限 600
- 密钥轮换后仅需修改 `.env`

---

## 11. 合规边界

以下约束在代码层实现，不依赖模型判断：

| 编号 | 约束 | 实现位置 |
|---|---|---|
| C1 | 不登录、不使用他人凭据 | `BiliSession.__init__` 的 `sessdata` 默认为 `None` |
| C2 | 请求间隔 ≥ 1 s | `BiliSession._throttle()` |
| C3 | 失败重试 ≤ 2 次且指数退避 | `BiliSession._request()` |
| C4 | 遇 403/412/429 立即停止，不重试 | `FATAL_HTTP` 集合 |
| C5 | 产出文件不出本地 | `.gitignore` 排除 `downloads/`、`logs/`、`.tools/`、`.env` |

ffmpeg 为本地进程，不产生网络请求，不受 C2 约束。

---

## 12. 测试

### 12.1 结构

| 文件 | 项数 | 覆盖范围 |
|---|---:|---|
| `tests/test_env.py` | 55 | 配置解析、优先级、脱敏、布尔开关 |
| `tests/test_tools.py` | 35 | B 站接口、外部工具封装、媒体管线 |
| `tests/test_loop.py` | 116 | 协议解析、指令校验、循环控制、上下文压缩、端到端 |
| `tests/test_llm.py` | 39 | 适配器错误分支（mock）、真实 API 冒烟 |

真实 API 调用由 `--real` 参数控制，默认不执行，避免常规回归产生费用。

### 12.2 测试策略

- **循环逻辑与模型不确定性解耦**：`ScriptedChat` 提供预设输出，
  失败时可先运行该部分以确定问题来源
- **错误分支使用注入式 mock**：401/402/429/5xx/超时/非 JSON 响应
  等路径无法通过反复调用真实 API 获得
- **依赖缺失时记为跳过而非失败**：音视频管线在无 ffmpeg 或网络时降级
- **测试 fixture 自建**：媒体管线测试自行合成测试视频，不依赖目录中
  既有的文件，避免结果随运行顺序变化

### 12.3 运行

```bash
bash scripts/test_all.sh              # 全部（含真实 API）
bash scripts/test_all.sh --no-real    # 离线执行
```

---

## 13. 已知限制

| 项 | 说明 |
|---|---|
| 音质 | 游客身份下 DASH 音频轨上限 187 kbps；当前实现使用 html5 变体（360P 视频所含音轨） |
| 画质 | 上限 360P |
| 契约有效期 | `playurl` 返回的签名 URL 有时效；本项目持久化 `bvid`/`cid` 而非 URL |
| 频率边界 | 未测试风控的触发阈值，仅验证十余次请求的量级 |
| 长会话 | 稳定性验证至 6 轮 |
| 流式输出 | 未实现，模型输出完整后才处理 |
| 并发 | 单会话串行执行，未实现多任务并行 |

---

## 14. 附录

### 14.1 变更记录

| 版本 | 日期 | 主要变更 |
|---|---|---|
| 1.0 | 2026-10-15 | 当前版本；整合此前 `DESIGN_v0.1`~`v0.3` 内容 |

版本演进期间的接口事实修正见 `FACTS.md`；各版本原始设计稿保留于
`archive/`。

### 14.2 相关文档

| 文档 | 内容 |
|---|---|
| `USAGE.md` | 安装、配置与使用 |
| `FACTS.md` | 外部接口的实测结论与版本敏感性 |
| `WALKTHROUGH.md` | 逐模块代码说明与函数参考 |
| `WORKFLOW.md` | 完整执行实例（含消息级记录与异常处理） |
| `ARCHITECTURE.md` | 分层结构说明 |
