#!/usr/bin/env python3
"""trace 契约：把 span 属性当成契约，用**内存 exporter 捞回来**断言。

为什么不是"日志里有字样"：`upstream_task_id` / `status` / `paid` / 请求响应原文
这些属性只活在运行时。任何重构都能把它们删掉，而**测试全绿、日志照打** ——
唯一的觉察时机是事故发生时的排查现场（那时你才发现"这一列是空的"）。

本文件钉三类东西：

1. **采集层**（`client._req` / `web_client.trpc`）：请求 / 响应原文、taskId、
   耗时；失败路径同样留痕（上游拒绝的理由只出现一次）。
2. **span 组装层**（app 里的四个具名 span）：属性名 + 值，并且**成功与失败都要有**
   （失败路径丢属性 = trace 里与"没发生过调用"无法区分）。
3. **凭证红线**：脱敏默认关着，所以"key / cookie / Authorization 不进 trace"
   只能靠 `capture_headers=False` 这条硬防线 —— 这里用真 app 跑一遍全量 span，
   断言密钥串**一次都不出现**。

一个必须照抄的坑：logfire 把**非原始类型**的属性值序列化成 JSON 文本再挂上 span
（并附加 `logfire.json_schema`），所以断言前要过一层 `json.loads`。
`TestExporter` 看到的就是导出前形态，不确定时先 `json.dumps` 打出来看。

运行：python3 -m unittest tests.test_trace_contract
"""

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import logfire  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from logfire.testing import (  # noqa: E402
    IncrementalIdGenerator,
    SimpleSpanProcessor,
    TestExporter,
    TimeGenerator,
)

from ark_compat import observability as O  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

# 复用 web 上游那套**真站点替身**（httpx.MockTransport），不另造一个 —— 只有真走
# WebClient.trpc 才会路过采集点；换成 FakeWebClient 就等于把被测的那层整个绕开。
from test_web_upstream import (  # noqa: E402
    FakeSite,
    TrpcError,
    ark_body,
    make_client,
    png_bytes,
    web_settings,
)

COOKIE = "auth_session=deadbeef"
GATE = "gate-secret-123"

# 站点侧的任务记录：succeed + 一个**带凭证的 presigned 出片地址**。
# 后者是刻意的：默认脱敏规则里 `credential` 会命中它，整值会被替换成
# `[Scrubbed due to 'Credential']` —— 正好用来证明"脱敏确实关着"。
SITE_TASK = {
    "id": "t1",
    "taskStatus": "succeed",
    "aiModel": "minimax-h3",
    "url": "https://cdn.test/a.mp4?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=abc",
    "kelingKeyId": "704",
    "credits": 0,
    "paid": False,
}


class TestExchangeRecorder(unittest.TestCase):
    """采集层：有没有原文、失败路径有没有留痕、凭证有没有被摘掉。"""

    def test_web_trpc_records_input_and_raw_envelope_without_cookie(self):
        site = FakeSite()
        site.set("ai.minimaxH3", "t1")
        client = make_client(site, user_id="u1")  # 免掉 auth.user 那次会话解析

        with O.upstream_exchanges() as box:
            self.assertEqual(client.create({"content": "a cat", "duration": 5}), "t1")

        # 提交前会先问一次风控闸门（needsCaptcha），所以按 procedure 取而不是取第 0 条
        rec = [c for c in box if c["call"] == "ai.minimaxH3"][0]
        self.assertEqual(rec["upstream"], "web")
        self.assertEqual(rec["task_id"], "t1")
        self.assertEqual(rec["request"]["input"]["content"], "a cat")
        self.assertEqual(rec["request"]["input"]["token"], None)
        # 原始信封（不是解开后的值）：排障要看到 result/error 结构本身
        self.assertEqual(rec["response"]["body"][0]["result"]["data"]["json"], "t1")
        blob = json.dumps(box)
        self.assertNotIn("cookie", blob.lower())
        self.assertNotIn(COOKIE, blob)

    def test_rejected_submit_is_visible_as_an_empty_response(self):
        """站点把 `token: null` 静默拒掉（返回空串）—— 这个空串必须留在 trace 里，
        否则"提交失败"与"没提交过"就分不清了。"""
        site = FakeSite()
        site.set("ai.minimaxH3", "")
        client = make_client(site, user_id="u1")

        with O.upstream_exchanges() as box:
            with self.assertRaises(Exception):
                client.create({"content": "a cat"})

        rec = [c for c in box if c["call"] == "ai.minimaxH3"][0]
        self.assertEqual(rec["response"]["body"][0]["result"]["data"]["json"], "")
        self.assertNotIn("task_id", rec)

    def test_nothing_is_collected_when_nobody_is_watching(self):
        """没有采集箱时立即返回：不建列表、不序列化（轮询线程里每 10 秒一次的长尾）。"""
        site = FakeSite()
        site.set("ai.minimaxH3", "t1")
        client = make_client(site, user_id="u1")
        self.assertIsNone(O._EXCHANGES.get())
        client.create({"content": "x"})
        self.assertIsNone(O._EXCHANGES.get())


class TestValueClipping(unittest.TestCase):
    """不脱敏，但要控体积：几 MB 的 data URI 原样挂上去会把 trace 撑爆。"""

    def test_data_uri_becomes_a_summary(self):
        uri = "data:image/png;base64," + "A" * 5000
        out = O.clip(uri)
        self.assertIn("data-uri image/png", out)
        self.assertNotIn("AAAA", out)

    def test_long_strings_are_truncated_not_dropped(self):
        out = O.clip("x" * 50, limit=10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("truncated", out)

    def test_containers_keep_their_shape(self):
        out = O.clip({"a": [1, {"b": "ok"}]})
        self.assertEqual(out, {"a": [1, {"b": "ok"}]})

    def test_bytes_are_summarised(self):
        self.assertEqual(O.clip(b"12345"), "<5 bytes>")

    def test_secret_headers_are_filtered(self):
        out = O.safe_headers({"key": "k", "Cookie": "c", "X-Max-Credits": "5", "Accept": "json"})
        self.assertEqual(out, {"X-Max-Credits": "5", "Accept": "json"})


class TestLogfireWiring(unittest.TestCase):
    """装配层：`Settings` 里的开关必须真的传到 `logfire.configure`。

    坑：只测"我自己 configure 出来的 span 长什么样"是测不到接线错的 ——
    生产是 `setup_observability` 调的 configure。这里把它拦下来看参数。
    """

    def _configure_kwargs(self, settings) -> dict:
        seen: dict = {}
        with mock.patch.object(logfire, "configure", lambda **kw: seen.update(kw)), mock.patch.object(
            logfire, "instrument_httpx", lambda **kw: None
        ):
            was = O._LOGFIRE_READY
            O._LOGFIRE_READY = False
            try:
                O.setup_observability(settings)
            finally:
                O._LOGFIRE_READY = was
        return seen

    def test_defaults_record_request_and_response_unscrubbed(self):
        kw = self._configure_kwargs(web_settings(enable_logfire=True))
        self.assertIs(kw["scrubbing"], False, "默认关脱敏：请求/响应要能直接读")
        self.assertIs(kw["inspect_arguments"], False, "函数参数可能是 key/token，不自动记录")

    def test_scrubbing_can_be_turned_back_on_by_env(self):
        from ark_compat.settings import Settings

        self.assertTrue(Settings.from_env({"AVM_LOGFIRE_SCRUBBING": "1"}).logfire_scrubbing)
        self.assertFalse(Settings.from_env({}).logfire_scrubbing)
        kw = self._configure_kwargs(web_settings(enable_logfire=True, logfire_scrubbing=True))
        self.assertIs(kw["scrubbing"], True)

    def test_env_knobs_are_read(self):
        from ark_compat.settings import Settings

        s = Settings.from_env({"AVM_LOGFIRE_MAX_CHARS": "500", "AVM_LOGFIRE_CAPTURE_HEADERS": "1"})
        self.assertEqual(s.logfire_max_chars, 500)
        self.assertTrue(s.logfire_capture_headers)
        self.assertEqual(Settings.from_env({}).logfire_max_chars, 20000)


class TestSpanContract(unittest.TestCase):
    """真 app + 真站点替身 + TestExporter：属性名与值都钉住。"""

    def setUp(self):
        # 陷阱：app 的 lifespan 会重新 configure 观测 SDK，把你的 exporter 顶掉。
        # 这里用 enable_logfire=False 建 app（它就不会碰 logfire 全局配置），
        # 再自己 configure 一次，并把 span() 的开关打开。
        self.exporter = TestExporter()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            scrubbing=False,  # 与生产默认一致：请求/响应原文要能直接读
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(),
                ns_timestamp_generator=TimeGenerator(),
            ),
            additional_span_processors=[SimpleSpanProcessor(self.exporter)],
        )
        O._LOGFIRE_READY = True

        self.site = FakeSite()
        self.site.set("model.getModel", dict(SITE_TASK))
        self.site.set("ai.minimaxH3", "t1")
        app = create_app(web_settings(gate_key=GATE))
        client = make_client(self.site)
        app.state.upstreams = {
            "web": WebUpstream(client, WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01))
        }
        self.client = TestClient(app)

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    # ---- helpers ----

    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def one_span(self, name: str) -> dict:
        found = self.spans(name)
        self.assertEqual(len(found), 1, f"期望恰好一条 {name}，实际 {len(found)} 条")
        return found[0]

    def attrs(self, name: str) -> dict:
        return self.one_span(name)["attributes"]

    def as_json(self, value):
        """容器属性导出时是 JSON 文本（logfire 行为），断言前必须还原。"""
        return json.loads(value) if isinstance(value, str) else value

    def post_task(self, body: dict | None = None) -> dict:
        r = self.client.post(TASKS_PATH, json=body or ark_body(), headers={"Authorization": f"Bearer {GATE}"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---- 创建 ----

    def test_create_span_carries_request_response_and_upstream_task_id(self):
        created = self.post_task()
        attrs = self.attrs("ark.create.submit")

        self.assertEqual(attrs["upstream"], "web")
        self.assertEqual(attrs["upstream_task_id"], "t1", "上游 taskId 必须能被反查到")
        self.assertIn("a cat", json.dumps(self.as_json(attrs["request"])), "Ark 请求原文")

        calls = self.as_json(attrs["upstream_calls"])
        self.assertEqual(attrs["upstream_call_count"], len(calls))
        kinds = [c["call"] for c in calls]
        self.assertIn("ai.minimaxH3", kinds)
        submit = [c for c in calls if c["call"] == "ai.minimaxH3"][0]
        self.assertEqual(submit["task_id"], "t1")
        self.assertEqual(submit["request"]["input"]["content"], "a cat")
        self.assertEqual(submit["response"]["body"][0]["result"]["data"]["json"], "t1")
        self.assertEqual(created["id"][:4], "cgt-")

    def test_create_span_records_the_ark_model_and_billing_line(self):
        self.post_task(ark_body(extra_body={"aivideomaker_tier": "base"}))
        attrs = self.attrs("ark.create.submit")
        self.assertEqual(attrs["ark_model"], "doubao-seedance-2-5-260628")
        self.assertTrue(attrs["billed"], "tier=base 在 web 线一律计费")

    def test_create_span_keeps_exchanges_when_the_upstream_rejects(self):
        """失败路径最需要证据：'站点把提交拒了' 与 '我们没提交' 必须能区分。"""
        self.site.set("ai.minimaxH3", "")
        r = self.client.post(TASKS_PATH, json=ark_body(), headers={"Authorization": f"Bearer {GATE}"})
        self.assertEqual(r.status_code, 502, "web 线提交被拒 → 上游错误映射（非 429，429 只留给验证码闸门）")
        attrs = self.attrs("ark.create.submit")
        self.assertIn("error", attrs)
        self.assertNotIn("upstream_task_id", attrs, "失败时不该有 taskId")
        calls = self.as_json(attrs["upstream_calls"])
        rejected = [c for c in calls if c["call"] == "ai.minimaxH3"][0]
        self.assertEqual(rejected["response"]["body"][0]["result"]["data"]["json"], "")

    def test_dry_run_records_the_request_without_touching_the_site(self):
        body = ark_body(extra_body={"aivideomaker_dry_run": True})
        self.post_task(body)
        attrs = self.attrs("ark.create.dry_run")
        self.assertIn("a cat", json.dumps(self.as_json(attrs["request"])))
        self.assertEqual(self.site.calls("ai.minimaxH3"), [], "dry-run 绝不产生提交")
        self.assertEqual(self.spans("ark.create.submit"), [])

    def test_inline_data_uri_is_summarised_everywhere(self):
        """data URI 要么变摘要、要么变站点 URL —— 绝不能以 base64 原文进 trace。"""
        raw = base64.b64encode(png_bytes()).decode()
        body = ark_body(
            content=[
                {"type": "text", "text": "a cat"},
                {"type": "image_url", "role": "first_frame", "image_url": {"url": f"data:image/png;base64,{raw}"}},
            ],
            ratio="adaptive",
        )
        self.post_task(body)
        blob = json.dumps(self.exporter.exported_spans_as_dict())
        self.assertIn("<data-uri image/png", blob)
        self.assertNotIn(raw, blob)

    # ---- 查询 ----

    def test_fetch_span_carries_status_paid_and_the_normalized_task(self):
        tid = self.post_task()["id"]
        r = self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})
        self.assertEqual(r.status_code, 200)

        attrs = self.attrs("ark.task.fetch")
        self.assertEqual(attrs["upstream_task_id"], "t1")
        self.assertEqual(attrs["status"], "succeeded")
        self.assertFalse(attrs["paid"], "paid=false 才是'这次没花钱'的判据")
        task = self.as_json(attrs["upstream_response"])
        self.assertEqual(task["content"]["video_url"], SITE_TASK["url"])
        self.assertEqual(task["resolution"], "704p", "站点回填的是实际值（kelingKeyId=704）")

    def test_fetch_span_keeps_the_site_record_verbatim(self):
        """归一化会把站点字段抹平；原始记录是唯一的对账依据，必须仍在 trace 里。"""
        tid = self.post_task()["id"]
        self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})
        calls = self.as_json(self.attrs("ark.task.fetch")["upstream_calls"])
        get_model = [c for c in calls if c["call"] == "model.getModel"][0]
        self.assertEqual(get_model["response"]["body"][0]["result"]["data"]["json"]["kelingKeyId"], "704")

    def test_fetch_failure_keeps_the_error_on_the_span(self):
        """上游说"没这条任务"时，失败原因与它回的原文都要留在 span 上 ——
        否则 trace 里与"我们根本没查"无法区分。"""
        tid = self.post_task()["id"]
        self.site.set("model.getModel", error=TrpcError("task not found", code="NOT_FOUND"))
        r = self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})
        self.assertEqual(r.status_code, 200, "查不到上游记录时仍回本地记录")
        attrs = self.attrs("ark.task.fetch")
        self.assertEqual(attrs["upstream_task_id"], "t1")
        self.assertIn("NOT_FOUND", attrs["error"])
        calls = self.as_json(attrs["upstream_calls"])
        self.assertEqual(calls[0]["call"], "model.getModel")
        self.assertEqual(calls[0]["status"], "error")
        self.assertEqual(calls[0]["response"]["body"][0]["error"]["json"]["data"]["code"], "NOT_FOUND")

    # ---- 取消 ----

    def test_cancel_span_reports_the_truth_about_the_missing_endpoint(self):
        tid = self.post_task()["id"]
        j = self.client.delete(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}).json()
        self.assertFalse(j["cancelled"])
        attrs = self.attrs("ark.task.cancel")
        self.assertEqual(attrs["upstream_task_id"], "t1")
        self.assertEqual(attrs["ark_id"], tid)
        self.assertFalse(attrs["cancelled"], "站点没有取消端点，绝不能上报已取消")
        self.assertIn("no cancel endpoint", self.as_json(attrs["upstream_response"])["reason"])

    # ---- 凭证红线 ----

    def test_no_credential_ever_reaches_a_span(self):
        """脱敏关着 ⇒ 防线只剩 capture_headers=False。这里断言它真的在。

        两个串都来自真实路径：站点 cookie 与调用方 Bearer。
        """
        tid = self.post_task()["id"]
        self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})
        blob = json.dumps(self.exporter.exported_spans_as_dict(), ensure_ascii=False)
        self.assertTrue(blob, "没捞到 span，断言会变成空跑")
        self.assertNotIn(COOKIE, blob)
        self.assertNotIn("auth_session", blob)
        self.assertNotIn(GATE, blob)

    def test_scrubbing_is_off_so_credential_bearing_urls_stay_readable(self):
        """默认脱敏会把这个出片地址整条替换成 `[Scrubbed due to 'Credential']`，
        host/桶/key 的分辨力一并消失 —— 那正是排障时要看的东西。"""
        tid = self.post_task()["id"]
        self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})
        blob = json.dumps(self.exporter.exported_spans_as_dict(), ensure_ascii=False)
        self.assertIn("X-Amz-Credential", blob)
        self.assertNotIn("[Scrubbed", blob)

    def test_spans_are_actually_produced_so_the_contract_can_fail(self):
        """空跑守卫：上面每一条断言都建立在"确实有 span 落地"之上。"""
        self.post_task()
        names = {s["name"] for s in self.exporter.exported_spans_as_dict()}
        self.assertIn("ark.create.submit", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
