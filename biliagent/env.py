"""配置文件加载（.env）。

优先级（从高到低）：
  1. 真实环境变量           ← 临时覆盖、CI 注入用
  2. 项目根的 .env          ← 日常使用放这里
  3. ~/.biliagent.env       ← 想跨项目共用时用这个

即"环境变量优先"，这样 `DEEPSEEK_API_KEY=xxx python3 -m biliagent.cli ...`
可以临时覆盖文件里的值，而不用改文件。

## 为什么自己做而不装 python-dotenv

本项目坚持**零第三方依赖**。`.env` 的语法在"够用"的范围内很简单，
五十行就能覆盖：`KEY=value`、引号、`export` 前缀、`#` 注释。

## 安全说明（重要）

`.env` 里是**明文 key**。这是用便利换安全的一步，代价必须显式承担：

  · `.gitignore` 已排除 `.env`，**绝不能提交到仓库**
  · 提交前请 `git status` 确认它不在列表里
  · 文件权限建议设为 600（只有自己能读）
  · 一旦怀疑泄漏，立刻去 DeepSeek 控制台轮换

如果想让 key 不落在项目目录里，用 `~/.biliagent.env`（在项目外）。
"""

from __future__ import annotations

import os

# 项目根目录（biliagent/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROJECT_ENV = os.path.join(_ROOT, ".env")
USER_ENV = os.path.join(os.path.expanduser("~"), ".biliagent.env")
EXAMPLE_ENV = os.path.join(_ROOT, ".env.example")

# 本模块负责管理的键。只有这些会从文件读进 os.environ，
# 避免 .env 里放个无关变量就把运行环境改脏了。
MANAGED_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "BILIAGENT_FFMPEG",
    "BILIAGENT_FFPROBE",
    "BILI_SESSDATA",
    "KEEP_INTERMEDIATE_MP4",
)


def env_bool(name: str, default: bool = False) -> bool:
    """从环境变量读布尔值。

    接受 true/1/yes/on（不分大小写）为真；其余为假。
    值不在白名单也无所谓 —— 只要调用方拿它当布尔用。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

_loaded = False


def parse_env_file(path: str) -> dict:
    """解析一个 .env 文件，返回 {key: value}。文件不存在返回 {}。

    支持的写法：
        KEY=value
        KEY="quoted value"
        KEY='single quoted'
        export KEY=value
        # 注释行
        （空行）
    不支持多行值和变量插值 —— 用不上，不值得增加复杂度。
    """
    out: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out

    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {}

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # 去引号（成对才去，避免把值里的单个引号吃掉）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        # 行内注释：只有 " #" 才当注释，避免把 URL 里的 # 当注释
        if " #" in value and not value.startswith("#"):
            value = value.split(" #", 1)[0].rstrip()

        if key:
            out[key] = value
    return out


def load_env(override: bool = False, verbose: bool = False) -> dict:
    """把配置文件里的值灌进 os.environ。

    override=False（默认）：**不覆盖**已存在的环境变量 —— 真实环境变量优先。
    override=True：文件值覆盖环境变量（一般不用）。

    返回 {key: 来源文件路径}，便于诊断"这个值是从哪来的"。
    """
    global _loaded
    if _loaded and not override and not verbose:
        return {}

    sources: dict[str, str] = {}
    # 顺序说明（两种情况都要正确）：
    #   override=False：先加载的赢（因为后来的不覆盖已存在的）
    #                   → 项目 .env 必须先加载，才能压过 ~/.biliagent.env
    #   override=True ：后加载的赢（因为后来的无条件覆盖）
    #                   → 项目 .env 必须后加载
    # 所以倒序遍历 (USER_ENV, PROJECT_ENV)：
    #   override=False → 项目先读并先占位 ✅
    #   override=True  → 项目最后写，覆盖掉用户级 ✅
    for path in ((USER_ENV, PROJECT_ENV) if override else (PROJECT_ENV, USER_ENV)):
        values = parse_env_file(path)
        for key, value in values.items():
            if key not in MANAGED_KEYS:
                continue
            if override or key not in os.environ or not os.environ.get(key):
                os.environ[key] = value
                sources[key] = path

    _loaded = True
    if verbose:
        for key, path in sources.items():
            print("  · %s ← %s" % (key, path))
    return sources


def mask(value: str | None) -> str:
    """把 key 变成可安全打印的形式：sk-49cbc…4af。"""
    if not value:
        return "(未设置)"
    if len(value) <= 12:
        return value[:2] + "***"
    return "%s…%s" % (value[:7], value[-4:])


def describe() -> dict:
    """当前配置状态，供 CLI 展示。"""
    load_env()
    info = {k: mask(os.environ.get(k)) for k in MANAGED_KEYS}
    info["_files"] = {
        ".env": os.path.isfile(PROJECT_ENV),
        "~/.biliagent.env": os.path.isfile(USER_ENV),
    }
    return info


def missing_key_hint() -> str:
    """缺少 API key 时给出的、可照做的指引。"""
    lines = [
        "没有找到 DEEPSEEK_API_KEY。以下三种方式任选其一：",
        "",
        "  方式一（推荐）：在项目根目录建 .env 文件",
        "      cp .env.example .env",
        "      # 然后编辑 .env，填入你的 key",
        "",
        "  方式二：临时环境变量（只对当前终端有效）",
        "      export DEEPSEEK_API_KEY=sk-...",
        "",
        "  方式三：放在项目外（不想让 key 落在项目目录里）",
        "      umask 077 && echo 'DEEPSEEK_API_KEY=sk-...' > ~/.biliagent.env",
        "",
        "优先级：真实环境变量 > 项目 .env > ~/.biliagent.env",
    ]
    return "\n".join(lines)
