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
from unittest import mock

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

    def upload_file(self, source, name=None, permanent=False, budget=None):
        # `budget` 是转存阶段新增的**跨媒体项总预算**（MediaFetchBudget），调用处按关键字传
        # ⇒ 替身必须收，否则 TypeError（同类坑：真签名变了、替身没跟着变）。
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
    # 映射/契约类门禁用**同步推进**（submit_inline，见 _schedule_videos_submit）：
    # 它们考的是参数映射与响应契约，不是受理时序 —— 时序由 test_videos_async_accept 钉住。
    app.state.submit_inline = True
    # 换掉真实上游：app 只读 upstreams 注册表（与 test_web_upstream 同一手法）
    app.state.upstreams = {
        "web": WebUpstream(fake, WebSubmitQueue(fake, max_concurrent=2, poll_interval=0.01))
    }
    return app


# ============================================================ 纯翻译层 ====


class TestArkBodyFromOpenai(unittest.TestCase):
    """OpenAI 字段 → Ark 请求体（复用 translate_create 做重活）。"""

    def test_minimal_form(self):
        """最小表单（只给 model + prompt）：这一层把字段落到哪儿。

        ⚠️ `model` 名**不再决定上游槽位**（本面只跑免费档，槽位在 app 层强制），但它带的
        分辨率后缀仍然决定档位。这里断言的是**本层**的产物；路由见 `test_videos_free_only`。
        """
        body, notes = ark_body_from_openai(
            {"model": "minimaxH3_480p", "prompt": "360度环绕运镜"}
        )
        self.assertEqual(body["model"], "minimaxH3_480p", "本层不改写调用方写下的名字")
        self.assertEqual(body["content"], [{"type": "text", "text": "360度环绕运镜"}])
        # 分辨率来自 model 名的档位后缀
        self.assertEqual(body["resolution"], "480p")
        plan = translate_create(body)
        self.assertEqual(plan["web_params"]["duration"], 10, "480p 被钉到免费区最长档")
        self.assertEqual(
            notes,
            ["seconds omitted — this endpoint pins 480p to 10s"],
            "补默认值必须留痕，且只该有这一条",
        )

    def test_1080p_is_downgraded_into_the_free_tier(self):
        """★ 本面只跑免费档：`_1080p` 先**降级**成 720p，时长再被钉到 8s。

        依据：站点对 1080p **没有**实测免费线（按秒计价）⇒ 不降级就等于"对外承诺只看免费档、
        实际却在花钱"。2026-09-20 用户口径：「**8s 720p**」。改档必须留痕（两条都留）。
        """
        body, notes = ark_body_from_openai(
            {"model": "minimaxH3_1080p", "prompt": "p", "seconds": 15}
        )
        self.assertEqual(body["resolution"], "720p", "1080p 必须降到免费档内的分辨率")
        self.assertEqual(body["duration"], 8, "降级之后按 720p 钉死在免费区最长档")
        self.assertTrue(any("downgraded" in n for n in notes), f"降级是改档，必须留痕：{notes}")
        plan = translate_create(body)
        self.assertFalse(plan["effective"]["billed"], "降级 + 钉死之后不该再落在计费区")

    def test_model_without_resolution_suffix_keeps_translate_default(self):
        body, _ = ark_body_from_openai({"model": "sora-2", "prompt": "p"})
        self.assertNotIn("resolution", body)

    def test_seconds_are_parsed_then_pinned(self):
        """秒数**解析**（字符串 / 整数都认）与**钉死**是两件事 —— 本面只保留后者。

        ⚠️ 旧的"未被钉死的载具"已不存在（2026-09-20：`_1080p` 也降级）⇒ "传 12 得到 12"
        这一格在本面**不可能**出现。解析本身仍要守住（非法值 400，见下一个用例），所以这里
        直接测解析函数；本面的**结果**由最后那条断言钉住。
        """
        from ark_compat.openai_videos import _seconds_to_duration

        for v, want in (("8", 8), (8, 8), ("12", 12)):
            self.assertEqual(_seconds_to_duration(v), want)
        body, _ = ark_body_from_openai({"model": "minimaxH3_720p", "prompt": "p", "seconds": "12"})
        self.assertEqual(body["duration"], 8, "任何合法秒数落到本面都进免费区最长档")

    def test_seconds_non_integer_is_rejected(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "seconds": "abc"})
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "seconds": "5.5"})

    def test_size_ratio_passthrough(self):
        # 静默载具：分辨率在免费档内 + 秒数恰好等于钉死值 ⇒ 本层不产生任何 note，
        # `assertEqual(notes, [])` 考的才是 size（旧载具 1080p 现在会带一条降级说明）。
        body, notes = ark_body_from_openai(
            {"model": "minimaxH3_480p", "prompt": "p", "seconds": 10, "size": "9:16"}
        )
        self.assertEqual(body["ratio"], "9:16")
        self.assertEqual(notes, [])

    def test_size_keep_ratio_maps_to_adaptive_with_a_note(self):
        body, notes = ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "size": "keep_ratio"})
        self.assertEqual(body["ratio"], "adaptive")
        self.assertTrue(any("keep_ratio" in n and "adaptive" in n for n in notes))

    def test_size_wxh_maps_to_reduced_ratio(self):
        body, notes = ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "size": "1920x1080"})
        self.assertEqual(body["ratio"], "16:9")
        self.assertTrue(any("1920x1080" in n and "16:9" in n for n in notes))

    def test_unknown_size_falls_back_instead_of_400(self):
        """本端点的 `size` 认不出时**兜底 16:9**（不再是"原样透传 → 下游 400"）。

        口径与 `translate_create` 的严格校验分家：Ark 线仍拒 `5:4`，这里是 OpenAI SDK
        用户，写错一个字符不该让整请求失败。详见 `tests/test_ratio_normalize.py`。
        """
        body, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "5:4"})
        self.assertEqual(body["ratio"], "4:3", "可读的比例串走就近吸附")
        self.assertTrue(any("5:4" in n for n in notes), "吸附必须留痕")
        # 且这条 body 在下游是**可提交**的（不再被 translate_create 挡下）
        plan = translate_create(body)
        self.assertEqual(plan["web_params"]["aspectRatio"], "4:3")

    def test_input_reference_single_string_and_list(self):
        body, _ = ark_body_from_openai(
            {"model": "minimaxH3", "prompt": "p", "input_reference": "https://x/a.jpg"}
        )
        body2, _ = ark_body_from_openai(
            {"model": "minimaxH3", "prompt": "p", "input_reference": ["https://x/a.jpg", "https://x/b.jpg"]}
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
            {"model": "minimaxH3", "prompt": "p", "input_reference": "", "first_frame_image": ""}
        )
        self.assertEqual(len(body["content"]), 1)

    def test_input_reference_json_string_array_is_parsed(self):
        """🔴 表单里"数组写成了字符串"（`--form 'input_reference=["u1","u2"]'`）必须被解析。

        原样透传会变成 `referenceImageUrls` 里的**一个垃圾项**（既不是 URL 也不是 data URI），
        一路静默到转存阶段才炸 —— 调用方完全看不出是自己把数组写成了字符串
        （实测：旧行为下 `warnings` 里一个字都没有）。
        """
        body, notes = ark_body_from_openai(
            {"model": "minimaxH3", "prompt": "p",
             "input_reference": '["https://x/a.png","https://x/b.png"]'}
        )
        urls = [c["image_url"]["url"] for c in body["content"] if c.get("type") == "image_url"]
        self.assertEqual(urls, ["https://x/a.png", "https://x/b.png"])
        self.assertTrue(any("JSON string" in n for n in notes), f"摊平必须留痕：{notes}")

    def test_input_reference_broken_json_string_is_rejected(self):
        """看着像数组但不是合法 JSON ⇒ **明确拒**（不许当成一个 URL 用）。"""
        with self.assertRaises(ParamError) as ctx:
            ark_body_from_openai(
                {"model": "minimaxH3", "prompt": "p", "input_reference": '["https://x/a.png"'}
            )
        self.assertIn("合法 JSON", str(ctx.exception))

    def test_frame_field_given_a_list_is_rejected_not_silently_dropped(self):
        """🔴 帧字段给成数组 ⇒ **明确拒**，不许**静默丢素材**（本项目红线之一）。

        实测过旧行为：`first_frame_image: ["https://x/f.png"]` → 图片项 0 个、`notes` 为空，
        调用方看到的是"提交成功、只是没有首帧"。参考素材静默丢失比直接报错糟得多。
        """
        with self.assertRaises(ParamError) as ctx:
            ark_body_from_openai(
                {"model": "minimaxH3", "prompt": "p", "first_frame_image": ["https://x/f.png"]}
            )
        self.assertIn("不接受数组", str(ctx.exception))

    def test_first_and_last_frame(self):
        body, _ = ark_body_from_openai(
            {
                "model": "minimaxH3",
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
                "model": "minimaxH3",
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
            {"model": "minimaxH3", "prompt": "p", "input_reference": b64}, reference_format="b64"
        )
        url = body["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_invalid_b64_is_a_clean_400(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai(
                {"model": "minimaxH3", "prompt": "p", "input_reference": "!!!not-b64!!!"},
                reference_format="b64",
            )

    def test_urls_survive_b64_mode(self):
        body, _ = ark_body_from_openai(
            {"model": "minimaxH3", "prompt": "p", "input_reference": "https://x/a.jpg"},
            reference_format="b64",
        )
        self.assertEqual(body["content"][1]["image_url"]["url"], "https://x/a.jpg")

    def test_model_and_prompt_are_required(self):
        with self.assertRaises(ParamError):
            ark_body_from_openai({"prompt": "p"})
        with self.assertRaises(ParamError):
            ark_body_from_openai({"model": "minimaxH3", "prompt": ""})

    def test_unknown_fields_are_reported_not_silently_dropped(self):
        _, notes = ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "seed": 7, "watermark": True})
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
            data={"model": "minimaxH3_480p", "prompt": "一只猫", "seconds": "5", "size": "16:9"},
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
            json={"model": "minimaxH3_480p", "prompt": "一只猫", "seconds": 10, "size": "adaptive"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(set(j.keys()), {"id", "object", "status", "created_at"})
        self.assertEqual(j["status"], "queued")
        # 参数真的进了翻译层：adaptive → 不设 aspectRatio
        self.assertIsNone(self.fake.created_params[0].get("aspectRatio"))
        self.assertEqual(self.fake.created_params[0]["resolution"], "480p")

    def test_model_suffix_drives_resolution(self):
        self.client.post(
            OPENAI_VIDEOS_PATH,
            json={"model": "minimaxH3_480p", "prompt": "p"},
        )
        self.assertEqual(self.fake.created_params[0]["resolution"], "480p")

    def test_uploaded_file_is_rehosted_before_submitting(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3_480p", "prompt": "p"},
            files={"first_frame_image": ("a.png", png_bytes(), "image/png")},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.uploaded), 1)
        self.assertTrue(self.fake.uploaded[0].startswith("<"), "文件应被解成字节再上传")
        self.assertTrue(
            self.fake.created_params[0]["imageUrl"].startswith("https://static.img2video.ai/")
        )

    def test_oversize_part_is_rejected_at_ingest_without_uploading(self):
        """★ 文件部件有**单件上限**：超了在读的时候就拒（400），一个字节都不上传。

        为什么这道闸必须有：`.form()` 会把 >1MB 的部件**落盘**、`read()` 再**整个**读进内存、
        然后 base64（+33%）⇒ 没有闸时一个超大件是"先写满磁盘 → 吃掉几倍内存 → 最后才被站点
        `maxBytes` 拒掉"。断言三件事：状态码、**消息点名上限**、以及"既没转存也没提交"。
        """
        from ark_compat import app as app_module

        big = png_bytes() + b"x" * 8192
        with mock.patch.object(app_module, "_FORM_PART_MAX_BYTES", 4096):
            r = self.client.post(
                OPENAI_VIDEOS_PATH,
                data={"model": "minimaxH3_480p", "prompt": "p"},
                files={"first_frame_image": ("a.png", big, "image/png")},
            )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("单件上限", r.text)
        self.assertEqual(self.fake.uploaded, [], "超限的件绝不许被转存")
        self.assertEqual(self.fake.created_params, [], "更不许把任务提交出去（那一步计费）")

    def test_oversize_body_is_rejected_from_content_length(self):
        """另一道闸在**解析之前**：光靠 `Content-Length` 就拒掉，连落盘都不做。

        ⚠️ 报文用词必须与"单件上限"**不同**（这里是"请求体"），断言各查各的词 —— 否则
        删掉其中一条、靠另一条兜住也会绿（这是变异自证里踩过的典型假绿）。
        """
        from ark_compat import app as app_module

        with mock.patch.object(app_module, "_FORMS_MAX_BODY_BYTES", 1024):
            r = self.client.post(
                OPENAI_VIDEOS_PATH,
                data={"model": "minimaxH3_480p", "prompt": "p"},
                files={"first_frame_image": ("a.png", png_bytes() + b"y" * 4096, "image/png")},
            )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("请求体", r.text)
        self.assertEqual(self.fake.uploaded, [])

    def test_a_normal_file_upload_passes_both_gates(self):
        """对照组：正常大小的文件必须照旧走通（否则上面两条只是"什么都过不去"）。"""
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3_480p", "prompt": "p"},
            files={"first_frame_image": ("a.png", png_bytes(), "image/png")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.fake.uploaded), 1)
        self.assertTrue(self.fake.created_params[0]["imageUrl"].startswith("https://static.img2video.ai/"))

    def test_form_json_string_array_is_flattened_per_element(self):
        """★ 表单路径的"数组写成字符串"必须**按元素**救援（不是只看顶层）。

        🔴 实测踩到：`input_reference` 在表单里**天生是一个列表**（重复部件），所以"数组写成
        字符串"落在**元素**上 —— 只救顶层时，form 路径照样把整串当成一个垃圾 URL 静默带走，
        而直接调翻译层的单测却是绿的。这条就是那个盲区的守门。
        """
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p",
                  "seconds": "5", "size": "adaptive",
                  "input_reference": '["https://x/a.png","https://x/b.png"]'},
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["web_params"]["referenceImageUrls"], ["https://x/a.png", "https://x/b.png"],
            "必须摊平成两项（原样透传会变成一个垃圾 URL）",
        )
        self.assertTrue(any("JSON string" in w for w in r.json()["warnings"]), "摊平必须留痕")

    def test_form_frame_field_json_array_is_rejected(self):
        """帧字段拿到数组 ⇒ 400（不许静默丢素材）。"""
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p", "first_frame_image": '["https://x/f.png"]'},
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1"},
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不接受数组", r.text)

    def test_repeated_file_streams_mix_with_urls_and_respect_the_cap(self):
        """`input_reference` 是列表：**文件流**、URL、两者混合都收；超 4 张截断且**留痕**。"""
        files = [("input_reference", (f"r{i}.png", png_bytes(), "image/png")) for i in range(2)]
        files.append(("input_reference", ("", "https://files.test/u.png")))
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p"},
            files=files,
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        refs = r.json()["web_params"]["referenceImageUrls"]
        self.assertEqual(len(refs), 3)
        self.assertEqual(sum(1 for x in refs if str(x).startswith("data:")), 2, "文件流成 data URI")
        self.assertEqual(refs[-1], "https://files.test/u.png", "URL 原样保留")

        many = [("input_reference", (f"r{i}.png", png_bytes(), "image/png")) for i in range(3)]
        many += [("input_reference", ("", f"https://files.test/{n}.png")) for n in ("u", "v")]
        r2 = self.client.post(
            OPENAI_VIDEOS_PATH, data={"model": "minimaxH3", "prompt": "p"}, files=many,
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1"},
        )
        self.assertEqual(len(r2.json()["web_params"]["referenceImageUrls"]), 4, "上限 4 张")
        self.assertTrue(
            any("at most 4" in w for w in r2.json()["warnings"]),
            "截断必须留痕（参考素材不能静默丢）",
        )

    def test_ignored_channel_header_is_warned(self):
        """🔴 `x-base-url` 这类"渠道选择"头本服务无法满足 ⇒ 必须**留痕**，不许静默。

        它表达的是"请走 XX 线"，而本服务只有 web 逆向线 —— 静默服务等于**悄悄换掉上游**
        （计费口径 / 排队 / 质量都不同）。留痕走 warnings（与"未知字段被忽略"同一通道）。
        """
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p"},
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1", "x-base-url": "volc"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        warns = r.json()["warnings"]
        self.assertTrue(any("x-base-url" in w and "volc" in w for w in warns), f"没留痕：{warns}")

        r2 = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p"},
            headers={"Authorization": "Bearer x", "x-avm-dry-run": "1"},
        )
        self.assertFalse(
            [w for w in r2.json()["warnings"] if "x-base-url" in w],
            "对照：没带这个头时不许凭空产生该告警（否则告警会被当噪声忽略）",
        )

    def test_dry_run_creates_nothing(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3_480p", "prompt": "p"},
            headers={"X-Avm-Dry-Run": "1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])
        self.assertEqual(self.fake.created_params, [])
        self.assertEqual(self.app.state.tasks.count(), 0)

    def test_query_returns_exactly_the_six_contract_fields(self):
        vid = self.client.post(
            OPENAI_VIDEOS_PATH, json={"model": "minimaxH3", "prompt": "p"}
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
        vid = client.post(OPENAI_VIDEOS_PATH, json={"model": "minimaxH3", "prompt": "p"}).json()["id"]
        j = client.get(f"{OPENAI_VIDEOS_PATH}/{vid}").json()
        self.assertEqual(j["status"], "in_progress")
        self.assertEqual(j["progress"], 0)
        self.assertIsNone(j["video_url"])

    def test_unknown_task_is_404(self):
        r = self.client.get(f"{OPENAI_VIDEOS_PATH}/cgt-nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TaskNotFound")

    def test_unreadable_size_falls_back_to_16_9_not_400(self):
        """`size` 认不出 ⇒ 200 且按 16:9 提交（2026-09-18 起，此前是 400）。

        曾经暴露的问题是：OpenAI SDK 用户把 `size` 写成 `1024x1792` / `5:4` 就整请求
        失败，而他能拿到的信息只有"请求失败了"。现在改成"最接近的一档 + warnings 里
        写清差多少"，并且 `5:4` 这类可读比例走就近吸附（→ 4:3）而不是兜底。
        ⚠️ 兜底**不等于什么都收**：本服务的 Ark 线 `/tasks` 仍然 400（见
        `test_ratio_normalize.TestArkLineStillRejectsUnknown`）。
        """
        r = self.client.post(
            OPENAI_VIDEOS_PATH, data={"model": "minimaxH3", "prompt": "p", "size": "5:4"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.created_params[-1]["aspectRatio"], "4:3")

    def test_gibberish_size_still_creates_a_task(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH, data={"model": "minimaxH3", "prompt": "p", "size": "banana"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.created_params[-1]["aspectRatio"], "16:9")

    def test_missing_prompt_is_400(self):
        r = self.client.post(OPENAI_VIDEOS_PATH, data={"model": "minimaxH3"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")


class TestOpenaiGate(unittest.TestCase):
    """闸门语义与方舟线完全一致。"""

    def setUp(self):
        self.app = _app_with_fake(FakeClient(), gate_key="sk-secret")
        self.client = TestClient(self.app)

    def test_missing_token_is_401(self):
        r = self.client.post(OPENAI_VIDEOS_PATH, data={"model": "minimaxH3", "prompt": "p"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AuthenticationError")

    def test_correct_token_passes(self):
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            data={"model": "minimaxH3", "prompt": "p"},
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
