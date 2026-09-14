#!/usr/bin/env python3
"""铸造 render 的**状态可观测性**门禁（E2E-AVM-004 F8 定位后的收口）。

背景：E2E-AVM-004 证伪了「冷启动 ≈46s 慢成功」模型 —— 全新 profile 的首铸在 120s
冷预算下两次精确压线失败（卡死，不是慢），疑似 CF 对低信誉 profile 下发交互式挑战。
此前 render 的求值结果只有一行 `{'why': 'TIMEOUT'}`，冷启动与真卡死完全无法区分。

本轮起 fixed 的契约：
  * MINT_JS 必须注册 `before-interactive-callback`（官方参数，挑战进入交互模式前触发）
    —— 交互式在宽限期内没自动完成就**快速失败**，不再吃满预算；
  * render 结果必须带**页面内状态采样**（iframe / getResponse / interactive / samples），
    超时与失败都要报告"停在了哪个阶段"；
  * 每轮 render 前必须清掉上一轮的半成品 widget（失败不再重启浏览器 ⇒ 防累积）；
  * `failure_reason()` 把求值结果归类成稳定短标签，服务层进 /healthz 的 last_failure；
  * 服务层铸造失败**保留浏览器**只换热页面（reset 不改变 CF 对 profile 的判定）；
  * 补货退避按连续失败**指数收敛**，不许退回固定短周期。

全部离线：不真起 Chrome、不发真实挑战（mock CDP / mock mint_on）。
运行：python3 tests/test_turnstile_render_states.py
"""

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "src"))

import turnstile_minter as M  # noqa: E402
import turnstile_service as S  # noqa: E402


class TestRenderScriptHasStateHooks(unittest.TestCase):
    def test_before_interactive_callback_is_registered(self):
        """官方参数（CF 文档：挑战进入交互模式前触发）—— 快速失败的信号源，不能少。"""
        self.assertIn("'before-interactive-callback'", M.MINT_JS_TMPL)

    def test_state_sampling_is_present(self):
        for anchor in ("challenges.cloudflare.com", "getResponse", "state.samples"):
            self.assertIn(anchor, M.MINT_JS_TMPL)

    def test_stale_widgets_are_cleaned_up(self):
        """失败不再重启浏览器 ⇒ 每轮 render 前必须清掉上轮宿主 div（防累积）。"""
        self.assertGreaterEqual(M.MINT_JS_TMPL.count("data-avm-ts-host"), 2)

    def test_budget_and_grace_are_injected(self):
        js = M.mint_js(12345, 6789)
        self.assertIn("fail('TIMEOUT'), 12345", js)
        self.assertIn("fail('INTERACTIVE'), 6789", js)
        self.assertIn(M.SITEKEY, js)

    def test_grace_defaults_to_the_module_constant(self):
        js = M.mint_js(45000)
        self.assertIn(f"fail('INTERACTIVE'), {M.INTERACTIVE_GRACE_MS}", js)


class TestFailureReasonClassification(unittest.TestCase):
    def test_each_shape_maps_to_its_label(self):
        cases = [
            ({"ok": True, "token": "t"}, "ok"),
            ({"ok": False, "why": "TIMEOUT", "state": {}}, "timeout"),
            ({"ok": False, "why": "INTERACTIVE", "state": {"interactive": True}}, "interactive"),
            ({"ok": False, "why": "ERR 110200"}, "error"),
            ({"ok": False, "why": "EX TypeError"}, "exception"),
            ({"ok": False, "why": "奇怪的东西"}, "unknown:奇怪的东西"),
            (None, "no-result"),
            ("garbage", "no-result"),
        ]
        for val, expected in cases:
            self.assertEqual(M.failure_reason(val), expected, val)


class TestServiceKeepsTheBrowserOnFailure(unittest.TestCase):
    """★ 铸造失败后**不整体 reset**：杀 Chrome 不改变 CF 对 profile 的判定，
    只会白付一次冷启动（E2E-AVM-004：失败 ⇒ reset ⇒ ~2.5min 周期无限杀重启）。"""

    def setUp(self):
        # _STATE 是模块级全局：逐项存照，测完还原，避免跨用例污染
        self._snapshot = dict(S._STATE)
        self._snapshot_stats = dict(S._STATE["stats"])

    def tearDown(self):
        S._STATE.clear()
        S._STATE.update(self._snapshot)
        S._STATE["stats"].clear()
        S._STATE["stats"].update(self._snapshot_stats)

    def _core_with_fakes(self, mint_on_results, open_pages):
        core = S.MintCore()
        core.ensure = lambda: None                     # 不真起 Chrome
        core.bc = object()                             # 真实 reload_page 会用它；失败即降级 reset
        pages = iter(open_pages)
        core.page = next(pages)
        calls = {"open": 0}

        def fake_open_warm_page(bc):
            calls["open"] += 1
            return next(pages)

        seq = iter(mint_on_results)

        def fake_mint_on(bc, sess, i, timeout_ms=None, grace_ms=None):
            return next(seq)

        return core, calls, fake_open_warm_page, fake_mint_on

    def test_interactive_failure_keeps_browser_and_reloads_page(self):
        core, calls, fake_open, fake_mint = self._core_with_fakes(
            mint_on_results=[(None, 0.1, {"ok": False, "why": "INTERACTIVE",
                                           "state": {"interactive": True, "samples": 4}})],
            open_pages=[("tid-0", "sess-0"), ("tid-1", "sess-1")],
        )
        with mock.patch.object(S.M, "open_warm_page", fake_open), \
                mock.patch.object(S.M, "mint_on", fake_mint):
            with self.assertRaises(RuntimeError) as ctx:
                core.mint()
        self.assertIn("interactive", str(ctx.exception))
        # ★ 关键断言：浏览器还在（没有被 reset 成 None），页面换成了新的
        self.assertIsNotNone(core.bc)
        self.assertEqual(core.page, ("tid-1", "sess-1"))
        self.assertEqual(calls["open"], 1)
        self.assertFalse(core.warmed)
        # 失败证据进了 /healthz
        self.assertEqual(S._STATE["last_failure"]["reason"], "interactive")
        self.assertTrue(S._STATE["last_failure"]["state"]["interactive"])

    def test_success_does_not_reload(self):
        core, calls, fake_open, fake_mint = self._core_with_fakes(
            mint_on_results=[("tok-1", 1.7, {"ok": True, "token": "tok-1",
                                              "state": {"response": True}})],
            open_pages=[("tid-0", "sess-0"), ("tid-1", "sess-1")],
        )
        with mock.patch.object(S.M, "open_warm_page", fake_open), \
                mock.patch.object(S.M, "mint_on", fake_mint):
            self.assertEqual(core.mint(), "tok-1")
        self.assertTrue(core.warmed)
        self.assertEqual(calls["open"], 0)
        self.assertEqual(core.page, ("tid-0", "sess-0"))

    def test_reload_failure_falls_back_to_full_reset(self):
        """reload 也失败（CDP 死了之类）⇒ 降级 reset，交给下次 ensure 重启浏览器。"""
        core, calls, _fake_open, fake_mint = self._core_with_fakes(
            mint_on_results=[(None, 0.1, {"ok": False, "why": "TIMEOUT", "state": {}})],
            open_pages=[("tid-0", "sess-0")],
        )

        def broken_open(bc):
            calls["open"] += 1
            raise ConnectionResetError("cdp gone")

        with mock.patch.object(S.M, "open_warm_page", broken_open), \
                mock.patch.object(S.M, "mint_on", fake_mint):
            with self.assertRaises(RuntimeError):
                core.mint()
        self.assertIsNone(core.bc)      # 已整体 reset
        self.assertIsNone(core.page)
        self.assertFalse(S._STATE["ready"])


class TestRefillBackoff(unittest.TestCase):
    def test_first_failure_is_short(self):
        d = S.backoff_delay(1)
        self.assertGreaterEqual(d, 8 * 0.75)
        self.assertLessEqual(d, 8 * 1.25)

    def test_backoff_grows_with_the_streak(self):
        low = min(S.backoff_delay(1) for _ in range(20))
        high = max(S.backoff_delay(4) for _ in range(20))
        self.assertLess(low, high, "连续失败越多，退避区间整体必须越大")

    def test_backoff_is_capped(self):
        # 封顶从连续第 7 次失败开始（8×2⁶=512>300；n=6 时 base 仍是 256，未到顶）
        for n in (8, 12, 50):
            d = S.backoff_delay(n)
            self.assertGreaterEqual(d, 300 * 0.75)
            self.assertLessEqual(d, 300 * 1.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
