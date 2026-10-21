#!/usr/bin/env python3
"""重试的**安全边界**门禁（2026-10-21，「能提高成功率就加重试」的落地）。

这份测试守的不是"重试有没有生效"，而是**哪些失败允许重发** —— 因为这条线没有幂等键：

    `POST ai.minimaxH3` 重发一次 ⇒ 可能建出两个任务 ⇒ tier=base 那是**两次扣费**

所以每条断言都对应一个生产事实（2026-10-12 → 10-21，1052 条受理 / 214 条后台失败）：

  · `queue is full`（19 条）—— 上游并发满，**必须**重试（隔几秒就有空位）；
  · `CAPTCHA_REQUIRED` / 铸造 503（16 条）—— 换 token 重试；
  · 空 taskId —— 站点用空串表示"不收"，**没建任务**，可重试；
  · `ReadTimeout`（54 条）—— **绝不重试**：60s 都没回，上游很可能已经建好了；
  · 内容审核（95 + 21 条）—— **绝不重试**：用户明确排除，且重发还是拒。

运行：python3 tests/test_submit_retry.py
"""

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.errors import WebApiError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import (  # noqa: E402
    WebClient,
    is_content_rejection,
    retryable_submit_error,
    safe_to_resend,
)

COOKIE = "auth_session=" + "c" * 40
USER = "u-1"


def ok_body(value):
    return [{"result": {"data": {"json": value}}}]


def err_body(message, code="INTERNAL_SERVER_ERROR", http=500):
    return [{
        "error": {
            "json": {
                "message": message,
                "data": {"code": code, "httpStatus": http},
            }
        }
    }]


class ScriptedTransport(httpx.BaseTransport):
    """按 method 分派脚本；每次请求消耗一条。没有脚本时给一个成功默认值。

    脚本动作：
      ("raise", exc)                 抛传输异常
      ("error", msg[, code, http])   tRPC 错误体
      ("body", value)                成功体
      ("empty",)                     成功但 taskId 为空（站点的"静默拒绝"）
    """

    def __init__(self, *, get=None, post=None, put=None, captcha=False):
        self.scripts = {"GET": list(get or []), "POST": list(post or []), "PUT": list(put or [])}
        self.captcha = captcha
        self.calls = []          # [(method, path)]
        self.posts = 0
        self.puts = 0

    # -- helpers ---------------------------------------------------------
    def _next(self, method):
        seq = self.scripts[method]
        return seq.pop(0) if seq else ("body", "cgt-default")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        # 探测类 GET 固定应答（不消耗脚本）：提交前那次 needsCaptcha
        if "needsCaptcha" in path:
            return httpx.Response(200, json=ok_body(self.captcha))
        if method == "POST":
            self.posts += 1
        if method == "PUT":
            self.puts += 1
        action = self._next(method)
        kind = action[0]
        if kind == "raise":
            raise action[1]
        if kind == "error":
            msg = action[1]
            code = action[2] if len(action) > 2 else "INTERNAL_SERVER_ERROR"
            http = action[3] if len(action) > 3 else 500
            return httpx.Response(200, json=err_body(msg, code, http))
        if kind == "empty":
            return httpx.Response(200, json=ok_body(""))
        value = action[1]
        if isinstance(value, (bytes, bytearray)):
            return httpx.Response(200, content=bytes(value))
        return httpx.Response(200, json=ok_body(value))


def make_client(transport, **kw) -> WebClient:
    return WebClient(
        cookie=COOKIE,
        base_url="https://site.test",
        user_id=USER,
        trust_env=False,
        transport=transport,
        submit_retry_backoff=0.0,   # 测试里不退避
        **kw,
    )


# =====================================================================
# 1) 传输层：哪些能证明"请求没发出去"
# =====================================================================
class TestSafeResendBoundary(unittest.TestCase):
    def test_connection_errors_are_safe(self):
        for cls in (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ProxyError,
            httpx.LocalProtocolError,
        ):
            self.assertTrue(safe_to_resend(cls("boom")), f"{cls.__name__} 应当可安全重发")

    def test_ambiguous_errors_are_not_safe(self):
        """这几种都可能是"上游已经收到并建好了任务" —— 重发就是重复扣费。"""
        for cls in (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
        ):
            self.assertFalse(safe_to_resend(cls("boom")), f"{cls.__name__} 绝不能重发")


# =====================================================================
# 2) 建任务失败：语义判定
# =====================================================================
class TestRetryableSubmitError(unittest.TestCase):
    def _err(self, msg, code="INTERNAL_SERVER_ERROR", http=500, transport=""):
        return WebApiError("ai.minimaxH3", msg, code=code, http_status=http, transport=transport)

    def test_queue_full_is_retryable(self):
        e = self._err(
            "The queue is full. The pro plan can only run 4 task at a time."
        )
        self.assertTrue(retryable_submit_error(e), "账号并发满 ⇒ 等一会儿就能进")

    def test_content_reject_is_never_retryable(self):
        for msg in (
            "Prompt or Image Sensitive",
            "The content could not be processed because it contained "
            "material flagged by a content checker.",
        ):
            self.assertFalse(retryable_submit_error(self._err(msg)), f"审核不该重试：{msg[:40]}")

    def test_content_wins_over_generic_500(self):
        """审核和 queue is full 都挂在 500 上 —— 判定必须先认审核。"""
        e = self._err("Prompt or Image Sensitive")
        self.assertEqual(e.http_status, 500)
        self.assertFalse(retryable_submit_error(e))

    def test_content_texts_are_recognised_directly(self):
        """**直接**断言两种审核文案都被识别成审核。

        为什么不能只验"最终没重试"：审核错误恰好也没命中其他白名单，所以
        即便文案表被清空，行为看起来一样对。这条断言让"清空文案表"立刻变红。
        """
        for msg in (
            "Prompt or Image Sensitive",
            "The content could not be processed because it contained material "
            "flagged by a content checker.",
        ):
            self.assertTrue(is_content_rejection(self._err(msg)), msg[:40])

    def test_content_wins_even_with_an_otherwise_retryable_signal(self):
        """审核 + 429（本来可重试的信号）⇒ 仍必须**审核优先**、不重试。

        这是"先判审核再判别的"那条顺序的**真门禁**：把顺序调换或清空文案表都会红。
        """
        for msg in ("Prompt or Image Sensitive", "flagged by a content checker"):
            e = self._err(msg, code="CAPTCHA_REQUIRED", http=429)
            self.assertFalse(retryable_submit_error(e), f"{msg} 不该借 429 混进重试")

    def test_captcha_gate_is_retryable(self):
        e = WebApiError("ai.minimaxH3", "needs captcha", code="CAPTCHA_REQUIRED", http_status=429)
        self.assertTrue(retryable_submit_error(e), "闸门/铸造失败 ⇒ 上游没到建任务那一步")

    def test_read_timeout_is_not_retryable(self):
        e = self._err("ReadTimeout: The read operation timed out", transport="ReadTimeout")
        self.assertFalse(retryable_submit_error(e))

    def test_connect_error_is_retryable(self):
        e = self._err("ConnectError: connection refused", transport="ConnectError")
        self.assertTrue(retryable_submit_error(e))

    def test_param_error_is_not_retryable(self):
        e = self._err("invalid duration", code="BAD_REQUEST", http=400)
        self.assertFalse(retryable_submit_error(e))


# =====================================================================
# 3) create：端到端（重试壳）
# =====================================================================
class TestCreateRetries(unittest.TestCase):
    def _client(self, **scripts):
        t = ScriptedTransport(**scripts)
        c = make_client(t)
        self.addCleanup(c.close)
        return c, t

    def test_queue_full_then_success(self):
        c, t = self._client(
            post=[
                ("error", "The queue is full. The pro plan can only run 4 task at a time."),
                ("body", "cgt-1"),
            ]
        )
        self.assertEqual(c.create({"content": "x"}), "cgt-1")
        self.assertEqual(t.posts, 2, "并发满 ⇒ 应当重发一次并成功")

    def test_content_reject_sends_exactly_once(self):
        c, t = self._client(
            post=[("error", "Prompt or Image Sensitive"), ("body", "must-not-happen")]
        )
        with self.assertRaises(WebApiError):
            c.create({"content": "x"})
        self.assertEqual(t.posts, 1, "内容审核绝不重试（用户明确排除）")

    def test_read_timeout_sends_exactly_once(self):
        c, t = self._client(
            post=[("raise", httpx.ReadTimeout("The read operation timed out")), ("body", "must-not-happen")]
        )
        with self.assertRaises(WebApiError):
            c.create({"content": "x"})
        self.assertEqual(t.posts, 1, "读超时后上游可能已建任务 ⇒ 重发＝重复扣费")

    def test_connect_timeout_is_retried(self):
        c, t = self._client(
            post=[("raise", httpx.ConnectTimeout("handshake timed out")), ("body", "cgt-2")]
        )
        self.assertEqual(c.create({"content": "x"}), "cgt-2")
        self.assertEqual(t.posts, 2, "连接阶段超时 ⇒ 请求没发出去 ⇒ 可重发")

    def test_empty_task_id_is_not_retried(self):
        """空串 = 站点静默拒收。`_create_once` 内部已经换过一次新 token，
        再空就是闸门没回落/参数问题 ⇒ 不做外层重试（生产 9 天 0 条，也没有可救的量）。"""
        c, t = self._client(post=[("empty",), ("body", "cgt-3")])
        with self.assertRaises(WebApiError):
            c.create({"content": "x"})
        self.assertEqual(t.posts, 1)

    def test_retries_exhausted_raises_last_error(self):
        c, t = self._client(
            post=[("error", "The queue is full. The pro plan can only run 4 task at a time.")] * 3
        )
        with self.assertRaises(WebApiError) as ctx:
            c.create({"content": "x"})
        self.assertIn("queue is full", str(ctx.exception))
        self.assertEqual(t.posts, 3, "1 次原始 + submit_retries(2)")

    def test_retries_can_be_disabled(self):
        c, t = self._client(post=[
            ("error", "The queue is full. The pro plan can only run 4 task at a time."),
            ("body", "cgt-x"),
        ])
        c.submit_retries = 0
        with self.assertRaises(WebApiError):
            c.create({"content": "x"})
        self.assertEqual(t.posts, 1)


# =====================================================================
# 4) 转存：下载 / PUT（幂等 ⇒ 抖动可重发）
# =====================================================================
class TestMediaRetries(unittest.TestCase):
    def test_download_retries_on_connect_error(self):
        t = ScriptedTransport(get=[("raise", httpx.ConnectError("reset")), ("body", b"abc")])
        c = make_client(t)
        self.addCleanup(c.close)
        self.assertEqual(c._fetch_media("https://x.test/a.png"), b"abc")

    def test_download_404_is_not_retried(self):
        t = ScriptedTransport(get=[("error", "nope", "NOT_FOUND", 404)])

        class NotFound(httpx.BaseTransport):
            def __init__(self):
                self.n = 0

            def handle_request(self, request):
                self.n += 1
                return httpx.Response(404, text="not found")

        t2 = NotFound()
        c = make_client(t2)
        self.addCleanup(c.close)
        with self.assertRaises(WebApiError):
            c._fetch_media("https://x.test/a.png")
        self.assertEqual(t2.n, 1, "404 是链接本身坏了，重发不会变好")

    def test_put_timeout_is_retried(self):
        t = ScriptedTransport(
            post=[("body", {"uploadUrl": "https://cdn.test/u1", "publicUrl": "https://cdn.test/p1", "headers": {}})],
            put=[("raise", httpx.WriteTimeout("write timed out")), ("body", "")],
        )
        c = make_client(t)
        self.addCleanup(c.close)
        out = c.upload_file(b"\x89PNG\r\n\x1a\n" + b"0" * 32, name="a.png")
        self.assertEqual(out["publicUrl"], "https://cdn.test/p1")
        self.assertEqual(t.puts, 2, "PUT 幂等 ⇒ 写超时可重发")

    def test_put_403_reissues_presign(self):
        """预签名只有 60s：403 视为过期 ⇒ 重签再传（不是拿旧 URL 硬撞）。"""
        t = ScriptedTransport(
            post=[
                ("body", {"uploadUrl": "https://cdn.test/u1", "publicUrl": "https://cdn.test/p1", "headers": {}}),
                ("body", {"uploadUrl": "https://cdn.test/u2", "publicUrl": "https://cdn.test/p2", "headers": {}}),
            ],
            put=[("body", "")],   # 占位，下面手工改写响应码
        )
        c = make_client(t)
        self.addCleanup(c.close)

        calls = {"put": 0}
        orig = t.handle_request

        def handle(request):
            if request.method == "PUT":
                calls["put"] += 1
                path = request.url.path
                if path == "/u1":
                    return httpx.Response(403, text="expired")
                return httpx.Response(200, text="ok")
            return orig(request)

        t.handle_request = handle  # type: ignore[method-assign]
        out = c.upload_file(b"\x89PNG\r\n\x1a\n" + b"0" * 32, name="a.png")
        self.assertEqual(calls["put"], 2)
        self.assertTrue(out["publicUrl"].endswith("/p2"), "应当用重签后的 publicUrl")


# =====================================================================
# 5) 配置：默认值 + 环境变量
# =====================================================================
class TestSettings(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_env({})
        self.assertEqual(s.submit_retries, 2)
        self.assertEqual(s.submit_retry_backoff, 3.0)
        self.assertEqual(s.media_retries, 2)
        self.assertEqual(s.minter_retries, 1)
        self.assertEqual(s.watch_timeout, 3600.0)
        self.assertEqual(s.upstream_timeout, 120.0, "2026-10-21 由 60 提到 120（生产 60 上限下仍有 54 条读超时）")

    def test_env_knobs(self):
        s = Settings.from_env({
            "AVM_SUBMIT_RETRIES": "0",
            "AVM_SUBMIT_RETRY_BACKOFF": "1.5",
            "AVM_MEDIA_RETRIES": "3",
            "AVM_MINTER_RETRIES": "0",
            "AVM_WATCH_TIMEOUT": "900",
        })
        self.assertEqual(s.submit_retries, 0)
        self.assertEqual(s.submit_retry_backoff, 1.5)
        self.assertEqual(s.media_retries, 3)
        self.assertEqual(s.minter_retries, 0)
        self.assertEqual(s.watch_timeout, 900.0)

    def test_builder_passes_retries_through(self):
        """配置必须真的传到客户端 —— 否则"配了不生效"，本项目最忌讳的形状。"""
        from unittest import mock

        from ark_compat import upstreams

        s = Settings(
            cookie=COOKIE, base_url="http://127.0.0.1:9", trust_env=False,
            task_store="memory", submit_retries=5, submit_retry_backoff=2.5,
            media_retries=4, minter_retries=3, watch_timeout=120.0,
        )
        seen: dict = {}
        orig = upstreams.WebClient

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        with mock.patch.object(upstreams, "WebClient", spy):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        self.assertEqual(seen.get("submit_retries"), 5)
        self.assertEqual(seen.get("submit_retry_backoff"), 2.5)
        self.assertEqual(seen.get("media_retries"), 4)
        self.assertEqual(up.queue.watch_timeout, 120.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
