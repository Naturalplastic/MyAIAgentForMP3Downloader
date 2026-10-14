# B 站媒体下载 Agent

将自然语言描述转换为本地媒体文件的 agent 实现。

```
输入：我想要泠鸢的泼墨漓江，要 mp3 音频
输出：downloads/BV1Ds411h7T1_【泠鸢·原创】泼墨漓江【玖玲】【茜色诗集】.mp3
```

agent 自主完成需求理解、站内检索、结果筛选、视频下载、音频抽取与结果汇报。

---

## 项目性质

个人学习项目。目标为完整实践纯文本模型与自建工具链的对接流程，包括：

- 手写 agent 循环与工具调用协议
- 外部工具（ffmpeg）的探测、调用与错误归纳
- 上下文预算管理与历史压缩
- 面向不可靠外部依赖的护栏设计

为此，本项目**不使用 agent 框架**，也**不使用第三方 B 站库**
（相关社区项目已于 2026 年归档，见 `docs/FACTS.md`）。
harness 的每一层均为自行实现。

---

## 主要特性

| 特性 | 说明 |
|---|---|
| 零第三方依赖 | 仅使用 Python 标准库；外部程序仅 ffmpeg / ffprobe |
| 游客身份 | 不登录、不使用他人凭据、不绕过会员鉴权 |
| 结构化工具契约 | 所有工具返回字典，不抛出异常；错误含 `error` / `message` / `hint` |
| 能力感知 | 启动时探测外部工具能力，结果注入系统提示词并用于工具守门 |
| 产物校验 | 音频抽取采用原子写入，并经 ffprobe 校验容器、音轨与时长 |
| 上下文压缩 | 超出预算时压缩早期轮次，保留系统提示词与原始任务 |
| 可回放 | 每次运行的消息级记录落盘至 `logs/` |

---

## 快速开始

无需安装 Python 依赖（`requirements.txt` 为空，运行时仅使用标准库）。

```bash
# 1. 获取便携 ffmpeg（无需 root）
bash scripts/setup_tools.sh

# 2. 配置 API key
cp .env.example .env
chmod 600 .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 验证环境
python3 -m biliagent.cli caps
python3 -m biliagent.cli cfg

# 4. 使用
python3 -m biliagent.cli agent "我想要泠鸢的泼墨漓江，要 mp3 音频"
```

完整说明见 [`docs/USAGE.md`](docs/USAGE.md)。

---

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10 及以上 |
| 第三方 Python 包 | 无 |
| ffmpeg / ffprobe | 抽取音频时必需，可由脚本自动获取 |
| 权限 | 无需 root |

---

## 目录结构

```
MyMP3Download/
├── .env.example            配置模板
├── biliagent/              源代码
│   ├── bili.py             B 站接口与会话层
│   ├── cli.py              命令行入口
│   ├── config.py           常量与护栏表
│   ├── context.py          上下文管理
│   ├── env.py              配置加载
│   ├── llm.py              模型适配
│   ├── loop.py             agent 循环
│   ├── media.py            外部工具封装
│   ├── protocol.py         协议层
│   └── tools.py            工具调度
├── docs/                   文档
├── scripts/                辅助脚本
└── tests/                  测试
```

---

## 常用命令

```bash
# 运行任务
python3 -m biliagent.cli agent "<自然语言需求>"
python3 -m biliagent.cli agent "..." --keep-mp4          # 保留中间 mp4
python3 -m biliagent.cli agent "..." --model deepseek-v4-pro --effort high

# 工具层直接调用（不经过模型）
python3 -m biliagent.cli search "泠鸢 泼墨漓江"
python3 -m biliagent.cli info BV1Ds411h7T1
python3 -m biliagent.cli download BV1Ds411h7T1 --audio mp3

# 环境查询
python3 -m biliagent.cli caps               # 外部工具能力
python3 -m biliagent.cli cfg                # 配置来源（密钥脱敏）
python3 -m biliagent.cli guardrails         # 护栏与合规约定

# 验证
python3 -m biliagent.cli agent --fake       # 使用内置脚本，不消耗额度
bash scripts/test_all.sh --no-real          # 离线测试
bash scripts/test_all.sh                    # 含真实 API 冒烟测试
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/README.md`](docs/README.md) | 文档索引与阅读路径 |
| [`docs/USAGE.md`](docs/USAGE.md) | 使用手册：安装、配置、使用、错误处理 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 设计文档：接口设计、协议、会话层、循环、配置 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构说明：分层、数据流、职责边界、关键决策 |
| [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) | 代码说明与接口参考 |
| [`docs/WORKFLOW.md`](docs/WORKFLOW.md) | 执行实例：消息级记录与分支处理 |
| [`docs/FACTS.md`](docs/FACTS.md) | 外部接口的实测结论与未验证项 |

---

## 测试

| 文件 | 项数 | 覆盖范围 |
|---|---:|---|
| `tests/test_env.py` | 55 | 配置解析、优先级、脱敏 |
| `tests/test_tools.py` | 41 | B 站接口、外部工具封装、媒体管线 |
| `tests/test_loop.py` | 116 | 协议、指令校验、循环控制、上下文压缩、端到端 |
| `tests/test_llm.py` | 39 | 模型适配错误分支、真实 API 冒烟 |

真实 API 调用由 `--real` 参数控制，默认不执行。

---

## 合规约定

以下约束在代码层实现：

- 仅使用公开接口，不登录、不使用他人凭据、不绕过会员鉴权
- 请求间隔不小于 1 秒，失败重试不超过 2 次并采用指数退避
- 遇 403 / 412 / 429 立即停止，不进行重试
- 产出文件保留在本地，不部署为公共服务，不用于商业用途

本项目仅用于个人学习。下载内容应限于本人投稿、采用开放许可
或符合个人学习用途的范围。

---

## 密钥处理

- 代码中不含硬编码密钥，仅从 `.env` 文件或环境变量读取
- `.env` 已在 `.gitignore` 中排除；`.env.example` 仅含占位符
- 配置展示命令对密钥做脱敏处理
- 模型适配层在输出错误前移除文本中的密钥

---

## 已知限制

| 项 | 说明 |
|---|---|
| 画质 / 音质 | 游客身份下画质上限 360P；当前实现使用 html5 变体所含音轨 |
| 签名 URL | 有时效，实现上仅持久化 `bvid` / `cid` |
| 频率边界 | 未测试风控的触发阈值 |
| 长会话 | 稳定性验证至 6 轮 |
| 流式输出 | 未实现 |
| 并发 | 单会话串行执行 |

详细列表见 `docs/ARCHITECTURE.md` §11 与 `docs/FACTS.md` §7。
