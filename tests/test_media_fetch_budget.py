#!/usr/bin/env python3
"""参考文件链接：取文件的上限/预算，以及上传链路的埋点。

为什么单开一个文件：**这条分支此前一行都没被执行过**。
app 层的"转存"用例用的是 `FakeWebClient`（把整个 `WebClient` 换掉），而真实客户端的上传
用例只传 **bytes** —— 于是 `WebClient.upload_file` 里那条
`isinstance(source, str) and ^https?://` 的**下载分支零覆盖**。用户报的
"接口带文件链接的图生视频都超时"正落在那个盲区里。

本文件钉四件事（每条都可被变异证伪）：
  1. **单项上限与预算**：链接声明过大 / 实际过大 ⇒ 400；超预算 ⇒ **504** 且点名 download。
  2. **总闸**：转存阶段跨媒体项**共享一把**预算；耗尽 ⇒ 504，并且**不会**接着提交上游
     —— 提交那一步是计费的，这是"调用方超时"最坏的后果（它超时了，我们还在跑并扣钱）。
  3. **预签名 PUT 的请求头**：只补预签名没给的（站点自己的实现就是原样用它返回的 headers）。
  4. **埋点**：download / uploads.getPresignedUrl / uploads.PUT 三条记录真的挂在
     `ark.create.submit` 的 `upstream_calls` 上（"上传有做埋点吗"的答案要有断言兜底）。

全部走 MockTransport，零真实网络。

运行：python3 -m unittest discover -s tests
"""

import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

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
from ark_compat.errors import ParamError, WebApiError  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_client import MediaFetchBudget, WebClient  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

from test_web_upstream import (  # noqa: E402
    FakeSite,
    ark_body,
    make_client,
    png_bytes,
    web_settings,
)

GATE = "gate-secret-123"
FILE_URL = "https://files.test/a.png"

# 站点侧任务记录：给 `_watch` 一个干净终态，免得后台盯梢刷一堆无关日志
SITE_TASK = {"id": "t1", "taskStatus": "succeed", "aiModel": "m", "url": "https://cdn/a.mp4",
             "kelingKeyId": "704", "credits": 0, "paid": False}


class _Chunks(httpx.SyncByteStream):
    """一块一块吐的响应体（**不带 Content-Length**）——用来构造"看起来小、其实很大"。"""

    def __init__(self, parts):
        self.parts = list(parts)

    def __iter__(self):
        yield from self.parts


class LinkSite(FakeSite):
    """在 FakeSite 之上再当"外部文件链接"的宿主 —— 下载与 tRPC 都走同一条 MockTransport。

    真客户端只有一个 httpx 客户端（tRPC + 下载 + PUT 共用），所以替身也得同时扮演两个角色。
    """

    def __init__(
        self,
        body: bytes | None = None,
        *,
        declared: int | None = None,
        delay: float = 0.0,
        chunks: list | None = None,
    ):
        super().__init__()
        self.body = png_bytes() if body is None else body
        self.declared = declared
        self.delay = delay
        self.chunks = chunks
        self.file_hits = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.test":
            self.requests.append(request)
            self.file_hits += 1
            if self.delay:
                time.sleep(self.delay)
            if self.chunks is not None:
                # 流式 + 无 Content-Length：声明检查看不到任何长度，只能靠"边读边判"
                return httpx.Response(200, stream=_Chunks(self.chunks))
            headers = {}
            if self.declared is not None:
                headers["Content-Length"] = str(self.declared)
            return httpx.Response(200, content=self.body, headers=headers)
        return super()._handle(request)


def first_frame(url: str) -> dict:
    return {"type": "image_url", "role": "first_frame", "image_url": {"url": url}}


def i2v_body(*urls: str) -> dict:
    return ark_body(
        content=[{"type": "text", "text": "a cat"}, *[first_frame(u) for u in urls]],
        ratio="adaptive",
    )


# ============================================================ 1 单项上限 / 预算 ----


class TestFetchMediaLimits(unittest.TestCase):
    """`_fetch_media`：声明上限、实际上限、单项预算、以及"404 不许变成 504"。"""

    def client_for(self, site: LinkSite, **kw) -> WebClient:
        return make_client(site, **kw)

    def test_a_normal_link_returns_the_bytes(self):
        site = LinkSite()
        self.assertEqual(self.client_for(site)._fetch_media(FILE_URL), png_bytes())

    def test_declared_size_over_the_hard_cap_is_rejected_before_reading(self):
        """链接**自己声明**超过硬上限 ⇒ 400，且压根不去读 body（别先缓冲再拒）。"""
        site = LinkSite(declared=WebClient.MEDIA_HARD_MAX_BYTES + 1)
        with self.assertRaises(ParamError) as ctx:
            self.client_for(site)._fetch_media(FILE_URL)
        self.assertIn("声明", str(ctx.exception), "必须是**声明**那条分支先拦下（而不是读完了才发现）")

    def test_actual_size_over_the_hard_cap_is_rejected_while_streaming(self):
        """**边读边判**：实际字节超上限要在读完之前拒掉（旧写法是全部读完再被 maxBytes 拒）。

        ⚠️ 用**无 Content-Length 的流式响应**，否则声明检查会先拦下，这条分支永远走不到
        （变异自证实测踩到：只断言"过大"的话，删掉流式检查靠声明检查兜住也会绿 ⇒
        断言必须只匹配本分支的用词「已读」）。
        """
        site = LinkSite(chunks=[b"\x89PNG", b"x" * 8192])
        with mock.patch.object(WebClient, "MEDIA_HARD_MAX_BYTES", 1024):
            with self.assertRaises(ParamError) as ctx:
                self.client_for(site)._fetch_media(FILE_URL)
        self.assertIn("已读", str(ctx.exception), "必须是**流式读到超量**那条分支拦下的")

    def test_slow_link_hits_the_per_item_budget_with_a_504(self):
        """🔴 核心修复：慢链接必须**在我们的预算内**失败，且报文点名是取文件这一步。"""
        site = LinkSite(delay=0.6)
        client = self.client_for(site)
        client.media_fetch_timeout = 0.2  # 直接压小：构造器有 1s 的下限
        with self.assertRaises(WebApiError) as ctx:
            client._fetch_media(FILE_URL)
        e = ctx.exception
        self.assertEqual(e.http_status, 504, "取文件超预算必须是 504（504=依赖超时，502=上游坏了）")
        self.assertIn("download", e.procedure)
        self.assertIn("预算", str(e))
        self.assertIn("files.test", str(e), "报文里必须带上网址，否则排障时不知道是哪个链接")

    def test_http_error_is_not_reported_as_a_timeout(self):
        """对照：链接 404 是 404、不是 504 —— 别把所有失败都糊成超时。"""
        site = LinkSite()
        site.body = b""
        client = self.client_for(site)
        client._http = httpx.Client(
            base_url="https://site.test", trust_env=False,
            transport=httpx.MockTransport(lambda r: httpx.Response(404, text="nope")),
        )
        with self.assertRaises(WebApiError) as ctx:
            client._fetch_media(FILE_URL)
        self.assertEqual(ctx.exception.http_status, 404)

    def test_connection_error_is_not_a_timeout_either(self):
        site = LinkSite()
        client = self.client_for(site)

        def boom(_r):
            raise httpx.ConnectError("refused")

        client._http = httpx.Client(
            base_url="https://site.test", trust_env=False, transport=httpx.MockTransport(boom)
        )
        with self.assertRaises(WebApiError) as ctx:
            client._fetch_media(FILE_URL)
        self.assertEqual(ctx.exception.http_status, 0, "连不上不是超时 —— 用 502 表达，别冒充 504")


    def test_oversize_bytes_source_is_rejected(self):
        """**字节路径**（`/v1/videos` 的文件部件、`input-reference-format: b64`）也受同一道上限。

        站点对"单件媒体"的上限与入口无关 —— 早点拒（400，说清上限）好过"传上去再被
        `maxBytes` 拒"；而这条路径此前**没有任何上限**。断言"没走到预签名"，即真的没出网。
        """
        site = LinkSite()
        client = self.client_for(site)
        with mock.patch.object(WebClient, "MEDIA_HARD_MAX_BYTES", 4096):
            with self.assertRaises(ParamError) as ctx:
                client.upload_file(png_bytes() + b"x" * 8192)
        self.assertIn("单件上限", str(ctx.exception))
        self.assertEqual(site.calls("uploads.getPresignedUrl"), [], "超限的件不该走到预签名")


class TestFormCapConsistency(unittest.TestCase):
    """两道闸的**口径一致性**：把它们放在同一处由门禁钉住，避免"改一处悄悄放行"。"""

    def test_the_documented_timeouts_match_the_code_defaults(self):
        """🔴 时间预算有**三处**载体（代码默认值 / `.env.example` / README 措辞），最容易漂。

        运维是照 `.env.example` 抄的、排障是照 README 读的 ⇒ 任一处漂了都会让人按错的数字
        设调用方超时。所以机械比对（与计费文案那条门禁同思路）。
        """
        from ark_compat.settings import Settings

        s = Settings.from_env({})
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        readme = (ROOT / "src/ark_compat/README.md").read_text(encoding="utf-8")
        for name, attr in (
            ("AVM_MEDIA_FETCH_TIMEOUT", "media_fetch_timeout"),
            ("AVM_MEDIA_REHOST_BUDGET", "media_rehost_budget"),
        ):
            # ⚠️ 每个默认值有**两个字面量**：dataclass 字段默认值（`Settings(...)` 直接构造时用，
            #    测试的 `web_settings()` 就走这条）与 `from_env` 里的 `or N` 兜底（读 env 时用）。
            #    只比其中一个的话，另一个漂了照样绿（变异自证实测踩到）。
            field_default = Settings.__dataclass_fields__[attr].default
            env_default = getattr(s, attr)
            self.assertEqual(
                int(field_default), int(env_default),
                f"{attr}：dataclass 默认 {field_default} 与 from_env 兜底 {env_default} 不一致 ——"
                f"会在「测试里构造」与「容器里读 env」两条路上给出不同的默认值",
            )
            declared = re.search(rf"^{name}=(\d+)", env_text, re.M)
            self.assertIsNotNone(declared, f".env.example 里没声明 {name}")
            self.assertEqual(
                int(declared.group(1)), int(field_default),
                f"{name}：模板写 {declared.group(1)}、代码默认 {field_default} —— 改一处必须改另一处",
            )
            self.assertIn(
                f"默认 **{int(field_default)}s**", readme,
                f"README 里没有把 {name} 的当前默认值写成「默认 **{int(field_default)}s**」——"
                f"排障的人会照错数字设调用方超时",
            )

    def test_the_two_media_caps_agree(self):
        """`app._FORM_PART_MAX_BYTES`（入口）与 `WebClient.MEDIA_HARD_MAX_BYTES`（转存）必须同值。

        分开写是为了不让 app 层反向导入 web_client；但**拆开就必须钉住**。
        """
        from ark_compat import app as app_module

        self.assertEqual(
            app_module._FORM_PART_MAX_BYTES, WebClient.MEDIA_HARD_MAX_BYTES,
            "两个上限不一致 ⇒ 一处改了另一处没改（同一个人为约定的上限有了两个口径）",
        )

    def test_the_body_cap_covers_the_legal_worst_case(self):
        """请求体上限必须容得下**站点合法的最坏组合**（图 4×10 + 视频 50 + 音频 2×15 = 120MB），
        否则我们自己的闸会拦掉正常请求。"""
        from ark_compat import app as app_module

        legal_worst_mb = 4 * 10 + 50 + 2 * 15
        self.assertGreater(
            app_module._FORMS_MAX_BODY_BYTES, legal_worst_mb * 1024 * 1024,
            "请求体上限小于合法最坏组合 ⇒ 正常请求会被自己的闸拦掉",
        )


# ================================================================ 2 总闸（预算） ----


class TestRehostBudget(unittest.TestCase):
    """跨媒体项的总预算：共享一把、可耗尽、耗尽后**绝不**继续提交。"""

    def test_budget_reports_elapsed_and_504_when_exhausted(self):
        b = MediaFetchBudget(30.0)
        self.assertFalse(b.expired())
        b.deadline = time.perf_counter() - 1.0
        self.assertTrue(b.expired())
        err = b.exhausted_error()
        self.assertEqual(err.http_status, 504)
        self.assertIn("预算", str(err))

    def test_step_timeout_never_exceeds_the_remaining_budget(self):
        client = WebClient("auth_session=x", base_url="https://site.test", trust_env=False)
        b = MediaFetchBudget(30.0)
        b.deadline = time.perf_counter() + 2.0
        self.assertLessEqual(client._step_timeout(120.0, b), 2.0 + 1e-6)
        self.assertEqual(client._step_timeout(120.0, None), 120.0)
        self.assertEqual(
            client._step_timeout(9999.0, None), WebClient.MAX_STEP_TIMEOUT,
            "没有下限夹取，但有上限 —— 防一个笔误把超时设成一天",
        )

    def test_one_budget_is_shared_by_every_media_item(self):
        """接线：`_rehost` 逐项传下去的是**同一把**预算（不是每项各造一个）。"""
        seen: list = []

        class RecordingClient:
            def upload_file(self, source, *, name=None, permanent=False, budget=None):
                seen.append(budget)
                return {"publicUrl": f"https://static.img2video.ai/r{len(seen)}.png"}

        up = WebUpstream(RecordingClient(), None, media_rehost_budget=45.0)
        params = up._rehost(
            {
                "imageUrl": "https://files.test/a.png",
                "lastFrameUrl": "https://files.test/b.png",
                "referenceImageUrls": ["https://files.test/c.png", "https://files.test/d.png"],
            },
            MediaFetchBudget(up.media_rehost_budget),
        )
        self.assertEqual(len(seen), 4)
        self.assertIs(seen[0], seen[-1], "4 个媒体项必须共用一把预算")
        self.assertIsNotNone(seen[0])
        self.assertEqual(up.media_rehost_budget, 45.0)
        self.assertTrue(all(v.startswith("https://static.img2video.ai/") for v in params.values()
                            if isinstance(v, str)))

    def test_a_failed_item_aborts_the_whole_submit(self):
        """🔴 底线：转存任何一项走不完，**绝不许**接着提交上游（提交即计费）。

        用真客户端 + 慢链接把第 1 项卡在预算上 ⇒ `submit` 一次都不能被调到。
        ⚠️ 2026-09-20 转存改**并行**后语义有一处变化：另一项**可能**已被同时取回
        （`file_hits` 不再恒为 1 —— 那只是带宽浪费，不是计费风险）。真正的不变量是
        「任一项失败 ⇒ 整次创建失败（504），上游提交一次都不发生」—— 下面照旧钉死。
        """
        site = LinkSite(delay=0.6)
        site.set("ai.minimaxH3", "t1")
        client = make_client(site)
        client.media_fetch_timeout = 0.2
        submitted: list = []

        class RecordingQueue:
            def submit(self, params, token=None):
                submitted.append(params)
                return "t1"

        up = WebUpstream(client, RecordingQueue(), media_rehost_budget=30.0)
        with self.assertRaises(WebApiError) as ctx:
            up.create({"web_params": {"referenceImageUrls": ["https://files.test/a.png",
                                                            "https://files.test/b.png"]}})
        self.assertEqual(ctx.exception.http_status, 504)
        self.assertEqual(submitted, [], "转存失败后绝不能接着提交（那一步会计费）")
        self.assertGreaterEqual(site.file_hits, 1, "至少第 1 项真的被尝试过（否则这条是空跑）")


# ============================================================ 3 预签名 PUT 的头 ----


class TestPresignedPutHeaders(unittest.TestCase):
    """站点自己的实现是 `fetch(v, {method:"PUT", headers: g, body: d})` —— **原样**用它给的头。"""

    def test_presign_headers_win_and_nothing_is_duplicated(self):
        site = FakeSite()
        # 预签名明确给了 Content-Type（与嗅探结果**不同**）⇒ 以它为准
        site.presign = {**site.presign, "headers": {"Content-Type": "image/jpeg", "x-amz-meta": "keep"}}
        make_client(site).upload_file(png_bytes())

        put = [r for r in site.requests if r.method == "PUT"][-1]
        self.assertEqual(put.headers["content-type"], "image/jpeg", "预签名给的头不许被我们覆盖")
        self.assertEqual(put.headers["x-amz-meta"], "keep", "自定义头必须原样带上（签名可能需要它）")
        self.assertEqual(len(put.headers.get_list("content-type")), 1, "不许出现重复头")
        self.assertEqual(len(put.headers.get_list("content-length")), 1, "Content-Length 交给 httpx 算")

    def test_content_type_is_filled_in_only_when_presign_omits_it(self):
        site = FakeSite()  # 默认 headers={}
        make_client(site).upload_file(png_bytes())
        put = [r for r in site.requests if r.method == "PUT"][-1]
        self.assertEqual(put.headers["content-type"], "image/png", "预签名没给才由我们补")


# ================================================================ 4 端到端 + 埋点 ----


class HttpCase(unittest.TestCase):
    """真客户端（MockTransport）+ 真 app：端到端看状态码与 span。"""

    def setUp(self):
        self.exporter = TestExporter()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            scrubbing=False,
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(), ns_timestamp_generator=TimeGenerator()
            ),
            additional_span_processors=[SimpleSpanProcessor(self.exporter)],
        )
        O._LOGFIRE_READY = True
        self.site = LinkSite()
        self.site.set("ai.minimaxH3", "t1")
        self.site.set("model.getModel", dict(SITE_TASK))
        self.client_api = make_client(self.site, media_fetch_timeout=5.0)
        app = create_app(web_settings(gate_key=GATE, enable_logfire=False))
        app.state.upstreams = {
            "web": WebUpstream(
                self.client_api,
                WebSubmitQueue(self.client_api, max_concurrent=2, poll_interval=0.01),
                media_rehost_budget=30.0,
            )
        }
        self.client = TestClient(app)

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    def post(self, body: dict):
        return self.client.post(TASKS_PATH, json=body, headers={"Authorization": f"Bearer {GATE}"})

    def create_span_calls(self) -> list:
        logfire.force_flush()
        spans = [s for s in self.exporter.exported_spans_as_dict() if s["name"] == "ark.create.submit"]
        self.assertEqual(len(spans), 1, f"期望恰好一条 ark.create.submit，实际 {len(spans)}")
        raw = spans[0]["attributes"].get("upstream_calls")
        return json.loads(raw) if isinstance(raw, str) else (raw or [])

    def test_upload_chain_is_recorded_on_the_create_span(self):
        """★ "文件上传有做埋点吗" —— 有，且这里把它钉住。

        三条记录都挂在 `ark.create.submit` 的 `upstream_calls` 上（**不是**独立 span）：
        下载 / 预签名 / PUT，各自带请求原文、耗时，失败时还带 `status=error`。
        """
        r = self.post(i2v_body(FILE_URL))
        self.assertEqual(r.status_code, 200, r.text)

        calls = self.create_span_calls()
        kinds = [c["call"] for c in calls]
        for expected in ("download", "uploads.getPresignedUrl", "uploads.PUT", "ai.minimaxH3"):
            self.assertIn(expected, kinds, f"{expected} 没被采集到 ⇒ 上传链路在那一段是无痕的")

        dl = [c for c in calls if c["call"] == "download"][0]
        self.assertEqual(dl["request"]["url"], FILE_URL)
        self.assertEqual(dl["response"]["bytes"], len(png_bytes()))
        self.assertEqual(dl["status"], "ok")
        put = [c for c in calls if c["call"] == "uploads.PUT"][0]
        self.assertEqual(put["response"]["publicUrl"], "https://cdn.example/x.png")
        self.assertEqual(put["request"]["fileSize"], len(png_bytes()))
        pre = [c for c in calls if c["call"] == "uploads.getPresignedUrl"][0]
        self.assertEqual(pre["request"]["method"], "POST")
        self.assertEqual(pre["request"]["input"]["fileSize"], len(png_bytes()))
        self.assertEqual(pre["request"]["input"]["contentType"], "image/png")
        # 凭证不进采集：请求头里绝不能出现站点 cookie
        self.assertNotIn("cookie", json.dumps(pre["request"]["headers"]).lower())

    def test_download_failure_is_recorded_as_an_error_record(self):
        """失败路径也要一条（`status=error` + 原始错误）——成功失败都留痕才是埋点。"""
        self.client_api.media_fetch_timeout = 0.2
        self.site.delay = 0.6
        r = self.post(i2v_body(FILE_URL))
        self.assertEqual(r.status_code, 504, r.text)

        calls = self.create_span_calls()
        dl = [c for c in calls if c["call"] == "download"]
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["status"], "error")
        self.assertIn("error", dl[0])

    def test_link_timeout_maps_to_504_and_never_reaches_the_upstream_submit(self):
        """🔴 三件事一起断言：**504**、**没有**发出创建请求（提交即计费）、以及**对外已脱敏**。

        脱敏后的对外报文只有"类别级事实 + Request ID"；步骤名（`download`）与调用方给的
        **链接**都属于内部细节，只留在 trace 里（证据换通道，不是消失）。
        """
        self.client_api.media_fetch_timeout = 0.2
        self.site.delay = 0.6
        r = self.post(i2v_body(FILE_URL))
        self.assertEqual(r.status_code, 504, r.text)
        self.assertEqual(
            self.site.calls("ai.minimaxH3"), [], "取文件失败后绝不能接着提交（提交即计费）"
        )
        msg = r.json()["error"]["message"]
        self.assertNotIn("download", msg, "步骤名是内部细节，不许出出口")
        self.assertNotIn("files.test", msg, "调用方给的链接不必回显到错误报文里")
        self.assertIn("Request ID:", msg, "脱敏之后 Request ID 是对账/报障的唯一抓手")
        # 对内：同一条链路的全量细节照旧在 trace 里
        dl = [c for c in self.create_span_calls() if c["call"] == "download"][0]
        self.assertIn("files.test", dl["request"]["url"])
        self.assertEqual(dl["status"], "error")

    def created_params(self) -> dict:
        """取出提交给站点的 tRPC input（`ai.minimaxH3` 那一次调用的实际载荷）。"""
        calls = self.site.calls("ai.minimaxH3")
        self.assertEqual(len(calls), 1, f"应当恰好提交一次，实际 {len(calls)}")
        return json.loads(calls[0].url.params["input"])["0"]["json"]

    def test_normal_link_creation_still_works(self):
        """对照组：正常链接必须走通（否则上面那些断言只是"什么都过不去"）—— 并断言
        **提交出去的 imageUrl 就是预签名给的 publicUrl**（"实现了" != "接线了"）。"""
        r = self.post(i2v_body(FILE_URL))
        self.assertEqual(r.status_code, 200, r.text)
        params = self.created_params()
        self.assertEqual(
            params.get("imageUrl"), self.site.presign["publicUrl"],
            "提交给站点的必须是**转存后**的地址（预签名返回的 publicUrl）",
        )
        self.assertNotIn("files.test", json.dumps(params), "外链不许原样透传给站点")


if __name__ == "__main__":
    unittest.main()
