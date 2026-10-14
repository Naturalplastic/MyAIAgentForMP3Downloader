# 外部接口事实核验

- 核验日期：2026-10-14
- 核验方式：本机直连（出口 IP 位于北京），游客身份（仅使用首页下发的 `buvid3` / `b_nut`）
- 用途：记录对外部接口的实测结论。本表为代码的契约基线，接口变更时应优先更新本文件

---

## 1. 接口速览

| 环节 | 接口 | 实测结论 | 状态 |
|---|---|---|---|
| 会话 | `GET https://www.bilibili.com/` | 下发 `b_nut`、`buvid3` | 必需 |
| 检索 | `x/web-interface/search/type?search_type=video` | `code=0`，每页 20 条，扁平数组，含 `favorites` | 采用 |
| 检索 | `x/web-interface/search/all/v2` | `code=0`，结构为 12 个分区块，不含 `favorites` | 弃用 |
| 检索 | `x/web-interface/wbi/search/type` | 未签名时 `code=0` 但 `result` 为空 | 禁用 |
| 视频信息 | `x/web-interface/view?bvid=` | `code=0`，返回 `cid`、`aid`、`title`、`pages` | 采用 |
| 播放地址 | `x/player/playurl?platform=html5&high_quality=1` | 无需签名，返回单一完整 mp4 | 采用 |
| 播放地址 | `x/player/playurl?fnval=16` | 无需签名，返回 3 条 DASH 音频轨 | 备用 |
| CDN 防盗链 | DASH `.m4s` | 带 Referer 返回 206；裸请求返回 403 | 仅 DASH 适用 |
| CDN 防盗链 | html5 `.mp4` | 带与不带 Referer 均返回 206 | 不强制 |
| 错误语义 | 全部接口 | HTTP 状态码恒为 200，`Content-Type` 恒为 `application/json`，错误位于 `data.code` | 必须检查 `code` |

---

## 2. 关键约束

### 2.1 签名要求

`playurl`（不带 `wbi` 前缀）无需 WBI 签名即可使用，实测返回 `code=0` 与完整
播放地址。WBI 签名的要求仅适用于 `wbi/search/*` 系列接口，本项目未使用。

关于 412：无 cookie 请求曾出现一次 HTTP 412，随后 5 组对照实验（带与不带
cookie、带与不带 Referer 的组合）均返回 200。成因未能稳定复现，故采取固定
成本措施：所有 API 调用前先请求一次首页。

### 2.2 Referer 的作用范围

| 流类型 | 带 Referer | 裸请求 |
|---|---|---|
| DASH 音频轨 `.m4s` | 206 | 403 |
| html5 `.mp4` | 206 | 206 |

Referer 校验仅对 DASH 分片生效。工具层仍统一注入该请求头作为防御措施。

### 2.3 错误检测方式

```
keyword="asdkjfhaskjdfhaskjdfh"  → HTTP 200, code=0,    result 20 条
keyword="   "（纯空格）           → HTTP 200, code=-1200, result 0 条
```

`Content-Type` 恒为 `application/json`，无法用于识别错误响应。
正确做法为先检查 `data.code == 0`，再解析 `data`。

---

## 3. 已知陷阱

### 3.1 无匹配关键词仍返回整页结果

```
keyword = "zzqqxx不存在的关键词zzqqxx"
code = 0, numResults = 1000
返回内容为与关键词无关的视频
```

`result` 几乎不会为空，因此空结果检测无法作为检索有效性的判据。
代码中的对策为逐条计算相关性（`relevance`），全部未命中时附加
`warning: "no_relevant_result"`。

### 3.2 WBI 端点静默失败

`x/web-interface/wbi/search/type` 在未签名时返回
`code=0`、`message="OK"`、`result=[]`。
状态码与消息均为正常值，但结果为空。本项目未使用该端点。

---

## 4. 实测数据

### 4.1 音频轨（游客身份）

| id | codec | 码率 |
|---|---|---|
| 30216 | `mp4a.40.5` | 41 kbps |
| 30232 | `mp4a.40.2` | 99 kbps |
| 30280 | `mp4a.40.2` | 187 kbps（游客上限） |

### 4.2 html5 mp4

```
bvid=BV16F411S7dW
quality=16（360P）, format=mp4, durl 条数=1
size=24,033,295 字节, 时长 500,577 ms
```

容器结构（解析结果）：

```
ftyp(32) / moov(492954) / free(8) / mdat(23540301)
trak 数量 = 2
  trak#1 → hdlr=vide → stsd: avc1  (H.264)
  trak#2 → hdlr=soun → stsd: mp4a  (AAC)
```

音视频已封装于单一文件，无需合并。仅在需要 mp3 时使用 ffmpeg 抽取。

### 4.3 性能基线

| 操作 | 耗时 | 说明 |
|---|---|---|
| 检索（含首页请求） | 约 1.5 s | 2 次 HTTP 请求 |
| 下载 22.92 MB | 2.17 s | 约 10.5 MB/s |
| mp4 转 mp3（8 分 20 秒音轨） | 4.43 s | 输出 10.04 MB，168 kbps |
| 便携 ffmpeg 下载（75 MB） | 3.79 s | 镜像站，约 20.7 MB/s |

### 4.4 ffmpeg

| 项 | 结论 |
|---|---|
| 系统安装 | `sudo apt-get` 被容器 `no new privileges` 标志阻止 |
| 可用方案 | 便携静态构建，来自镜像站 `ffmpeg-static/b6.0`，无需 root |
| 版本 | `ffmpeg 6.0-static`，含 `libmp3lame`、`aac`、`mp3` muxer、`mov,mp4,m4a` demuxer |
| ffprobe | 同源独立文件 `ffprobe-linux-x64.gz` |
| 输出解析 | `-encoders` / `-muxers` 输出含说明性头部（如 `D. = Demuxing supported`），解析时须排除 |

---

## 5. DeepSeek API

| 项 | 结论 |
|---|---|
| endpoint | `https://api.deepseek.com/chat/completions`（OpenAI 兼容） |
| 鉴权 | `Authorization: Bearer sk-...` |
| 可用模型 | `deepseek-flash`、`deepseek-v4-pro` |
| 上下文 / 输出上限 | 1,048,576 / 393,216 tokens |
| 旧模型名 | `deepseek-chat`、`deepseek-reasoner` 仍可用，但均路由至 `deepseek-flash` |
| 推理强度 | `reasoning_effort` 取值 `low` / `high` / `max` |
| 思考内容 | v4 系列返回 `message.reasoning_content`，与 `content` 并列 |
| 思考计费 | `usage.completion_tokens_details.reasoning_tokens` |
| JSON 模式 | `response_format={"type":"json_object"}` 支持 |
| 函数调用 | `tools` / `tool_choice` 支持，返回标准 `message.tool_calls` |

### 5.1 对协议设计的影响

`response_format=json_object` 要求输出为纯 JSON，与本项目的
`<tool_call>...</tool_call>` 标记包裹结构冲突，两者不能同时使用。
原生 `tools` 方案会使 `protocol.py` 的解析与校验逻辑失去作用。
本项目两者均未采用，保留纯文本协议。

### 5.2 思考内容处理

| 处理方式 | 结果 |
|---|---|
| 多轮时原样回传 | 不报错 |
| 多轮时剥离 | 不报错 |
| 本项目采用 | 剥离，仅记录长度 |

另：`max_tokens` 过小时，推理模型可能将预算全部用于思考，导致 `content`
为空。实测 `deepseek-v4-pro` 在 `max_tokens=50` 时 `content=""`，
增至 600 后正常。

### 5.3 标记协议遵循度

```
system: 必须只输出 <tool_call>{"action":...}</tool_call> 或 <final_answer>...</final_answer>
user:   用户想下载周杰伦的稻香。请先搜索。
模型:   <tool_call>{"action":"search","args":{"query":"周杰伦 稻香 下载"}}</tool_call>
```

标记格式遵循度较高，但参数名可能出现偏差（使用 `query` 而非 `keyword`）。
`validate_directive()` 用于拦截此类问题，实测已成功拦截并完成自我修正。

---

## 6. 生态状况

| 项目 | 归档 | 最后提交 | 仓库现状 |
|---|---|---|---|
| `SocialSisterYi/bilibili-API-collect` | 是 | 2026-01-30 | 仅保留 `README.md` 与 `reason.jpg` |
| `Nemo2011/bilibili-api` | 是 | 2026-07-06 | 仅保留 `README.md` |

两个项目均已归档且清空代码。第三方 B 站库的依赖路径不可用。

---

## 7. 未验证项

| 项 | 说明 |
|---|---|
| 风控频率边界 | 仅执行十余次请求，未测试触发阈值 |
| 签名 URL 有效期 | 未测量。实现上按会过期处理，仅持久化 `bvid` / `cid` |
| 付费与充电专属内容 | 游客身份必然失败，未测试 |
| 412 的触发条件 | 观察到 1 次，无法稳定复现 |
| ffmpeg 处理超长视频 | 未测试；超时上限设为 600 秒 |
| `copy` 模式产物兼容性 | 仅通过 ffprobe 校验容器与流，未在播放器上验证 |
| 长会话稳定性 | 标记协议仅在 1 至 6 轮范围内验证 |
| 流式输出 | 本项目使用非流式，未测试 `stream=true` |
| `reasoning_effort=max` | 仅使用默认 `high`，未评估成本与延迟 |
