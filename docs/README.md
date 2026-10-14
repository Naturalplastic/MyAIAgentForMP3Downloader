# 文档索引

B 站媒体下载 Agent —— 项目文档。

---

## 文档列表

| 文档 | 类型 | 内容 |
|---|---|---|
| [`USAGE.md`](USAGE.md) | 使用手册 | 环境要求、安装、配置、验证、使用方式、错误处理、部署 |
| [`DESIGN.md`](DESIGN.md) | 设计文档 | 概述、架构、设计原则、接口设计、会话层、循环、协议、上下文、配置、合规、测试 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构说明 | 系统边界、分层结构、数据流、职责边界、不可靠性处理、关键决策 |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | 代码参考 | 逐模块说明、函数签名、参数与返回值、变更指引 |
| [`WORKFLOW.md`](WORKFLOW.md) | 执行实例 | 一次完整运行的消息级记录、函数调用序列、分支处理 |
| [`FACTS.md`](FACTS.md) | 接口参考 | 外部接口的实测结论、约束、陷阱、性能基线、未验证项 |

---

## 阅读路径

### 首次使用

```
USAGE.md §1–4     环境要求 → 安装 → 配置 → 验证
USAGE.md §5       使用方式
```

### 了解设计

```
DESIGN.md §1      概述与设计约束
ARCHITECTURE.md §2–4   分层、数据流、职责边界
DESIGN.md §4–9    接口与协议细节
```

### 了解代码

```
WALKTHROUGH.md §1      模块总览与分层职责
WALKTHROUGH.md §2–11   逐模块说明
WALKTHROUGH.md §12     函数索引与变更指引
```

### 理解实际执行过程

```
WORKFLOW.md §2–3   单次运行的流程与完整消息记录
WORKFLOW.md §7     各分支的处理方式
```

### 排查外部接口问题

```
FACTS.md §1        接口速览
FACTS.md §3        已知陷阱
FACTS.md §7        未验证项
```

---

## 项目结构

```
MyMP3Download/
├── .env                    本地配置（含密钥，不入版本控制）
├── .env.example            配置模板
├── .gitignore
├── README.md
├── biliagent/              源代码
│   ├── __init__.py
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
│   └── archive/            历史版本设计稿
├── downloads/              输出目录（不入版本控制）
├── logs/                   运行日志（不入版本控制）
├── scripts/
│   ├── setup_tools.sh      获取便携 ffmpeg
│   └── test_all.sh         全量测试
├── tests/
│   ├── test_env.py
│   ├── test_llm.py
│   ├── test_loop.py
│   └── test_tools.py
└── .tools/                 便携外部工具（不入版本控制）
                            由 scripts/setup_tools.sh 获取
```

---

## 术语

| 术语 | 含义 |
|---|---|
| harness | 本项目实现的代理框架：循环、协议解析、工具调度、护栏 |
| action | 模型请求执行的操作，取值见 `protocol.TOOL_SPECS` |
| 标记（marker） | 模型输出中的 `<tool_call>` 与 `<final_answer>` |
| 观察（observation） | 工具执行结果经压缩后回传给模型的内容 |
| 指令（directive） | 由 `<tool_call>` 解析并校验后的结构化对象 |
| 护栏（guardrail） | 由代码强制执行的约束，取值见 `config.GUARDRAILS` |
| 能力探测 | 检测外部工具可用性的过程，见 `media.probe_capabilities()` |
| 中间产物 | 音频抽取所需的源 mp4 文件 |
| 相关性命中（matched） | 检索结果中实际包含关键词的条目数 |

---

## 文档约定

### 行号标注

代码引用格式为 `file.py:NNN` 或 `file.py:NNN-MMM`，表示文件中的行或行区间。
行号对应代码版本 1.0。代码变更后行号可能偏移，结构关系保持稳定。

### 代码块标注

文档中的 Python 代码块有两种来源：

- 摘自实际代码，标注了对应的文件与行号
- 用于说明结构或行为的简化示例

两者在上下文中区分。

### 数据来源

`WORKFLOW.md` 中的执行记录来自 `logs/cli_20261015.jsonl`，
为真实运行的输出，未做删改。`FACTS.md` 中的数据来自直接调用接口的测量结果。

### 未验证项

各文档设有未验证项章节，列出未经过测试的结论或行为。
这些条目不应被视为已确认的事实。

---

## 版本

| 文档版本 | 日期 | 说明 |
|---|---|---|
| 1.0 | 2026-10-15 | 文档重构：统一为规范风格，新增执行实例文档 |

历史设计稿（`DESIGN_v0.2.md`、`DESIGN_v0.3.md`）保留于 `archive/`，
记录了接口事实的发现过程与设计演进，内容已整合进 `DESIGN.md`。
