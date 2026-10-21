#!/usr/bin/env python3
"""提交**提速**的三条契约（2026-10-21）：

    ① 提前过 CF —— minter 的 `?reserve=N` 自适应预约（不是本地缓存 token）；
    ② 闸门探测的短期缓存（每次提交少一次上游往返）；
    ③ 转存并发按项数 + 转存期间并行预热闸门探测。

为什么这些要写成门禁：

  · **本地缓存 token 是错的**（Turnstile token 单次有效）。唯一正确的"提前过 CF"
    是让铸造服务维持池子，而它的显式 `reserve` 在服务端是**累加**的 ——
    每条都传 N 会把池深顶到上限，等于"只要有流量就永久满池铸造"，主动抬高风控分。
    自适应（pool ⇒ 0 / live ⇒ N）这条属性必须有测试守着。
  · **闸门探测缓存"关"是有风险窗口的**：缓存说要/不要过闸门，TTL 内可能已经翻转。
    TTL 必须短、且"拿到空 taskId"时必须 `force=True` 真探 —— 后者是本文件的核心断言。
  · **预热不能改变行为**：它只是把一次只读探测塞进转存那 1~3 秒里；
    有 BYO token / 关掉缓存时**不该**多打一次上游。

运行：python3 tests/test_submit_speedup.py
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.minter import TokenMinter  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

COOKIE = "auth_session=" + "c" * 40
USER = "u-1"


def ok_body(value):
    return [{"result": {"data": {"json": value}}}]


class CountingTransport(httpx.BaseTransport):
    """记录每个 path 被调了几次；needsCaptcha 由 `gate` 决定。"""

    def __init__(self, gate=False, *, empty_create=False):
        self.gate = gate
        self.empty_create = empty_create
        self.hits: list[str] = []

    def count(self, needle: str) -> int:
        return sum(1 for h in self.hits if needle in h)

    def handle_request(self, request):
        path = request.url.path
        self.hits.append(path)
        if "needsCaptcha" in path:
            return httpx.Response(200, json=ok_body(self.gate))
        if "ai.minimaxH3" in path:
            return httpx.Response(200, json=ok_body("" if self.empty_create else "cgt-1"))
        return httpx.Response(200, json=ok_body({}))


def make_client(transport, **kw) -> WebClient:
    return WebClient(
        cookie=COOKIE, base_url="https://site.test", user_id=USER,
        trust_env=False, transport=transport, submit_retry_backoff=0.0, **kw,
    )


# =====================================================================
# ① 闸门探测缓存
# =====================================================================
class TestCaptchaProbeCache(unittest.TestCase):
    def test_repeated_probe_hits_cache(self):
        t = CountingTransport(gate=False)
        c = make_client(t, captcha_probe_ttl=5.0)
        self.addCleanup(c.close)
        for _ in range(3):
            self.assertFalse(c.needs_captcha())
        self.assertEqual(t.count("needsCaptcha"), 1, "TTL 内只该探一次")

    def test_force_bypasses_cache(self):
        t = CountingTransport(gate=False)
        c = make_client(t, captcha_probe_ttl=60.0)
        self.addCleanup(c.close)
        c.needs_captcha()
        c.needs_captcha(force=True)
        self.assertEqual(t.count("needsCaptcha"), 2, "force 必须真探（空 taskId 兜底靠它）")

    def test_ttl_zero_disables_cache(self):
        t = CountingTransport(gate=False)
        c = make_client(t, captcha_probe_ttl=0.0)
        self.addCleanup(c.close)
        c.needs_captcha()
        c.needs_captcha()
        self.assertEqual(t.count("needsCaptcha"), 2, "0 = 关闭缓存（退回每次真探）")

    def test_cache_expires(self):
        t = CountingTransport(gate=False)
        c = make_client(t, captcha_probe_ttl=0.05)
        self.addCleanup(c.close)
        c.needs_captcha()
        time.sleep(0.06)
        c.needs_captcha()
        self.assertEqual(t.count("needsCaptcha"), 2)

    def test_empty_task_id_forces_fresh_probe(self):
        """空 taskId 之后必须**强制真探** —— 这正是"缓存说不用过、其实翻起"的场景。

        流程：首次探测说"不用过" ⇒ 提交拿到空串 ⇒ 强制真探说"要过" ⇒ 换 token 重试。
        """
        t = CountingTransport(gate=False, empty_create=True)

        class GateFlips(CountingTransport):
            def handle_request(self, request):
                if "needsCaptcha" in request.url.path:
                    self.hits.append(request.url.path)
                    # 第一次说不用过，之后说要用
                    self.gate = self.count("needsCaptcha") > 1
                    return httpx.Response(200, json=ok_body(self.gate))
                return super().handle_request(request)

        ct = GateFlips(empty_create=True)
        c = make_client(ct, captcha_probe_ttl=60.0, submit_retries=0)
        self.addCleanup(c.close)
        m = mock.Mock()
        m.configured = True
        m.last_error = None
        m.mint.return_value = "FRESH"
        c.minter = m
        with self.assertRaises(Exception):
            c.create({"content": "x"})
        self.assertGreaterEqual(ct.count("needsCaptcha"), 2, "空串后必须强制真探，不能信 60s 缓存")
        self.assertEqual(m.mint.call_count, 1, "探到要过闸门才换 token")


# =====================================================================
# ② 提前过 CF：minter 的自适应 reserve
# =====================================================================
class FakeHTTP:
    """记录每次 POST 的 params；按脚本返回 source。"""

    def __init__(self, sources):
        self.sources = list(sources)
        self.requests: list[dict] = []

    def post(self, url, *, params=None, headers=None, **kw):
        self.requests.append({"url": url, "params": dict(params or {})})
        src = self.sources.pop(0) if self.sources else "live"
        return httpx.Response(200, json={"token": f"tok-{len(self.requests)}", "source": src})


class TestMinterReserve(unittest.TestCase):
    def _minter(self, sources, reserve=8):
        m = TokenMinter("http://minter.test", reserve=reserve)
        m._http = FakeHTTP(sources)
        return m

    def test_first_mint_declares_reserve(self):
        m = self._minter(["live"])
        self.assertEqual(m.mint(), "tok-1")
        self.assertEqual(m._http.requests[0]["params"].get("reserve"), 8,
                         "池子空（首次）⇒ 必须声明提前量")

    def test_pool_source_stops_asking(self):
        """拿到 pool **之后的那一次**传 0：显式 reserve 在服务端累加，继续要会把池深顶到上限。

        时序（别按"返回值立刻生效"理解 —— 预约是给**下一次**用的）：
          第 1 次：没历史 ⇒ reserve=8，返回 live
          第 2 次：last_source=live ⇒ reserve=8，返回 pool
          第 3 次：last_source=pool ⇒ reserve=0   ← 这条才是不再追加
        """
        m = self._minter(["live", "pool", "pool"])
        m.mint()
        m.mint()
        m.mint()
        self.assertEqual(m._http.requests[2]["params"].get("reserve"), 0)

    def test_live_again_re_arms(self):
        """池子再次见底（上一次又是 live）⇒ 重新声明提前量。

        序列：live → pool → pool → live → live
        第 4 次请求发出时 last_source 还是 pool（所以它自己传的是 0，并且拿到了 live），
        第 5 次才看到 live ⇒ 重新声明 8。差一拍是设计使然：预约只在**下一次**生效。
        """
        m = self._minter(["live", "pool", "pool", "live", "live"])
        for _ in range(5):
            m.mint()
        self.assertEqual(m._http.requests[4]["params"].get("reserve"), 8,
                         "上次 source=live ⇒ 重新声明提前量")

    def test_reserve_zero_disables(self):
        m = self._minter(["live"], reserve=0)
        m.mint()
        self.assertEqual(m._http.requests[0]["params"].get("reserve"), 0, "0 = 不声明")

    def test_source_is_observable(self):
        m = self._minter(["pool"])
        m.mint()
        self.assertEqual(m.last_source, "pool", "source 必须能被读出（这是池命中率的唯一证据）")


# =====================================================================
# ③ 转存并发 + 转存期间并行预热
# =====================================================================
class TestRehostAndWarmup(unittest.TestCase):
    def _upstream(self, client, workers=7):
        from ark_compat.upstreams import WebUpstream
        from ark_compat.web_queue import WebSubmitQueue

        q = WebSubmitQueue(client, max_concurrent=1)
        return WebUpstream(client, q, media_rehost_budget=60.0, rehost_workers=workers)

    def test_workers_capped_by_item_count(self):
        """4 项只该起 4 个 worker（不是 7），且并发度由配置决定。"""
        t = CountingTransport()
        c = make_client(t)
        self.addCleanup(c.close)
        up = self._upstream(c, workers=7)
        seen: list[int] = []
        real = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor

        class Spy(real):
            def __init__(self, max_workers=None, **kw):
                seen.append(max_workers)
                super().__init__(max_workers=max_workers, **kw)

        with mock.patch("ark_compat.upstreams.ThreadPoolExecutor", Spy):
            with mock.patch.object(up, "_upload_one", side_effect=lambda v, b=None: v):
                up._rehost({"referenceImageUrls": ["u1", "u2", "u3", "u4"]},
                           __import__("ark_compat.web_client", fromlist=["MediaFetchBudget"])
                           .MediaFetchBudget(30.0))
        self.assertEqual(seen, [4], "4 项 ⇒ 4 个 worker（min(项数, 配置)）")

    def test_gate_warmup_runs_during_rehost(self):
        """转存期间应当**并行**把闸门探了（有缓存时那条探测就该命中，不再打上游）。"""
        t = CountingTransport(gate=False)
        c = make_client(t, captcha_probe_ttl=60.0)
        self.addCleanup(c.close)
        up = self._upstream(c)
        with mock.patch.object(up, "_rehost", return_value={}), \
             mock.patch.object(up.queue, "submit", return_value="cgt-1"):
            up.create({"web_params": {}, "captcha_token": None})
            # 预热是 daemon 线程，给它一点时间
            for _ in range(50):
                if t.count("needsCaptcha"):
                    break
                time.sleep(0.01)
        self.assertEqual(t.count("needsCaptcha"), 1, "转存期间应当预热一次闸门探测")

    def test_no_warmup_with_byo_token(self):
        t = CountingTransport()
        c = make_client(t, captcha_probe_ttl=60.0)
        self.addCleanup(c.close)
        up = self._upstream(c)
        with mock.patch.object(up, "_rehost", return_value={}), \
             mock.patch.object(up.queue, "submit", return_value="cgt-1"):
            up.create({"web_params": {}, "captcha_token": "BYO-TOKEN"})
            time.sleep(0.05)
        self.assertEqual(t.count("needsCaptcha"), 0, "有 BYO token 时不该多打一次上游")

    def test_no_warmup_when_cache_disabled(self):
        t = CountingTransport()
        c = make_client(t, captcha_probe_ttl=0.0)
        self.addCleanup(c.close)
        up = self._upstream(c)
        with mock.patch.object(up, "_rehost", return_value={}), \
             mock.patch.object(up.queue, "submit", return_value="cgt-1"):
            up.create({"web_params": {}, "captcha_token": None})
            time.sleep(0.05)
        self.assertEqual(t.count("needsCaptcha"), 0, "缓存关掉时预热等于凭空多一次请求")


# =====================================================================
# ④ 配置
# =====================================================================
class TestSettings(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_env({})
        self.assertEqual(s.captcha_probe_ttl, 5.0)
        self.assertEqual(s.rehost_workers, 7)
        self.assertEqual(s.minter_reserve, 8)

    def test_env_knobs(self):
        s = Settings.from_env({
            "AVM_CAPTCHA_PROBE_TTL": "0",
            "AVM_REHOST_WORKERS": "3",
            "AVM_MINTER_RESERVE": "16",
        })
        self.assertEqual(s.captcha_probe_ttl, 0.0)
        self.assertEqual(s.rehost_workers, 3)
        self.assertEqual(s.minter_reserve, 16)

    def test_builder_passes_through(self):
        from ark_compat import upstreams

        s = Settings(
            cookie=COOKIE, base_url="http://127.0.0.1:9", trust_env=False,
            task_store="memory", captcha_probe_ttl=1.5, rehost_workers=5, minter_reserve=12,
        )
        seen: dict = {}
        orig = upstreams.WebClient

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        with mock.patch.object(upstreams, "WebClient", spy):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        self.assertEqual(seen.get("captcha_probe_ttl"), 1.5)
        self.assertEqual(up.rehost_workers, 5)
        self.assertEqual(up.client.minter.reserve, 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
