#!/usr/bin/env python3
"""上游错误的**对外脱敏**：出口只给"类别级事实 + Request ID"，全量留在 trace。

为什么单开一个门禁：这是一条**出口契约**，而它的失败形态是"看起来一切正常"——
状态码照样 4xx/5xx、客户端照样解析得了，只是**把内部实现白送出去了**：

  · 站点内部 procedure 名（`ai.minimaxH3` / `model.needsCaptcha`）
  · 铸造服务主机与端口（`host.docker.internal:8899`）
  · **运维口令**（`ufw allow proto tcp from 172.16.0.0/12 to any port 8899`）
  · 内部 runbook 编号（`E2E-AVM-008`）与工具路径（`tools/compose_wiring_check.py`）
  · 上游返回的原始报文

没有**扫描型**断言就永远发现不了它（改一行 `str(exc)` 就够了，而功能测试全绿）。

用户口径（2026-09-15）：**客户端接口侧上游错误脱敏，参考火山 seedance 报错形态；
logfire 侧全部上报上游。** ⇒ 两个通道，本文件把两边都钉住：

  · 出口：官方形状（`code` / `message` / `param` / `type`，message 以 `Request ID: {id}` 结尾），
    且**黑名单扫描不过**（任何内部标识都不许出现）
  · 入口：trace 里照旧**全量**（`error` / `minter_last_error` / `upstream_calls`），
    另外多挂一条 `client_message` —— "调用方实际收到了什么"也要能被复盘

运行：python3 -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import logfire  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from logfire.testing import TestExporter  # noqa: E402

from ark_compat import observability as O  # noqa: E402
from ark_compat.app import OPENAI_VIDEOS_PATH, TASKS_PATH, create_app  # noqa: E402
from ark_compat.minter import TokenMinter  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

from test_poll_suppression import configure_tracing  # noqa: E402
from test_web_upstream import FakeSite, TrpcError, ark_body, make_client, web_settings  # noqa: E402

GATE = "gate-secret-123"
SITE_TASK = {"id": "t1", "taskStatus": "succeed", "aiModel": "m", "url": "https://cdn/a.mp4",
             "kelingKeyId": "704", "credits": 0, "paid": False}

# 🔴 出口黑名单：这些串**任何一条**出现在对外报文里就是泄漏。
#    只收"无歧义的内部标识"（`minter` 是我们的组件名、`8899` 是它的端口、`ufw` 是运维口令…），
#    不把 `captcha` / `Turnstile` / `upstream` 这类**调用方需要知道**的词放进来 ——
#    否则门禁会逼着我们写一句没用的"出错了"。
INTERNAL_TOKENS = (
    "ai.minimaxH3",
    "model.needsCaptcha",
    "model.getModel",
    "uploads.",
    "procedure",
    "minter",
    "host.docker.internal",
    "8899",
    "ufw",
    "172.16",
    "E2E-AVM",
    "compose_wiring_check",
    "docker",
    "127.0.0.1",
    "TRPC_ERROR",
    "Traceback",
)


class RedactionCase(unittest.TestCase):
    """真 app + 真站点替身 + 内存 exporter：每一条失败路径都过一遍出口黑名单。"""

    def setUp(self):
        self.exporter = TestExporter()
        configure_tracing(self.exporter)
        O._LOGFIRE_READY = True
        self.site = FakeSite()
        self.site.set("ai.minimaxH3", "t1")
        self.site.set("model.getModel", dict(SITE_TASK))

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    # ---- helpers ----
    def app_with(self, client) -> TestClient:
        """用给定的客户端建 app（**替身要连 minter 一起造进 client 里** —— 见
        `make_client(site, minter=...)`；把 minter 传给这里没有用）。"""
        app = create_app(web_settings(gate_key=GATE, enable_logfire=False))
        app.state.upstreams = {
            "web": WebUpstream(
                client, WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01)
            )
        }
        self.client = TestClient(app)
        return self.client

    def post(self, body: dict | None = None, *, path: str = TASKS_PATH, **kw):
        return self.client.post(
            path, json=body if body is not None else ark_body(),
            headers={"Authorization": f"Bearer {GATE}"}, **kw,
        )

    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def assert_redacted(self, r, *, code: str) -> dict:
        """出口三连：官方形状 + 黑名单扫描 + Request ID 可对账。"""
        self.assertEqual(set(r.json().keys()), {"error"}, r.text)
        err = r.json()["error"]
        self.assertEqual(
            set(err.keys()), {"code", "message", "param", "type"},
            f"官方错误对象只有这四个键（多一个键就是坏一个客户端）：{err}",
        )
        self.assertEqual(err["code"], code)
        blob = json.dumps(r.json(), ensure_ascii=False)
        for token in INTERNAL_TOKENS:
            self.assertNotIn(token, blob, f"内部标识 {token!r} 漏到出口了：{blob}")
        rid = r.headers.get("x-request-id")
        self.assertTrue(rid, "缺 x-request-id 头 —— 脱敏后它是调用方唯一的抓手")
        self.assertIn(f"Request ID: {rid}", err["message"], "message 必须以 Request ID 结尾（官方形态）")
        return err

    # ---- 场景 1：闸门开着 + 铸造服务不可达（用户贴的那条）----
    def test_captcha_gate_with_dead_minter_leaks_nothing(self):
        def boom(_request):
            raise httpx.ConnectError("connection refused")

        dead = TokenMinter("http://host.docker.internal:8899", timeout=2)
        dead._http = httpx.Client(transport=httpx.MockTransport(boom))
        self.site.set("model.needsCaptcha", True)
        self.app_with(make_client(self.site, minter=dead))

        r = self.post()
        self.assertEqual(r.status_code, 429, r.text)
        err = self.assert_redacted(r, code="RateLimitExceeded")
        # 调用方需要的**事实**必须留着（别脱敏成一句没用的"出错了"）
        self.assertIn("captcha", err["message"].lower())
        self.assertIn("aivideomaker_captcha_token", err["message"])

    def test_the_full_detail_is_still_reported_to_the_trace(self):
        """★ 对偶断言（用户口径"logfire 侧全部上报上游"）：出口脱敏，**入口全量**。"""

        def boom(_request):
            raise httpx.ConnectError("connection refused")

        dead = TokenMinter("http://host.docker.internal:8899", timeout=2)
        dead._http = httpx.Client(transport=httpx.MockTransport(boom))
        self.site.set("model.needsCaptcha", True)
        self.app_with(make_client(self.site, minter=dead))

        r = self.post()
        attrs = self.spans("ark.create.submit")[0]["attributes"]
        # ① 全量原文照旧（procedure 名 / 主机 / 归因）
        self.assertIn("ai.minimaxH3", str(attrs.get("error", "")))
        self.assertIn("host.docker.internal", str(attrs.get("minter_last_error", "")))
        self.assertIs(attrs.get("minter_unreachable"), True)
        # ② 调用方**实际收到的那句话**也留一份（出口回归只能靠它复盘）：
        #    span 上存的是**不含 Request ID** 的正文，客户端那份在其后追加了 ID
        self.assertTrue(attrs.get("client_message"), "span 上必须能看到对外那句")
        self.assertTrue(
            r.json()["error"]["message"].startswith(attrs["client_message"]),
            f"对外报文应当以 span 上的 client_message 开头：{r.json()['error']['message']!r}",
        )
        self.assertNotIn("minter", str(attrs.get("client_message", "")), "对外那句必须是脱敏版")

    # ---- 场景 2：上游读超时（用户贴的第二条）----
    def test_upstream_read_timeout_leaks_nothing(self):
        def timeout(_request):
            raise httpx.ReadTimeout("The read operation timed out")

        # `user_id` 显式给死 ⇒ 建任务时**第一发**就是 `model.needsCaptcha`（否则会先打
        # `auth.user`，断言就得跟着"谁先失败"漂）
        client = make_client(self.site, user_id="u-test")
        client._http = httpx.Client(
            base_url="https://site.test", trust_env=False, transport=httpx.MockTransport(timeout)
        )
        self.app_with(client)

        r = self.post()
        self.assertEqual(r.status_code, 502, r.text)
        err = self.assert_redacted(r, code="UpstreamError")
        # 调用方**需要的事实**要留住：这是"超时（可重试）"，不是笼统的"上游出错了"
        # （`ReadTimeout` 是通用 HTTP 客户端词汇，不含内部标识 ⇒ 不违反脱敏）
        self.assertIn("did not respond in time", err["message"])
        # 全量仍在 trace 里：**procedure 名 + httpx 原文**（脱敏只作用于出口）
        full = str(self.spans("ark.create.submit")[0]["attributes"]["error"])
        self.assertIn("model.needsCaptcha", full, "trace 里必须留着内部 procedure 名")
        self.assertIn("ReadTimeout", full, "trace 里必须留着 httpx 原文")

    # ---- 场景 3：上游 5xx（原始报文不许外泄）----
    def test_upstream_5xx_body_is_not_echoed_to_the_client(self):
        def boom(_request):
            return httpx.Response(503, text="internal-svc stacktrace at /srv/app/main.py:42")

        client = make_client(self.site)
        client._http = httpx.Client(
            base_url="https://site.test", trust_env=False, transport=httpx.MockTransport(boom)
        )
        self.app_with(client)

        r = self.post()
        self.assertEqual(r.status_code, 502, r.text)
        self.assert_redacted(r, code="UpstreamError")
        self.assertNotIn("stacktrace", r.text, "上游原始报文不许回显给调用方")
        # 但它在 trace 里照旧（对账/排障要看）
        self.assertIn("stacktrace", json.dumps(self.spans("ark.create.submit"), ensure_ascii=False))

    # ---- 场景 4：任务不存在（上游 tRPC 错误）----
    def test_upstream_not_found_leaks_nothing(self):
        self.site.set("model.getModel", error=TrpcError("task not found", code="NOT_FOUND"))
        self.app_with(make_client(self.site))
        r = self.post()
        self.assertEqual(r.status_code, 200, "查不到上游记录时仍回本地记录")
        # 拿任务的 404 走的是 ArkError（我们自己的报文），这里只验证它也没夹带内部标识
        r2 = self.client.get(f"{TASKS_PATH}/cgt-nope", headers={"Authorization": f"Bearer {GATE}"})
        self.assert_redacted(r2, code="TaskNotFound")

    # ---- 站点的内部错误码不许透传成我们的 code ----
    def test_site_error_code_is_not_passed_through(self):
        """🔴 `trpc` 会把站点信封里的 `data.code` 一起带进来 —— 那是**上游的内部词表**，
        不许出现在我们的 `code` 字段里（对外 code 是**白名单**，认不出的一律 `UpstreamError`）。"""
        self.site.set("ai.minimaxH3", error=TrpcError("nope", code="SITE_INTERNAL_42"))
        self.app_with(make_client(self.site, user_id="u-test"))
        r = self.post()
        self.assertEqual(r.status_code, 400, r.text)
        self.assert_redacted(r, code="UpstreamError")
        self.assertNotIn("SITE_INTERNAL_42", json.dumps(r.json(), ensure_ascii=False))
        # 它在 trace 里照旧（对账 / 排障要看站点到底给了什么码）
        self.assertIn(
            "SITE_INTERNAL_42", json.dumps(self.spans("ark.create.submit"), ensure_ascii=False)
        )

    # ---- 场景 5：OpenAI 面必须同样脱敏（两个入口共用一套出口）----
    def test_openai_face_is_redacted_too(self):
        """🔴 受理分离后（2026-09-20）本面**没有同步错误报文**可泄漏了：

        POST 恒 200 + 四字段契约；上游侧失败（含 transport 级异常）写进记录、
        由轮询给出 `failed` —— 而**六字段契约根本不带原因**。所以这条改为钉住：
        ① 受理报文不含任何上游细节；② 轮询报文不含任何上游细节与内部归因
        （`submit_error` 是给日志/span 的，不是给调用方的）；③ 完整细节照旧只在 trace。
        """

        def timeout(_request):
            raise httpx.ReadTimeout("The read operation timed out")

        # `user_id` 显式给死 ⇒ 建任务时**第一发**就是 `model.needsCaptcha`（否则会先打
        # `auth.user`，断言就得跟着"谁先失败"漂）
        client = make_client(self.site, user_id="u-test")
        client._http = httpx.Client(
            base_url="https://site.test", trust_env=False, transport=httpx.MockTransport(timeout)
        )
        self.app_with(client)
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            json={"model": "minimaxH3", "prompt": "p", "seconds": 5, "size": "adaptive"},
            headers={"Authorization": f"Bearer {GATE}"},
        )
        # 受理即返：四字段契约，**没有**任何上游细节可泄漏
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(r.json()), {"id", "object", "status", "created_at"})
        self.assertNotIn("The read operation timed out", r.text)
        tid = r.json()["id"]

        # 等后台提交真正失败（有界等待 —— 它跑在 daemon 线程里）
        import time

        deadline = time.time() + 5
        while time.time() < deadline:
            rec = self.client.app.state.tasks.get(tid)
            if rec and rec.get("submit_error"):
                break
            time.sleep(0.02)
        else:
            self.fail("后台提交没有按预期失败（submit_error 一直没落库）")

        # 轮询：failed 是合法终态；六字段契约 + 不含上游细节/内部归因
        r2 = self.client.get(
            f"{OPENAI_VIDEOS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["status"], "failed")
        dumped = json.dumps(r2.json(), ensure_ascii=False)
        self.assertNotIn("The read operation timed out", dumped, "上游原始异常文本不许出出口")
        self.assertNotIn("site.test", dumped, "上游域名不许出出口")
        self.assertNotIn("worker restart", dumped, "内部归因（submit_error）不许出现在响应体里")
        # 完整细节照旧只在 trace 里（对账/排障要看）
        self.assertIn("ReadTimeout", json.dumps(self.spans("ark.create.submit"), ensure_ascii=False))

    # ---- 对照：我们**自己**的报文不许被误伤 ----
    def test_our_own_messages_keep_their_useful_text(self):
        """脱敏只作用于**上游**错误。参数校验之类是我们自己的契约文案，必须**原样保留**
        （否则调用方拿不到"哪个字段错了"），只是补一个 Request ID。"""
        self.app_with(make_client(self.site))
        r = self.post({
            "model": "minimaxH3",
            "content": [{"type": "text", "text": "p"}],
            "ratio": "bogus",          # 不在 ratio 枚举里 ⇒ translate 层给 400
        })
        self.assertEqual(r.status_code, 400, r.text)
        err = self.assert_redacted(r, code="InvalidParameter")
        self.assertIn("bogus", err["message"], "我们自己的报文要留着'哪个值错了'")

    # ---- 场景 6：站点把「套餐并发打满」报成 tRPC **500 + INTERNAL_SERVER_ERROR** ----
    def test_upstream_500_queue_full_from_the_customer_report_leaks_nothing(self):
        """客户报障原文（2026-09-16）：

            INTERNAL_SERVER_ERROR status=500 — ai.minimaxH3: The queue is full.
            The pro plan can only run 4 task at a time.

        站点把并发打满报成 **tRPC 500 + `data.code=INTERNAL_SERVER_ERROR`**（见
        `docs/web-reverse/TESTCASES.md` ④；premium 是 2、pro 是 4）。

        🔴 这是一条**历史泄漏**的回归锚：`eeef9db` 之前 `_web_code_for` 是
        `… else e.code` 兜底、message 直接给 `str(exc)` ⇒ 站点自己的错误码与原文**原样**
        出现在出口（`code: "INTERNAL_SERVER_ERROR"` + `ai.minimaxH3: The queue is full…`）。
        把 `_web_code_for` 的兜底改回 `e.code` 时本用例必须变红（已变异自证）。
        """
        raw = "ai.minimaxH3: The queue is full. The pro plan can only run 4 task at a time."

        class QueueFullSite(FakeSite):
            """只把创建 procedure 换成「站点 500 + INTERNAL_SERVER_ERROR」。"""

            def _handle(self, request):
                if request.url.path == "/api/ai.minimaxH3":
                    self.requests.append(request)
                    return httpx.Response(
                        500,
                        json=[{"error": {"json": {
                            "message": raw,
                            "data": {"code": "INTERNAL_SERVER_ERROR", "httpStatus": 500},
                        }}}],
                    )
                return super()._handle(request)

        site = QueueFullSite()
        site.set("model.needsCaptcha", False)
        self.app_with(make_client(site, user_id="u-test"))

        r = self.post()
        # 上游 500 不在我们对外放行的状态码白名单里 ⇒ 收敛成 502（别把上游的 500 当我们的 500）
        self.assertEqual(r.status_code, 502, r.text)
        self.assert_redacted(r, code="UpstreamError")
        for token in ("INTERNAL_SERVER_ERROR", "queue is full", "pro plan", "4 task"):
            self.assertNotIn(token, r.text, f"{token!r} 漏到出口了：{r.text}")
        # 对偶：全量原文仍在 trace（脱敏只作用于出口）
        span = self.spans("ark.create.submit")[0]["attributes"]
        self.assertIn("queue is full", str(span.get("error", "")))
        self.assertIn("ai.minimaxH3", str(span.get("error", "")))


class TestClientMessagePhases(unittest.TestCase):
    """阶段区分：**调用方自己的素材** vs **上游自己**。

    用户口径（2026-09-16）：超时**多发生在传图片 / 传 URL 这条路上**。那时快慢取决于调用方
    的素材，一句笼统的"上游没响应"会把责任指错方向、也拿不到"换更快直链"这个行动项。
    """

    @staticmethod
    def msg(procedure: str, *, http_status: int = 0, transport: str = "", code: str | None = None) -> str:
        from ark_compat.app import _upstream_client_message
        from ark_compat.errors import WebApiError

        return _upstream_client_message(
            WebApiError(procedure, "boom", code=code, http_status=http_status, transport=transport)
        )

    def test_supplied_link_timeout_points_at_the_caller_file(self):
        m = self.msg("download", http_status=504)
        self.assertIn("reference file you supplied", m)
        self.assertNotIn("upstream did not respond", m, "别把责任指向上游 —— 是调用方给的链接慢")
        self.assertNotIn("download", m, "procedure 名不许出现")

    def test_reference_upload_timeout_points_at_the_upload_step(self):
        self.assertIn("uploading a reference file", self.msg("uploads.PUT", transport="ReadTimeout"))

    def test_create_timeout_stays_generic_and_retryable(self):
        m = self.msg("ai.minimaxH3", transport="ReadTimeout")
        self.assertIn("did not respond in time", m)
        self.assertIn("retry", m, "超时要给出'可重试'这个行动项")
        self.assertNotIn("ai.minimaxH3", m)

    def test_transport_failures_are_classified_without_internals(self):
        self.assertIn("unreachable", self.msg("model.getModel", transport="ConnectError"))

    def test_no_phase_variant_leaks_an_internal_token(self):
        variants = [
            self.msg("download", http_status=504),
            self.msg("download", http_status=404),
            self.msg("uploads.getPresignedUrl", http_status=500),
            self.msg("uploads.PUT", transport="ReadTimeout"),
            self.msg("uploads.PUT", http_status=403),
            self.msg("ai.minimaxH3", transport="ReadTimeout"),
            self.msg("ai.minimaxH3", http_status=500),
            self.msg("model.needsCaptcha", transport="ConnectError"),
            self.msg("model.getModel", http_status=403),
        ]
        for m in variants:
            for token in INTERNAL_TOKENS:
                self.assertNotIn(token, m, f"{token!r} 出现在对外句里：{m}")


if __name__ == "__main__":
    unittest.main()
