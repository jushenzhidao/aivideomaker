#!/usr/bin/env python3
"""探测提速的契约测试：短超时 + **只读可重试、写入绝不重试**。

为什么这三条要写成门禁（都是实测踩出来的）：

1. **只读重试**：出口代理会偶发 502 / 超时，实测"下一发就成功"。而失败后等 10s，
   会白烧掉一整个闸门窗口（实测一次真提交被吞 ⇒ 该条延后 393s）。
2. **写入绝不重试**：这条线**没有幂等键**，响应丢失时重试可能重复建任务
   （计费线上就是重复扣费）⇒ 这条属性必须有测试守着，不能被后人"顺手统一"掉。
3. **探测用短超时**：默认 30s 的一发卡住探测会把重试循环占满。
   顺带守住一个 httpx 陷阱：`timeout=None` 表示**关闭超时**（不是"用默认"），
   所以 None 时**必须不传**这个参数。

运行：python3 tests/test_probe_retry.py
"""

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.errors import WebApiError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

COOKIE = "auth_session=" + "c" * 40
USER = "u-1"


def trpc_body(value):
    return [{"result": {"data": {"json": value}}}]


class FlakyTransport(httpx.BaseTransport):
    """前 `fail_times` 次调用抛传输层错误，之后正常返回。记录每次的 method。"""

    def __init__(self, fail_times: int = 0, error: Exception | None = None, value=True):
        self.fail_times = fail_times
        self.error = error or httpx.ConnectTimeout("handshake timed out")
        self.value = value
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.method)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return httpx.Response(200, json=trpc_body(self.value))


def make_client(transport, **kw) -> WebClient:
    return WebClient(
        cookie=COOKIE,
        base_url="https://site.test",
        user_id=USER,
        trust_env=False,
        transport=transport,
        **kw,
    )


class TestReadRetries(unittest.TestCase):
    def test_read_retries_once_then_succeeds(self):
        t = FlakyTransport(fail_times=1, value=True)
        c = make_client(t)
        self.assertTrue(c.needs_captcha(), "第一发失败后应当立即重试并拿到结果")
        self.assertEqual(len(t.calls), 2, "应为 1 次失败 + 1 次重试")

    def test_read_gives_up_after_configured_retries(self):
        t = FlakyTransport(fail_times=99)
        c = make_client(t, probe_retries=2)
        with self.assertRaises(WebApiError):
            c.needs_captcha()
        self.assertEqual(len(t.calls), 3, "1 次原始 + 2 次重试")

    def test_retries_can_be_disabled(self):
        t = FlakyTransport(fail_times=1)
        c = make_client(t, probe_retries=0)
        with self.assertRaises(WebApiError):
            c.needs_captcha()
        self.assertEqual(len(t.calls), 1)


class TestWriteIsNeverRetried(unittest.TestCase):
    """★ 安全门禁：写入没有幂等键 ⇒ 一次都不能重试。"""

    def test_post_transport_error_is_not_retried(self):
        t = FlakyTransport(fail_times=99)
        c = make_client(t)
        with self.assertRaises(WebApiError):
            c.trpc("ai.minimaxH3", {"content": "1"}, method="POST")
        self.assertEqual(t.calls, ["POST"], "写请求必须恰好发一次（重试可能重复建任务）")

    def test_get_is_retried_but_post_is_not_in_the_same_client(self):
        t = FlakyTransport(fail_times=99)
        c = make_client(t)
        with self.assertRaises(WebApiError):
            c.needs_captcha()
        reads = len(t.calls)
        with self.assertRaises(WebApiError):
            c.trpc("ai.minimaxH3", {"content": "1"}, method="POST")
        self.assertEqual(reads, 3)
        self.assertEqual(len(t.calls) - reads, 1, "POST 只发一次")


class TestProbeTimeout(unittest.TestCase):
    """探测要短超时；其余请求必须用 client 默认（**不能**传 None）。"""

    def _spy(self, c: WebClient) -> list[dict]:
        seen: list[dict] = []
        for verb in ("get", "post"):
            orig = getattr(c._http, verb)

            def spy(*a, _orig=orig, _verb=verb, **kw):
                seen.append({"verb": _verb, **kw})
                return _orig(*a, **kw)

            setattr(c._http, verb, spy)
        return seen

    def test_probe_uses_the_short_timeout(self):
        c = make_client(FlakyTransport(value=True), probe_timeout=3.5)
        seen = self._spy(c)
        c.needs_captcha()
        self.assertEqual(seen[-1]["timeout"], 3.5, "探测应当走 probe_timeout")

    def test_credits_probe_also_uses_the_short_timeout(self):
        c = make_client(FlakyTransport(value={"totalRemaining": 796}), probe_timeout=2.0)
        seen = self._spy(c)
        self.assertEqual(c.get_credits(), 796)
        self.assertEqual(seen[-1]["timeout"], 2.0)

    def test_write_does_not_pass_timeout_none(self):
        # httpx 的 timeout=None 表示"关闭超时"，传下去会让卡住的请求永远挂着
        c = make_client(FlakyTransport(value="t-1"))
        seen = self._spy(c)
        c.trpc("ai.minimaxH3", {"content": "1"}, method="POST")
        self.assertNotIn("timeout", seen[-1], "None 时不能把 timeout 传下去")

    def test_defaults(self):
        c = make_client(FlakyTransport())
        self.assertEqual(c.probe_timeout, 8.0)
        self.assertEqual(c.probe_retries, 2)


class TestSettings(unittest.TestCase):
    def test_env_knob(self):
        self.assertEqual(Settings.from_env({}).probe_timeout, 8.0)
        self.assertEqual(Settings.from_env({"AVM_PROBE_TIMEOUT": "3"}).probe_timeout, 3.0)

    def test_upstream_builder_passes_it_through(self):
        from unittest import mock

        from ark_compat import upstreams

        # ⚠️ 重构后 Settings 已无 upstream 字段（web 单线）
        s = Settings(
            cookie=COOKIE, base_url="http://127.0.0.1:9",
            trust_env=False, task_store="memory", probe_timeout=1.5,
        )
        seen: dict = {}
        orig = upstreams.WebClient

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        with mock.patch.object(upstreams, "WebClient", spy):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        self.assertEqual(seen.get("probe_timeout"), 1.5, "构造上游时必须把探测超时传下去")


if __name__ == "__main__":
    unittest.main(verbosity=2)
