#!/usr/bin/env python3
"""`logfire.instrument_*` 用到的 extra 必须**显式声明**，且在本机真的能 import。

为什么单开一个门禁 —— 这类坑本项目已经踩过**两次**，而且两次的症状都不在"能不能起服务"上：

  · 2026-09-14：`logfire>=3.0` 没带 `[fastapi]` ⇒ `instrument_fastapi` 抛 RuntimeError，
    被 `setup_observability` 的 try/except 吞成一条警告 ⇒ **每个请求的自动 span 静默消失**
    （容器日志里每人 2 行，看不出后果）。
  · 2026-09-16：**同一类事在 `instrument_httpx` 上重演** ⇒ 上游调用没有子 span，
    "网关慢还是上游慢"分不出来。CI（干净环境）由 `tests/test_poll_suppression.py`
    **裸调** `instrument_httpx` 才炸出来（4 个 error）—— 而**本机恰好装着 ⇒ 本地全绿**。

⇒ 只盯住"某个测试恰好裸调它"是靠不住的。这里改成**机械发现**所有 `logfire.instrument_*` 调用，
逐个要求：① 在映射表里登记；② 它的 extra 出现在 `requirements.txt` 的 `logfire[...]` 里；
③ 对应模块在**当前环境**真能 import（本地缺了当场红，不等 CI）。

运行：python3 -m unittest discover -s tests
"""

import re
import sys
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# instrument 名 → (logfire extra, 提供它的模块)。**未登记的 name 会让门禁红** ——
# 这是刻意的：新增一处埋点时必须顺手确认它的 extra 谁来装，而不是等干净环境来发现。
_KNOWN = {
    "fastapi": ("fastapi", "opentelemetry.instrumentation.fastapi"),
    "httpx": ("httpx", "opentelemetry.instrumentation.httpx"),
    "sqlite3": ("sqlite3", "opentelemetry.instrumentation.sqlite3"),
    "pydantic_ai": ("pydantic-ai", "opentelemetry.instrumentation.pydantic_ai"),
    "starlette": ("starlette", "opentelemetry.instrumentation.starlette"),
    "logging": ("logging", "opentelemetry.instrumentation.logging"),
}

CALL_RE = re.compile(r"logfire\.instrument_(\w+)\s*\(")
LOGFIRE_REQ_RE = re.compile(r"^logfire\[([^\]]+)\]", re.M)


def instrumented_names() -> dict:
    """扫源码里实际调用的 instrument 名 → 命中的文件集合。"""
    found: dict = {}
    for path in sorted((ROOT / "src/ark_compat").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in CALL_RE.findall(text):
            found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


class TestLogfireExtrasDeclared(unittest.TestCase):
    def test_every_instrumented_name_is_registered_in_the_map(self):
        """机械发现：源码里用到的每个 instrument 名都要在 `_KNOWN` 里有条目。

        否则新加一处埋点会**悄悄**漏掉 extra 声明 —— 而它的失败形态是"追踪少一层"，
        没有任何测试会红（本次就是被一个**裸调**的测试偶然炸出来的）。
        """
        unknown = {n: sorted(files) for n, files in instrumented_names().items() if n not in _KNOWN}
        self.assertEqual(
            unknown, {},
            f"这些 logfire.instrument_* 未在 _KNOWN 登记（补上它对应的 extra 与模块）：{unknown}",
        )

    def test_requirements_declare_the_needed_extras(self):
        """`requirements.txt` 的 `logfire[...]` 必须覆盖所有用到的 extra。"""
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        m = LOGFIRE_REQ_RE.search(req)
        self.assertIsNotNone(m, "requirements.txt 里找不到 `logfire[...]` 声明行")
        declared = {e.strip() for e in m.group(1).split(",") if e.strip()}
        need = {_KNOWN[n][0] for n in instrumented_names() if n in _KNOWN}
        self.assertTrue(
            need <= declared,
            f"缺 extra：{sorted(need - declared)}（已声明 {sorted(declared)}）——"
            f"缺了它 instrument_* 会抛 RuntimeError 并被 try/except 吞掉，追踪链路上静默少一层",
        )

    def test_the_modules_are_actually_importable_here(self):
        """本机也要真的能 import —— 否则"恰好装着"的差异会让本地全绿、CI 红。"""
        missing = []
        for name in instrumented_names():
            entry = _KNOWN.get(name)
            if not entry:
                continue
            if importlib.util.find_spec(entry[1]) is None:
                missing.append((name, entry[1]))
        self.assertEqual(
            missing, [],
            f"本机缺这些模块 ⇒ 与 CI/镜像行为不一致：{missing}；"
            f"`pip install -r requirements.txt` 后重跑",
        )


if __name__ == "__main__":
    unittest.main()
