# B 站媒体下载 Agent —— 设计方案 v0.3

> 日期：2026-10-14 ｜ 状态：**端到端可用，161 项测试全绿**（D6 护栏补齐，含上下文压缩）
> 性质：个人学习项目。练习"纯文本 LLM + 自建工具 + 调用外部工具 + 手写 agent loop"。
> 前身：v0.1（同日，原设计稿）→ v0.2（事实修正）→ **v0.3（本文，ffmpeg 升级为一等公民）**
>
> 图例：✅ 已完成并验收 ｜ ⬜ 未开始 ｜ ⚠️ 修正早期判断 ｜ ➕ v0.3 新增
>
> **进度更新（D6 完成后补记）**：真 DeepSeek 已接入并跑通端到端；
> 上下文压缩与护栏集中化已完成。一句话自然语言 → 自主搜索 → 挑选 → 下载 →
> 转 mp3 → 汇报，4 轮完成、0 格式错误。
> 开发中由测试抓出的 6 个真实缺陷记于 §12、§14。
> **另有面向"读懂项目"的大白话说明：`docs/EXPLAINED.md`。**

---

## 0. v0.2 → v0.3 变更：为什么 ffmpeg 值得单独一版

**触发**：「ffmpeg 的调用也是 agent 实践的一环。」

这个判断是对的，而且它把 ffmpeg 从「实现细节」提升为「**agent 必须感知和驾驭的外部工具**」，
由此暴露出一整类此前被忽略的工程问题——**这些问题在真实 agent 里普遍存在，
但用框架时会被完全隐藏**：

| 问题 | 天真做法 | v0.3 做法 |
|---|---|---|
| 工具不存在怎么办 | `subprocess.run(["ffmpeg"...])` → `FileNotFoundError` 崩溃 | **能力探测** `probe_capabilities()`，agent 据此决定能不能做 mp3 |
| 外部工具失败原因 | 把整个 stderr 倒给模型（几百行噪声） | **stderr 分类器**：7 类模式 → `error` + `hint` + 精炼 `stderr_tail` |
| 产物是否真的有效 | 只看 ffmpeg 返回码 0 | **ffprobe 校验** codec + 时长一致性 |
| 中途失败 | 留下半个坏文件，agent 以为成功 | **原子写入** `.part` → 校验 → `os.replace` |
| 输入根本没有音频轨 | 让 ffmpeg 报错，模型猜 | **先 ffprobe 探测** → `no_audio_stream` + 明确 hint |

**这正是"调用外部工具"作为 agent 实践的核心价值**：外部工具是不可靠的、
有环境依赖的、会给出晦涩错误的。harness 的职责就是**把不可靠的外部世界翻译成
模型能据以自救的结构化信号**。

---

## 1. 一句话概述

用户用自然语言描述想要的视频/音频（如"周董的稻香 MV"），agent 自主完成：
**理解需求 → 搜索 B 站 → 挑选结果 → 下载 mp4 → 调用 ffmpeg 抽取/转码音频 → 汇报结果**。

核心学习点：模型是纯粹的文本进/文本出（DeepSeek API），
所有工具调用协议、解析、循环控制、**外部工具驾驭**都由自己实现的 harness 完成。

**明确不使用任何 agent 框架**（OpenAI Agents SDK / LangGraph / CrewAI 等），
也**无法**使用第三方 B 站库（已全部归档清空，见 §4）。
harness 的每一层都必须亲手实现 —— 这是本项目最大的学习价值。

---

## 2. 需求与场景

- **输入**：自然语言描述（歌名、歌手、视频主题，可能含糊）
- **输出**：本地文件（mp4 / m4a / **mp3**）+ 一份文字汇报
- **典型流程**：用户一句话 → agent 多轮循环 → 落盘文件
- **非目标**：
  - 不登录、不模拟登录、不碰大会员/付费内容
  - 不做批量爬虫、不做订阅整个频道
  - 不分发工具、不商用
  - 不绕过 DRM / 反爬（遇到风控错误就停止并如实汇报）
  - ⚠️ **v0.3 调整**：v0.2 曾把"不追求转码完美（可接受 .m4a 降级）"列为非目标；
    现在 **mp3 是正式交付物**，ffmpeg 调用是主线而非降级路径

---

## 3. 总体架构

```
用户输入
   │
┌──▼──────────────────────────────────────────────────────────────┐
│  harness（自己实现）                                  ⬜ 未实现    │
│                                                                   │
│   ┌─────────────┐   调用协议    ┌────────────────────────┐       │
│   │  LLM        │◄────────────►│  agent loop            │       │
│   │ (DeepSeek)  │  标记包裹指令  │  解析/校验/路由/回喂    │       │
│   └─────────────┘              └───┬───────────┬────────┘       │
│                                     │           │                │
│                          ┌──────────▼──┐   ┌───▼──────────┐    │
│                          │  B 站工具    │   │  媒体工具      │    │
│                          │ search/info │   │ probe/extract │    │
│                          │ /download   │   │ /transcode    │    │
│                          └─────────────┘   └───┬──────────┘    │
│                          ✅ bili.py            ✅ media.py       │
└─────────────────────────────────────────────────┼───────────────┘
                                                   │ subprocess
                                        ┌──────────▼───────────┐
                                        │  ffmpeg / ffprobe     │
                                        │  （外部，可缺失/可失败）│
                                        └──────────────────────┘
```

**v0.3 架构要点**：`media.py` 与 `bili.py` **平级**，不是下载模块的内部细节。
理由：外部工具是一个独立的、有自己失败模式的依赖，必须能被单独探测、单独测试。

设计原则：
- LLM 只做**判断**；代码做**执行**和**护栏**
- 循环上限、写操作确认、失败兜底都留在 harness（AI 是参谋，你是司令）
- ➕ **不可靠性必须被翻译，而不是被传播**：外部工具的错误要变成结构化信号

---

## 4. 外部接口事实（✅ 已实测核验）

> 完整证据见 `docs/FACTS.md`。核验方法：本机直连（出口 IP 北京联通 123.118.219.39）、
> 纯游客身份、14 组对照实验。

| 环节 | 接口 | ✅ 游客实测结论 | 状态 |
|---|---|---|---|
| 会话 | `GET https://www.bilibili.com/` | 下发 `b_nut` / `buvid3` | ✅ 必须做 |
| 搜索 | `x/web-interface/search/type?search_type=video` | `code=0`，20 条/页，扁平数组，含 `favorites` | ✅ 首选 |
| ~~搜索~~ | ~~`search/all/v2`~~ | ⚠️ 12 分区块嵌套，无 `favorites` | ❌ 弃用 |
| ~~搜索~~ | ~~`wbi/search/type`~~ | ⚠️ 无签名时 `code=0` 但 `result=[]` **静默失败** | ❌ 禁用 |
| 视频信息 | `x/web-interface/view?bvid=` | `code=0`，返回 `cid`/`aid`/`title`/`pages` | ✅ |
| 播放地址 | `x/player/playurl?platform=html5&high_quality=1` | **免 WBI 签名**，返回**单一完整 mp4**（360P） | ✅ 主线 |
| 播放地址 | `x/player/playurl?fnval=16`（DASH） | **免 WBI 签名**，3 条音轨 41/99/187 kbps | ✅ 进阶 |
| CDN 防盗链 | DASH `.m4s` | 带 Referer ✅ 206 ／ 裸请求 ❌ 403 | ⚠️ 仅 DASH |
| CDN 防盗链 | html5 `.mp4` | 带/不带 Referer **都是 206** | ⚠️ 不强制 |
| 错误语义 | 所有接口 | ⚠️ **HTTP 200 + `application/json`**，错误只在 `data.code` | ✅ 必须查 code |

### ffmpeg 侧实测事实（➕ v0.3）

| 项 | 实测结论 |
|---|---|
| 安装方式 | ⚠️ `sudo apt-get` **被容器 `no new privileges` 标志阻止**，提权也无解 |
| 可用方案 | ✅ **便携静态构建**：npmmirror 的 `ffmpeg-static/b6.0/ffmpeg-linux-x64`（75MB，20.7 MB/s，无需 root） |
| 版本 | `ffmpeg 6.0-static`，含 `libmp3lame` / `aac` / `mp3` muxer / `mov,mp4,m4a` demuxer |
| ffprobe | 同源独立文件 `ffprobe-linux-x64.gz`，可单独下载 |
| 转码实测 | mp4(22.92MB, 8:20) → mp3 **10.04MB / 168kbps VBR / 44.1kHz 立体声 / 4.4s** |
| 关键教训 | 解析 `-encoders` / `-muxers` 输出时，**头部行**（`' D. = Demuxing supported'`）会被误当条目，正则必须排除含 `=` 的段 |

### 生态现状（✅ 已核验）

| 项目 | 归档 | 最后 push | 仓库现状 |
|---|---|---|---|
| `SocialSisterYi/bilibili-API-collect` | ✅ archived | 2026-01-30 | 顶层仅剩 `README.md` + `reason.jpg` |
| `Nemo2011/bilibili-api` | ✅ archived | 2026-07-06 | 顶层仅剩 `README.md` |

→ **第三方库路线已关闭**。手写工具层不是"学习偏好"，而是**唯一可行路线**。

---

## 5. 工具清单（含实测契约）

### 5.1 B 站工具 ✅ `biliagent/bili.py`

```python
search_videos(session, keyword, page=1, limit=5)
    -> {ok, keyword, total, items:[Candidate], returned, matched, warning?, hint?}
    ✅ 清洗：剥 <em>、反转义 HTML 实体、过滤无 bvid 的广告位
    ✅ 相关性守门（头号风险 R1）
    ✅ 排序键 (relevance, play) 降序

Candidate = {bvid, title, author, duration, play, favorites, pubdate, url, relevance}

get_video_info(session, bvid) -> {ok, bvid, aid, title, owner, duration, cid, pages, url}
    ✅ 非法 bvid 格式本地拦截（bad_bvid），不打网络

download_html5_mp4(session, bvid, cid=None, out_dir, filename=None, progress=None)
    ✅ playurl?platform=html5 → 流式下载 1MiB/块
    ✅ 文件头 'ftyp' 校验（防下到错误页）
    ✅ 磁盘空间守门（insufficient_space）
    ✅ 失败/异常时删除半成品
    ✅ HTTP 403/412/429 → risk_control（不重试，如实汇报）
```

### 5.2 媒体工具 ✅ `biliagent/media.py`（➕ v0.3 新增，独立模块）

```python
probe_capabilities(refresh=False) -> {
    ok, ffmpeg:{path,version}, ffprobe:{path,version},
    encoders:{has_libmp3lame,has_native_mp3,has_aac},
    muxers:{mp3,mp4,ipod},
    can_encode_mp3, can_extract_audio, errors:[...]
}
    ✅ 能力探测。**agent 据此决定"我能做什么"**，而不是撞墙失败
    ✅ 查找顺序：环境变量 > 项目 .tools/ > PATH
    ✅ 结果缓存

probe_media(src) -> {ok, format, duration, size, bit_rate, streams, audio, video,
                     has_audio, has_video} | 结构化错误
    ✅ 用 ffprobe JSON 输出，不解析人类可读文本
    ✅ moov 缺失 → incomplete_mp4 + hint「应重新下载，而不是尝试修复」

classify_ffmpeg_error(stderr, returncode) -> {error, message, hint, stderr_tail, matched_rule}
    ✅ **核心**：把 ffmpeg 噪声压缩成模型可自救的结构化错误
    ✅ 覆盖 7 类：encoder_missing / decoder_missing / no_such_stream /
       no_output_stream / corrupt_input / incomplete_mp4 / permission_denied /
       no_space / ffmpeg_failed
    ✅ 未匹配时保留 stderr_tail —— 这是模型唯一能据以自救的线索

extract_audio(src, mode="mp3"|"copy", out_dir=None, out_name=None)
    -> {ok, path, size, mode, codec, duration, verified, elapsed, ffmpeg_version}
    ✅ mode="mp3"  → libmp3lame VBR -q:a 2（实测 ~168kbps）；缺 libmp3lame 自动退回内置 mp3
    ✅ mode="copy" → 无损复制音轨为 .m4a
    ✅ 原子写入：.part → ffprobe 校验 → os.replace
    ✅ 参数校验最先执行（曾因顺序错误被"无音频流"掩盖）
    ✅ 先探测输入，无音频轨直接给 no_audio_stream + hint
```

### 5.3 Agent loop ⬜ 未开始（核心学习目标）

harness 能力清单：
- 解析 LLM 输出为「工具调用」或「最终答复」—— **标记包裹，见 §6.1**
- 校验 action 与必填参数，非法输出 → 让 LLM 自我修正（≤2 次）
- 执行工具、结构化回喂、循环上限（≤10 轮，超出强制总结退出）
- **静默失败上报**：工具返回 `warning` / `empty_result` 时必须在 observation
  里显式标注，不能当正常成功喂给模型
- ➕ **能力感知**：启动时调用 `probe_capabilities()`，把结果注入 system prompt，
  让模型知道"本机能不能做 mp3"；不能做时应主动 ask_user 而非硬试
- 全程日志：每轮 thought / action / observation 落盘可回放
  （✅ CLI 侧机制已就绪，见 `logs/cli_*.jsonl`）

---

## 6. LLM 调用协议

### 6.1 标记包裹，不靠 `JSON.parse` 成败判断

v0.1 的规则「parse 失败 → 判为最终答复」有漏洞：模型输出**带解释的 JSON**
（`好的，我来搜索：\n{"action":...}`）时会被误判为答复直接返回给用户。

```
系统 prompt：
你每轮必须输出且只输出以下之一，用标记包裹：

<tool_call>{"action":"search","args":{"keyword":"稻香"}}</tool_call>
<final_answer>已下载完成，文件在 …</final_answer>
```

harness 解析：
```
1. 提取 <tool_call>…</tool_call> → 解析 JSON
     成功 → 校验 action/args → 执行
     失败 → 追加"格式错误"提示让模型重发（不计入答复）
2. 无 tool_call 但有 <final_answer>…</final_answer> → 结束
3. 两者皆无 → 格式违规，要求重发（≤2 次，超限报错退出）
```

### 6.2 action 集合

```
action ∈ {search, get_info, download, extract_audio, ask_user, finish}
```
- ➕ `extract_audio` 的 `args = {mode: "mp3"|"copy"}`：**让模型显式选择**，
  而不是 harness 猜。这本身就是一次有意义的 agent 决策
- ⚠️ **不新增 `retry` action** —— 重试归 harness（§6.3）

规则：
- 需要更多信息时 `ask_user`，不要瞎猜
- 搜索不满意可换关键词重搜，**最多 3 次**（`search_rounds` 强制）
- 不确定要视频还是音频时 `ask_user`
- **若 observation 含 `warning: no_relevant_result`，禁止在其中选择**
- **若 observation 含 `error: ffmpeg_missing`，不要重试，应 ask_user 或降级交付 mp4**
- 每次只输出一个 action，禁止并列

### 6.3 护栏职责边界（落实"AI 是参谋"）

| 事项 | 归谁 | 理由 |
|---|---|---|
| HTTP 重试（≤2 次 + 指数退避） | harness / 工具层 | 暴露给 LLM 会导致无限重试 |
| 请求节流（≥1s 间隔） | harness / 工具层 | 合规要求 |
| 磁盘空间检查 | 工具层 | 硬约束 |
| ffmpeg 缺失判定 | 工具层（能力探测） | 环境事实，非模型可决策 |
| **ffmpeg 失败后是否重试** | **harness**（默认不重试，除超时） | ffmpeg 失败通常是确定性的，重试无益 |
| 产物有效性校验 | 工具层（ffprobe） | 硬约束 |
| 循环总轮数上限 | harness | 防失控 |
| 换关键词重搜 | LLM（限 3 次） | 需要语义判断 |
| 候选中挑选 | LLM | 需要语义判断 |
| ➕ 音频格式选择（mp3/copy） | LLM（可先 ask_user） | 涉及质量/速度权衡 |
| 风控错误处理 | 工具层停止 + 汇报 | 合规红线 §8.5 |

---

## 7. 状态设计（State）

```python
@dataclass
class TaskState:
    user_text: str
    intent: str | None            # video | audio（ask_user 后确定）
    audio_mode: str | None        # ➕ mp3 | copy
    query: str | None
    candidates: list[dict]        # 结构见 §5.1 Candidate；≤5 条
    selected_bvid: str | None
    cid: int | None
    mp4_path: str | None          # ➕ 分离：下载产物
    audio_path: str | None        # ➕ 分离：转码产物（汇报时两者都要报）
    capabilities: dict | None     # ➕ 启动时探测结果，可回放
    search_rounds: int = 0        # 上限 3
    loop_rounds: int = 0          # 上限 10
    error: str | None
    report: str | None
```

**持久化规则（v0.2 引入，v0.3 保留）**：
```
✅ 可持久化 bvid / cid / title —— 永久有效，任务可恢复
❌ 绝不持久化 playurl 的 baseUrl —— 带 deadline 签名，会过期；
   过期后必然 403，而模型会误判为 CDN 问题并开始瞎重试
```

---

## 8. 合规边界（硬性，✅ 已在代码层落实）

1. 仅个人学习使用；工具不分发、不部署为公共服务、不商用
   → ✅ `.gitignore` 已排除 `downloads/`、`logs/`、`.tools/`
2. 只调用公开接口；不登录、不带他人 Cookie、不绕过会员鉴权
   → ✅ `BiliSession` 默认纯游客；`sessdata` 仅接受显式传入
3. 只下载：自己投稿的 / CC 或明确允许下载的 / 纯学习用途的自用内容
4. 请求频率：HTTP 间隔 ≥ 1 秒；失败重试 ≤ 2 次且指数退避
   → ✅ `MIN_REQUEST_INTERVAL=1.0`、`MAX_RETRIES=2`
   → ➕ 注：ffmpeg 是本地进程，**不受此限**（不产生网络请求）
5. 遇到 HTTP 412 / 403 / 429 或风控迹象 → 立即停止并如实汇报，不做反反爬
   → ✅ `FATAL_HTTP={403,412,429}` 不重试
6. 产出文件不出本地、不上传、不分享

---

## 9. 已知风险与对策

| # | 风险 | 对策 | 状态 |
|---|---|---|---|
| **R1** | **头号风险**：B 站对垃圾关键词返回满页无关结果（`numResults=1000`），空结果检测永不触发 → agent 自信选错并汇报成功 | 逐条 `relevance` + `warning: no_relevant_result` + hint「请勿选择」 | ✅ 已实现 + 回归测试 |
| R2 | 错误语义藏在 `data.code`（HTTP 永远 200） | 先判 `code != 0` 再解析 | ✅ |
| R3 | WBI 端点静默失败（`code=0` + 空数组） | 禁用 wbi；`unexpected_shape` 提示该怀疑方向 | ✅ |
| R4 | 缺 `buvid3` 导致概率性 412 | 所有调用前强制 `bootstrap()` 首页 | ✅ |
| R5 | `Referer` 缺失导致 DASH 403 | 工具层统一注入 | ✅ |
| R6 | 签名 URL 过期 | 只存 bvid/cid，需要时重新取流 | ⬜ 待 loop |
| R7 | 模型 JSON 输出不稳 | 标记包裹 + few-shot + 重试 ≤2 + `response_format` | ⬜ D4 |
| R8 | LLM 选错结果（同名不同内容） | `relevance` + `play` 给依据；`ask_user`；汇报附链接 | ✅ 部分 |
| R9 | 文件名/路径安全 | `_safe_filename()` 过滤保留字符 + 截断 + 防穿越 | ✅ + 测试 |
| R10 | 磁盘写满 | 下载前 `statvfs` 检查 | ✅ |
| R11 | 接口随时变更 | 工具层统一出入口；`docs/FACTS.md` 为契约基线 | ✅ |
| R12 | 工具缺失时模型编造能力 | `capability_missing` + system prompt 明确列出可用工具 | ⬜ D6 |
| **R13** | ➕ **ffmpeg 不在 PATH**（本机 `sudo` 被容器禁止） | 便携静态构建 + 三级查找（env > `.tools/` > PATH）+ 能力探测 | ✅ 已实现 |
| **R14** | ➕ **ffmpeg 失败原因晦涩**（stderr 几百行） | `classify_ffmpeg_error()` 7 类模式 + 保留 `stderr_tail` | ✅ + 测试 |
| **R15** | ➕ **"看似成功"的坏产物**（返回码 0 但文件损坏） | 原子写入 `.part` → ffprobe 校验 codec/时长 → `os.replace` | ✅ + 测试 |
| **R16** | ➕ **误解析 ffmpeg 输出头部行**（`' D. = Demuxing supported'`） | 正则排除含 `=` 的段 | ✅ + 测试 |
| **R17** | ➕ **输入无音频轨** | 先 `probe_media` → `no_audio_stream` + hint | ✅ + 测试 |

---

## 10. 分阶段实施计划（v0.3 重排）

| 天 | 目标 | 验收标准 | 状态 |
|---|---|---|---|
| **D1** | `search_videos` 跑通 | 关键词 → 5 条干净结果 | ✅ 完成 |
| **D2** | 下载链路（html5 直链） | bvid → 本地 mp4，失败有结构化报错 | ✅ 完成（22.92MB / 2.17s） |
| **D2.5** | 异常路径 + 日志 | 4 类异常正确；日志可回放 | ✅ 完成 |
| **D3-lite** | 便携 ffmpeg 落地 + 能力探测 | 无 root 装好 ffmpeg/ffprobe；`caps` 命令正确报告能力 | ✅ 完成 |
| **D3** | **mp4 → mp3 端到端** | 一条命令出 mp3，产物经 ffprobe 校验 | ✅ 完成（10.04MB / 168kbps） |
| **D3.5** | **外部工具鲁棒性** | stderr 分类 7 类 / 原子写入 / 无音频轨 / 损坏输入 | ✅ 完成（35 项测试全绿） |
| **D4** | 手写 agent loop 骨架（**不接 LLM**） | 假 JSON 指令走完 搜索→挑选→下载→转码 | ✅ 完成（66 项测试，含真实全链路） |
| **D4.5** | 协议鲁棒性专项 | 3 类非法输出（纯文本 / 坏 JSON / **带解释的 JSON**）行为正确 | ✅ 完成（11 项解析用例 + 9 项校验用例） |
| **D5** | 接真 LLM（DeepSeek） | 自然语言一句话端到端出 mp3 | ✅ 完成（4 轮 / 0 格式错误 / 3882+409 tokens） |
| **D6** | 护栏补齐 + 上下文压缩 | 护栏集中表可查询；压缩后任务仍能完成 | ✅ 完成（161 项测试全绿） |
| **D7** | README + demo + GitHub | 作品集三件套 | ⬜ |

---

## 11. 最终验收（Definition of Done）

- [ ] 一句话自然语言输入 → 本地 mp3 输出，端到端可复现
- [x] ✅ 每一步工具调用/结果都有日志，可回放（`logs/cli_*.jsonl`）
- [x] ✅ 工具层异常路径正确：搜不到 / 下载失败 / 非法 bvid / 无 ffmpeg /
      损坏输入 / 无音频轨 / 非法 mode
- [ ] ⬜ 异常路径补充：LLM 输出非法（3 类型，含"带解释的 JSON"）
- [ ] ⬜ 合规边界写入 README 显著位置（代码层 ✅ 已实现）
- [x] ✅ **工具层零第三方依赖**（纯标准库）
- [x] ✅ `docs/FACTS.md` 记录全部接口契约
- [x] ✅ **R1 有专门回归用例**（垃圾关键词 → 必须报警告）
- [x] ✅ **ffmpeg 能力探测有测试**（编码器/容器解析正确性）
- [ ] ⬜ 干净虚拟环境可一键跑通（`setup.sh` 拉便携 ffmpeg）

---

## 附录 A：已交付文件

| 文件 | 说明 | 状态 |
|---|---|---|
| `biliagent/bili.py` | B 站工具层：会话/搜索/信息/下载 | ✅ |
| `biliagent/media.py` | ➕ 媒体工具层：能力探测/媒体探测/stderr 分类/抽取转码 | ✅ |
| `biliagent/config.py` | ➕ 可配置项（ffmpeg 路径、超时、参数、合规常量） | ✅ |
| `biliagent/cli.py` | CLI（search/info/download/audio/**caps**） | ✅ |
| `tests/test_tools.py` | ➕ 回归测试（35 项，含降级跳过机制） | ✅ 全绿 |
| `docs/FACTS.md` | 接口事实核验表 | ✅ |
| `docs/DESIGN_v0.2.md` | 上一版设计（保留供对照） | ✅ |
| `.tools/ffmpeg`、`.tools/ffprobe` | ➕ 便携静态构建（不入库） | ✅ |
| `logs/cli_*.jsonl` | 工具调用日志 | ✅ |
| `downloads/*.mp4`、`*.mp3` | 实测产物（不入库） | ✅ |

## 附录 B：实测性能基线

| 操作 | 耗时 | 备注 |
|---|---|---|
| 搜索（含首页 bootstrap） | ~1.5s | 2 次 HTTP |
| 下载 22.92 MB | 2.17s | ≈10.5 MB/s |
| **mp3 转码 8:20 音轨** | **4.43s** | 168kbps VBR，输出 10.04MB |
| 便携 ffmpeg 下载 75MB | 3.79s | npmmirror，20.7 MB/s |

## 附录 C：未验证项（诚实声明）

1. 高频请求的风控边界（仅做数十次请求）
2. `playurl` 签名 URL 的具体有效期（设计上已按"会过期"处理）
3. 付费/充电专属内容的行为（游客必然失败，未测）
4. 412 的精确触发条件（观察到 1 次，无法稳定复现，故采保守策略）
5. ffmpeg 在**超长视频**（>1 小时）上的表现与超时边界
6. `copy` 模式产出的 `.m4a` 在各类播放器上的兼容性（仅 ffprobe 校验了容器与流）

## 附录 D：v0.3 的核心学习收获

> **外部工具是 agent 世界里最不可靠的一环，而 harness 的价值就在于把这种
> 不可靠性翻译成模型能据以自救的信号。**

这一版由一句「ffmpeg 调用也是 agent 实践的一环」触发，实际暴露出 5 个此前
被忽略的工程问题（R13–R17），每一个都是真实 agent 项目的常见故障源：

1. **工具可能不存在** → 能力探测，而非假设
2. **失败原因藏在人类可读的 stderr 里** → 分类器 + 保留原文尾部
3. **返回码 0 ≠ 成功** → 独立校验产物
4. **失败会留下垃圾** → 原子写入
5. **输入可能不满足前提** → 先探测，再执行

用框架时这 5 点全被隐藏，出问题时你只会看到「工具调用失败」。
亲手写一遍，才知道护栏该加在哪。

---

## 12. D4 开发中由测试抓出的真实缺陷（补记）

D4（agent loop）实现过程中，测试抓出 4 个缺陷。**它们的共同价值在于：
每个都是"看起来能跑，但触发异常路径就坏"的类型** —— 只有主动构造异常用例
才会暴露。

| # | 缺陷 | 为什么危险 | 修法 |
|---|---|---|---|
| **B1** | **分层泄漏**：`download_html5_mp4()` 直接访问 `session._throttle()` / `session._opener` / `session.request_count` 私有成员 | 节流合规约束可被绕过，私有成员一改就到处崩 | 给 `BiliSession` 加公开的 `open_stream()`，把节流/cookie/重试/错误语义全收在 L1 |
| **B2** | **loop 依赖工具自觉吞异常**：`AgentLoop` 直接调 `box.dispatch()`，假定它一定返回 dict | 任何工具实现的一处疏漏都会让整个循环崩掉，**而模型看到的是 Python 堆栈，它完全无法据此自救** | loop 自己兜 `BiliFatal` + `Exception`，转成结构化错误回喂 |
| **B3** | **格式重试计数用"累计"而非"连续"** | 模型偶尔格式错但整体在推进（违规→成功→违规→成功）会被误判为卡死而放弃 | 改为连续计数（成功动作后归零），累计值另存供诊断 |
| **B4** | **`--script` 默认空列表导致预置脚本永不触发** | CLI 分支顺序错误，demo 形同虚设 | 先决定脚本来源（显式 > 预设），再判空 |

### 由 B1–B3 提炼的两条通用原则

**原则 1：harness 不能信任工具层会遵守契约。**
契约（"工具永远返回 dict"）是**设计意图**，不是**运行时保证**。
真正的护栏必须在 harness 侧再兜一层，否则契约的一个漏洞就是一个崩溃点。

**原则 2：计数器的语义比数值更重要。**
"连续 3 次格式错误"与"累计 3 次格式错误"是完全不同的策略：
- 累计 → 惩罚"长期运行的正常抖动"
- 连续 → 精准定位"卡死"
选错语义会让护栏本身变成故障源（B3 就是这个）。

### D4 的最终验收数据

```
协议解析（11 项）   标记识别、7 类格式违规、带解释 JSON 的容错
指令校验（9 项）    missing/unknown action、必填、枚举越界、多余参数
提示词（7 项）      与 TOOL_SPECS 同源、能力感知三分支
Loop 控制（27 项）  正常路径、自愈、连续计数、卡死、轮数上限、
                   ask_user、模型失败、工具崩溃、静默失败转发、
                   观察压缩与字段过滤、状态可序列化
真实全链路（6 项）  假指令 + 真工具：搜索→下载→转 mp3 全通
────────────────────────────────────────────────
合计 66 项全绿（工具层另有 35 项，总 101 项）
```

---

## 13. D5：真 DeepSeek 接入（补记）

### 一句话结论

```
$ python3 -m biliagent.cli agent "帮我把老番茄播放量最高的视频下载下来并做成 mp3"
  结束原因: finished ｜ 总轮数: 4 ｜ 格式纠正: 0 次
  token: in=3882 out=409（其中思考 230）｜ 调用 4 次
  mp3: downloads/BV1Wb411v7WN_【老番茄】史上最骚杀手(第一集).mp3
```

模型自主完成：搜索 → 从 5 个候选里按 `play` 挑出播放量最高的（3415 万）
→ 下载 → 转 mp3 → 汇报。**零格式错误、零人工干预。**

### 为什么 loop 一行都没改

这是架构分层最直接的回报。`AgentLoop` 只依赖一个可注入的
`chat_fn(messages) -> {"text": ...}`。D4 注入 `ScriptedChat`，
D5 注入 `DeepSeekClient.__call__` —— 对 loop 而言**两者没有区别**。

> **可测试性不是测试写出来的，是架构留出来的。**
> 如果 D4 时直接把 `requests.post` 写进 loop，D5 就不得不改 loop 并重测全部逻辑。

### D5 实测到的四个真实问题

| # | 问题 | 处置 |
|---|---|---|
| **D5-1** | 旧模型名 `deepseek-chat` / `deepseek-reasoner` **仍可用但都路由到 `deepseek-flash`** | 显式使用 `deepseek-flash` / `deepseek-v4-pro`，避免误以为在用别的模型 |
| **D5-2** | `message.reasoning_content` 是独立字段，与 `content` 并列 | **剥掉不保留，只记长度** —— 回传会持续吃上下文 |
| **D5-3** | `response_format=json_object` 与 `<tool_call>` 标记包裹**结构冲突** | **刻意不用** JSON mode，保住手写协议层的学习目标（代价见 FACTS §8 未验证项） |
| **D5-4** | `max_tokens` 过小时推理模型把预算全花在思考上，`content` 为空 | 文档化该现象；把"content 空但 finish_reason=stop"标为可疑 |

### 由测试抓出的第 5 个缺陷（B5）

**`llm_error` 只报错误代码，丢掉了原因和 hint。**

```python
# 修前
state.report = "模型调用失败: %s" % resp["error"]
# → "模型调用失败: http_401"    用户完全不知道该改什么

# 修后
"模型调用失败：http_401
Authentication Fails, Your api key: ****cdef is invalid
建议：API key 无效或已失效。请在 DeepSeek 控制台确认。"
```

**这和 L2 工具层的 `hint` 是同一个原则，只是发生在更外层**：
错误信息必须让**读者能采取行动**。
`http_401` 是给机器看的，`建议：...` 是给人看的。
harness 同时面向两个读者（用户和模型），所以两者都要给。

### 安全处理

- API key **只从配置文件 / 环境变量读取**（`env.py`），适配器里没有默认值，
  也没有任何写入代码。优先级：真实环境变量 > 项目 `.env` > `~/.biliagent.env`
- `.env` 已被 `.gitignore` 排除；`.env.example` 只有占位符，可安全提交
- `env.py` 用 `MANAGED_KEYS` 白名单，只加载受管键，不污染运行环境
- `cli.py cfg` 展示配置时自动脱敏
- `redact()` 在返回错误前抹掉文本中出现的 key（有专门测试）
- ⚠️ **仍在待办**：key 曾以明文出现在会话记录中，应轮换

### D5 验收数据

```
mock 测试 34 项   构造/redact、正常路径、401/402/404/429/5xx/网络错误、
                 坏响应体、key 抹除、与 loop 集成、模型原始输出不预处理
真实 API 4 项    可调用、遵循标记协议、返回 token 统计、记录思考长度
（--real 参数控制，避免常规回归产生费用）
```

**最值得记的一条**：`B. 真实全链路` 那一组用**假指令驱动真工具**，
把"loop 逻辑"和"模型不确定性"彻底解耦。出问题时先跑 A 部分（纯本地），
全绿就说明锅在模型/prompt 而不在 loop —— 这个排查秩序能省掉大量时间。
