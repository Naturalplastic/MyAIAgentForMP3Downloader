# 执行实例

- 文档版本：1.0
- 数据来源：`logs/cli_20261015.jsonl`（真实运行记录，未做删改）
- 目的：以一次完整执行为例，说明各层之间的实际数据交换形式

---

## 1. 实例概况

| 项 | 值 |
|---|---|
| 用户输入 | `我想要泠鸢的泼墨漓江，要 mp3 音频` |
| 结束原因 | `finished` |
| 总轮数 | 5（含 1 轮格式纠正） |
| 消息总数 | 11 |
| 模型 | `deepseek-flash` |
| 模型调用次数 | 5 |
| token 用量 | 输入 5,533 / 输出 268（其中思考 87） |
| HTTP 请求 | 5 次（首页 bootstrap、检索、视频详情、取流、CDN 下载） |
| 外部进程 | ffmpeg 2 次（转码、产物校验） |
| 格式纠正 | 1 次 |
| 上下文压缩 | 未触发（峰值 1,361 token，预算 20,000） |
| 产物 | mp4 15.52 MB（已清理）、mp3 5.82 MB |

该实例同时包含正常路径与一次真实的格式纠正，可作为循环行为的完整样本。

---

## 2. 执行流程

```
                        ┌──────────────────────────────────┐
开始 ──────────────────►│ messages 数组（模型的全部输入）    │
                        └──────────────────────────────────┘
                                        │
    ┌───────────────────────────────────▼──────────────────────────────────┐
    │ 第 1 轮                                                              │
    │   m[0] system  +  m[1] user（原始输入）                              │
    │   → 模型输出 search                                                 │
    │   → 校验通过 → 执行 bili.search_videos()                            │
    │   → HTTP: GET 首页(bootstrap) + GET search/type                     │
    │   → 返回 20 条，清洗打分后取前 5 条                                   │
    │   → m[3] observation 回喂                                           │
    └───────────────────────────────────┬──────────────────────────────────┘
                                        │
    ┌───────────────────────────────────▼──────────────────────────────────┐
    │ 第 2 轮                                                              │
    │   → 模型依据 author 与 play 选定 BV1Ds411h7T1                        │
    │   → 输出 download                                                   │
    │   → 执行 bili.download_html5_mp4()                                  │
    │       ① 获取 cid 与标题（GET view）                                  │
    │       ② 获取播放地址（GET playurl）                                  │
    │       ③ 检查磁盘空间                                                 │
    │       ④ 流式下载 15.52 MB，校验文件头                                │
    │   → m[5] observation 回喂                                           │
    └───────────────────────────────────┬──────────────────────────────────┘
                                        │
    ┌───────────────────────────────────▼──────────────────────────────────┐
    │ 第 3 轮（格式纠正）                                                   │
    │   → 模型输出 extract_audio，但 JSON 尾部含非法内容                    │
    │   → parse_response() 返回 bad_json                                  │
    │   → m[7] 回喂修正指示（原样引用 fix 字段）                            │
    │   → 本轮不计入工具调用                                               │
    └───────────────────────────────────┬──────────────────────────────────┘
                                        │
    ┌───────────────────────────────────▼──────────────────────────────────┐
    │ 第 4 轮                                                              │
    │   → 模型重新输出 extract_audio，格式合法                              │
    │   → 执行 media.extract_audio()                                      │
    │       ① 校验 mode                                                   │
    │       ② 探测能力（ffmpeg 存在且支持 mp3）                             │
    │       ③ 探测输入（含音频轨，时长 306.18 s）                           │
    │       ④ 转码至 .part 文件                                            │
    │       ⑤ ffprobe 校验产物                                             │
    │       ⑥ 原子改名                                                     │
    │   → m[9] observation 回喂                                           │
    └───────────────────────────────────┬──────────────────────────────────┘
                                        │
    ┌───────────────────────────────────▼──────────────────────────────────┐
    │ 第 5 轮                                                              │
    │   → 模型输出 <final_answer>                                          │
    │   → 循环终止（outcome = finished）                                   │
    └───────────────────────────────────┬──────────────────────────────────┘
                                        ▼
                                    结束
```

---

## 3. 消息级记录

以下为实际发送给模型与由模型返回的完整内容。行号 `m[n]` 表示
`AgentState.messages` 中的下标。

### 3.1 初始化（循环开始前）

由 `loop.AgentLoop.run()` 写入两条初始消息。

**`m[0]` — role: `system`（956 字符）**

该内容由 `protocol.render_system_prompt()` 从 `TOOL_SPECS` 与
`probe_capabilities()` 的结果生成，非人工编写。

```
你是一个 B 站媒体下载助手。你通过调用工具完成任务，不能直接访问网络。

## 输出格式（强制）
你每轮必须输出且只输出以下之一，用标记包裹，标记外不要有任何文字：

  <tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>
  <final_answer>任务完成，文件在 downloads/xxx.mp3</final_answer>

规则：
1. 每轮只能输出一个 <tool_call>，不能并列多个（后一个可能依赖前一个的结果）
2. 不要用 markdown 代码块包裹 JSON，不要加注释
3. 不确定用户意图时用 ask_user 提问，不要瞎猜
4. 搜索最多重试 3 次（换关键词），之后应 ask_user
5. 若观察里出现 warning 或 error 字段，先读懂它的 hint 再决定下一步
6. 任务完成后必须用 finish 或 <final_answer> 汇报，不要静默结束

## 可用工具
  - ask_user: 向用户提问以澄清需求（当描述含糊、或需要确认选择时使用）
      参数: question(必填)
  - download: 下载指定 bvid 的视频为 mp4（游客画质 360P）
      参数: bvid(必填)
  - extract_audio: 从已下载的 mp4 中抽取音频：mp3=转码(约168kbps)，copy=无损复制(.m4a)
      参数: mode(必填, 取 ['mp3', 'copy'])
  - finish: 任务完成，向用户汇报结果
      参数: summary(必填)
  - get_info: 查看指定 bvid 的视频详情（标题、UP主、时长、分P）
      参数: bvid(必填)
  - search: 按关键词搜索 B 站视频，返回候选列表（含 bvid/标题/UP主/播放量/相关性）
      参数: keyword(必填)，limit(可选, 取 [1, 2, 3, 4, 5])

## 本机能力
  - ffmpeg 可用（v6.0-static）：可以产出 mp3
```

**`m[1]` — role: `user`（19 字符）**

原始用户输入，未做任何预处理。

```
我想要泠鸢的泼墨漓江，要 mp3 音频
```

### 3.2 第 1 轮：检索

**`m[2]` — role: `assistant`（81 字符）**

```xml
<tool_call>{"action":"search","args":{"keyword":"泠鸢 泼墨漓江","limit":5}}</tool_call>
```

模型自行决定检索词为 `泠鸢 泼墨漓江`（以空格分隔），并指定 `limit` 为 5。

处理过程：

| 步骤 | 函数 | 结果 |
|---|---|---|
| 解析 | `protocol.parse_response()` | `{"status": "tool_call", "directive": {...}}` |
| 校验 | `protocol.validate_directive()` | `{"ok": true, "action": "search", "args": {...}}` |
| 路由 | `tools.ToolBox.dispatch()` | 转发至 `_search()` |
| 执行 | `bili.search_videos()` | 见下 |

发起两次 HTTP 请求：首页（获取 cookie）与检索接口。
接口返回 20 条，清洗打分后按 `(relevance, play)` 降序取前 5 条。

**`m[3]` — role: `user`（1070 字符）**

由 `tools.ToolBox.make_observation()` 压缩后的观察结果。

```xml
<observation>{"ok": true, "keyword": "泠鸢 泼墨漓江", "total": 1000, "matched": 5, "candidates": [{"bvid": "BV1Ds411h7T1", "title": "【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】", "author": "泠鸢yousa", "duration": "5:6", "play": 1710053, "relevance": 1.0, "url": "https://www.bilibili.com/video/BV1Ds411h7T1"}, {"bvid": "BV1Ss411h7ZP", "title": "【南北組】泼墨漓江【玖玲p】", "author": "玖玲p", "duration": "4:55", "play": 392327, "relevance": 1.0, "url": "https://www.bilibili.com/video/BV1Ss411h7ZP"}, {"bvid": "BV14t4y1x7du", "title": "【洛天依AI乐正绫V5】泼墨漓江【AI修复画质】【南北组COVER】", "author": "花间有鹿来", "duration": "5:2", "play": 97119, "relevance": 1.0, "url": "https://www.bilibili.com/video/BV14t4y1x7du"}, {"bvid": "BV1Yx411S7HV", "title": "【粤语翻唱】泼墨漓江【雪霏岚岚】", "author": "雪霏岚岚", "duration": "5:6", "play": 57477, "relevance": 1.0, "url": "https://www.bilibili.com/video/BV1Yx411S7HV"}, {"bvid": "BV1Pa4y1g7HA", "title": "【钢琴改编】《泼墨漓江》- 泠鸢yousa|2023泠鸢生贺", "author": "是小音符Sxyfff", "duration": "5:4", "play": 18657, "relevance": 1.0, "url": "https://www.bilibili.com/video/BV1Pa4y1g7HA"}], "_action": "search"}</observation>
```

模型可从该观察中提取的判别依据：

| # | `author` | `play` | 标题特征 | 判断 |
|---|---|---:|---|---|
| 1 | 泠鸢yousa | 1,710,053 | 标注"原创" | 目标（UP 主与关键词一致，播放量最高） |
| 2 | 玖玲p | 392,327 | 南北組 | 非目标 UP 主 |
| 3 | 花间有鹿来 | 97,119 | AI 修复画质 | 搬运版本 |
| 4 | 雪霏岚岚 | 57,477 | 粤语翻唱 | 翻唱版本 |
| 5 | 是小音符Sxyfff | 18,657 | 钢琴改编 | 改编版本 |

### 3.3 第 2 轮：下载

**`m[4]` — role: `assistant`（75 字符）**

```xml
<tool_call>{"action":"download","args":{"bvid":"BV1Ds411h7T1"}}</tool_call>
```

处理过程（`bili.download_html5_mp4()`）：

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1 | 获取 `cid` 与标题（`GET view`） | `cid=1261421445` |
| 2 | 获取播放地址（`GET playurl?platform=html5`） | 单一 mp4 直链 |
| 3 | `os.statvfs()` 磁盘空间检查 | 通过 |
| 4 | 流式下载（1 MiB 分块） | 16,276,355 字节 |
| 5 | 首 64 字节含 `ftyp` 校验 | 通过 |

**`m[5]` — role: `user`（207 字符）**

```xml
<observation>{"ok": true, "path": "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp4", "size_mb": 15.52, "quality": 16, "complete": true, "title": "【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】", "_action": "download"}</observation>
```

### 3.4 第 3 轮：格式纠正

**`m[6]` — role: `assistant`（93 字符）**

模型输出的 `<tool_call>` 内 JSON 之后附带了额外内容，导致解析失败。
原始输出如下：

```xml
<tool_call>{"action":"extract_audio","args":{"mode":"mp3"}}</｜｜DSML｜｜ parameter>
</tool_call>
```

`protocol.parse_response()` 判定：

```python
{"status": "bad_json",
 "bad_body": '{"action":"extract_audio","args":{"mode":"mp3"}}</｜｜DSML｜｜ parameter>',
 "fix": "<tool_call> 内的内容不是合法 JSON（Extra data）。必须严格形如 "
        "<tool_call>{\"action\":\"...\",\"args\":{...}}</tool_call>，"
        "不要用 markdown 代码块包裹，不要加注释或额外文字。"}
```

由于 `status` 不属于 `OK_TOOL_CALL` 或 `OK_FINAL`，
`AgentLoop.run()` 执行格式违规分支：

```python
state.format_fixes += 1        # 连续计数 0 → 1
state.total_format_fixes += 1  # 累计计数 0 → 1
state.messages.append({"role": "user",
                       "content": "<system>格式错误：%s</system>" % parsed["fix"]})
state.trace.append({"round": 3, "type": "format_violation", "status": "bad_json"})
continue
```

本轮未执行任何工具调用，也未产生产物。

**`m[7]` — role: `user`（150 字符）**

```xml
<system>格式错误：<tool_call> 内的内容不是合法 JSON（Extra data）。必须严格形如 <tool_call>{"action":"...","args":{...}}</tool_call>，不要用 markdown 代码块包裹，不要加注释或额外文字。</system>
```

### 3.5 第 4 轮：音频抽取

**`m[8]` — role: `assistant`（71 字符）**

```xml
<tool_call>{"action":"extract_audio","args":{"mode":"mp3"}}</tool_call>
```

模型修正后重新输出，格式合法。注意该指令未包含文件路径参数 ——
`ToolBox.mp4_path` 保存了上一轮下载的结果，`_extract_audio()` 直接引用该值。

处理过程（`media.extract_audio()`）：

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1 | 校验 `mode` | 合法 |
| 2 | `probe_capabilities()` | ffmpeg 6.0-static，含 libmp3lame |
| 3 | `probe_media()` | 含音频轨，时长 306.18 s |
| 4 | 执行 ffmpeg，输出至 `.part` | 退出码 0 |
| 5 | `_verify_output()` | 通过（时长差 0.03 s） |
| 6 | `os.replace()` | 改名完成 |

实际执行的命令：

```
.tools/ffmpeg -hide_banner -loglevel error -nostdin -y
  -i "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp4"
  -vn -map 0:a:0 -c:a libmp3lame -q:a 2
  -f mp3 "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3.part"
```

**`m[9]` — role: `user`（209 字符）**

```xml
<observation>{"ok": true, "path": "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3", "codec": "libmp3lame", "size_mb": 5.82, "duration": 306.181224, "verified": true, "_action": "extract_audio"}</observation>
```

> 注：该实例运行于中间产物清理功能实现之前，因此观察中不含
> `note` 字段。当前版本在此处会产生清理记录，见 `DESIGN.md` §4.4。

### 3.6 第 5 轮：终止

**`m[10]` — role: `assistant`（119 字符）**

```xml
<final_answer>任务完成，文件在 downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3（泠鸢yousa 原唱，时长 5:06，mp3 约 5.82MB）</final_answer>
```

`parse_response()` 返回 `{"status": "final_answer", "text": "..."}`，
`AgentLoop.run()` 终止循环：

```python
if parsed["status"] == P.OK_FINAL:
    state.report = parsed["text"]
    state.outcome = "finished"
    state.trace.append({"round": 5, "type": "final_answer", "text": parsed["text"]})
    break
```

---

## 4. 函数调用序列

按执行顺序列出实际调用的函数及其位置。

```
cli.cmd_agent()                                    cli.py:233
  └─ loop.AgentLoop.run()                          loop.py:108
      ├─ protocol.render_system_prompt()           protocol.py:238
      │
      ├─ [第 1 轮] llm.DeepSeekClient.chat()       llm.py:134
      │   ├─ protocol.parse_response()             protocol.py:54
      │   ├─ protocol.validate_directive()         protocol.py:175
      │   ├─ tools.ToolBox.dispatch()              tools.py:162
      │   │   └─ tools.ToolBox._search()           tools.py:68
      │   │       └─ bili.search_videos()          bili.py:281
      │   │           ├─ bili.BiliSession.get_json()    bili.py:162
      │   │           │   ├─ bili.BiliSession.bootstrap()  bili.py:113
      │   │           │   └─ bili.BiliSession._request()   bili.py:121
      │   │           ├─ bili._clean_candidate()   bili.py:249
      │   │           └─ bili._err()               bili.py:65
      │   └─ tools.ToolBox.make_observation()      tools.py:188
      │
      ├─ [第 2 轮] llm.DeepSeekClient.chat()
      │   ├─ protocol.parse_response() / validate_directive()
      │   ├─ tools.ToolBox.dispatch()
      │   │   └─ tools.ToolBox._download()         tools.py:98
      │   │       └─ bili.download_html5_mp4()     bili.py:469
      │   │           ├─ bili.get_video_info()     bili.py:371
      │   │           ├─ bili._playurl_html5()     bili.py:418
      │   │           ├─ bili._safe_filename()     bili.py:459
      │   │           └─ bili.BiliSession.open_stream()  bili.py:187
      │   └─ tools.ToolBox.make_observation()
      │
      ├─ [第 3 轮] llm.DeepSeekClient.chat()
      │   └─ protocol.parse_response()  → bad_json
      │       （回喂 fix，本轮无工具调用）
      │
      ├─ [第 4 轮] llm.DeepSeekClient.chat()
      │   ├─ protocol.parse_response() / validate_directive()
      │   ├─ tools.ToolBox.dispatch()
      │   │   └─ tools.ToolBox._extract_audio()    tools.py:106
      │   │       └─ media.extract_audio()         media.py:308
      │   │           ├─ media.probe_capabilities()     media.py:101
      │   │           ├─ media.probe_media()            media.py:175
      │   │           ├─ media._run()                   media.py:84
      │   │           ├─ media.classify_ffmpeg_error()  media.py:264
      │   │           └─ media._verify_output()         media.py:421
      │   └─ tools.ToolBox.make_observation()
      │
      └─ [第 5 轮] llm.DeepSeekClient.chat()
          └─ protocol.parse_response()  → final_answer → break

cli._log()                                         cli.py:40
```

---

## 5. 状态记录

运行结束后由 `AgentState.to_dict()` 生成并写入 `logs/cli_*.jsonl`。

```json
{
  "user_text": "我想要泠鸢的泼墨漓江，要 mp3 音频",
  "round": 5,
  "format_fixes": 0,
  "total_format_fixes": 1,
  "compressions": 0,
  "outcome": "finished",
  "report": "任务完成，文件在 downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3（泠鸢yousa 原唱，时长 5:06，mp3 约 5.82MB）",
  "mp4_path": "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp4",
  "audio_path": "downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3",
  "elapsed": 12.93
}
```

字段说明：

| 字段 | 值 | 说明 |
|---|---|---|
| `format_fixes` | 0 | 连续格式违规计数。第 4 轮成功后归零 |
| `total_format_fixes` | 1 | 累计计数，不重置，用于诊断 |
| `compressions` | 0 | 未触发上下文压缩 |

`trace` 数组记录每轮的类别与数据：

```json
[
  {"round": 1, "type": "tool_result", "action": "search",
   "args": {"keyword": "泠鸢 泼墨漓江", "limit": 5}, "observation": {...}},
  {"round": 2, "type": "tool_result", "action": "download",
   "args": {"bvid": "BV1Ds411h7T1"}, "observation": {...}},
  {"round": 3, "type": "format_violation", "status": "bad_json"},
  {"round": 4, "type": "tool_result", "action": "extract_audio",
   "args": {"mode": "mp3"}, "observation": {...}},
  {"round": 5, "type": "final_answer", "text": "..."}
]
```

---

## 6. 数据流小结

### 6.1 消息数组的增长

| 轮次 | 追加的消息 | 数组长度 |
|---:|---|---:|
| 初始化 | `m[0]` system、`m[1]` user | 2 |
| 1 | `m[2]` assistant、`m[3]` observation | 4 |
| 2 | `m[4]` assistant、`m[5]` observation | 6 |
| 3 | `m[6]` assistant、`m[7]` 格式纠正 | 8 |
| 4 | `m[8]` assistant、`m[9]` observation | 10 |
| 5 | `m[10]` assistant | 11 |

每轮的模型调用均发送当前完整数组。第 5 轮的输入为前 10 条消息之和，
因此输入 token 累计量（5,533）显著高于输出（268）。

### 6.2 职责分布

| 层次 | 本实例中的行为 |
|---|---|
| L1 会话层 | 5 次 HTTP 请求的限流、cookie 复用、错误分级（对上层透明） |
| L2 工具层 | 结果清洗与打分、磁盘检查、文件头校验、ffmpeg 调用与产物校验 |
| L3 循环层 | 标记解析、指令校验、格式纠正、重复动作检测、观察压缩 |
| L4 入口层 | 参数解析、过程输出、日志落盘 |

### 6.3 模型与代码的职责边界

| 决策 | 由谁决定 | 依据 |
|---|---|---|
| 检索关键词为 `泠鸢 泼墨漓江` | 模型 | 语义理解 |
| 选取 `BV1Ds411h7T1` | 模型 | 观察中的 `author` 与 `play` |
| 音频格式为 `mp3` | 模型 | 用户输入中的"要 mp3 音频" |
| 请求间隔 1 秒 | 代码 | `BiliSession._throttle()` |
| 403/412/429 不重试 | 代码 | `FATAL_HTTP` |
| 写入前检查磁盘空间 | 代码 | `download_html5_mp4()` |
| 产物需通过 ffprobe 校验 | 代码 | `_verify_output()` |
| 格式违规重试上限 3 次 | 代码 | `config.GUARDRAILS` |

---

## 7. 分支处理

上述实例走的是正常路径（含一次格式纠正）。其余分支的处理方式如下。

### 7.1 需求不明确

输入 `我想要泠鸢的泼墨漓江`（未指定格式）时的实际输出：

```
第 1 轮：search → 命中 BV1Ds411h7T1
第 2 轮：<tool_call>{"action":"ask_user","args":{"question":
        "找到泠鸢yousa原版《泼墨漓江》(BV1Ds411h7T1)。你想要哪种格式？
         1) 原始 MP4 视频  2) MP3 音频（转码约168kbps）  3) 无损音频 .m4a"}}</tool_call>

结束原因: asked ｜ 2 轮
```

循环以 `outcome = asked` 终止，`report` 字段为模型提出的问题。
此分支不视为失败：模型将不确定的部分交由用户决定，而非自行假设。
若需求中已指定格式，则直接进入下载流程。

### 7.2 检索结果全部不相关

当返回条目全部 `relevance == 0` 时，`search_videos()` 仍返回成功状态，
但附加警告字段：

```json
{
  "ok": true,
  "matched": 0,
  "candidates": [{"title": "无关视频", "relevance": 0.0}],
  "warning": "no_relevant_result",
  "hint": "本次返回的条目均未包含关键词「...」，B 站对无匹配关键词也会返回满页模糊结果。请勿在其中选择，应换关键词重搜或向用户澄清。"
}
```

系统提示词第 5 条要求模型优先读取 `warning` / `error` 字段的 `hint`。
模型的预期行为为更换关键词（上限 3 次）或调用 `ask_user`。

若缺少该字段，模型可能在无关结果中选定一条并报告成功。

### 7.3 下载失败

| 失败类型 | 错误码 | `hint` 要点 |
|---|---|---|
| 签名 URL 过期 | `risk_control` | 已停止，不重试；可重新取流 |
| 磁盘空间不足 | `insufficient_space` | 需要与可用的具体数值 |
| 视频已失效 | `api_error`（`api_code=-404`） | 视频不存在或已失效 |
| CDN 返回非 mp4 | `not_mp4` | 附带响应首 32 字节的十六进制 |

上述错误均为不可重试类别，由工具层直接返回，模型据此选择其他候选或如实汇报。

### 7.4 外部工具不可用

ffmpeg 缺失时的影响面覆盖三层：

| 层次 | 机制 |
|---|---|
| 系统提示词 | 声明无法抽取音频，要求交付 mp4 或询问用户，并明确"不要反复尝试" |
| 工具守门 | `ToolBox._extract_audio()` 返回 `ffmpeg_missing`，附 `fallback_path` |
| 适配器 | `probe_capabilities()` 的 `errors` 字段说明安装方式 |

三层均不依赖模型自行发现能力边界。

### 7.5 模型持续格式违规

连续违规计数超过 `max_format_fixes`（默认 3）时终止：

```
outcome: format_stuck
report:  模型连续 3 次输出格式违规，已放弃。最后一次输出：<模型原始输出>
```

汇报中包含模型最后一次的原始输出，以便定位原因。

计数采用连续语义：任意成功动作后归零。若采用累计语义，
"偶发格式错误但整体推进"的执行会被误判为失败。

### 7.6 同一动作重复

两个独立机制：

| 机制 | 触发条件 | 行为 |
|---|---|---|
| 硬护栏 | 同一 `(action, args)` 连续 3 次 | 终止，`outcome = stuck` |
| 软提示 | 同一 `(action, args)` 此前已执行过 | 观察中追加 `_note` 字段，仍允许执行 |

软提示不阻止执行的原因：重复调用存在合法场景（如重新获取过期的签名 URL）。
该机制将判断依据提供给模型，而非代替模型决定。

实测中曾观察到同一检索被重复执行一次（第 1 轮与第 4 轮），
产生额外的 12.5 秒与 1,051 token 开销。软提示即为此类情形设计。

### 7.7 达到轮数上限

```
outcome: max_rounds
report:  达到最大轮数 10，任务未完成。最后一次工具结果：{...}
```

汇报中包含最后一轮的工具结果，以便判断阻塞位置。

### 7.8 上下文超预算

`ContextPolicy.maybe_compress()` 在每次模型调用前执行。
超过预算时压缩早期轮次为摘要，保留 system 提示词、原始用户任务
与最近 3 轮完整内容。压缩发生时会写入 `context_compressed` 日志事件，
并递增 `state.compressions`。

实测在将预算压低至 600 token 的条件下，压缩触发 3 次，
任务仍正常完成（6 轮，39.9 秒）。
