#!/usr/bin/env python3
"""号池上报：余额 / 闸门 / 可达性 → Logfire 指标。

监控号池要盯三件事，本文件把它们各自钉住：

  1. **余额**是耗尽信号（`avm.account.credits`）；
  2. **Turnstile 闸门**是免费档的可用性信号，而且它是**按速率动态翻转**的
     （`avm.account.captcha_required`）——有了时间序列才谈得上"开多久会衰减"；
  3. **凭据是否可用**（会话过期 / Key 失效 —— `avm.account.reachable`）。

★ 最重要的一条不是功能，而是**安全**：指标是外发数据，标签里**只能有凭据指纹**。
  本文件里 `TestNoCredentialInTelemetry` 专门守这条，且做过变异测试。

纪律：零外发（`send_to_logfire=False` + 内存 reader）、零额度消耗（假上游，不发任何请求）。

运行：python3 tests/test_pool_metrics.py
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import logfire  # noqa: E402
from logfire.testing import (  # noqa: E402
    InMemoryMetricReader,
    IncrementalIdGenerator,
    TimeGenerator,
)

from ark_compat import observability as O  # noqa: E402
from ark_compat.app import _pool_accounts, _report_pool_once, create_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402

DEAD_UPSTREAM = "http://127.0.0.1:9"
COOKIE = "auth_session=" + "c" * 40
KEY = "ak_" + "d" * 64


def settings(**kw) -> Settings:
    base = dict(
        upstream="web",
        cookie=COOKIE,
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,  # app 不碰 logfire 全局配置，测试自己 configure
        trust_env=False,
        task_store="memory",
    )
    base.update(kw)
    return Settings(**base)


def fp(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


class FakeWebUp:
    kind = "web"
    supports_cancel = False

    def __init__(self, credits=796, captcha=False, boom=False):
        self._credits = credits
        self._boom = boom
        self.client = SimpleNamespace(needs_captcha=lambda: captcha)

    def balance(self):
        if self._boom:
            raise RuntimeError("UNAUTHORIZED: session expired")
        return self._credits


class FakeOfficialUp:
    kind = "official"
    supports_cancel = True

    def __init__(self, credits=796, boom=False):
        self._credits = credits
        self._boom = boom
        self.client = SimpleNamespace()

    def balance(self):
        if self._boom:
            raise RuntimeError("AUTH_FAILED: invalid API key")
        return self._credits


class MetricsCase(unittest.TestCase):
    """真实 logfire 装配（内存 reader），断言导出形态。"""

    def setUp(self):
        O.reset_gauge_cache()  # 换 reader 后必须重建指标对象
        self.reader = InMemoryMetricReader()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(),
                ns_timestamp_generator=TimeGenerator(),
            ),
            metrics=logfire.MetricsOptions(additional_readers=[self.reader]),
        )
        O._LOGFIRE_READY = True
        self._points = None
        self.addCleanup(self._teardown)

    def _teardown(self):
        O._LOGFIRE_READY = False
        O.reset_gauge_cache()
        logfire.force_flush()

    def points(self) -> list[tuple[str, float, dict]]:
        """(指标名, 值, 标签) 三元组 —— 导出前形态，直接断言。

        ⚠️ 两个坑（都踩过）：
          1. `InMemoryMetricReader.get_metrics_data()` 内部 `collect()` 之后会把
             数据**清空**（读完即置 None）⇒ 同一个测试里只能读一次，所以这里缓存。
          2. 读之前**不要** `logfire.force_flush()` —— 那会先触发一次 collection，
             把数据消费掉，随后 get_metrics_data() 返回 None（本次就是这么踩的）。
        """
        if self._points is None:
            data = self.reader.get_metrics_data()
            out = []
            for rm in (data.resource_metrics if data is not None else []):
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        for dp in m.data.data_points:
                            out.append((m.name, dp.value, dict(dp.attributes)))
            self._points = out
        return self._points

    def named(self, name: str) -> list[tuple[str, float, dict]]:
        return [p for p in self.points() if p[0] == name]


class TestRecordAccount(MetricsCase):
    def test_three_gauges_are_emitted_with_labels(self):
        O.record_account(
            upstream="web", account="abc123", source="process",
            credits=796, captcha_required=False, reachable=True,
        )
        credits = self.named("avm.account.credits")
        captcha = self.named("avm.account.captcha_required")
        reachable = self.named("avm.account.reachable")
        self.assertEqual([p[1] for p in credits], [796])
        self.assertEqual([p[1] for p in captcha], [0])
        self.assertEqual([p[1] for p in reachable], [1])
        attrs = credits[0][2]
        self.assertEqual(attrs["upstream"], "web")
        self.assertEqual(attrs["account"], "abc123")
        self.assertEqual(attrs["source"], "process")
        self.assertIn("pid", attrs, "多 worker 下要靠 pid 分辨是谁报的")

    def test_captcha_open_is_one(self):
        O.record_account(upstream="web", account="abc", captcha_required=True)
        self.assertEqual([p[1] for p in self.named("avm.account.captcha_required")], [1])

    def test_missing_fields_are_simply_not_reported(self):
        O.record_account(upstream="official", account="abc", credits=15)
        self.assertEqual(len(self.named("avm.account.credits")), 1)
        self.assertEqual(self.named("avm.account.captcha_required"), [])
        self.assertEqual(self.named("avm.account.reachable"), [])

    def test_noop_when_logfire_is_down(self):
        O._LOGFIRE_READY = False
        O.record_account(upstream="web", account="abc", credits=1, captcha_required=False)
        O._LOGFIRE_READY = True
        self.assertEqual(self.points(), [], "Logfire 没装配时不该产生任何指标")


class TestNoCredentialInTelemetry(MetricsCase):
    """★ 安全门禁：遥测里只能有指纹，绝不能有凭据原文。"""

    def _app_with_pool(self):
        app = create_app(settings(cookie=COOKIE))
        app.state.upstreams = {"web": FakeWebUp(credits=796)}
        # 透传池：键是 cookie 的 sha256[:16]（与 app._passthrough_web_upstream 一致）
        app.state.passthrough_web = {fp(COOKIE): FakeWebUp(credits=700)}
        app.state.passthrough_upstreams = {KEY: FakeOfficialUp(credits=700)}
        return app

    def test_account_labels_are_fingerprints(self):
        app = self._app_with_pool()
        rows = {(kind, source): account for kind, account, source, _ in _pool_accounts(app)}
        self.assertEqual(rows[("web", "process")], fp(COOKIE))
        self.assertEqual(rows[("web", "passthrough")], fp(COOKIE))
        self.assertEqual(rows[("official", "passthrough")], fp(KEY))

    def test_credentials_never_reach_the_metrics(self):
        app = self._app_with_pool()
        asyncio_run(_report_pool_once(app))
        dumped = json.dumps([(n, v, a) for n, v, a in self.points()], ensure_ascii=False)
        self.assertNotIn(COOKIE, dumped, "cookie 原文进了遥测")
        self.assertNotIn("auth_session", dumped, "cookie 名都不该出现")
        self.assertNotIn(KEY, dumped, "API Key 原文进了遥测")
        self.assertTrue(self.named("avm.account.credits"), "至少要有余额指标")


class TestPoolSampling(MetricsCase):
    def _app(self, **kw):
        app = create_app(settings(**kw))
        app.state.upstreams = {}
        app.state.passthrough_web = {}
        app.state.passthrough_upstreams = {}
        return app

    def test_each_account_reports_its_own_balance(self):
        app = self._app()
        app.state.upstreams = {"web": FakeWebUp(credits=796)}
        app.state.passthrough_web = {fp("x" * 40): FakeWebUp(credits=12)}
        asyncio_run(_report_pool_once(app))
        by_account = {a["account"]: v for _, v, a in self.named("avm.account.credits")}
        self.assertEqual(len(by_account), 2)
        self.assertIn(796, by_account.values())
        self.assertIn(12, by_account.values())

    def test_web_accounts_report_the_captcha_gate(self):
        app = self._app()
        app.state.upstreams = {"web": FakeWebUp(credits=5, captcha=True)}
        asyncio_run(_report_pool_once(app))
        self.assertEqual([p[1] for p in self.named("avm.account.captcha_required")], [1])

    def test_unreachable_credential_marks_reachable_zero_and_skips_credits(self):
        app = self._app()
        app.state.upstreams = {"web": FakeWebUp(boom=True)}
        rows = asyncio_run(_report_pool_once(app))
        self.assertEqual([p[1] for p in self.named("avm.account.reachable")], [0])
        self.assertEqual(
            self.named("avm.account.credits"), [], "读不到余额就不该报一个假数字"
        )
        self.assertFalse(rows[0][3]["reachable"])

    def test_one_bad_account_does_not_stop_the_others(self):
        app = self._app()
        app.state.upstreams = {"official": FakeOfficialUp(boom=True)}
        app.state.passthrough_web = {fp("y" * 40): FakeWebUp(credits=99)}
        rows = asyncio_run(_report_pool_once(app))
        self.assertEqual(len(rows), 2, "一个账号挂了不能拖掉整轮采样")
        self.assertEqual([p[1] for p in self.named("avm.account.credits")], [99])

    def test_report_survives_repeated_cycles(self):
        app = self._app()
        app.state.upstreams = {"web": FakeWebUp(credits=1, boom=True)}
        for _ in range(3):
            rows = asyncio_run(_report_pool_once(app))  # 不能因为失败而抛出去
            self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(len(self.named("avm.account.reachable")), 1)


class TestReporterWiring(unittest.TestCase):
    """`0` 要真的关掉，`>0` 要真的起来 —— 且**不依赖遥测出口是否可用**。"""

    def _run_app(self, seconds: int, logfire_ready: bool) -> dict:
        from fastapi.testclient import TestClient

        was = O._LOGFIRE_READY
        O._LOGFIRE_READY = logfire_ready
        try:
            app = create_app(settings(account_report_seconds=seconds))
            with TestClient(app):
                return {
                    "reporter": getattr(app.state, "account_reporter", None),
                    "metrics": getattr(app.state, "account_metrics_enabled", None),
                }
        finally:
            O._LOGFIRE_READY = was

    def test_zero_disables_the_reporter(self):
        self.assertIsNone(self._run_app(0, logfire_ready=True)["reporter"])

    def test_positive_seconds_starts_it_and_marks_metrics_available(self):
        state = self._run_app(60, logfire_ready=True)
        self.assertIsNotNone(state["reporter"])
        self.assertTrue(state["metrics"])

    def test_sampling_does_not_depend_on_the_telemetry_sink(self):
        # 出口（Logfire）不可用时**照样采样**：采集与出口是两件事，
        # 出口坏了的时刻恰恰最需要号池视图（本地日志兜底）。
        state = self._run_app(60, logfire_ready=False)
        self.assertIsNotNone(state["reporter"], "遥测出口挂了不该连采集也停")
        self.assertFalse(state["metrics"], "但必须如实标注指标不可用")


class TestSettings(unittest.TestCase):
    def test_env_knob(self):
        self.assertEqual(Settings.from_env({}).account_report_seconds, 300)
        self.assertEqual(
            Settings.from_env({"AVM_ACCOUNT_REPORT_SECONDS": "0"}).account_report_seconds, 0
        )
        self.assertEqual(
            Settings.from_env({"AVM_ACCOUNT_REPORT_SECONDS": "45"}).account_report_seconds, 45
        )

    def test_fingerprint_helper_is_stable_and_short(self):
        from ark_compat.app import _fingerprint

        self.assertEqual(_fingerprint(COOKIE), fp(COOKIE))
        self.assertEqual(len(_fingerprint(COOKIE)), 16)
        self.assertNotEqual(_fingerprint(COOKIE), _fingerprint(COOKIE + "x"))
        self.assertEqual(_fingerprint(""), "")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
