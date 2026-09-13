#!/usr/bin/env python3
"""web 上游（tRPC 逆向线）的测试。

**全程离线**：用 `httpx.MockTransport` 当站点替身，一条真实网络请求都不发，
更不会创建任何生成任务。

覆盖三块：
  1. `sniff` —— 按真实字节识别媒体类型（不信扩展名）
  2. `WebClient` —— tRPC 信封解包、验证码闸门、上传链路、读任务的三级回落
  3. `WebSubmitQueue` + app 层 —— 并发闸门、`AVM_UPSTREAM=web` 的对外行为

运行：python3 tests/test_web_upstream.py
"""

import base64
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.errors import CaptchaRequiredError, ParamError, WebApiError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.sniff import image_dimensions, sniff_file  # noqa: E402
from ark_compat.translate import normalize_web_task  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_client import WebClient, parse_sse  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

# ------------------------------------------------------------------ fixtures ----


def png_bytes(w: int = 800, h: int = 1200) -> bytes:
    """一个带合法 IHDR 的最小 PNG 头（够 image_dimensions 读出尺寸）。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
    )


def jpeg_bytes(w: int = 2560, h: int = 1440) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + (16).to_bytes(2, "big")
        + b"JFIF\x00"
        + b"\x00" * 9
        + b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + h.to_bytes(2, "big")
        + w.to_bytes(2, "big")
        + b"\x03"
        + b"\x00" * 6
        + b"\x00" * 16
    )


MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 16
M4A = b"\x00\x00\x00\x14ftypM4A " + b"\x00" * 16
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 16
MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
OGG = b"OggS\x00\x00" + b"\x00" * 16


class TrpcError:
    """让替身站点返回一个 tRPC 错误信封。"""

    def __init__(self, message: str, code: str = "BAD_REQUEST"):
        self.message = message
        self.code = code


class FakeSite:
    """内存里的站点替身：记录收到的请求，按 procedure 返回预设信封。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, object] = {}
        self.uploads: list[bytes] = []
        self.presign: dict = {"uploadUrl": "https://cdn.example/put", "publicUrl": "https://cdn.example/x.png", "maxBytes": 10485760, "headers": {}}

    def set(self, procedure: str, value=None, error: TrpcError | None = None) -> None:
        self.responses[procedure] = error if error is not None else value

    def calls(self, procedure: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/{procedure}"]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/model-status/token":
            return httpx.Response(200, json={"token": "tok-123", "expiresInSec": 300})
        if path == "/api/model-status":
            return httpx.Response(
                200, text='data: {"model": {"id": "t1", "taskStatus": "processing"}}\n\n'
            )
        if request.method == "PUT":  # 预签名上传
            self.uploads.append(request.content)
            return httpx.Response(200, text="ok")
        if path.startswith("/api/"):
            proc = path[len("/api/") :]
            if proc == "uploads.getPresignedUrl":
                return httpx.Response(200, json=[{"result": {"data": {"json": self.presign}}}])
            value = self.responses.get(proc, None)
            if isinstance(value, TrpcError):
                return httpx.Response(
                    200,
                    json=[{"error": {"json": {"message": value.message, "data": {"code": value.code, "httpStatus": 400}}}}],
                )
            return httpx.Response(200, json=[{"result": {"data": {"json": value}}}])
        return httpx.Response(404, json={"nope": path})


def make_client(site: FakeSite, **kw) -> WebClient:
    return WebClient(
        cookie="auth_session=deadbeef",
        base_url="https://site.test",
        trust_env=False,
        transport=site.transport(),
        **kw,
    )


# -------------------------------------------------------------------- sniff ----


class TestSniff(unittest.TestCase):
    def test_image_families(self):
        self.assertEqual(sniff_file(png_bytes())["content_type"], "image/png")
        self.assertEqual(sniff_file(jpeg_bytes())["content_type"], "image/jpeg")
        self.assertEqual(sniff_file(WEBP)["content_type"], "image/webp")

    def test_video_families(self):
        self.assertEqual(sniff_file(MP4)["kind"], "video")
        self.assertEqual(sniff_file(MP4)["ext"], "mp4")
        self.assertEqual(sniff_file(MOV)["content_type"], "video/quicktime")
        self.assertEqual(sniff_file(WEBM)["content_type"], "video/webm")

    def test_audio_families(self):
        self.assertEqual(sniff_file(MP3)["content_type"], "audio/mpeg")
        self.assertEqual(sniff_file(WAV)["content_type"], "audio/wav")
        self.assertEqual(sniff_file(OGG)["content_type"], "audio/ogg")
        self.assertEqual(sniff_file(M4A)["kind"], "audio")

    def test_unknown_is_not_guessed(self):
        info = sniff_file(b"\x00\x01\x02\x03not-media")
        self.assertEqual(info["kind"], "unknown")
        self.assertEqual(info["content_type"], "application/octet-stream")

    def test_empty_input_does_not_crash(self):
        self.assertEqual(sniff_file(b"")["kind"], "unknown")

    def test_the_real_bug_png_bytes_named_jpg(self):
        """真实踩过的坑：`.jpg` + `image/jpg`，实际是 PNG。必须按字节判。"""
        self.assertEqual(sniff_file(png_bytes())["content_type"], "image/png")

    def test_dimensions(self):
        self.assertEqual(image_dimensions(png_bytes(800, 1200)), (800, 1200))
        self.assertEqual(image_dimensions(jpeg_bytes(2560, 1440)), (2560, 1440))
        self.assertEqual(image_dimensions(b"not an image"), (0, 0))


class TestParseSse(unittest.TestCase):
    def test_first_data_frame(self):
        self.assertEqual(parse_sse('data: {"a": 1}\n\ndata: {"a": 2}'), {"a": 1})

    def test_skips_done_and_garbage(self):
        self.assertIsNone(parse_sse("event: ping\n\ndata: [DONE]\n"))
        self.assertIsNone(parse_sse(""))

    def test_tolerates_invalid_json_then_valid(self):
        self.assertEqual(parse_sse('data: {broken\ndata: {"ok": true}'), {"ok": True})


# ----------------------------------------------------------------- trpc layer ----


class TestTrpc(unittest.TestCase):
    def test_unwraps_result_data_json(self):
        site = FakeSite()
        site.set("auth.user", {"id": "u1", "email": "a@b.c"})
        self.assertEqual(make_client(site).get_user(), {"id": "u1", "email": "a@b.c"})

    def test_error_envelope_becomes_webapierror(self):
        site = FakeSite()
        site.set("model.getModel", error=TrpcError("boom", "NOT_FOUND"))
        with self.assertRaises(WebApiError) as ctx:
            make_client(site).get_model("t1")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_request_carries_cookie_and_referer(self):
        site = FakeSite()
        site.set("auth.user", {"id": "u1"})
        make_client(site).get_user()
        req = site.calls("auth.user")[0]
        self.assertEqual(req.headers["cookie"], "auth_session=deadbeef")
        self.assertIn("/zh/", req.headers["referer"])
        self.assertEqual(req.url.params["batch"], "1")

    def test_void_input_is_sent_as_undefined_with_meta(self):
        site = FakeSite()
        site.set("credits.getCredits", {"totalRemaining": 796})
        self.assertEqual(make_client(site).get_credits(), 796)
        req = site.calls("credits.getCredits")[0]
        self.assertIn("undefined", req.url.params["input"])


class TestCreditsAndSession(unittest.TestCase):
    def test_balance_comes_from_total_remaining(self):
        site = FakeSite()
        site.set("credits.getCredits", {"totalRemaining": 796})
        self.assertEqual(make_client(site).get_credits(), 796)

    def test_user_id_is_resolved_once_and_cached(self):
        site = FakeSite()
        site.set("auth.user", {"id": "u42"})
        c = make_client(site)
        self.assertEqual(c.get_user_id(), "u42")
        self.assertEqual(c.get_user_id(), "u42")
        self.assertEqual(len(site.calls("auth.user")), 1, "userId 应当被缓存")

    def test_needs_captcha_passes_user_id(self):
        site = FakeSite()
        site.set("auth.user", {"id": "u7"})
        site.set("model.needsCaptcha", False)
        self.assertFalse(make_client(site).needs_captcha())
        req = site.calls("model.needsCaptcha")[0]
        self.assertIn("u7", req.url.params["input"])


# -------------------------------------------------------------------- create ----


class TestCreate(unittest.TestCase):
    def test_happy_path_returns_task_id(self):
        site = FakeSite()
        site.set("model.needsCaptcha", False)
        site.set("ai.minimaxH3", "qaul40pcx9emuc8")
        c = make_client(site)
        tid = c.create({"content": "a cat", "duration": 5, "resolution": "480p", "tier": "turbo"})
        self.assertEqual(tid, "qaul40pcx9emuc8")

        body = site.calls("ai.minimaxH3")[0].content.decode()
        self.assertIn('"tier":"turbo"', body.replace(" ", ""))
        self.assertIn('"duration":5', body.replace(" ", ""))
        # visitorId 服务端不校验，但必须带上
        self.assertIn("visitorId", body)

    def test_captcha_gate_blocks_before_submitting(self):
        site = FakeSite()
        site.set("model.needsCaptcha", True)
        c = make_client(site)
        with self.assertRaises(CaptchaRequiredError) as ctx:
            c.create({"content": "x"})
        self.assertEqual(ctx.exception.code, "CAPTCHA_REQUIRED")
        self.assertEqual(site.calls("ai.minimaxH3"), [], "闸门开着就不该发起提交")

    def test_byo_token_passes_the_gate(self):
        site = FakeSite()
        site.set("model.needsCaptcha", True)
        site.set("ai.minimaxH3", "t-1")
        tid = make_client(site).create({"content": "x"}, token="real-turnstile-token")
        self.assertEqual(tid, "t-1")
        self.assertIn("real-turnstile-token", site.calls("ai.minimaxH3")[0].content.decode())

    def test_empty_task_id_is_an_explicit_failure(self):
        """站点用空串表示"静默拒绝" —— 必须当成失败，不能当成功。"""
        site = FakeSite()
        site.set("model.needsCaptcha", False)
        site.set("ai.minimaxH3", "")
        with self.assertRaises(WebApiError) as ctx:
            make_client(site).create({"content": "x"})
        self.assertIn("rejected", str(ctx.exception))


# ---------------------------------------------------------------- read task ----


class TestGetTask(unittest.TestCase):
    def test_prefers_model_get_model(self):
        site = FakeSite()
        site.set("model.getModel", {"id": "t1", "taskStatus": "succeed", "url": "https://cdn/a.mp4"})
        rec = make_client(site).get_task("t1")
        self.assertEqual(rec["taskStatus"], "succeed")
        self.assertEqual(site.calls("model.listModel"), [], "首选命中就不该再打列表")

    def test_falls_back_to_the_list(self):
        site = FakeSite()
        site.set("model.getModel", None)  # 站点返回空
        site.set("auth.user", {"id": "u1"})
        site.set("model.listModel", {"models": [{"id": "t2", "taskStatus": "processing"}]})
        rec = make_client(site).get_task("t2")
        self.assertEqual(rec["id"], "t2")

    def test_falls_back_to_sse_when_list_misses(self):
        site = FakeSite()
        site.set("model.getModel", None)
        site.set("auth.user", {"id": "u1"})
        site.set("model.listModel", {"models": []})
        rec = make_client(site).get_task("t3")
        self.assertEqual(rec["id"], "t1", "SSE 帧里的任务被返回")

    def test_not_found_after_all_fallbacks(self):
        site = FakeSite()
        site.set("model.getModel", error=TrpcError("task x not found", "NOT_FOUND"))
        with self.assertRaises(WebApiError) as ctx:
            make_client(site).get_task("x")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_queue_position(self):
        site = FakeSite()
        site.set("model.queryQueueByModel", {"queue": 0, "etaSeconds": 0})
        self.assertEqual(make_client(site).query_queue("t1")["queue"], 0)

    def test_delete_only_removes_the_record(self):
        site = FakeSite()
        site.set("model.deleteModel", None)
        make_client(site).delete_tasks(["t1"])
        self.assertEqual(len(site.calls("model.deleteModel")), 1)


# -------------------------------------------------------------------- upload ----


class TestUpload(unittest.TestCase):
    def test_presign_then_put_and_sniff_by_bytes(self):
        site = FakeSite()
        c = make_client(site)
        # 名字说是 jpg，字节其实是 PNG —— 必须按 PNG 上传
        r = c.upload_file(png_bytes(800, 1200), name="photo.jpg")
        self.assertEqual(r["contentType"], "image/png")
        self.assertEqual(r["fileName"], "photo.png")
        self.assertEqual((r["width"], r["height"]), (800, 1200))
        self.assertEqual(site.uploads[0][:4], b"\x89PNG")

    def test_rejects_oversize_against_max_bytes(self):
        site = FakeSite()
        site.presign = {**site.presign, "maxBytes": 4}
        with self.assertRaises(ParamError) as ctx:
            make_client(site).upload_file(png_bytes())
        self.assertIn("too large", str(ctx.exception))

    def test_upload_image_rejects_non_image(self):
        site = FakeSite()
        with self.assertRaises(ParamError):
            make_client(site).upload_image(MP4, name="clip.mp4")

    def test_bad_source_type_is_rejected(self):
        with self.assertRaises(ParamError):
            make_client(FakeSite()).upload_file(12345)  # type: ignore[arg-type]


# --------------------------------------------------------------------- queue ----


class FakeWebClient:
    """只实现闸门需要的两个方法，并记录峰值并发。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self.active = 0
        self.peak = 0
        self.created: list[str] = []
        self.created_params: list[dict] = []
        self.uploaded: list = []
        self.deleted: list[str] = []

    def create(self, params, token=None):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            task_id = f"t{len(self.created) + 1}"
            self.created.append(task_id)
            self.created_params.append(dict(params))
            self._events[task_id] = threading.Event()
        return task_id

    def upload_file(self, source, name=None, permanent=False):
        self.uploaded.append(source if isinstance(source, str) else f"<{len(source)} bytes>")
        return {"publicUrl": f"https://static.img2video.ai/rehost-{len(self.uploaded)}.png", "kind": "image"}

    def wait_for_task(self, task_id, *, timeout=600.0, interval=10.0):
        self._events[task_id].wait(timeout=5)
        with self._lock:
            self.active -= 1
        return {"done": True, "ok": True, "status": "succeed", "task": {}, "ms": 1}

    def release(self, task_id: str) -> None:
        self._events[task_id].set()

    def get_task(self, task_id: str) -> dict:
        return {"id": task_id, "taskStatus": "succeed", "aiModel": "minimax-h3", "url": "https://cdn/a.mp4", "kelingKeyId": "480"}

    def get_credits(self):
        return 796

    def delete_tasks(self, ids):
        self.deleted.extend(ids)


class TestSubmitQueue(unittest.TestCase):
    def test_gate_holds_at_max_concurrent(self):
        client = FakeWebClient()
        q = WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01, watch_timeout=5, acquire_timeout=5)
        first = q.submit({"content": "a"})
        second = q.submit({"content": "b"})
        self.assertEqual(client.peak, 2)

        third: list[str] = []
        t = threading.Thread(target=lambda: third.append(q.submit({"content": "c"})), daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertEqual(client.peak, 2, "闸门必须拦住第 3 个（上游 premium 只跑 2 个）")
        self.assertEqual(third, [])

        client.release(first)  # 第一个进终态 → 释放槽位
        t.join(timeout=3)
        self.assertEqual(len(third), 1, "空出槽位后第 3 个应被放行")
        self.assertEqual(client.peak, 2)

        client.release(second)
        client.release(third[0])
        self.assertEqual(q.stats()["served_total"], 3)

    def test_slot_is_released_even_when_the_watcher_fails(self):
        client = FakeWebClient()
        q = WebSubmitQueue(client, max_concurrent=1, poll_interval=0.01, watch_timeout=0.2, acquire_timeout=2)

        def boom(task_id, **kw):
            raise RuntimeError("watcher exploded")
            self._events[task_id].wait(1)

        client.wait_for_task = boom  # type: ignore[method-assign]
        q.submit({"content": "a"})
        time.sleep(0.4)
        # 盯梢失败也必须放槽位，否则闸门会永久卡死
        self.assertTrue(q._sem.acquire(timeout=1), "槽位没被释放 → 闸门将永久卡死")
        q._sem.release()

    def test_rejected_submission_does_not_leak_a_slot(self):
        client = FakeWebClient()

        def boom(params, token=None):
            raise WebApiError("ai.minimaxH3", "nope")

        client.create = boom  # type: ignore[method-assign]
        q = WebSubmitQueue(client, max_concurrent=1, acquire_timeout=1)
        with self.assertRaises(WebApiError):
            q.submit({"content": "a"})
        self.assertTrue(q._sem.acquire(timeout=1), "提交失败不该占着槽位")
        q._sem.release()


# ---------------------------------------------------------------- normalize ----


class TestNormalizeWebTask(unittest.TestCase):
    def test_status_vocabulary_is_the_sites_not_the_officials(self):
        self.assertEqual(normalize_web_task({"taskStatus": "processing"})["status"], "running")
        self.assertEqual(
            normalize_web_task({"taskStatus": "succeed", "url": "u"})["status"], "succeeded"
        )
        self.assertEqual(normalize_web_task({"taskStatus": "queueing"})["status"], "queued")
        self.assertEqual(normalize_web_task({"taskStatus": "failed"})["status"], "failed")

    def test_succeed_without_a_url_is_a_failure(self):
        """站点会把「没产出 URL」的任务也留在 succeed（`taskStatusMsg="not found url"`）。

        实测踩到过：照搬映射会让调用方看到"succeeded 但 `video_url` 为 null"这种
        自相矛盾的状态，下游据此判成功、却拿到空链接。必须判成 failed 并带出原因。
        """
        t = normalize_web_task(
            {"taskStatus": "succeed", "url": None, "taskStatusMsg": "not found url"}
        )
        self.assertEqual(t["status"], "failed")
        self.assertIsNone(t["content"]["video_url"])
        self.assertEqual(t["error"]["message"], "not found url")

    def test_keling_key_id_is_the_real_resolution(self):
        t = normalize_web_task({"taskStatus": "succeed", "url": "u", "kelingKeyId": "1080"})
        self.assertEqual(t["resolution"], "1080p")

    def test_paid_flag_not_credits_decides_billing(self):
        """站点侧 paid=false 记 credits=1 —— 判是否花钱只看 paid。"""
        free = normalize_web_task({"taskStatus": "succeed", "url": "u", "credits": 1, "paid": False})
        self.assertFalse(free["usage"]["paid"])
        self.assertEqual(free["usage"]["credits"], 1)
        billed = normalize_web_task({"taskStatus": "succeed", "url": "u", "credits": 0, "paid": True})
        self.assertTrue(billed["usage"]["paid"])

    def test_video_url_only_when_succeeded(self):
        self.assertIsNone(normalize_web_task({"taskStatus": "processing", "url": "u"})["content"]["video_url"])
        self.assertEqual(normalize_web_task({"taskStatus": "succeed", "url": "u"})["content"]["video_url"], "u")


# ----------------------------------------------------------------- app layer ----


def web_settings(**kw) -> Settings:
    base = dict(
        upstream="web",
        cookie="auth_session=deadbeef",
        base_url="https://site.test",
        log_level="WARNING",
        enable_logfire=False,
        trust_env=False,
        task_store="memory",  # 任务表落内存：单测不落盘、不互相污染
    )
    base.update(kw)
    return Settings(**base)


def ark_body(**kw) -> dict:
    b = {
        "model": "doubao-seedance-2-5-260628",
        "content": [{"type": "text", "text": "a cat"}],
        "ratio": "16:9",
        "resolution": "480p",
        "duration": 5,
    }
    b.update(kw)
    return b


class TestWebAppLayer(unittest.TestCase):
    """`AVM_UPSTREAM=web` 时对外仍是一套 Ark 协议。"""

    def setUp(self):
        self.app = create_app(web_settings())
        # 换掉真实上游，避免任何网络活动（app 读的是 upstreams 这张注册表）
        self.fake = FakeWebClient()
        self.app.state.upstreams = {
            "web": WebUpstream(
                self.fake, WebSubmitQueue(self.fake, max_concurrent=2, poll_interval=0.01)
            )
        }
        self.client = TestClient(self.app)

    def test_dry_run_reports_the_web_billing_line(self):
        r = self.client.post(TASKS_PATH, json=ark_body(extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["upstream"], "web")
        self.assertFalse(j["effective"]["billed"], "turbo / 5s 落在 web 线免费区")
        self.assertIn("free up to 8s", j["effective"]["billing_note"])
        self.assertIn("web_params", j)

    def test_dry_run_tier_base_is_billed(self):
        r = self.client.post(TASKS_PATH, json=ark_body(extra_body={"aivideomaker_dry_run": True, "aivideomaker_tier": "base"}))
        self.assertTrue(r.json()["effective"]["billed"])

    def test_web_needs_no_spend_cap(self):
        """web 线有免费窗口，不该像官方线那样强制要 X-Max-Credits。"""
        r = self.client.post(TASKS_PATH, json=ark_body())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["id"].startswith("cgt-"))

    def test_healthz_describes_the_web_line(self):
        j = self.client.get("/healthz").json()
        self.assertEqual(j["upstream"], "web")  # 默认线
        self.assertEqual(j["available_upstreams"], ["web"])
        self.assertFalse(j["supports_cancel"]["web"])
        self.assertIn("submit_queue", j)
        self.assertNotIn("supported_models", j)

    def test_delete_reports_that_it_did_not_cancel(self):
        tid = self.client.post(TASKS_PATH, json=ark_body()).json()["id"]
        j = self.client.delete(f"{TASKS_PATH}/{tid}").json()
        self.assertFalse(j["cancelled"], "站点没有取消端点，绝不能谎报已取消")
        self.assertIn("no cancel endpoint", j["reason"])
        self.assertEqual(self.fake.deleted, ["t1"])

    def test_task_view_is_normalized_to_the_ark_shape(self):
        tid = self.client.post(TASKS_PATH, json=ark_body()).json()["id"]
        j = self.client.get(f"{TASKS_PATH}/{tid}").json()
        self.assertEqual(j["id"], tid)
        self.assertEqual(j["status"], "succeeded")
        self.assertEqual(j["content"]["video_url"], "https://cdn/a.mp4")
        self.assertEqual(j["resolution"], "480p")
        self.assertEqual(j["requested"]["model"], "doubao-seedance-2-5-260628")

    # ---- 媒体转存（站点只收自己 CDN 的地址）----

    def first_frame(self, url: str) -> dict:
        return {"type": "image_url", "role": "first_frame", "image_url": {"url": url}}

    def test_external_media_is_rehosted_before_submitting(self):
        body = ark_body(
            content=[{"type": "text", "text": "a cat"}, self.first_frame("https://elsewhere.example/a.jpg")],
            ratio="adaptive",
        )
        self.client.post(TASKS_PATH, json=body)
        self.assertEqual(self.fake.uploaded, ["https://elsewhere.example/a.jpg"])
        self.assertIn("static.img2video.ai/rehost-1.png", self.fake.created_params[0]["imageUrl"])

    def test_media_already_on_the_site_cdn_is_left_alone(self):
        cdn = "https://static.img2video.ai/1789-abc.png"
        body = ark_body(content=[{"type": "text", "text": "a cat"}, self.first_frame(cdn)], ratio="adaptive")
        self.client.post(TASKS_PATH, json=body)
        self.assertEqual(self.fake.uploaded, [], "已在站点 CDN 上的地址不该重复上传")
        self.assertEqual(self.fake.created_params[0]["imageUrl"], cdn)

    def test_inline_data_uri_is_decoded_and_uploaded_as_bytes(self):
        uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
        body = ark_body(content=[{"type": "text", "text": "a cat"}, self.first_frame(uri)], ratio="adaptive")
        self.client.post(TASKS_PATH, json=body)
        self.assertEqual(len(self.fake.uploaded), 1)
        self.assertTrue(self.fake.uploaded[0].startswith("<"), "data URI 应被解成字节再上传")
        self.assertIn("rehost-1.png", self.fake.created_params[0]["imageUrl"])

    def test_dry_run_does_not_upload_anything(self):
        body = ark_body(
            content=[{"type": "text", "text": "a cat"}, self.first_frame("https://elsewhere.example/a.jpg")],
            ratio="adaptive",
            extra_body={"aivideomaker_dry_run": True},
        )
        self.client.post(TASKS_PATH, json=body)
        self.assertEqual(self.fake.uploaded, [], "dry-run 不该产生任何上传 / 副作用")


class TestTwoUpstreamsCoexist(unittest.TestCase):
    """两条线**并存**：默认线由 AVM_UPSTREAM 定，按请求还能切。"""

    def setUp(self):
        self.app = create_app(
            Settings(
                upstream="official",
                upstream_key="ak_test",
                cookie="auth_session=deadbeef",
                base_url="http://127.0.0.1:9",
                log_level="WARNING",
                enable_logfire=False,
                trust_env=False,
            )
        )
        self.fake_web = FakeWebClient()
        self.app.state.upstreams["web"] = WebUpstream(
            self.fake_web, WebSubmitQueue(self.fake_web, max_concurrent=2, poll_interval=0.01)
        )
        self.client = TestClient(self.app)

    def test_healthz_lists_both_lines(self):
        j = self.client.get("/healthz").json()
        self.assertEqual(j["available_upstreams"], ["official", "web"])
        self.assertTrue(j["supports_cancel"]["official"])
        self.assertFalse(j["supports_cancel"]["web"])
        self.assertIn("free up to 8s", j["billing_notes"]["web"])
        self.assertIn("every submit is billed", j["billing_notes"]["official"])

    def test_same_request_switches_billing_semantics_by_header(self):
        """同一个 5s/turbo：web 线免费、官方线计费 —— 一趟看清两条线的差别。"""
        body = ark_body(extra_body={"aivideomaker_dry_run": True})
        web = self.client.post(TASKS_PATH, json=body, headers={"X-Avm-Upstream": "web"}).json()
        official = self.client.post(TASKS_PATH, json=body, headers={"X-Avm-Upstream": "official"}).json()
        self.assertEqual(web["upstream"], "web")
        self.assertFalse(web["effective"]["billed"])
        self.assertEqual(official["upstream"], "official")
        self.assertTrue(official["effective"]["billed"])

    def test_query_parameter_also_switches(self):
        body = ark_body(extra_body={"aivideomaker_dry_run": True})
        j = self.client.post(f"{TASKS_PATH}?upstream=web", json=body).json()
        self.assertEqual(j["upstream"], "web")

    def test_default_is_the_configured_line(self):
        body = ark_body(extra_body={"aivideomaker_dry_run": True})
        self.assertEqual(self.client.post(TASKS_PATH, json=body).json()["upstream"], "official")

    def test_unknown_upstream_is_rejected(self):
        r = self.client.post(TASKS_PATH, json=ark_body(), headers={"X-Avm-Upstream": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")

    def test_switching_to_an_unconfigured_line_says_what_is_missing(self):
        c = TestClient(create_app(web_settings()))  # 只配了 web
        r = c.post(TASKS_PATH, json=ark_body(), headers={"X-Avm-Upstream": "official"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"]["code"], "UpstreamUnavailable")
        self.assertIn("AVM_KEY", r.json()["error"]["message"])


class TestUpstreamSwitch(unittest.TestCase):
    def test_both_lines_configured_means_both_available(self):
        s = Settings(upstream="web", upstream_key="ak_x", cookie="auth_session=y")
        s.validate()
        self.assertEqual(s.available_upstreams, ["official", "web"])

    def test_default_line_without_credentials_is_reported_clearly(self):
        with self.assertRaises(ValueError) as ctx:
            Settings(upstream="official", cookie="auth_session=y").validate()
        self.assertIn("官方线需要 AVM_KEY", str(ctx.exception))

    def test_web_settings_require_a_cookie(self):
        with self.assertRaises(ValueError) as ctx:
            Settings(upstream="web", cookie="").validate()
        self.assertIn("AVM_COOKIE", str(ctx.exception))

    def test_official_settings_require_a_key(self):
        with self.assertRaises(ValueError):
            Settings(upstream="official", upstream_key="").validate()

    def test_unknown_upstream_is_rejected(self):
        with self.assertRaises(ValueError):
            Settings(upstream="nope").validate()

    def test_from_env_reads_the_switch(self):
        s = Settings.from_env({"AVM_UPSTREAM": "WEB", "AVM_COOKIE": "auth_session=x"})
        self.assertEqual(s.upstream, "web")
        s.validate()  # 不应抛


if __name__ == "__main__":
    unittest.main(verbosity=2)
