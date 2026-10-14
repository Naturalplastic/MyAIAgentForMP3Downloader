# 使用手册

- 文档版本：1.0
- 适用代码版本：`biliagent` 1.0

---

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.10 及以上 | 代码使用 `X | None` 与 `tuple[list, int]` 语法 |
| 第三方 Python 包 | 无 | 仅依赖标准库 |
| ffmpeg / ffprobe | 抽取音频时必需 | 可由 `scripts/setup_tools.sh` 自动获取 |
| 网络 | 需可访问 `www.bilibili.com` 与 `api.deepseek.com` | |
| 权限 | 无需 root | 便携工具安装于项目目录内 |

---

## 2. 安装

### 2.1 获取代码

```bash
git clone <repository-url>
cd MyMP3Download
```

### 2.2 安装 ffmpeg / ffprobe

```bash
bash scripts/setup_tools.sh
```

`.tools/` 目录约 150 MB，已排除在版本控制之外，因此**不会随仓库分发**，
在新环境上必须执行本步骤一次。

脚本行为：

| 项 | 说明 |
|---|---|
| 权限 | 无需 root，不修改系统目录 |
| 平台识别 | 自动识别操作系统与 CPU 架构，不支持的组合会给出替代方案 |
| 幂等性 | 已存在且可用的文件跳过下载 |
| 原子安装 | 先下载至临时文件并校验可执行，通过后才替换正式文件 |
| 文件校验 | 判定标准为实际执行并输出版本号，而非仅检查文件是否存在 |

相关参数：

```bash
bash scripts/setup_tools.sh --check    # 仅检查是否就绪，不下载
bash scripts/setup_tools.sh --force    # 强制重新下载
```

支持的平台：Linux（x64 / arm64 / arm / ia32）、macOS（x64 / arm64）、
Windows（x64 / ia32）、FreeBSD（x64）。

网络受限时可指定其他镜像：

```bash
FFMPEG_STATIC_MIRROR=https://your-mirror/path bash scripts/setup_tools.sh
```

若系统已安装 ffmpeg，可跳过本步骤。查找顺序为：
环境变量 → 项目 `.tools/` → `PATH`。也可显式指定：

```bash
export BILIAGENT_FFMPEG=/usr/bin/ffmpeg
export BILIAGENT_FFPROBE=/usr/bin/ffprobe
```

### 2.3 配置 API key

复制模板并填入密钥：

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```bash
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`.env` 已在 `.gitignore` 中排除，不会被提交。

---

## 3. 配置

### 3.1 配置来源与优先级

```
真实环境变量  >  项目 .env  >  ~/.biliagent.env
```

三种方式适用于不同场景：

| 方式 | 位置 | 适用场景 |
|---|---|---|
| 项目 `.env` | 项目根目录 | 常规使用。项目自包含，无需修改 shell 配置 |
| 环境变量 | 当前 shell | 临时替换密钥；CI 环境注入 |
| 用户级文件 | `~/.biliagent.env` | 多项目共用同一密钥；不希望密钥位于项目目录内 |

用户级文件示例：

```bash
umask 077 && echo 'DEEPSEEK_API_KEY=sk-你的密钥' > ~/.biliagent.env
```

### 3.2 配置项

| 键 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | 使用 agent 时必需 | 无 | API 密钥 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-flash` | `deepseek-flash` 或 `deepseek-v4-pro` |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | API 地址 |
| `BILIAGENT_FFMPEG` | 否 | 自动查找 | ffmpeg 路径 |
| `BILIAGENT_FFPROBE` | 否 | 自动查找 | ffprobe 路径 |
| `BILI_SESSDATA` | 否 | 空（游客身份） | B 站登录态。默认不设置 |
| `KEEP_INTERMEDIATE_MP4` | 否 | `false` | 抽取音频后是否保留中间 mp4 |

仅上述键会从配置文件载入，其他键不受影响。

### 3.3 查看当前配置

```bash
python3 -m biliagent.cli cfg
```

输出中密钥自动脱敏：

```
=== 当前配置 ===
  DEEPSEEK_API_KEY     = sk-49cb…84af
  DEEPSEEK_MODEL       = deepsee…lash
  DEEPSEEK_BASE_URL    = https:/….com

=== 配置文件 ===
  项目 .env            存在        /path/to/.env
  ~/.biliagent.env     未创建      /home/user/.biliagent.env
```

---

## 4. 验证安装

### 4.1 检查外部工具

```bash
python3 -m biliagent.cli caps
```

预期输出：

```
=== 外部工具能力探测 ===
  ffmpeg  : v6.0-static @ /path/.tools/ffmpeg
  ffprobe : v6.0-static @ /path/.tools/ffprobe
  编码器  : {'has_libmp3lame': True, 'has_native_mp3': False, 'has_aac': True}
  容器    : {'mp3': True, 'mp4': True, 'ipod': True}

  → 能产出 .mp3      : 是
  → 能抽取音频流     : 是
```

若显示未找到，执行 `bash scripts/setup_tools.sh`。

### 4.2 检查模型连通性

```bash
python3 -m biliagent.cli agent "搜索老番茄的视频，告诉我播放量最高的一个" --max-rounds 3
```

若密钥无效，输出中包含具体原因与处理建议：

```
模型调用失败：http_401
Authentication Fails, Your api key: ****cdef is invalid
建议：API key 无效或已失效。请在 DeepSeek 控制台确认，并检查环境变量。
```

若未配置密钥，输出中包含三种配置方式的完整命令。

### 4.3 不消耗 API 额度的验证

```bash
python3 -m biliagent.cli agent --fake
```

使用内置脚本代替真实模型，验证工具链路与循环逻辑。

### 4.4 运行测试

```bash
bash scripts/test_all.sh --no-real    # 离线执行，不产生费用
bash scripts/test_all.sh              # 含真实 API 冒烟测试
```

---

## 5. 使用

### 5.1 基本用法

```bash
python3 -m biliagent.cli agent "<自然语言需求>"
```

示例：

```bash
# 获取音频
python3 -m biliagent.cli agent "我想要泠鸢的泼墨漓江，要 mp3 音频"

# 获取视频
python3 -m biliagent.cli agent "下载老番茄那个讲结婚的视频"

# 仅检索
python3 -m biliagent.cli agent "搜一下泠鸢的泼墨漓江有哪些版本，告诉我播放量"

# 无损音频
python3 -m biliagent.cli agent "把泠鸢泼墨漓江做成无损 m4a"
```

### 5.2 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--model <id>` | `deepseek-flash` | 模型：`deepseek-flash` 或 `deepseek-v4-pro` |
| `--effort {low,high,max}` | API 默认 | 推理强度。值越大耗时与费用越高 |
| `--max-rounds <n>` | 10 | 循环轮数上限 |
| `--max-context <n>` | 20000 | 上下文预算（估算 token） |
| `--keep-recent <n>` | 3 | 压缩时保留的最近轮数 |
| `--out-dir <dir>` | `downloads` | 输出目录 |
| `--keep-mp4` | 关 | 抽取音频后保留中间 mp4 |
| `--quiet` | 关 | 不输出模型原始返回 |
| `--fake` | 关 | 使用内置脚本代替模型 |
| `--script <...>` | 无 | 指定模型输出序列，用于验证循环逻辑 |

### 5.3 工具层直接调用

以下命令不经过模型，不产生 API 费用。

```bash
# 检索
python3 -m biliagent.cli search "泠鸢 泼墨漓江" --limit 5

# 视频详情
python3 -m biliagent.cli info BV1Ds411h7T1

# 下载
python3 -m biliagent.cli download BV1Ds411h7T1

# 下载并抽取音频
python3 -m biliagent.cli download BV1Ds411h7T1 --audio mp3

# 从本地文件抽取音频
python3 -m biliagent.cli audio downloads/xxx.mp4 --mode mp3
```

`search` 输出示例：

```
关键词「泠鸢 泼墨漓江」— 该词总匹配 1000 条，按相关性+播放量排序后取前 5 条：

  1. [hit] 【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】
     UP: 泠鸢yousa ｜ 时长: 5:6 ｜ 播放: 1,710,053 ｜ 收藏: 91,383
     https://www.bilibili.com/video/BV1Ds411h7T1  (BV1Ds411h7T1)
  ...

  相关性命中 5/5 条 ｜ 本次会话累计 HTTP 请求: 2
```

标记含义：`[hit]` 全部关键词命中，`[part]` 部分命中，`[miss]` 未命中。
若全部为 `[miss]`，说明该关键词无匹配结果（B 站对此仍会返回整页无关内容）。

### 5.4 约束查询

```bash
python3 -m biliagent.cli guardrails
```

列出全部护栏参数（取值、强制位置、越界行为、设置原因）与合规边界。

---

## 6. 输出

### 6.1 目录结构

```
downloads/
└── BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3

logs/
└── cli_20261015.jsonl
```

### 6.2 文件命名

格式为 `{bvid}_{标题}.{扩展名}`。

- 以 `bvid` 为前缀，保证唯一性与可追溯性
- 标题中 `/\:*?"<>|` 及控制字符替换为 `_`，并限制长度
- 防止路径穿越

### 6.3 中间产物

音频抽取以 mp4 为源文件，该 mp4 为必要中间产物，
体积通常为音频的 2~3 倍（实测 15.52 MB 对 5.82 MB）。

默认行为为抽取成功后删除中间 mp4：

```bash
$ python3 -m biliagent.cli agent "我要泠鸢的泼墨漓江 mp3"
  音频    : downloads/BV1Ds411h7T1_....mp3
  已清理  : 中间 mp4 downloads/BV1Ds411h7T1_....mp4

$ ls downloads/
  BV1Ds411h7T1_....mp3
```

保留中间 mp4 的方式：

```bash
# 单次
python3 -m biliagent.cli agent "..." --keep-mp4

# 永久（写入 .env）
KEEP_INTERMEDIATE_MP4=true
```

抽取失败时中间 mp4 一律保留，因为它是唯一的重试依据。

### 6.4 日志

日志按日期写入 `logs/cli_YYYYMMDD.jsonl`，每行一个 JSON 对象。
记录内容包括每次工具调用与 agent 运行的完整状态。

查看最近一次 agent 运行的过程：

```bash
python3 -c "
import json
d = json.loads(open('logs/cli_20261015.jsonl').readlines()[-1])
r = d['result']
print('输入:', r['user_text'], '| 结果:', r['outcome'], '| 轮数:', r['round'])
for t in r['trace']:
    print('  r%-2s %-16s %s' % (t['round'], t['type'], t.get('action', '')))
"
```

输出：

```
输入: 我想要泠鸢的泼墨漓江，要 mp3 音频 | 结果: finished | 轮数: 5
  r1  tool_result      search
  r2  tool_result      download
  r3  format_violation
  r4  tool_result      extract_audio
  r5  final_answer
```

查看完整对话内容：

```bash
python3 -c "
import json
d = json.loads(open('logs/cli_20261015.jsonl').readlines()[-1])
for i, m in enumerate(d['result']['messages']):
    print('\n--- [%d] %s (%d 字符) ---' % (i, m['role'], len(m['content'])))
    print(m['content'])
"
```

---

## 7. 结果状态说明

`agent` 命令的结束原因与退出码：

| 结束原因 | 退出码 | 含义 | 处理方式 |
|---|---:|---|---|
| `finished` | 0 | 任务完成 | — |
| `asked` | 0 | 模型向用户提问 | 查看"汇报"字段，补充信息后重新执行 |
| `max_rounds` | 1 | 达到轮数上限 | 增大 `--max-rounds` 或细化需求描述 |
| `format_stuck` | 1 | 连续格式违规 | 改用 `--model deepseek-v4-pro` |
| `stuck` | 1 | 同一动作重复 | 需求可能存在冲突 |
| `llm_error` | 1 | 模型调用失败 | 查看"建议"字段 |

### 7.1 `asked` 状态

该状态不属于失败。当需求缺少必要信息时，模型会提问而非自行假设。
例如输入 `我想要泠鸢的泼墨漓江`（未指定格式）：

```
找到泠鸢yousa原版《泼墨漓江》(BV1Ds411h7T1)。你想要哪种格式？
1) 原始 MP4 视频  2) MP3 音频（转码约168kbps）  3) 无损音频 .m4a
```

若希望一次完成，在需求中明确格式即可。

---

## 8. 错误处理

### 8.1 工具层错误码

| 错误码 | 原因 | 处理方式 |
|---|---|---|
| `empty_keyword` | 关键词为空 | 补充检索词 |
| `no_relevant_result` | 检索结果全部不相关 | 使用更准确的名称 |
| `bad_bvid` | bvid 格式不合法 | 检查输入 |
| `api_error` | 接口返回非零 `code` | 查看 `hint` 字段 |
| `risk_control` | 403 / 412 / 429 | 稍后重试。程序不进行自动重试 |
| `insufficient_space` | 磁盘空间不足 | 清理空间或更换 `--out-dir` |
| `not_mp4` | CDN 返回非视频内容 | 重新下载 |
| `ffmpeg_missing` | 未找到 ffmpeg | 执行 `scripts/setup_tools.sh` |
| `no_audio_stream` | 源文件不含音频轨 | 更换候选视频 |
| `verify_failed` | 产物校验未通过 | 重试；若持续失败改用 `--audio copy` |
| `incomplete_mp4` | 文件不完整（缺 `moov`） | 重新下载 |

### 8.2 密钥相关

| 现象 | 原因 | 处理方式 |
|---|---|---|
| `missing_api_key` | 未配置密钥 | 按输出的三种方式之一配置 |
| `http_401` | 密钥无效或已失效 | 在控制台确认后更新 `.env` |
| `http_402` | 余额不足 | 充值 |
| `http_429` | 触发限流 | 降低调用频率或稍后重试 |

---

## 9. 以库形式使用

### 9.1 调用工具层

```python
import sys
sys.path.insert(0, "/path/to/MyMP3Download")

from biliagent.bili import BiliSession, search_videos, download_html5_mp4
from biliagent.media import extract_audio

session = BiliSession()                     # 默认游客身份
result = search_videos(session, "泠鸢 泼墨漓江", limit=5)

if result["ok"]:
    for item in result["items"]:
        print(item["relevance"], item["play"], item["title"])
else:
    print(result["error"], result.get("hint"))
```

调用约定：所有函数不抛出异常，返回字典；应先检查 `ok` 字段。
失败时包含 `error`（机器可读）、`message`（描述）、`hint`（处理建议）。

`BiliSession` 建议复用同一实例，以共享 cookie 与限流状态。

### 9.2 调用 agent 循环

```python
from biliagent.loop import AgentLoop
from biliagent.tools import ToolBox
from biliagent.llm import DeepSeekClient

loop = AgentLoop(DeepSeekClient(), toolbox=ToolBox(out_dir="downloads"))
state = loop.run("把泠鸢的泼墨漓江做成 mp3")

print(state.outcome)      # finished
print(state.report)       # 模型的汇报文本
print(state.audio_path)   # 产物路径
print(state.round)        # 轮数
print(len(state.trace))   # 每轮记录条数
```

### 9.3 替换模型

`AgentLoop` 仅依赖一个函数，签名为 `(messages) -> {"text": str, ...}`。
实现该接口即可替换模型，无需修改循环代码：

```python
class MyClient:
    def __call__(self, messages):
        # 调用自定义模型
        return {"text": "模型返回的文本", "usage": {"prompt_tokens": 100}}

loop = AgentLoop(MyClient(), toolbox=ToolBox())
```

---

## 10. 部署到新环境

`.tools/` 目录体积约 150 MB，已排除在版本控制之外，新环境需重新获取。

```bash
git clone <repository-url>
cd MyMP3Download

bash scripts/setup_tools.sh             # 获取 ffmpeg / ffprobe
cp .env.example .env && chmod 600 .env  # 编辑填入密钥

python3 -m biliagent.cli caps           # 验证外部工具
python3 -m biliagent.cli cfg            # 验证配置
bash scripts/test_all.sh --no-real      # 运行测试
```

---

## 11. 合规约定

以下约束在代码层实现，不依赖运行时判断：

| 约束 | 说明 |
|---|---|
| 不登录 | `sessdata` 默认为空，即游客身份 |
| 请求间隔 | 不小于 1 秒 |
| 失败重试 | 不超过 2 次，指数退避 |
| 风控响应 | 遇 403 / 412 / 429 立即停止，不进行重试 |
| 数据不外发 | `.gitignore` 排除 `downloads/`、`logs/`、`.tools/`、`.env` |

本项目仅用于个人学习。不得用于分发、部署为公共服务或商业用途。
下载内容应限于本人投稿、采用开放许可或符合个人学习用途的范围。

---

## 12. 密钥安全

- 代码中不含任何硬编码密钥，仅从配置文件与环境变量读取
- `redact()` 在输出前移除文本中的密钥
- `cfg` 命令展示配置时自动脱敏
- `.env` 已在 `.gitignore` 中排除；`.env.example` 仅含占位符
- 建议设置文件权限为 600
- 密钥轮换后仅需修改 `.env`

---

## 13. 命令速查

```bash
# 安装与配置
bash scripts/setup_tools.sh
cp .env.example .env && chmod 600 .env
python3 -m biliagent.cli cfg

# 验证
python3 -m biliagent.cli caps
python3 -m biliagent.cli guardrails
python3 -m biliagent.cli agent --fake

# 使用
python3 -m biliagent.cli agent "我想要泠鸢的泼墨漓江，要 mp3 音频"
python3 -m biliagent.cli agent "..." --keep-mp4
python3 -m biliagent.cli agent "..." --model deepseek-v4-pro --effort high

# 工具层
python3 -m biliagent.cli search "泠鸢 泼墨漓江"
python3 -m biliagent.cli info BV1Ds411h7T1
python3 -m biliagent.cli download BV1Ds411h7T1 --audio mp3
python3 -m biliagent.cli audio downloads/xxx.mp4 --mode copy

# 调试
tail -1 logs/cli_$(date +%Y%m%d).jsonl | python3 -m json.tool | head -60

# 测试
bash scripts/test_all.sh --no-real
bash scripts/test_all.sh
```
