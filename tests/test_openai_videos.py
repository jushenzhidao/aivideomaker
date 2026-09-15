#!/usr/bin/env python3
"""OpenAI `/v1/videos` 兼容面（Chatfire「OpenaiVideos格式 / Seedance」契约）的测试。

契约来源（务必一样）：
    创建  https://oneapis.apifox.cn/369966278e0   POST /v1/videos
    查询  https://oneapis.apifox.cn/369966279e0   GET  /v1/videos/{id}

**全程离线**：上游一律是内存替身，不创建任何真实任务、零外发、零消耗。
"""

import base64
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import OPENAI_VIDEOS_PATH, create_app  # noqa: E402
from ark_compat.errors import ParamError  # noqa: E402
from ark_compat.openai_videos import (  # noqa: E402
    ark_body_from_openai,
    openai_task_view,
)
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.translate import translate_create  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

# ------------------------------------------------------------------ fixtures ----


def png_bytes(w: int = 32, h: int = 32) -> bytes:
    """一个带合法 IHDR 的最小 PNG 头（够 sniff_file 识别）。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 8
    )


class FakeClient:
    """内存替身：记录 create/upload 参数，get_task 返回可配置的站点记录。"""

    def __init__(self, record: dict | None = None):
        self.record = record or {
            "id": "t1",
            "taskStatus": "succeed",
            "aiModel": "minimax-h3",
            "url": "https://cdn.example/a.mp4",
            "kelingKeyId": "480",
            "createdAt": "2026-09-15T05:00:00Z",
            "paid": False,
            "credits": 1,
        }
        self.created_params: list[dict] = []
        self.uploaded: list = []

    def create(self, params, token=None):
        self.created_params.append(dict(params))
        return "t1"

    def upload_file(self, source, name=None, permanent=False):
        self.uploaded.append(source if isinstance(source, str) else f"<{len(source)} bytes>")
        return {"publicUrl": f"https://static.img2video.ai/rehost-{len(self.uploaded)}.png", "kind": "image"}

    def wait_for_task(self, task_id, *, timeout=600.0, interval=10.0):
        return {"done": True, "ok": True, "status": "succeed", "task": {}, "ms": 1}

    def release(self, task_id: str) -> None:
        pass

    def get_task(self, task_id: str) -> dict:
        return dict(self.record)

    def get_credits(self):
        return 100


def settings(**kw) -> Settings:
    base = dict(
        cookie="auth_session=deadbeef",
        base_url="https://site.test",
        log_level="WARNING",
        enable_logfire=False,
        trust_env=False,
        task_store="memory",
    )
    base.update(kw)
    return Settings(**base)


def _app_with_fake(fake: FakeClient, **kw):
    app = create_app(settings(**kw))
    # 换掉真实上游：app 只读 upstreams 注册表（与 test_web_upstream 同一手法）
    app.state.upstreams = {
        "web": WebUpstream(fake, WebSubmitQueue(fake, max_concurrent=2, poll_interval=0.01))
    }
    return app


# ============================================================ 纯翻译层 ====


class TestArkBodyFromOpenai(unittest.TestCase):
    """OpenAI 字段 → Ark 请求体（复用 translate_create 做重活）。"""

    def test_minimal_form(self):
        body, notes = ark_body_from_openai(
            {"model": "doubao-seedance-1-0-pro_1080p", "prompt": "360度环绕运镜"}
        )
        self.assertEqual(body["model"], "doubao-seedance-1-0-pro_1080p")
        self.assertEqual(body["content"], [{"type": "text", "text": "360度环绕运镜"}])
        # 分辨率来自 model 名的档位后缀
        self.assertEqual(body["resolution"], "1080p")
        # seconds 缺省 → 交给 translate 的默认（5s，免费窗口内）
        plan = translate_create(body)
        self.assertEqual(plan["web_params"]["duration"], 5)
        self.assertEqual(notes, [])

    def test_model_without_resolution_suffix_keeps_translate_default(self):
        body, _ = ark_body_from_openai({"model": "sora-2", "prompt": "p"})
        self.assertNotIn("resolution", body)

    def test_seconds_string_and_int(self):
        for v, want in (("8", 8), (8, 8), ("12", 12)):
            body, _ = ark_body_from_openai({"model": "m", "prompt": "p", "seconds": v})
            self.assertEqual(body["duration"], want)

    def test_seconds_non_integer_is_rejected(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "m", "prompt": "p", "seconds": "abc"})
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "m", "prompt": "p", "seconds": "5.5"})

    def test_size_ratio_passthrough(self):
        body, notes = ark_body_from_openai({"model": "m", "prompt": "p", "size": "9:16"})
        self.assertEqual(body["ratio"], "9:16")
        self.assertEqual(notes, [])

    def test_size_keep_ratio_maps_to_adaptive_with_a_note(self):
        body, notes = ark_body_from_openai({"model": "m", "prompt": "p", "size": "keep_ratio"})
        self.assertEqual(body["ratio"], "adaptive")
        self.assertTrue(any("keep_ratio" in n and "adaptive" in n for n in notes))

    def test_size_wxh_maps_to_reduced_ratio(self):
        body, notes = ark_body_from_openai({"model": "m", "prompt": "p", "size": "1920x1080"})
        self.assertEqual(body["ratio"], "16:9")
        self.assertTrue(any("1920x1080" in n and "16:9" in n for n in notes))

    def test_unknown_size_falls_through_to_translate_validation(self):
        body, _ = ark_body_from_openai({"model": "m", "prompt": "p", "size": "5:4"})
        with self.assertRaises(ParamError):
            translate_create(body)

    def test_input_reference_single_string_and_list(self):
        body, _ = ark_body_from_openai(
            {"model": "m", "prompt": "p", "input_reference": "https://x/a.jpg"}
        )
        body2, _ = ark_body_from_openai(
            {"model": "m", "prompt": "p", "input_reference": ["https://x/a.jpg", "https://x/b.jpg"]}
        )
        for b in (body, body2):
            self.assertEqual(b["content"][0], {"type": "text", "text": "p"})
        self.assertEqual(len(body["content"]), 2)
        self.assertEqual(len(body2["content"]), 3)
        plan = translate_create(body2)
        self.assertEqual(
            plan["web_params"]["referenceImageUrls"], ["https://x/a.jpg", "https://x/b.jpg"]
        )

    def test_input_reference_empty_string_is_absent(self):
        """Chatfire 的 curl 惯例：不用的字段传空串。"""
        body, _ = ark_body_from_openai(
            {"model": "m", "prompt": "p", "input_reference": "", "first_frame_image": ""}
        )
        self.assertEqual(len(body["content"]), 1)

    def test_first_and_last_frame(self):
        body, _ = ark_body_from_openai(
            {
                "model": "m",
                "prompt": "p",
                "first_frame_image": "https://x/f.png",
                "last_frame_image": "https://x/l.png",
            }
        )
        plan = translate_create(body)
        self.assertEqual(plan["web_params"]["imageUrl"], "https://x/f.png")
        self.assertEqual(plan["web_params"]["lastFrameUrl"], "https://x/l.png")

    def test_frame_and_reference_mixing_is_rejected_upstream_shape(self):
        """首帧/首尾帧与参考图互斥 —— 上游硬约束，由 translate_create 前置 400。"""
        body, _ = ark_body_from_openai(
            {
                "model": "m",
                "prompt": "p",
                "first_frame_image": "https://x/f.png",
                "input_reference": ["https://x/r.png"],
            }
        )
        with self.assertRaises(ParamError):
            translate_create(body)

    def test_b64_reference_becomes_a_data_uri_with_sniffed_mime(self):
        b64 = base64.b64encode(png_bytes()).decode()
        body, _ = ark_body_from_openai(
            {"model": "m", "prompt": "p", "input_reference": b64}, reference_format="b64"
        )
        url = body["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_invalid_b64_is_a_clean_400(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai(
                {"model": "m", "prompt": "p", "input_reference": "!!!not-b64!!!"},
                reference_format="b64",
            )

    def test_urls_survive_b64_mode(self):
        body, _ = ark_body_from_openai(
            {"model": "m", "prompt": "p", "input_reference": "https://x/a.jpg"},
            reference_format="b64",
        )
        self.assertEqual(body["content"][1]["image_url"]["url"], "https://x/a.jpg")

    def test_model_and_prompt_are_required(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai({"prompt": "p"})
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "m", "prompt": ""})

    def test_unknown_fields_are_reported_not_silently_dropped(self):
        _, notes = ark_body_from_openai({"model": "m", "prompt": "p", "seed": 7, "watermark": True})
        self.assertTrue(any('"seed"' in n for n in notes))
        self.assertTrue(any('"watermark"' in n for n in notes))


class TestOpenaiTaskView(unittest.TestCase):
    """内部任务视图 → 查询契约（恰好六个字段）。"""

    def test_completed_shape_matches_the_contract(self):
        view = {"id": "cgt-1", "status": "succeeded", "created_at": 1764240669,
                "content": {"video_url": "https://cdn/a.mp4"}}
        out = openai_task_view(view)
        self.assertEqual(
            set(out.keys()), {"id", "object", "status", "progress", "video_url", "created_at"}
        )
        self.assertEqual(out["object"], "video")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["progress"], 100)
        self.assertEqual(out["video_url"], "https://cdn/a.mp4")
        self.assertEqual(out["created_at"], 1764240669)

    def test_status_vocabulary(self):
        cases = {
            "queued": "queued",
            "running": "in_progress",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "failed",
            "whatever": "queued",
        }
        for ark, want in cases.items():
            out = openai_task_view({"id": "x", "status": ark})
            self.assertEqual(out["status"], want)

    def test_in_progress_has_zero_progress_and_no_url(self):
        out = openai_task_view({"id": "x", "status": "running", "content": {"video_url": None}})
        self.assertEqual(out["progress"], 0)
        self.assertIsNone(out["video_url"])

    def test_created_at_fallback_for_failed_upstream_fetch(self):
        out = openai_task_view({"id": "x", "status": "queued"}, created_at_fallback=1764240518)
        self.assertEqual(out["created_at"], 1764240518)


# ============================================================== HTTP 层 ====


class TestOpenaiHttpLayer(unittest.TestCase):
    """真实 ASGI 往返。上游是内存替身 —— 零外发、零消耗。"""

    def setUp(self):
        self.fake = FakeClient()
        self.app = _app_with_fake(self.fake)
        self.client = TestClient(self.app)

    def test_multipart_create_returns_the_contract_shape(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "doubao-seedance-1-0-lite_480p", "prompt": "一只猫", "seconds": "5", "size": "16:9"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        # 契约：恰好四个字段，一个不多一个不少
        self.assertEqual(set(j.keys()), {"id", "object", "status", "created_at"})
        self.assertTrue(j["id"].startswith("cgt-"))
        self.assertEqual(j["object"], "video")
        self.assertEqual(j["status"], "queued")
        self.assertIsInstance(j["created_at"], int)

    def test_json_create_returns_the_same_shape(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            json={"model": "doubao-seedance-1-0-pro_1080p", "prompt": "一只猫", "size": "adaptive"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(set(j.keys()), {"id", "object", "status", "created_at"})
        self.assertEqual(j["status"], "queued")
        # 参数真的进了翻译层：adaptive → 不设 aspectRatio
        self.assertIsNone(self.fake.created_params[0].get("aspectRatio"))
        self.assertEqual(self.fake.created_params[0]["resolution"], "1080p")

    def test_model_suffix_drives_resolution(self):
        self.client.post(
            OPENAI_VIDEOS_PATH,
            json={"model": "doubao-seedance-1-0-lite_480p", "prompt": "p"},
        )
        self.assertEqual(self.fake.created_params[0]["resolution"], "480p")

    def test_uploaded_file_is_rehosted_before_submitting(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "doubao-seedance-1-0-lite_480p", "prompt": "p"},
            files={"first_frame_image": ("a.png", png_bytes(), "image/png")},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.uploaded), 1)
        self.assertTrue(self.fake.uploaded[0].startswith("<"), "文件应被解成字节再上传")
        self.assertTrue(
            self.fake.created_params[0]["imageUrl"].startswith("https://static.img2video.ai/")
        )

    def test_dry_run_creates_nothing(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "doubao-seedance-1-0-lite_480p", "prompt": "p"},
            headers={"X-Avm-Dry-Run": "1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])
        self.assertEqual(self.fake.created_params, [])
        self.assertEqual(self.app.state.tasks.count(), 0)

    def test_query_returns_exactly_the_six_contract_fields(self):
        vid = self.client.post(
            OPENAI_VIDEOS_PATH, json={"model": "m", "prompt": "p"}
        ).json()["id"]
        r = self.client.get(f"{OPENAI_VIDEOS_PATH}/{vid}")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(
            set(j.keys()), {"id", "object", "status", "progress", "video_url", "created_at"}
        )
        self.assertEqual(j["id"], vid)
        self.assertEqual(j["object"], "video")
        self.assertEqual(j["status"], "completed")
        self.assertEqual(j["progress"], 100)
        self.assertEqual(j["video_url"], "https://cdn.example/a.mp4")
        self.assertIsInstance(j["created_at"], int)

    def test_query_of_a_running_task(self):
        fake = FakeClient(record={"id": "t1", "taskStatus": "processing", "aiModel": "minimax-h3"})
        app = _app_with_fake(fake)
        client = TestClient(app)
        vid = client.post(OPENAI_VIDEOS_PATH, json={"model": "m", "prompt": "p"}).json()["id"]
        j = client.get(f"{OPENAI_VIDEOS_PATH}/{vid}").json()
        self.assertEqual(j["status"], "in_progress")
        self.assertEqual(j["progress"], 0)
        self.assertIsNone(j["video_url"])

    def test_unknown_task_is_404(self):
        r = self.client.get(f"{OPENAI_VIDEOS_PATH}/cgt-nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TaskNotFound")

    def test_invalid_size_is_400(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH, data={"model": "m", "prompt": "p", "size": "5:4"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")

    def test_missing_prompt_is_400(self):
        r = self.client.post(OPENAI_VIDEOS_PATH, data={"model": "m"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")


class TestOpenaiGate(unittest.TestCase):
    """闸门语义与方舟线完全一致。"""

    def setUp(self):
        self.app = _app_with_fake(FakeClient(), gate_key="sk-secret")
        self.client = TestClient(self.app)

    def test_missing_token_is_401(self):
        r = self.client.post(OPENAI_VIDEOS_PATH, data={"model": "m", "prompt": "p"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AuthenticationError")

    def test_correct_token_passes(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "m", "prompt": "p"},
            headers={"Authorization": "Bearer sk-secret"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "queued")


class TestMultipartDependency(unittest.TestCase):
    """★ multipart 依赖必须**显式声明** —— "本地恰好装着、干净环境才现形"的典型。

    2026-09-15 实测：`request.form()` 在 starlette 里是**可选**能力，缺
    `python-multipart` 时**不是导入期报错**，而是**第一个 multipart 请求**才抛
    `AssertionError: The python-multipart library must be installed to use form parsing`。
    本地 venv 恰好装着（0.0.32）⇒ 550 项全绿；CI 的干净环境 **6 项失败**、镜像同样缺
    （Dockerfile 只装 `requirements.txt`）⇒ 整条发版流水线红。

    门禁把"依赖从哪来"钉在**声明**上：只要生产代码解析表单，`requirements.txt` 就必须
    有这一行，**同时**声明不许是死的（反向断言）——`pytest`/`pyyaml` 那种"只进
    requirements-dev"的做法在这里**不适用**：它在运行时真的会被用到。
    """

    ROOT = Path(__file__).resolve().parent.parent

    @staticmethod
    def declared_requirements(text: str) -> set:
        """`requirements.txt` 里**真正声明**的包名（注释一律不算）。

        为什么必须这么做：本文件的注释里也写了 "python-multipart"（解释它为什么必须声明），
        用 `assertIn("python-multipart", 全文)` 去判 ⇒ **把声明删掉**的变异照样全绿。
        这与 tests/test_minter_timezone.py 里 `apt_packages()` 是同一个坑（门禁被自己写的
        文档骗过），项目里已踩过两次 —— 凡"扫清单"一律先剥注释。
        """
        import re

        names = set()
        for line in text.splitlines():
            line = line.split(" #", 1)[0].strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                names.add(m.group(1).lower())
        return names

    def test_declared_in_requirements(self):
        declared = self.declared_requirements(
            (self.ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertIn(
            "python-multipart", declared,
            "requirements.txt 缺 python-multipart ⇒ 干净环境（CI / 镜像）里第一条 "
            "multipart 请求就会 AssertionError（详见该文件里的注释）："
            "`request.form()` 是 starlette 的可选能力，不会被任何依赖自动带进来",
        )

    def test_importable_in_this_environment(self):
        """本环境也得真装着：本地漏装时这条先红，而不是等到 CI 才红。"""
        import importlib.util

        self.assertTrue(
            any(importlib.util.find_spec(n) for n in ("python_multipart", "multipart")),
            "python-multipart 没装 ⇒ `pip install -r requirements.txt`",
        )

    def test_declaration_is_not_idle(self):
        """反向：声明了就必须有生产代码真用它，否则是给镜像白加依赖。"""
        code = (self.ROOT / "src" / "ark_compat" / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "request.form()", code,
            "requirements.txt 声明了 python-multipart，但 `app.py` 不再解析表单 ⇒ "
            "要么恢复用法，要么把这条依赖删掉",
        )


if __name__ == "__main__":
    unittest.main()
