#!/usr/bin/env python3
"""minter 的 **render 超时预算**门禁（报告 AVM12-MINT）。

实测现象：冷 profile 起一次性 minter 实例，`/healthz` 10 秒即 `ready=true`，但
`POST /v1/turnstile/mint` 两次都返回 **503 + `{'why': 'TIMEOUT'}（耗时 46.01s）`**。
根因不是守卫、不是网络、也不是新镜像的功能回归 —— 而是**冷启动**：
`tools/turnstile_minter.py` 里页面内的 `setTimeout(..., 45000)` 是 45 秒超时，而项目自己在
`Dockerfile.minter` 写着「首次 render 有 ~46 秒」⇒ **新部署的第一次铸造必然失败一次**。

修法：预算可配（`MINT_TIMEOUT_MS` / `COLD_MINT_TIMEOUT_MS`），**首轮**用冷预算、
之后回落常规值；且超时后用冷预算**重试一次**（那一轮之后页面已经热了）。

运行：python3 tests/test_minter_render_budget.py
"""

import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "src"))

import turnstile_minter as M  # noqa: E402
import turnstile_service as S  # noqa: E402

_BUDGET_RE = re.compile(r"why:'TIMEOUT'\}\), (\d+)\)")


def budgets_in(expression: str) -> int:
    return int(_BUDGET_RE.search(expression or "").group(1))


class FakeCDP:
    """假 CDP：PROBE 立即就绪；render 依次返回给定结果。"""

    def __init__(self, renders):
        self.renders = list(renders)
        self.expressions = []

    def call(self, method, session=None, **params):
        expr = params.get("expression")
        self.expressions.append(expr)
        if expr == M.PROBE:
            return {"result": {"result": {"value": "object|complete|https://site/robots.txt"}}}
        return {"result": {"result": {"value": self.renders.pop(0)}}}

    def render_budgets(self):
        return [budgets_in(e) for e in self.expressions if e and "setTimeout" in e]


class TestBudgetIsConfigurable(unittest.TestCase):
    def test_defaults_match_the_documented_values(self):
        self.assertEqual(M.MINT_TIMEOUT_MS, 45000)
        self.assertEqual(M.COLD_MINT_TIMEOUT_MS, 120000)

    def test_cold_budget_is_wider_than_the_normal_one(self):
        """冷预算若不比常规宽，这条修复就是空的 —— 46s 的首次 render 照样会超时。"""
        self.assertGreater(M.COLD_MINT_TIMEOUT_MS, M.MINT_TIMEOUT_MS)

    def test_the_budget_is_injected_into_the_render_script(self):
        self.assertEqual(budgets_in(M.mint_js(90000)), 90000)
        self.assertEqual(budgets_in(M.MINT_JS), M.MINT_TIMEOUT_MS)

    def test_budgets_are_overridable_from_the_environment(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, r'%s'); import turnstile_minter as M;"
             "print(M.MINT_TIMEOUT_MS, M.COLD_MINT_TIMEOUT_MS)" % TOOLS],
            capture_output=True, text=True,
            env={**os.environ, "MINT_TIMEOUT_MS": "1000", "COLD_MINT_TIMEOUT_MS": "2000"},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "1000 2000")


class TestTimeoutRetriesWithTheColdBudget(unittest.TestCase):
    """★ 首轮 45s 超时不再等于"铸造失败"：页面已经热了，用冷预算再试一次。"""

    def test_timeout_then_success_uses_both_budgets(self):
        bc = FakeCDP([{"ok": False, "why": "TIMEOUT"}, {"ok": True, "token": "tok-1"}])
        tok, _ = M.mint_on(bc, "sess", 1, timeout_ms=M.MINT_TIMEOUT_MS)
        self.assertEqual(tok, "tok-1")
        self.assertEqual(bc.render_budgets(), [M.MINT_TIMEOUT_MS, M.COLD_MINT_TIMEOUT_MS])

    def test_non_timeout_failure_does_not_retry(self):
        """`no turnstile api` / `ERR …` 这类失败重试没有意义，别白等一轮。"""
        bc = FakeCDP([{"ok": False, "why": "ERR 110200"}])
        tok, _ = M.mint_on(bc, "sess", 1, timeout_ms=M.MINT_TIMEOUT_MS)
        self.assertIsNone(tok)
        self.assertEqual(bc.render_budgets(), [M.MINT_TIMEOUT_MS])

    def test_an_explicit_cold_budget_is_not_retried_again(self):
        """已经在用最宽预算了 ⇒ 不能再重试（否则一次真卡死会等两倍时间）。"""
        bc = FakeCDP([{"ok": False, "why": "TIMEOUT"}, {"ok": True, "token": "x"}])
        tok, _ = M.mint_on(bc, "sess", 1, timeout_ms=M.COLD_MINT_TIMEOUT_MS)
        self.assertIsNone(tok)
        self.assertEqual(bc.render_budgets(), [M.COLD_MINT_TIMEOUT_MS])


class TestServiceUsesTheColdBudgetOnlyForTheFirstMint(unittest.TestCase):
    def _core(self):
        core = S.MintCore()
        core.ensure = lambda: None            # 不真起 Chrome
        core.bc, core.page = object(), ("tid", "sess")
        return core

    def test_first_mint_is_cold_and_later_ones_are_normal(self):
        core = self._core()
        seen = []

        def fake_mint_on(bc, sess, i, timeout_ms=None):
            seen.append(timeout_ms)
            return "tok", 0.01

        with mock.patch.object(S.M, "mint_on", fake_mint_on):
            core.mint()
            core.mint()
            core.mint()
        self.assertEqual(
            seen, [M.COLD_MINT_TIMEOUT_MS, M.MINT_TIMEOUT_MS, M.MINT_TIMEOUT_MS],
            "只有**首轮**该用冷预算：常态放宽会把一次真卡死的等待从 45s 拉成 120s",
        )
        self.assertTrue(core.warmed)

    def test_a_failed_first_mint_does_not_mark_the_page_as_warmed(self):
        core = self._core()
        with mock.patch.object(S.M, "mint_on", lambda *a, **k: (None, 0.01)):
            with self.assertRaises(RuntimeError):
                core.mint()
        self.assertFalse(core.warmed, "没铸出 token 就不能算热 —— 否则冷启动会被漏掉")

    def test_health_reports_readiness_and_warmth_separately(self):
        """`ready`（Chrome 就绪）与 `warmed`（真的铸出过）是两件事，必须分开可见。"""
        h = S.health()
        self.assertIn("warmed", h)
        self.assertIn("render_timeout_ms", h)
        self.assertEqual(h["render_timeout_ms"],
                         {"cold": M.COLD_MINT_TIMEOUT_MS, "normal": M.MINT_TIMEOUT_MS})


if __name__ == "__main__":
    unittest.main(verbosity=2)
