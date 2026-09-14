#!/usr/bin/env python3
"""`/healthz` 必须区分「logfire 装上了」与「数据真的在往外发」（报告 AVM12-OPEN-LOGFIRE）。

实测踩到的形状：`LOGFIRE_TOKEN` 为空、`send_to_logfire="if-token-present"`（默认），
`logfire.configure()` **照样成功**、span 照样生成 ⇒ 旧的 `/healthz` 报 `logfire: true`，
运维据此以为 trace 在云端，实际**一条都没出去**。两个信号必须分开报：
`logfire`（装配成功）与 `logfire_exporting`（真的会外发）。

同一份报告的另一半：镜像里的 `logfire>=3.0` **没带 `[fastapi]` extra** ⇒
`instrument_fastapi` 每次启动都抛异常、被吞成一条警告，**每个请求的自动 span 静默消失**。

运行：python3 tests/test_logfire_export_signal.py
"""

import os
import pathlib
import sys
import unittest
from importlib.metadata import PackageNotFoundError, metadata
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat import observability as obs  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402


class _FakeLogfire:
    """只实现 `setup_observability` 会调的三个入口 —— 用来观察装配逻辑，不真连云端。"""

    def __init__(self):
        self.configured = None

    def configure(self, **kw):
        self.configured = kw

    def loguru_handler(self):        # 代码里有 try/except，抛出去等于"桥接不可用"
        raise RuntimeError("no bridge in test")

    def instrument_httpx(self, **kw):
        raise RuntimeError("no httpx instrumentation in test")


class TestWillExport(unittest.TestCase):
    """`will_export` 是"会不会真发"的**唯一**判据。"""

    def _with_token(self, token):
        return mock.patch.dict(os.environ, {"LOGFIRE_TOKEN": token})

    def test_disabled_never_exports(self):
        with self._with_token("pylf_x"):
            self.assertFalse(obs.will_export(Settings(enable_logfire=False)))

    def test_send_false_never_exports_even_with_a_token(self):
        with self._with_token("pylf_x"):
            self.assertFalse(obs.will_export(Settings(logfire_send=False)))

    def test_send_true_always_exports(self):
        with self._with_token(""):
            self.assertTrue(obs.will_export(Settings(logfire_send=True)))

    def test_if_token_present_follows_the_token(self):
        """★ 被误报的那一幕：装配成功，但没有 token ⇒ 不该说"在发"。"""
        for token, expected in (("", False), ("   ", False), ("pylf_real", True)):
            with self.subTest(token=token):
                with self._with_token(token):
                    self.assertIs(
                        obs.will_export(Settings(logfire_send="if-token-present")), expected
                    )


class TestExportingFlagWiring(unittest.TestCase):
    """装配时必须把 `logfire_exporting` 从配置算出来，而不是留默认值。"""

    def setUp(self):
        self._saved = (obs._LOGFIRE_READY, obs._LOGFIRE_EXPORTING, obs._MAX_ATTR_CHARS)
        self.addCleanup(self._restore)

    def _restore(self):
        obs._LOGFIRE_READY, obs._LOGFIRE_EXPORTING, obs._MAX_ATTR_CHARS = self._saved

    def _setup(self, token, **kw):
        obs._LOGFIRE_READY = False            # 绕开"重复装配直接 return True"的短路
        fake = _FakeLogfire()
        with self._with_token(token), mock.patch.dict(sys.modules, {"logfire": fake}):
            ok = obs.setup_observability(Settings(
                service_name="t", logfire_send="if-token-present", **kw))
        return ok, fake

    def _with_token(self, token):
        return mock.patch.dict(os.environ, {"LOGFIRE_TOKEN": token})

    def test_assembled_without_a_token_is_ready_but_not_exporting(self):
        ok, _ = self._setup("")
        self.assertTrue(ok, "装配本身应当成功（这正是旧口径骗人的地方）")
        self.assertTrue(obs.logfire_ready())
        self.assertFalse(obs.logfire_exporting(), "没 token 却在报『在发』 —— 就是那个缺陷")

    def test_assembled_with_a_token_is_exporting(self):
        ok, _ = self._setup("pylf_real")
        self.assertTrue(ok)
        self.assertTrue(obs.logfire_exporting())

    def test_disabled_stays_both_false(self):
        ok, _ = self._setup("pylf_real", enable_logfire=False)
        self.assertFalse(ok)
        self.assertFalse(obs.logfire_ready())
        self.assertFalse(obs.logfire_exporting())


class TestHealthzExposesBothSignals(unittest.TestCase):
    def _healthz(self, **kw):
        from fastapi.testclient import TestClient

        from ark_compat.app import create_app

        settings = Settings(
            cookie="auth_session=deadbeef",
            base_url="http://127.0.0.1:9",      # 死端口：本用例不碰任何网络
            enable_logfire=False,
            trust_env=False,
            task_store="memory",
            **kw,
        )
        # 刻意**不**用 `with TestClient(...)`：那会触发 lifespan（起号池上报 → 打真上游）
        return TestClient(create_app(settings)).get("/healthz").json()

    def test_both_fields_are_present(self):
        j = self._healthz()
        self.assertIn("logfire", j)
        self.assertIn("logfire_exporting", j)
        self.assertIn("logfire_send", j)

    def test_closed_logfire_reports_neither(self):
        j = self._healthz()
        self.assertIs(j["logfire"], False)
        self.assertIs(j["logfire_exporting"], False)

    def test_exporting_is_never_true_without_a_token(self):
        """把"装配 true + 外发 false"这个组合钉死（有 token / 无 token 各一次）。"""
        for token in ("", "pylf_real"):
            with self.subTest(token=token):
                with mock.patch.dict(os.environ, {"LOGFIRE_TOKEN": token}):
                    j = self._healthz()
                    self.assertIs(j["logfire_exporting"], False,
                                  "本用例关掉了 logfire（AVM_DISABLE_LOGFIRE=1 等价）⇒ 必须 false")


class TestFastapiInstrumentationIsDeclared(unittest.TestCase):
    """镜像里必须真的装上 `opentelemetry-instrumentation-fastapi`。"""

    def _requirement_lines(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        return [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def test_requirements_declare_the_fastapi_extra(self):
        lines = self._requirement_lines()
        self.assertTrue(
            any(ln.startswith("logfire[fastapi]") for ln in lines),
            f"requirements.txt 里没有 logfire[fastapi] extra：{lines}",
        )
        self.assertFalse(
            any(ln.startswith("logfire>=") for ln in lines),
            "又出现了不带 extra 的裸 logfire —— instrument_fastapi 会静默失效",
        )

    def test_the_extra_really_pulls_the_fastapi_instrumentation(self):
        """用已装 logfire 的元数据证明 extra → 包 的映射（不能只信注释）。"""
        try:
            meta = metadata("logfire")
        except PackageNotFoundError:  # pragma: no cover - CI 里由 requirements.txt 保证
            self.fail("logfire 没装 ⇒ 无法验证 extra 映射；CI 里它由 requirements.txt 装")
        reqs = [r for r in (meta.get_all("Requires-Dist") or []) if "extra == 'fastapi'" in r]
        self.assertTrue(reqs, "logfire 声明了 fastapi extra 却没有对应依赖？写法会装不上")
        self.assertTrue(
            any("opentelemetry-instrumentation-fastapi" in r for r in reqs),
            f"fastapi extra 没带 fastapi 的 instrument 包：{reqs}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
