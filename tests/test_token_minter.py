#!/usr/bin/env python3
"""铸造服务接线（`AVM_MINTER_URL`）的契约测试。

三条必须钉住的属性（都是"错了会很难查"的那一类）：

1. **取到 token 就真用上**：闸门开着 + 调用方没带 token ⇒ 用铸造服务给的 token 提交；
2. 🔴 **取不到就如实失败**：铸造服务没配/挂了 ⇒ 抛 `CaptchaRequiredError`，**一次提交都不发**。
   绝不能"没 token 也提交" —— 上游会静默返回空串，看起来像成功（本站最难查的故障形态）；
3. **调用方给的 token 优先**：别自说自话去铸造（BYO 语义不能被覆盖）。

运行：python3 tests/test_token_minter.py
"""

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.errors import CaptchaRequiredError  # noqa: E402
from ark_compat.minter import TokenMinter  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

COOKIE = "auth_session=" + "c" * 40
USER = "u1"


class FakeSite(httpx.BaseTransport):
    """站点替身：记录每次调用（method/path/body），按闸门状态回 needsCaptcha。"""

    def __init__(self, gate: bool = True):
        self.gate = gate
        self.calls: list[tuple[str, str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode("utf-8", "replace") if request.content else ""
        self.calls.append((request.method, path, body))
        if path.endswith("model.needsCaptcha"):
            return httpx.Response(200, json=[{"result": {"data": {"json": self.gate}}}])
        if path.endswith("ai.minimaxH3"):
            return httpx.Response(200, json=[{"result": {"data": {"json": "t-1"}}}])
        return httpx.Response(200, json=[{"result": {"data": {"json": None}}}])

    def creates(self) -> list[str]:
        return [b for _m, p, b in self.calls if p.endswith("ai.minimaxH3")]


class FakeMinter:
    """铸造服务替身。"""

    def __init__(self, token: str | None = "TOKEN-X", configured: bool = True):
        self.token = token
        self.configured = configured
        self.calls = 0

    def mint(self) -> str | None:
        self.calls += 1
        return self.token


def make_client(site: FakeSite, minter=None, gate_note: str = "") -> WebClient:
    return WebClient(
        cookie=COOKIE,
        base_url="https://site.test",
        user_id=USER,
        trust_env=False,
        transport=site,
        minter=minter,
        probe_retries=0,
    )


def body_params() -> dict:
    return {"content": "a cat", "duration": 5, "resolution": "480p", "tier": "turbo"}


class TestMinterIsUsed(unittest.TestCase):
    def test_minted_token_is_used_in_create(self):
        site = FakeSite(gate=True)
        m = FakeMinter("TOKEN-FROM-MINTER")
        c = make_client(site, m)
        self.assertEqual(c.create(body_params()), "t-1")
        self.assertEqual(m.calls, 1, "闸门开着且没带 token 时应当去要一个")
        creates = site.creates()
        self.assertEqual(len(creates), 1)
        self.assertIn("TOKEN-FROM-MINTER", creates[0], "取到的 token 必须真的带上")

    def test_caller_supplied_token_wins(self):
        site = FakeSite(gate=True)
        m = FakeMinter("TOKEN-FROM-MINTER")
        c = make_client(site, m)
        self.assertEqual(c.create(body_params(), token="BYO-TOKEN"), "t-1")
        self.assertEqual(m.calls, 0, "调用方自带 token 时不该去铸造（BYO 语义优先）")
        self.assertIn("BYO-TOKEN", site.creates()[0])

    def test_gate_closed_does_not_call_the_minter(self):
        site = FakeSite(gate=False)
        m = FakeMinter()
        c = make_client(site, m)
        self.assertEqual(c.create(body_params()), "t-1")
        self.assertEqual(m.calls, 0, "闸门关着不需要 token，别白铸")


class TestDegradation(unittest.TestCase):
    """★ 核心安全属性：取不到 token 必须如实失败，一次提交都不能发。"""

    def test_minter_failure_raises_and_sends_nothing(self):
        site = FakeSite(gate=True)
        m = FakeMinter(token=None)          # 铸造服务挂了/返回空
        c = make_client(site, m)
        with self.assertRaises(CaptchaRequiredError):
            c.create(body_params())
        self.assertEqual(m.calls, 1, "应当尝试过一次铸造")
        self.assertEqual(site.creates(), [], "⛔ 取不到 token 却仍然提交 —— 这是静默失败")

    def test_no_minter_configured_keeps_old_behaviour(self):
        site = FakeSite(gate=True)
        c = make_client(site, None)
        with self.assertRaises(CaptchaRequiredError):
            c.create(body_params())
        self.assertEqual(site.creates(), [])

    def test_error_message_says_the_minter_was_tried(self):
        site = FakeSite(gate=True)
        c = make_client(site, FakeMinter(token=None))
        with self.assertRaises(CaptchaRequiredError) as ctx:
            c.create(body_params())
        self.assertIn("minter", str(ctx.exception).lower(), "错误里要能看出是铸造服务这条线的问题")


class TestTokenMinterClient(unittest.TestCase):
    """客户端本体：只该做"取回 token 或返回 None"，绝不抛异常。"""

    def test_not_configured_is_a_noop(self):
        m = TokenMinter("")
        self.assertFalse(m.configured)
        self.assertIsNone(m.mint())

    def test_200_returns_token(self):
        m = TokenMinter("http://minter.test")
        m._http = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"token": "TK"})))
        self.assertEqual(m.mint(), "TK")

    def test_503_and_garbage_are_none(self):
        for resp in (httpx.Response(503, json={"error": "boom"}),
                     httpx.Response(200, json={"no_token": True})):
            m = TokenMinter("http://minter.test")
            m._http = httpx.Client(transport=httpx.MockTransport(lambda r, _r=resp: _r))
            self.assertIsNone(m.mint())

    def test_connection_error_is_none_not_raise(self):
        def boom(_r):
            raise httpx.ConnectError("minter down")

        m = TokenMinter("http://minter.test")
        m._http = httpx.Client(transport=httpx.MockTransport(boom))
        self.assertIsNone(m.mint(), "铸造服务挂了不能把异常抛给业务路径")

    def test_key_header_is_sent_when_configured(self):
        seen = {}

        def handler(r: httpx.Request) -> httpx.Response:
            seen["key"] = r.headers.get("X-Minter-Key")
            return httpx.Response(200, json={"token": "TK"})

        m = TokenMinter("http://minter.test", key="secret")
        m._http = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(m.mint(), "TK")
        self.assertEqual(seen["key"], "secret")


class TestSettingsWiring(unittest.TestCase):
    def test_env_knobs(self):
        s = Settings.from_env({"AVM_MINTER_URL": "http://127.0.0.1:8899/",
                               "AVM_MINTER_KEY": "k", "AVM_MINTER_TIMEOUT": "9"})
        self.assertEqual(s.minter_url, "http://127.0.0.1:8899/")
        self.assertEqual(s.minter_key, "k")
        self.assertEqual(s.minter_timeout, 9.0)
        d = Settings.from_env({})
        self.assertEqual(d.minter_url, "", "默认不启用 ⇒ 行为与以前完全一致")

    def test_upstream_builder_passes_it_through(self):
        from unittest import mock

        from ark_compat import upstreams

        s = Settings(cookie=COOKIE, base_url="http://127.0.0.1:9", trust_env=False,
                     task_store="memory", minter_url="http://minter.test", minter_key="k")
        seen = {}
        orig = upstreams.WebClient

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        with mock.patch.object(upstreams, "WebClient", spy):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        m = seen.get("minter")
        self.assertIsNotNone(m, "构造上游时必须把铸造客户端传下去")
        self.assertTrue(m.configured)
        self.assertEqual(m.url, "http://minter.test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
