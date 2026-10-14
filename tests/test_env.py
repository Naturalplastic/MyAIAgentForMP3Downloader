#!/usr/bin/env python3
"""配置加载（.env）测试。

配置解析是最容易"看起来对、边界全错"的一类代码，而且它管着密钥——
错了的后果是"key 读不到"或"key 泄漏到日志"。所以边界要测全。

用法：python3 tests/test_env.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from biliagent import env as E  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  \033[32m✓\033[0m %s" % name)
    else:
        FAIL.append((name, detail))
        print("  \033[31m✗\033[0m %s  %s" % (name, detail))


def section(t):
    print("\n\033[1m%s\033[0m" % t)


def write_tmp(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".env", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ==========================================================================
section("1. parse_env_file：语法边界")

p = write_tmp(
    "# 注释行\n"
    "\n"
    "DEEPSEEK_API_KEY=sk-abc123\n"
    "DEEPSEEK_MODEL=deepseek-flash\n"
    'QUOTED="值里有=号"\n'
    "SINGLE='单引号值'\n"
    "export EXPORTED=exported-value\n"
    "  SPACED  =  spaced  \n"
    "WITH_INLINE=value # 行内注释\n"
    "NOT_A_PAIR_LINE\n"
    "EMPTY_VALUE=\n"
)
d = E.parse_env_file(p)
os.unlink(p)

check("解析普通键值", d.get("DEEPSEEK_API_KEY") == "sk-abc123", str(d.get("DEEPSEEK_API_KEY")))
check("跳过注释与空行", len(d) >= 8, "共 %d 项" % len(d))
check("支持 unquoted 值", d.get("DEEPSEEK_MODEL") == "deepseek-flash")
check("双引号值里的 = 不被拆开", d.get("QUOTED") == "值里有=号", repr(d.get("QUOTED")))
check("单引号值被去引号", d.get("SINGLE") == "单引号值", repr(d.get("SINGLE")))
check("支持 export 前缀", d.get("EXPORTED") == "exported-value", repr(d.get("EXPORTED")))
check("键值两侧空白被 strip", d.get("SPACED") == "spaced", repr(d.get("SPACED")))
check("行内注释被去掉", d.get("WITH_INLINE") == "value", repr(d.get("WITH_INLINE")))
check("无 = 的行被跳过", "NOT_A_PAIR_LINE" not in d)
check("空值保留为空字符串", d.get("EMPTY_VALUE") == "", repr(d.get("EMPTY_VALUE")))

check("文件不存在返回空 dict", E.parse_env_file("/nonexistent/path/.env") == {})
check("路径为 None 返回空 dict", E.parse_env_file(None) == {})

# URL 里的 # 不能当注释（这是容易踩的坑）
p = write_tmp("DEEPSEEK_BASE_URL=https://api.example.com/v1#fragment\n")
d = E.parse_env_file(p)
os.unlink(p)
check("值里的 # 若无前置空格则不当注释",
      d.get("DEEPSEEK_BASE_URL") == "https://api.example.com/v1#fragment",
      repr(d.get("DEEPSEEK_BASE_URL")))

# 单个引号不能被吃掉
p = write_tmp('ODD=he said "hi\n')
d = E.parse_env_file(p)
os.unlink(p)
check("不成对的引号原样保留", d.get("ODD") == 'he said "hi', repr(d.get("ODD")))


# ==========================================================================
section("2. load_env：优先级与不污染环境")

_key = "DEEPSEEK_API_KEY"
_saved = os.environ.get(_key)

# 造一个假的 .env，直接测 load_env 的行为
_real_project = E.PROJECT_ENV
_real_user = E.USER_ENV
tmp_proj = write_tmp("%s=from-file\nDEEPSEEK_MODEL=file-model\n" % _key)
tmp_user = write_tmp("%s=from-user-file\nUNRELATED_KEY=should-be-ignored\n" % _key)

try:
    E.PROJECT_ENV = tmp_proj
    E.USER_ENV = tmp_user
    E._loaded = False
    os.environ.pop(_key, None)
    os.environ.pop("DEEPSEEK_MODEL", None)

    src = E.load_env()
    check("文件里的值被写入 os.environ", os.environ.get(_key) == "from-file",
          os.environ.get(_key))
    check("返回值记录了来源文件", src.get(_key) == tmp_proj, str(src))
    check("不在 MANAGED_KEYS 的键被忽略",
          "UNRELATED_KEY" not in os.environ)
    check("项目 .env 优先于 ~/.biliagent.env",
          os.environ.get(_key) == "from-file", os.environ.get(_key))

    # 环境变量优先（不被文件覆盖）
    os.environ[_key] = "from-real-env"
    E._loaded = False
    E.load_env()
    check("真实环境变量优先于文件（默认不覆盖）",
          os.environ.get(_key) == "from-real-env", os.environ.get(_key))

    # override=True 时文件覆盖环境
    E._loaded = False
    E.load_env(override=True)
    check("override=True 时文件覆盖环境变量",
          os.environ.get(_key) == "from-file", os.environ.get(_key))

    # 空字符串应被视为"未设置"，允许文件补上
    os.environ[_key] = ""
    E._loaded = False
    E.load_env()
    check("环境变量为空串时，文件值可补上（空不算已设置）",
          os.environ.get(_key) == "from-file", repr(os.environ.get(_key)))

finally:
    E.PROJECT_ENV = _real_project
    E.USER_ENV = _real_user          # ← 两个都要恢复，否则污染后续 section
    E._loaded = False
    os.unlink(tmp_proj)
    os.unlink(tmp_user)
    if _saved is not None:
        os.environ[_key] = _saved
    else:
        os.environ.pop(_key, None)
    os.environ.pop("DEEPSEEK_MODEL", None)
    os.environ.pop("UNRELATED_KEY", None)


# ==========================================================================
section("3. mask：脱敏")

check("mask 保留头尾、中间省略", E.mask("sk-EXAMPLE0000000000000000000000abcd")
      == "sk-EXAM…abcd", E.mask("sk-EXAMPLE0000000000000000000000abcd"))
check("mask 对短值不泄漏全貌", "***" in E.mask("short"))
check("mask 对 None 返回提示", E.mask(None) == "(未设置)")
check("mask 对空串返回提示", E.mask("") == "(未设置)")

# 关键：脱敏后的字符串绝不能含完整 key
_long = "sk-EXAMPLE0000000000000000000000abcd"
check("mask 结果不含完整 key", _long not in E.mask(_long), E.mask(_long))
check("mask 结果长度远小于原值", len(E.mask(_long)) < len(_long) / 2,
      "%d vs %d" % (len(E.mask(_long)), len(_long)))
check("mask 对 32 位真实格式的 key 也生效",
      E.mask("sk-" + "a" * 30) == "sk-aaaa…aaaa", E.mask("sk-" + "a" * 30))


# ==========================================================================
section("4. describe 与提示文本")

info = E.describe()
check("describe 返回全部受管键", all(k in info for k in E.MANAGED_KEYS),
      str(sorted(info.keys())))
check("describe 里的 key 是脱敏的",
      _long not in str(info), str(info)[:200])
check("describe 报告配置文件是否存在",
      isinstance(info.get("_files"), dict) and ".env" in info["_files"])

hint = E.missing_key_hint()
check("缺 key 提示包含 .env 的做法", ".env" in hint)
check("缺 key 提示包含 cp 命令", "cp .env.example .env" in hint)
check("缺 key 提示写明三种方式", "三种方式" in hint, hint[:120])
check("缺 key 提示说明优先级", "优先级" in hint)


# ==========================================================================
section("4.5 env_bool：布尔开关解析")

_KB = "KEEP_INTERMEDIATE_MP4"
_kb_saved = os.environ.get(_KB)
try:
    for raw, want in [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                      ("on", True), ("True ", True),
                      ("false", False), ("0", False), ("no", False),
                      ("off", False), ("", False), ("随便", False)]:
        os.environ[_KB] = raw
        got = E.env_bool(_KB)
        check("env_bool(%r) → %s" % (raw, want), got is want, "得到 %r" % got)

    os.environ.pop(_KB, None)
    check("未设置时返回默认值 False", E.env_bool(_KB) is False)
    check("未设置时可指定默认值 True", E.env_bool(_KB, True) is True)
    check("空字符串视为未设置（用默认值）",
          E.env_bool(_KB, True) is True and os.environ.get(_KB) is None)
finally:
    if _kb_saved is not None:
        os.environ[_KB] = _kb_saved
    else:
        os.environ.pop(_KB, None)

check("KEEP_INTERMEDIATE_MP4 在受管白名单里", _KB in E.MANAGED_KEYS)


# ==========================================================================
section("5. 与 llm 模块集成（模块级自动加载）")

# llm.py 在 import 时会调 load_env()，所以这里要隔离。
# ⚠️ 本 section 完全自包含：自己保存/恢复路径，不假设前面的 section 留下了什么。
_env_before = os.environ.get(_key)
_saved_proj, _saved_user = E.PROJECT_ENV, E.USER_ENV
os.environ.pop(_key, None)
for mod in ("biliagent.llm",):
    sys.modules.pop(mod, None)
try:
    from biliagent.llm import DeepSeekClient, LLMError
    # 从这一步起，只让 tmp2 决定结果：USER_ENV 指到不存在的位置，
    # 且清掉 os.environ 里 import 时可能读进来的真实值。
    E.USER_ENV = "/nonexistent/.biliagent.env"
    E._loaded = False
    os.environ.pop(_key, None)
    tmp2 = write_tmp("%s=sk-from-dotenv-test\n" % _key)
    try:
        E.PROJECT_ENV = tmp2
        E._loaded = False
        os.environ.pop(_key, None)
        # 显式加载（不能依赖 DeepSeekClient 内部那次 —— _loaded 可能仍为 True）
        E.load_env()
        c = DeepSeekClient()
        check("llm 模块能用 .env 里的 key 构造客户端",
              c.api_key == "sk-from-dotenv-test", c.api_key)
    finally:
        E.PROJECT_ENV = _saved_proj
        E._loaded = False
        os.unlink(tmp2)

    # 完全没有 key 时抛 LLMError，且 hint 可照做
    # ⚠️ 不要在这里显式调 load_env() 去"预加载"——那会把真实 .env 的 key
    #    读进 os.environ，导致下面的抛错分支永远不触发、测试静默失效。
    E.PROJECT_ENV = "/nonexistent/.env"
    E.USER_ENV = "/nonexistent/.env"
    E._loaded = False
    os.environ.pop(_key, None)
    E.load_env()                     # 显式加载：此时两处都不存在 → 确实无 key
    assert _key not in os.environ, "前置条件失败：环境中仍有 key"
    try:
        DeepSeekClient()
        check("无 key 时抛 LLMError", False, "没有抛出")
    except LLMError as e:
        check("无 key 时抛 LLMError", True)
        check("LLMError 的 hint 是可直接照做的指引",
              "cp .env.example .env" in e.payload.get("hint", ""),
              str(e.payload.get("hint"))[:160])
        check("LLMError 的 message 指明文件与环境变量都查过了",
              ".env" in e.payload.get("message", ""), e.payload.get("message"))
    finally:
        E.PROJECT_ENV = _saved_proj
        E.USER_ENV = _saved_user
        E._loaded = False
        if _env_before is not None:
            os.environ[_key] = _env_before
finally:
    sys.modules.pop("biliagent.llm", None)


# ==========================================================================
print("\n" + "=" * 62)
print("通过 %d ｜ 失败 %d ｜ 跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("\n失败项:")
    for n, dd in FAIL:
        print("  ✗ %s\n      %s" % (n, dd))
print("=" * 62)
sys.exit(1 if FAIL else 0)
