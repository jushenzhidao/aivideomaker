#!/usr/bin/env python3
"""成片对外出口（`/v/{ark_id}.mp4`）的契约与**脱敏门禁**。

本文件钉住的唯一一件事：**上游的域名与模型名不许从任何一条出口漏出去** ——
包括 URL、路径，以及**响应头**。

为什么响应头必须单独钉（2026-09-16 实测取证）：上游回的响应里带着

    content-disposition: attachment; filename="1635002_0_minimax_h3_1635002.mp4"
    server: cloudflare
    cf-ray: a3bc9fde5fde5d56-AMS

也就是说**只把 URL 换成自己的域名是不够的**：只要还在转发上游响应头，模型名就
跟着每一次下载暴露，`cf-ray` / `server` 还会指认上游。本文件对这几个头一并钉住，
并采用**白名单**语义（不在名单里的一律不透传）—— 这样以后上游新增什么头都不会
顺着这条管子漏出去，而黑名单会静默失守。

两条纪律与其余测试一致：**零额度消耗、零外发**（回源走 `httpx.MockTransport`，
上游 base_url 指向死端口）。

运行：python3 tests/test_media_proxy.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import TASKS_PATH, _media_ext, _strip_media_ext, create_app  # noqa: E402
from ark_compat.media_proxy import (  # noqa: E402
    MediaProxy,
    extension_of,
)
from ark_compat.settings import Settings, _normalize_media_path  # noqa: E402

DEAD_UPSTREAM = "http://127.0.0.1:9"  # discard 端口，保证不出网
PUBLIC_BASE = "https://api.example.com"

# 真实形态的上游直链（取自 2026-09-16 的实测样本）
SITE_URL = (
    "https://static2.img2video.ai/1789479777409-129fadb6-87a4-46ed-86ae-be46163debdc"
    "-1635002_0_minimax_h3_1635002.mp4"
)
SITE_FILENAME = "1635002_0_minimax_h3_1635002.mp4"
BODY = b"FAKE-MP4-BYTES" * 512
ARK_ID = "cgt-20260916-a1b2c3d4"

# 上游会回的头（原样照抄实测样本）
SITE_HEADERS = {
    "content-type": "video/mp4",
    "content-length": str(len(BODY)),
    "content-disposition": f'attachment; filename="{SITE_FILENAME}"',
    "server": "cloudflare",
    "cf-ray": "a3bc9fde5fde5d56-AMS",
    "nel": '{"report_to":"cf-nel"}',
    "report-to": '{"group":"cf-nel"}',
    "accept-ranges": "bytes",
    "etag": '"0d8d4e00bf111471528acca41f2e95a8"',
    "last-modified": "Tue, 15 Sep 2026 13:42:58 GMT",
}


def site_handler(request: httpx.Request) -> httpx.Response:
    """假上游 CDN：按真实形态回头。Range 请求照 206 回。"""
    rng = request.headers.get("range") or ""
    if rng:
        return httpx.Response(
            206,
            headers={**SITE_HEADERS, "content-range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY,
        )
    return httpx.Response(200, headers=SITE_HEADERS, content=BODY)


class StubUpstream:
    """只实现 `_internal_view` 需要的 `get_task`（返回**已归一化**的视图）。"""

    kind = "web"

    def __init__(self, status: str = "succeeded", url: str = SITE_URL):
        self.status = status
        self.url = url
        self.calls = 0

    def get_task(self, task_id):
        self.calls += 1
        content = {"video_url": self.url} if self.status == "succeeded" and self.url else {}
        return {
            "id": task_id,
            "status": self.status,
            "content": content,
            "usage": {"credits": 1, "paid": False},
            "duration": 5,
            "ratio": "16:9",
            "created_at": 1,
            "updated_at": 2,
        }

    def balance(self):
        return 0

    def health(self):
        return {}


def build_app(*, enabled=True, status="succeeded", handler=None, task=True,
              passthrough=False, owner=""):
    """返回 `(app, client)`。刻意**不**用 `with TestClient(...)` —— 那会触发 lifespan。"""
    s = Settings(
        cookie="" if passthrough else "auth_session=deadbeef",
        passthrough_cookie=passthrough,
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,        # 避免测试互相污染全局 logfire
        trust_env=False,             # 绕开系统代理，保证"死端口"真的是死端口
        task_store="memory",
        account_report_seconds=0,    # 不起号池上报后台任务
        task_cache_ttl=0,            # 关节流缓存：让每次查询都真的走上游（stub，零成本）
        public_base=PUBLIC_BASE if enabled else "",
    )
    s.validate()
    app = create_app(s)
    stub = StubUpstream(status)
    app.state.upstreams["web"] = stub
    app.state.media_proxy = MediaProxy(
        public_base=PUBLIC_BASE if enabled else "",
        trust_env=False,
        transport=httpx.MockTransport(handler or site_handler),
    )
    if task:
        app.state.tasks.put({
            "id": ARK_ID, "taskId": "up-1", "upstream": "web", "owner": owner,
            "model": "doubao-seedance-2-5-260628", "requested": {}, "effective": {},
            "warnings": [], "unsupported": [], "createdAtMs": 1,
        })
    return app, TestClient(app), stub


def explode(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("boom")


class TestAddressShape(unittest.TestCase):
    """对外地址的形状：三段（基址 / 前缀 / 任务 id）必须全是我们自己的。"""

    def test_download_url_carries_no_upstream_trace(self):
        p = MediaProxy(public_base=PUBLIC_BASE + "/")
        url = p.download_url(ARK_ID, SITE_URL)
        self.assertEqual(url, f"{PUBLIC_BASE}/v/{ARK_ID}.mp4")
        for leak in ("img2video", "minimax", "static2", "1635002"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, url)

    def test_extension_is_taken_from_the_source_only(self):
        """扩展名只从源地址**后缀**取 —— 上游文件名里的任何字符都不许带进来。"""
        self.assertEqual(extension_of(SITE_URL), ".mp4")
        self.assertEqual(extension_of("https://x/a/b.mov"), ".mov")
        # 白名单之外（含无后缀、超长"后缀"、非 ASCII）一律归 .mp4
        for weird in ("https://x/a.exe", "https://x/a", "https://x/a.b", "https://x/中文名"):
            with self.subTest(url=weird):
                self.assertEqual(extension_of(weird), ".mp4")

    def test_download_url_is_the_same_without_a_known_extension(self):
        p = MediaProxy(public_base=PUBLIC_BASE)
        self.assertEqual(p.download_url(ARK_ID), f"{PUBLIC_BASE}/v/{ARK_ID}.mp4")

    def test_name_round_trip(self):
        """`/v/{name}` 里的名字要能还原回任务 id（下载端点按它查记录）。"""
        name = f"{ARK_ID}.mp4"
        self.assertEqual(_strip_media_ext(name), ARK_ID)
        self.assertEqual(_media_ext(name), ".mp4")
        # 剥不动就原样返回 ⇒ 查不到 = 404，不会误命中别的记录
        self.assertEqual(_strip_media_ext("cgt-20260916-a1b2c3d4"), ARK_ID)
        self.assertEqual(_media_ext("cgt-20260916-a1b2c3d4"), ".mp4")


class TestPathNormalization(unittest.TestCase):
    def test_blank_and_slash_normalize_to_v(self):
        for raw in ("", "   ", "/", "v", "v/", "/v/", None):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_media_path(raw), "/v")

    def test_custom_path(self):
        self.assertEqual(_normalize_media_path("media"), "/media")
        self.assertEqual(_normalize_media_path("/dl/"), "/dl")

    def test_custom_path_is_wired_into_the_route(self):
        s = Settings(cookie="auth_session=x", base_url=DEAD_UPSTREAM, enable_logfire=False,
                     task_store="memory", account_report_seconds=0, media_path="/dl")
        app = create_app(s)
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertIn("/dl/{name}", paths)
        self.assertNotIn("/v/{name}", paths)


class TestResponseHeaderHygiene(unittest.TestCase):
    """响应头白名单 —— 本文件最重要的一组。"""

    def _headers(self, **kw):
        app, c, _ = build_app(**kw)
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 200)
        return r.headers

    def test_content_disposition_is_rewritten_by_us(self):
        h = self._headers()
        # ★ 第三条泄露面：上游那份头里带着模型名，必须被我们换成任务 id
        self.assertEqual(h["content-disposition"], f'attachment; filename="{ARK_ID}.mp4"')
        self.assertNotIn("minimax", h["content-disposition"])
        self.assertNotIn(SITE_FILENAME, h["content-disposition"])

    def test_upstream_identity_headers_are_dropped(self):
        h = self._headers()
        for name in ("server", "cf-ray", "nel", "report-to"):
            with self.subTest(header=name):
                self.assertNotIn(name, h, f"{name} 会指认上游，不该透传")

    def test_useful_headers_pass_through(self):
        h = self._headers()
        self.assertEqual(h["content-type"], "video/mp4")
        self.assertEqual(h["accept-ranges"], "bytes")
        self.assertEqual(h["etag"], SITE_HEADERS["etag"])
        self.assertEqual(h["content-length"], str(len(BODY)))

    def test_no_whole_response_body_is_small_enough(self):
        app, c, _ = build_app()
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.content, BODY)


class TestTaskViewGate(unittest.TestCase):
    """`GET /tasks/{id}` 是主出口：它给出的地址必须已经是我们的。"""

    def test_video_url_is_swapped(self):
        app, c, _ = build_app()
        j = c.get(f"{TASKS_PATH}/{ARK_ID}").json()
        self.assertEqual(j["content"]["video_url"], f"{PUBLIC_BASE}/v/{ARK_ID}.mp4")

    def test_response_body_contains_no_upstream_trace(self):
        app, c, _ = build_app()
        body = c.get(f"{TASKS_PATH}/{ARK_ID}").text
        for leak in ("img2video", "minimax_h3", "static2", SITE_FILENAME):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, body)

    def test_disabled_keeps_the_upstream_url(self):
        """未配置 `AVM_PUBLIC_BASE` ⇒ 行为与从前**逐字节一致**（原样透传）。"""
        app, c, _ = build_app(enabled=False)
        j = c.get(f"{TASKS_PATH}/{ARK_ID}").json()
        self.assertEqual(j["content"]["video_url"], SITE_URL)

    def test_running_task_is_untouched(self):
        app, c, _ = build_app(status="running")
        j = c.get(f"{TASKS_PATH}/{ARK_ID}").json()
        self.assertEqual(j["status"], "running")
        self.assertNotIn("content", j)

    def test_source_url_is_remembered_for_the_download_endpoint(self):
        """查询路径顺手记下上游地址 ⇒ 下载时不必再查一次上游。"""
        app, c, stub = build_app()
        c.get(f"{TASKS_PATH}/{ARK_ID}")
        self.assertEqual(app.state.tasks.get(ARK_ID)["source_url"], SITE_URL)
        before = stub.calls
        c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(stub.calls, before, "已记下源地址却还去查上游")

    def test_openai_face_gets_the_same_swapped_url(self):
        app, c, _ = build_app()
        j = c.get(f"/v1/videos/{ARK_ID}").json()
        self.assertEqual(j["video_url"], f"{PUBLIC_BASE}/v/{ARK_ID}.mp4")


class TestDownloadEndpoint(unittest.TestCase):
    def test_serves_the_bytes(self):
        app, c, _ = build_app()
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, BODY)

    def test_range_is_forwarded_to_the_source(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["range"] = request.headers.get("range")
            return httpx.Response(
                206,
                headers={**SITE_HEADERS, "content-range": f"bytes 0-99/{len(BODY)}",
                         "content-length": "100"},
                content=BODY[:100],
            )

        app, c, _ = build_app(handler=handler)
        r = c.get(f"/v/{ARK_ID}.mp4", headers={"Range": "bytes=0-99"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(seen["range"], "bytes=0-99")
        self.assertEqual(r.headers["content-range"], f"bytes 0-99/{len(BODY)}")
        self.assertEqual(r.content, BODY[:100])

    def test_unknown_task_is_404(self):
        app, c, _ = build_app()
        r = c.get("/v/cgt-20260916-ffffffff.mp4")
        self.assertEqual(r.status_code, 404)

    def test_unknown_task_never_touches_the_source(self):
        """不存在的任务不该触发任何回源（否则等于把端点变成任意探测面）。"""
        hit = []

        def handler(request: httpx.Request) -> httpx.Response:
            hit.append(str(request.url))
            return httpx.Response(200, headers=SITE_HEADERS, content=BODY)

        app, c, _ = build_app(handler=handler)
        c.get("/v/cgt-20260916-ffffffff.mp4")
        self.assertEqual(hit, [])

    def test_not_ready_is_409(self):
        app, c, _ = build_app(status="running")
        r = c.get(f"/v/{ARK_ID}.mp4")
        # 任务在、只是还没出片 —— 与 404（不存在）刻意分开，调用方据此决定"重试"还是"放弃"
        self.assertEqual(r.status_code, 409)

    def test_source_failure_is_502_and_does_not_leak(self):
        app, c, _ = build_app(handler=explode)
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("img2video", r.text)
        self.assertNotIn("boom", r.text)

    def test_source_404_is_404(self):
        app, c, _ = build_app(handler=lambda _r: httpx.Response(404, text="gone"))
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("gone", r.text)

    def test_endpoint_is_absent_when_not_configured(self):
        app, c, _ = build_app(enabled=False)
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 404)

    def test_bad_ark_id_is_404(self):
        app, c, _ = build_app()
        r = c.get("/v/../../etc/passwd")
        self.assertIn(r.status_code, (404, 400))


class TestCrossTenant(unittest.TestCase):
    """透传模式下下载端点必须与任务查询同一套归属校验。"""

    def _passthrough_app(self, owner):
        app, c, _ = build_app(passthrough=True, owner=owner)
        return app, c

    def test_other_tenant_gets_404(self):
        app, c = self._passthrough_app(owner="deadbeefdeadbeef")
        r = c.get(f"/v/{ARK_ID}.mp4", headers={"Authorization": "Bearer auth_session=other"})
        self.assertEqual(r.status_code, 404)
        # 顺带钉住"404 而不是 403"：403 会透露"这条任务存在"
        self.assertNotIn("exists", r.text.lower())

    def test_owner_matches_its_own_tenant(self):
        import hashlib

        cookie = "auth_session=deadbeef"
        owner = hashlib.sha256(cookie.encode()).hexdigest()[:16]
        app, c, _ = build_app(passthrough=True, owner=owner)
        # 预置源地址：本用例只验证**归属放行**，不该顺带建一个真实（死端口）上游客户端
        app.state.tasks.patch(ARK_ID, source_url=SITE_URL)
        r = c.get(f"/v/{ARK_ID}.mp4", headers={"Authorization": f"Bearer {cookie}"})
        self.assertEqual(r.status_code, 200)

    def test_missing_bearer_in_passthrough_is_401(self):
        app, c = self._passthrough_app(owner="")
        r = c.get(f"/v/{ARK_ID}.mp4")
        self.assertEqual(r.status_code, 401)


class TestHealthz(unittest.TestCase):
    def test_healthz_reports_the_media_sink_shape(self):
        app, c, _ = build_app()
        j = c.get("/healthz").json()
        self.assertTrue(j["media_proxy"]["enabled"])
        self.assertEqual(j["media_proxy"]["public_base"], PUBLIC_BASE)

    def test_healthz_never_reports_credentials(self):
        """`/healthz` 不鉴权 —— 它上面**任何**字段都不该是凭据。"""
        app, c, _ = build_app()
        body = c.get("/healthz").text
        self.assertNotIn("auth_session", body)
        self.assertNotIn("deadbeef", body)

    def test_disabled_is_reported_as_disabled(self):
        app, c, _ = build_app(enabled=False)
        self.assertFalse(c.get("/healthz").json()["media_proxy"]["enabled"])


if __name__ == "__main__":
    unittest.main()
