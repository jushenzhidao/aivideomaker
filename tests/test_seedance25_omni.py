#!/usr/bin/env python3
"""Seedance 2.5「全能参考」协议的适配用例。

场景取自火山方舟《创建视频生成任务》文档的官方示例：
1 张参考图 + 6 段参考视频 + `duration:15` + `omni_reference_task_type:"reference"`
+ `output_format:"mov"` + `generate_audio:true`。

本适配层对参考素材的策略是**按上游标称上限截断**（站点 UI 文案，中英双份一致）：
参考图 ≤4 / 参考视频 ≤1 / 参考音频 ≤2。截断本身是"改变用户想要什么"，所以
每一个用例都要同时盯住两件事 —— **素材确实被截断了**，且**截断有留痕**
（点名被丢弃的 URL 与因此悬空的提示词占位符）。静默截断是本项目最贵的一类缺陷。

三条纪律（与 `test_ark_compat.py` 一致）：
  1. **零额度消耗** —— 提交一律走 dry-run，或让拦截发生在发出上游请求**之前**
     （死端口上游从头到尾不被碰到）；
  2. **零外发** —— 上游 base_url = 127.0.0.1:9，客户端 trust_env=False；
  3. 主要压纯函数，HTTP 层只做路由与错误信封的往返回归。

运行：python3 tests/test_seedance25_omni.py
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat import translate as T  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.errors import ParamError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

ARK_MODEL = "doubao-seedance-2-5-260628"
DEAD_UPSTREAM = "http://127.0.0.1:9"

TOS = "https://arkdocs.tos-cn-beijing.volces.com"
REF_IMAGE = f"{TOS}/images/video-generation/seedance2.5_reference1.png"
REF_VIDEOS = [f"{TOS}/videos/video-generation/seedance2.5_reference{n}.mp4" for n in (2, 3, 4, 5, 6, 7)]

PROMPT = (
    "明亮多彩的广告片风格，草莓味参考@图像1，开场参考@视频1的构图，随后参考@视频2的动态和运镜，"
    "参考@视频3的冲击感，参考@视频4的运动，结尾参考@视频5，最终参考@视频6收束。"
)


def doc_example(**kw) -> dict:
    """文档里那条完整示例（1 图 + 6 视频参考）。"""
    b = {
        "model": ARK_MODEL,
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": REF_IMAGE}, "role": "reference_image"},
            *[
                {"type": "video_url", "video_url": {"url": u}, "role": "reference_video"}
                for u in REF_VIDEOS
            ],
        ],
        "generate_audio": True,
        "ratio": "16:9",
        "duration": 15,
        "omni_reference_task_type": "reference",
        "output_format": "mov",
    }
    b.update(kw)
    return b


def minimal(**kw) -> dict:
    b = {
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "a red balloon"}],
        "ratio": "16:9",
        "resolution": "480p",
        "duration": 5,
    }
    b.update(kw)
    return b


def ref_video(url: str) -> dict:
    return {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}


def settings(**kw) -> Settings:
    base = dict(
        cookie="auth_session=" + "x" * 40,
        base_url=DEAD_UPSTREAM,
        log_level="CRITICAL",
        enable_logfire=False,
        trust_env=False,
        task_store="memory",  # 任务表落内存：单测不落盘、不互相污染
    )
    base.update(kw)
    return Settings(**base)


def dropped_notes(plan, kind: str) -> list[str]:
    return [w for w in plan["warnings"] if w.startswith(f"{kind}:") and "dropped" in w]


# ==================================================== 纯翻译层：官方示例 ========


class TestDocumentedExample(unittest.TestCase):
    """文档示例：6 段参考视频按上游上限截断到 1 段，且截断必须留痕。"""

    def setUp(self):
        self.plan = T.translate_create(doc_example())
        self.wp = self.plan["web_params"]

    def test_only_the_first_reference_video_survives(self):
        self.assertEqual(self.wp["referenceVideoUrl"], REF_VIDEOS[0])
        note = dropped_notes(self.plan, "reference_video")[0]
        for u in REF_VIDEOS[1:]:
            self.assertIn(u, note, "每一段被丢弃的视频都必须被点名")
        self.assertIn("5 dropped", note)

    def test_truncation_is_never_silent(self):
        # 6 段视频丢 5；@视频2…@视频6 的占位符因此全部悬空，必须逐个点名
        joined = " ".join(self.plan["warnings"])
        for n in (2, 3, 4, 5, 6):
            self.assertIn(f"@视频{n}", joined)

    def test_dangling_prompt_placeholders_are_named(self):
        dangling = [w for w in self.plan["warnings"] if "cannot resolve" in w]
        self.assertTrue(dangling, "悬空占位符必须告警")
        self.assertIn("@视频2", dangling[0])

    def test_extra_parameters_are_echoed_not_dropped(self):
        self.assertEqual(self.wp["aspectRatio"], "16:9")
        self.assertEqual(self.plan["requested"]["omni_reference_task_type"], "reference")
        self.assertEqual(self.plan["requested"]["output_format"], "mov")

    def test_billing_is_flagged_on_the_web_line(self):
        eff, _ = T.billing_view(self.plan)
        self.assertTrue(eff["billed"])  # 15s 已越过 ≤8s 的免费窗口
        self.assertEqual(eff["tier"], "turbo")

    def test_effective_reports_the_actual_container(self):
        self.assertEqual(self.plan["effective"]["output_format"], "mp4")

    def test_nothing_is_silently_lost(self):
        """本次请求的每一项要么发出去了、要么出现在 warnings 里。"""
        joined = " ".join(self.plan["warnings"] + self.plan["unsupported"])
        self.assertIn("mov", joined)
        self.assertIn("generate_audio", joined)


class TestReferenceTruncation(unittest.TestCase):
    def test_one_reference_video_is_kept_as_is(self):
        plan = T.translate_create(
            minimal(content=[{"type": "text", "text": "x"}, ref_video(REF_VIDEOS[0])])
        )
        self.assertEqual(plan["web_params"]["referenceVideoUrl"], REF_VIDEOS[0])
        self.assertFalse(dropped_notes(plan, "reference_video"))

    def test_two_videos_are_truncated_to_one(self):
        plan = T.translate_create(
            minimal(content=[{"type": "text", "text": "x"}, ref_video(REF_VIDEOS[0]), ref_video(REF_VIDEOS[1])])
        )
        self.assertEqual(plan["web_params"]["referenceVideoUrl"], REF_VIDEOS[0])
        self.assertNotIn("referenceVideoUrls", plan["web_params"])
        self.assertIn(REF_VIDEOS[1], dropped_notes(plan, "reference_video")[0])

    def test_surplus_reference_images_are_truncated_to_four(self):
        imgs = [{"type": "image_url", "image_url": {"url": f"https://x/{i}.png"}} for i in range(6)]
        plan = T.translate_create(minimal(content=[{"type": "text", "text": "x"}, *imgs]))
        self.assertEqual(len(plan["web_params"]["referenceImageUrls"]), 4)
        note = dropped_notes(plan, "reference_image")[0]
        self.assertIn("https://x/4.png", note)
        self.assertIn("https://x/5.png", note)

    def test_surplus_reference_audio_is_truncated_to_two(self):
        auds = [
            {"type": "audio_url", "audio_url": {"url": f"https://x/{i}.mp3"}, "role": "reference_audio"}
            for i in range(3)
        ]
        plan = T.translate_create(minimal(content=[{"type": "text", "text": "x"}, *auds]))
        self.assertEqual(len(plan["web_params"]["referenceAudioUrls"]), 2)
        self.assertIn("https://x/2.mp3", dropped_notes(plan, "reference_audio")[0])

    def test_web_client_body_never_carries_a_multi_video_field(self):
        """出口一律单槽位 —— 截断发生在翻译层，客户端不再拼数组。"""
        c = WebClient("auth_session=" + "x" * 40, base_url=DEAD_UPSTREAM, trust_env=False)
        seen: dict = {}

        def fake_trpc(proc, inp=None, **kw):
            seen.clear()
            seen.update(inp or {})
            return "t-1"

        with mock.patch.object(WebClient, "needs_captcha", return_value=False), mock.patch.object(
            WebClient, "trpc", side_effect=fake_trpc
        ):
            c.create({"content": "x", "referenceVideoUrl": REF_VIDEOS[0]})
            self.assertEqual(seen["referenceVideoUrl"], REF_VIDEOS[0])
            self.assertNotIn("referenceVideoUrls", seen)


class TestOmniTaskType(unittest.TestCase):
    def test_reference_and_auto_are_accepted(self):
        for value in ("reference", "auto"):
            plan = T.translate_create(minimal(omni_reference_task_type=value))
            self.assertEqual(plan["requested"]["omni_reference_task_type"], value)

    def test_enum_is_checked(self):
        with self.assertRaises(ParamError):
            T.translate_create(minimal(omni_reference_task_type="resurrect"))

    def test_edit_and_extend_are_incompatible_with_the_web_upstream(self):
        for value in ("edit", "extend"):
            plan = T.translate_create(minimal(omni_reference_task_type=value))
            self.assertTrue(plan["incompatible"], value)

    def test_edit_ratio_and_duration_constraints_are_reported(self):
        plan = T.translate_create(minimal(omni_reference_task_type="edit", duration=6))
        joined = " ".join(plan["warnings"])
        self.assertIn('ratio="adaptive"', joined)
        self.assertIn("duration=-1", joined)

    def test_read_from_extra_body_too(self):
        plan = T.translate_create(minimal(extra_body={"omni_reference_task_type": "extend"}))
        self.assertTrue(plan["incompatible"])


class TestOutputFormatAndAudio(unittest.TestCase):
    def test_mov_is_downgraded_with_an_explicit_warning(self):
        plan = T.translate_create(minimal(output_format="mov"))
        self.assertTrue(any("mp4" in w and "not mov" in w for w in plan["warnings"]))
        self.assertEqual(plan["effective"]["output_format"], "mp4")

    def test_mp4_needs_no_warning(self):
        plan = T.translate_create(minimal(output_format="mp4"))
        self.assertFalse([w for w in plan["warnings"] if "not mp4" in w])

    def test_output_format_enum_is_checked(self):
        with self.assertRaises(ParamError):
            T.translate_create(minimal(output_format="avi"))

    def test_generate_audio_is_modelled_not_silently_accepted(self):
        plan = T.translate_create(minimal(generate_audio=True))
        self.assertTrue(plan["requested"]["generate_audio"])
        self.assertTrue(any("not a station-side switch" in w for w in plan["warnings"]))

    def test_generate_audio_must_be_boolean(self):
        with self.assertRaises(ParamError):
            T.translate_create(minimal(generate_audio="yes"))


class TestPromptPlaceholders(unittest.TestCase):
    def test_index_helper(self):
        found = T.ref_indices_in_prompt("看图@图像1 与 @图像3，再看@视频2、@视频2")
        self.assertEqual(found["图像"], [1, 3])
        self.assertEqual(found["视频"], [2])
        self.assertEqual(found["音频"], [])

    def test_dangling_reference_is_reported(self):
        plan = T.translate_create(
            minimal(content=[{"type": "text", "text": "参考@视频7"}, ref_video(REF_VIDEOS[0])])
        )
        self.assertTrue(any("@视频7" in w and "cannot resolve" in w for w in plan["warnings"]))

    def test_complete_references_produce_no_warning(self):
        plan = T.translate_create(
            minimal(content=[{"type": "text", "text": "参考@视频1"}, ref_video(REF_VIDEOS[0])])
        )
        self.assertFalse([w for w in plan["warnings"] if "cannot resolve" in w])


# ============================================================ HTTP 层 =========


class TestOmniHttpLayer(unittest.TestCase):
    """真实 ASGI 往返：上游是死端口，任何真提交都会立刻连接失败。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app(settings()))

    def test_dry_run_returns_the_full_translation(self):
        r = self.client.post(TASKS_PATH, json=doc_example(extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["dry_run"])
        self.assertEqual(j["web_params"]["referenceVideoUrl"], REF_VIDEOS[0])
        self.assertTrue(j["effective"]["billed"])
        self.assertEqual(j["incompatible"], [])
        self.assertTrue(any("5 dropped" in w for w in j["warnings"]))

    def test_edit_is_visible_in_dry_run_but_refused_before_submitting(self):
        payload = doc_example(omni_reference_task_type="edit")

        dry = self.client.post(TASKS_PATH, json={**payload, "extra_body": {"aivideomaker_dry_run": True}})
        self.assertEqual(dry.status_code, 200)
        self.assertTrue(dry.json()["incompatible"])

        live = self.client.post(TASKS_PATH, json=payload)
        self.assertEqual(live.status_code, 400)
        # 关键：拿到的是 InvalidParameter —— 证明拦截发生在"发出上游请求"之前，
        # 死端口上游从头到尾没被碰过（否则这里会是 502 连接失败）。
        self.assertEqual(live.json()["error"]["code"], "InvalidParameter")
        self.assertIn("edit", live.json()["error"]["message"])

    def test_reference_task_is_not_refused_by_the_compatibility_gate(self):
        """对照组：reference 类请求不该被兼容门禁拦下 —— 它会真的打上游（死端口），
        以 5xx 收场。关键是**不是** 400 InvalidParameter。"""
        r = self.client.post(TASKS_PATH, json=doc_example())
        self.assertNotEqual(r.status_code, 400)
        self.assertNotEqual(r.json().get("error", {}).get("code"), "InvalidParameter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
