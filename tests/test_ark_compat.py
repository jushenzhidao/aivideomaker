#!/usr/bin/env python3
"""ark_compat 的测试。

两条纪律：
  1. **零额度消耗**：所有提交路径都走 dry-run；真实提交只测"没有支出上限被拒绝"
     这一条 —— 它在发出上游请求**之前**就返回，所以永远不会真的计费。
  2. **零外发**：上游 base_url 指向一个没人监听的本地端口（死端口），任何"手滑
     发出真实请求"都会立刻连接失败；Logfire 用 `send_to_logfire=False`。

运行：python3 tests/test_ark_compat.py
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat import translate as T  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.client import OfficialClient  # noqa: E402
from ark_compat.errors import BudgetUnsetError, OfficialApiError, ParamError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402

ARK_MODEL = "doubao-seedance-2-5-260628"
DEAD_UPSTREAM = "http://127.0.0.1:9"  # discard 端口，保证不出网


def settings(**kw) -> Settings:
    base = dict(
        upstream_key="ak_test",
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,  # 默认关掉，避免测试互相污染全局 logfire
        trust_env=False,  # 绕开系统代理，保证"死端口"真的是死端口
        task_store="memory",  # 任务表落内存：单测不落盘、不互相污染
    )
    base.update(kw)
    return Settings(**base)


def body(**kw) -> dict:
    b = {
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "a red balloon"}],
        "ratio": "16:9",
        "resolution": "480p",
        "duration": 5,
    }
    b.update(kw)
    return b


def text(s="a red balloon"):
    return {"type": "text", "text": s}


# ============================================================ 纯翻译层 ========


class TestModelMapping(unittest.TestCase):
    def test_seedance_maps_to_seedance20(self):
        self.assertEqual(T.pick_official_model(ARK_MODEL), "seedance20")
        self.assertEqual(T.pick_official_model("doubao-seedance-1-0-lite-t2v"), "seedance20")

    def test_other_families(self):
        self.assertEqual(T.pick_official_model("minimax-hailuo-02"), "minimax")
        self.assertEqual(T.pick_official_model("wan-2.7-t2v"), "wan27")
        self.assertEqual(T.pick_official_model("happyhorse-1-0"), "happyhorse")

    def test_unknown_defaults_to_seedance20(self):
        self.assertEqual(T.pick_official_model("some-new-model"), "seedance20")

    def test_override_wins(self):
        self.assertEqual(T.pick_official_model(ARK_MODEL, "i2v"), "i2v")

    def test_eight_official_models(self):
        self.assertEqual(len(T.OFFICIAL_MODELS), 8)


class TestToOfficialBody(unittest.TestCase):
    """类型转换 —— 官方线 INVALID_PAYLOAD 的主要来源。"""

    def params(self, **kw):
        p = {"content": "a cat", "duration": 5, "resolution": "480p", "aspectRatio": "16:9", "tier": "turbo"}
        p.update(kw)
        return p

    def test_seedance20_uses_prompt_and_numeric_types(self):
        b = T.to_official_body("seedance20", self.params(), [])
        self.assertEqual(b["prompt"], "a cat")
        self.assertNotIn("content", b)
        self.assertIsInstance(b["duration"], int)
        self.assertIsInstance(b["resolution"], int)
        self.assertEqual(b["resolution"], 480)
        self.assertEqual(b["ratio"], "16:9")

    def test_seedance20_downgrades_1080p(self):
        w = []
        b = T.to_official_body("seedance20", self.params(resolution="1080p"), w)
        self.assertEqual(b["resolution"], 720)
        self.assertTrue(any("480/720" in x for x in w))

    def test_seedance20_image_field_flagged(self):
        w = []
        b = T.to_official_body("seedance20", self.params(imageUrl="https://x/y.png"), w)
        self.assertEqual(b["image"], "https://x/y.png")
        self.assertTrue(any("unverified" in x for x in w))

    def test_minimax_lowercase_resolution_and_tier(self):
        w = []
        b = T.to_official_body("minimax", self.params(resolution="1080p", tier="base"), w)
        self.assertEqual(b["content"], "a cat")
        self.assertEqual(b["resolution"], "1080p")
        self.assertEqual(b["tier"], "base")
        self.assertEqual(b["aspectRatio"], "16:9")
        self.assertEqual([], w)

    def test_minimax_downgrades_480p(self):
        w = []
        b = T.to_official_body("minimax", self.params(resolution="480p"), w)
        self.assertEqual(b["resolution"], "720p")
        self.assertTrue(any("720p/1080p" in x for x in w))

    def test_t2v_duration_is_a_string(self):
        b = T.to_official_body("t2v", self.params(), [])
        self.assertEqual(b["duration"], "5")
        self.assertIsInstance(b["duration"], str)

    def test_i2v_requires_image(self):
        with self.assertRaises(ParamError):
            T.to_official_body("i2v", self.params(), [])
        b = T.to_official_body("i2v", self.params(imageUrl="https://x/f.png"), [])
        self.assertEqual(b["image"], "https://x/f.png")
        self.assertEqual(b["duration"], "5")

    def test_wan27_uppercase_resolution_and_ratio_field(self):
        b = T.to_official_body("wan27", self.params(resolution="720p"), [])
        self.assertEqual(b["resolution"], "720P")
        self.assertEqual(b["ratio"], "16:9")

    def test_happyhorse_drops_ratio_with_a_warning(self):
        w = []
        b = T.to_official_body("happyhorse", self.params(resolution="1080p"), w)
        self.assertEqual(b["resolution"], "1080P")
        self.assertNotIn("ratio", b)
        self.assertTrue(any("no ratio" in x for x in w))

    def test_unknown_model_raises(self):
        with self.assertRaises(ParamError):
            T.to_official_body("nope-9", self.params(), [])


class TestSnapDuration(unittest.TestCase):
    def test_exact_values_pass_through(self):
        self.assertEqual(T.snap_duration(5, "480p"), 5)
        self.assertEqual(T.snap_duration(10, "480p"), 10)

    def test_480p_accepts_any_integer(self):
        """站点对时长是"连续秒数 + 上限"，不是离散档位。

        曾经 480p 被写成 `[5, 10, 15, 20]`，会把 `duration=8`（本就在免费线内）
        吸附到 10s，从而**跨进计费区** —— 静默变贵，是本项目最该避免的一类。
        """
        self.assertEqual(T.snap_duration(8, "480p"), 8)
        self.assertEqual(T.snap_duration(6, "480p"), 6)

    def test_out_of_range_clamps_to_the_nearest_legal_value(self):
        self.assertEqual(T.snap_duration(1, "480p"), 5)
        self.assertEqual(T.snap_duration(99, "480p"), 20)

    def test_prefer_free_pulls_billed_durations_back_under_the_line(self):
        """`prefer_free` 的用途是**主动省钱**：合法但已计费的时长要能拉回免费区。"""
        self.assertEqual(T.snap_duration(12, "480p"), 12)
        self.assertEqual(T.snap_duration(12, "480p", prefer_free=True), 8)

    def test_prefer_free_leaves_free_durations_alone(self):
        self.assertEqual(T.snap_duration(5, "480p", prefer_free=True), 5)
        self.assertEqual(T.snap_duration(8, "480p", prefer_free=True), 8)

    def test_720p_accepts_any_integer(self):
        self.assertEqual(T.snap_duration(6, "720p"), 6)


class TestTranslateCreate(unittest.TestCase):
    def test_minimal_request(self):
        plan = T.translate_create(body())
        self.assertEqual(plan["official_model"], "seedance20")
        self.assertEqual(plan["official_payload"]["prompt"], "a red balloon")
        self.assertEqual(plan["official_payload"]["resolution"], 480)
        # 裸翻译按 **web 口径**给默认值（turbo / 5s → 落在免费区）。
        # 最终口径由 billing_view 按上游渲染，见 TestBillingView。
        self.assertFalse(plan["effective"]["billed"])
        self.assertIn("web_params", plan)

    def test_effective_resolution_reflects_the_official_payload(self):
        plan = T.translate_create(body(resolution="1080p"))
        self.assertEqual(plan["requested"]["resolution"], "1080p")
        self.assertEqual(plan["effective"]["resolution"], "720p")
        # 降级提示属于官方线专属，收在 official_warnings 里，由 billing_view 按线展示
        self.assertTrue(any("downgraded" in w for w in plan["official_warnings"]))
        self.assertFalse(any("downgraded" in w for w in plan["warnings"]))

    def test_raw_translation_leaves_billing_wording_to_the_view(self):
        """翻译层只给事实，计费口径的措辞由 `billing_view` 按线渲染。

        站点对时长是"连续秒数 + 上限"，免费线 8s 落在合法区间**内部**，
        因此"吸附导致跨进计费区"这类叙事不会再从翻译层产出（那正是本次修掉的缺陷）。
        """
        plan = T.translate_create(body(resolution="480p", duration=5))
        self.assertFalse(any("free window" in w or "billed range" in w for w in plan["warnings"]))
        web_eff, _ = T.billing_view(plan, "web")
        self.assertIn("free up to 8s", web_eff["billing_note"])


class TestBillingView(unittest.TestCase):
    """两条线的计费口径**不能混** —— 这是本项目最贵的一类 bug 的防线。"""

    def plan(self, **kw):
        return T.translate_create(body(**kw))

    def test_web_reports_the_free_window(self):
        eff, _ = T.billing_view(self.plan(resolution="480p", duration=5), "web")
        self.assertFalse(eff["billed"])
        self.assertIn("free up to 8s", eff["billing_note"])
        self.assertEqual(eff["tier"], "turbo")

    def test_web_base_tier_is_billed_regardless_of_duration(self):
        plan = self.plan(resolution="480p", duration=5)
        plan["web_params"]["tier"] = "base"
        eff, _ = T.billing_view(plan, "web")
        self.assertTrue(eff["billed"])

    def test_web_long_turbo_leaves_the_free_window(self):
        eff, _ = T.billing_view(self.plan(resolution="720p", duration=10), "web")
        self.assertTrue(eff["billed"])

    def test_official_is_always_billed_even_at_5s(self):
        eff, _ = T.billing_view(self.plan(resolution="480p", duration=5), "official")
        self.assertTrue(eff["billed"], "官方线没有免费窗口，5s 照样计费")
        self.assertIn("every submit is billed", eff["billing_note"])
        self.assertNotIn("tier", eff)

    def test_official_drops_the_web_billing_narrative(self):
        """web 口径的"免费窗口 / 跨档"叙事必须整条过滤掉，不能端给官方线的调用方。

        这里直接构造 plan：该叙事已不再由翻译层产出（见上一条用例），
        但 `billing_view` 的这条过滤契约本身仍然必须成立。
        """
        plan = self.plan(resolution="480p", duration=5)
        plan["warnings"] = [
            "duration 8s snapped to 10s (site limit for 480p); "
            "this crosses into the billed range — set extra_body.aivideomaker_prefer_free=true instead",
            "a neutral warning that must survive",
        ]
        _, official_warns = T.billing_view(plan, "official")
        self.assertFalse(any("billed range" in w for w in official_warns))
        self.assertTrue(any("neutral" in w for w in official_warns))
        _, web_warns = T.billing_view(plan, "web")
        self.assertTrue(any("billed range" in w for w in web_warns))

    def test_billing_note_names_the_line(self):
        self.assertIn("official", T.billing_note("official"))
        self.assertIn("web", T.billing_note("web"))

    def test_web_keeps_1080p_even_though_the_official_payload_downgrades_it(self):
        """站点支持 1080p，官方 seedance20 只到 720 —— 别把官方线的限制报给 web 线。"""
        plan = self.plan(resolution="1080p", duration=5)
        self.assertEqual(plan["official_payload"]["resolution"], 720, "官方侧确实降了")
        eff, warns = T.billing_view(plan, "web")
        self.assertEqual(eff["resolution"], "1080p")
        self.assertFalse(
            any("downgraded" in w for w in warns), "官方线的降级提示不该出现在 web 线"
        )

    def test_official_carries_the_official_only_warnings(self):
        plan = self.plan(resolution="1080p", duration=5)
        eff, warns = T.billing_view(plan, "official")
        self.assertEqual(eff["resolution"], "720p")
        self.assertTrue(any("downgraded" in w for w in warns))

    def test_missing_model_and_content_are_rejected(self):
        with self.assertRaises(ParamError):
            T.translate_create({"content": [text()]})
        with self.assertRaises(ParamError):
            T.translate_create({"model": ARK_MODEL})
        with self.assertRaises(ParamError):
            T.translate_create({"model": ARK_MODEL, "content": []})

    def test_invalid_ratio_and_resolution_are_rejected(self):
        with self.assertRaises(ParamError):
            T.translate_create(body(ratio="5:4"))
        with self.assertRaises(ParamError):
            T.translate_create(body(resolution="4k"))

    def test_unsupported_parameters_are_listed_not_dropped(self):
        plan = T.translate_create(body(watermark=True, seed=7, tools=[{"type": "web_search"}]))
        for k in ("watermark", "seed", "tools"):
            self.assertIn(k, plan["unsupported"])

    def test_modelled_25_fields_leave_the_unsupported_list(self):
        """2.5 的 output_format / generate_audio / omni_reference_task_type 现在是**建模字段**，
        不再只是"回显但无效"。它们重新出现在 unsupported 里就说明建模被回退了。"""
        plan = T.translate_create(
            body(output_format="mov", generate_audio=True, omni_reference_task_type="reference")
        )
        for k in ("output_format", "generate_audio", "omni_reference_task_type"):
            self.assertNotIn(k, plan["unsupported"])

    def test_unsupported_detected_inside_extra_body_too(self):
        """Ark SDK 把未建模字段塞进 extra_body，真正发出时是顶层的 —— 必须同样告警。"""
        plan = T.translate_create(body(extra_body={"watermark": True, "seed": 7}))
        for k in ("watermark", "seed"):
            self.assertIn(k, plan["unsupported"])

    def test_content_roles(self):
        img = {"type": "image_url", "role": "first_frame", "image_url": {"url": "https://x/f.png"}}
        plan = T.translate_create(body(content=[text(), img], ratio="adaptive"))
        self.assertEqual(plan["official_payload"]["image"], "https://x/f.png")

    def test_reference_roles_are_collected(self):
        ref = {"type": "image_url", "role": "reference_image", "image_url": {"url": "https://x/r.png"}}
        plan = T.translate_create(body(content=[text(), ref]))
        self.assertEqual(plan["official_payload"]["referenceImages"], ["https://x/r.png"])

    def test_frame_and_reference_are_mutually_exclusive(self):
        first = {"type": "image_url", "role": "first_frame", "image_url": {"url": "https://x/f.png"}}
        ref = {"type": "image_url", "role": "reference_image", "image_url": {"url": "https://x/r.png"}}
        with self.assertRaises(ParamError):
            T.translate_create(body(content=[text(), first, ref]))

    def test_multiple_texts_are_joined(self):
        plan = T.translate_create(body(content=[text("第一段"), text("第二段")]))
        self.assertEqual(plan["official_payload"]["prompt"], "第一段\n第二段")

    def test_unknown_content_type_is_reported(self):
        plan = T.translate_create(body(content=[text(), {"type": "weird_type"}]))
        self.assertTrue(any("weird_type" in x for x in plan["unsupported"]))

    def test_frames_convert_to_seconds(self):
        b = body()
        b.pop("duration")
        b["frames"] = 120
        plan = T.translate_create(b)
        self.assertEqual(plan["requested"]["frames"], 120)
        self.assertTrue(any("24fps" in w for w in plan["warnings"]))

    def test_official_model_override(self):
        plan = T.translate_create(body(extra_body={"aivideomaker_official_model": "minimax"}))
        self.assertEqual(plan["official_model"], "minimax")
        self.assertEqual(plan["official_payload"]["content"], "a red balloon")

    def test_unknown_official_model_is_rejected_with_the_available_list(self):
        with self.assertRaises(ParamError) as ctx:
            T.translate_create(body(extra_body={"aivideomaker_official_model": "nope"}))
        self.assertIn("seedance20", str(ctx.exception))

    def test_idempotency_key_and_cap_pass_through(self):
        plan = T.translate_create(
            body(extra_body={"aivideomaker_idempotency_key": "k-12345678", "aivideomaker_max_credits": 60})
        )
        self.assertEqual(plan["idempotency_key"], "k-12345678")
        self.assertEqual(plan["max_credits"], 60)


class TestSpendGuard(unittest.TestCase):
    def test_per_request_wins(self):
        self.assertEqual(T.resolve_max_credits({"extra_body": {"aivideomaker_max_credits": 20}}, {}), 20)

    def test_env_fallback(self):
        self.assertEqual(T.resolve_max_credits({}, {"AVM_OFFICIAL_MAX_CREDITS": "30"}), 30)

    def test_per_request_beats_env(self):
        self.assertEqual(
            T.resolve_max_credits({"extra_body": {"aivideomaker_max_credits": 20}}, {"AVM_OFFICIAL_MAX_CREDITS": "30"}),
            20,
        )

    def test_no_cap_anywhere_returns_none(self):
        self.assertIsNone(T.resolve_max_credits({}, {}))

    def test_zero_is_a_valid_cap(self):
        self.assertEqual(T.resolve_max_credits({"extra_body": {"aivideomaker_max_credits": 0}}, {}), 0)

    def test_invalid_caps_rejected(self):
        with self.assertRaises(ParamError):
            T.resolve_max_credits({"extra_body": {"aivideomaker_max_credits": "abc"}}, {})
        with self.assertRaises(ParamError):
            T.resolve_max_credits({"extra_body": {"aivideomaker_max_credits": -1}}, {})


class TestNormalizeTask(unittest.TestCase):
    """字段形状对齐火山方舟《查询视频生成任务》的响应。"""

    def test_status_mapping(self):
        self.assertEqual(T.normalize_task({"status": "SUBMITTED"})["status"], "queued")
        self.assertEqual(T.normalize_task({"status": "PROGRESS"})["status"], "running")
        self.assertEqual(T.normalize_task({"status": "COMPLETED"})["status"], "succeeded")
        self.assertEqual(T.normalize_task({"status": "FAILED"})["status"], "failed")
        self.assertEqual(T.normalize_task({"status": "CANCEL"})["status"], "cancelled")

    def test_full_success_envelope(self):
        # 取自官方文档的响应示例
        t = T.normalize_task(
            {
                "id": "cgt-20260414114820-abc",
                "model": "doubao-seedance-2-0-260128",
                "status": "COMPLETED",
                "output": {"url": "https://xxx"},
                "creditsCharged": 51,
                "creditsRefunded": 0,
                "input": {"prompt": "p", "duration": 11, "ratio": "16:9", "resolution": 720},
                "createdAt": "2026-04-14T11:48:20Z",
                "completedAt": "2026-04-14T11:53:20Z",
            }
        )
        self.assertEqual(t["content"]["video_url"], "https://xxx")
        self.assertEqual(t["content"]["file_url"], None)
        self.assertEqual(t["framespersecond"], 24)
        self.assertEqual(t["duration"], 11)
        self.assertEqual(t["ratio"], "16:9")
        self.assertEqual(t["resolution"], "720p")
        self.assertEqual(t["execution_expires_after"], 172800)
        self.assertEqual(t["usage"]["credits"], 51)
        self.assertTrue(t["usage"]["paid"])
        self.assertIsInstance(t["created_at"], int)
        self.assertIsInstance(t["updated_at"], int)

    def test_full_refund_marks_unpaid(self):
        t = T.normalize_task({"status": "CANCEL", "creditsCharged": 110, "creditsRefunded": 110})
        self.assertEqual(t["usage"]["credits"], 0)
        self.assertFalse(t["usage"]["paid"])

    def test_failed_carries_the_reason(self):
        t = T.normalize_task({"status": "FAILED", "output": {"error": "content rejected"}})
        self.assertEqual(t["error"]["message"], "content rejected")

    def test_queued_has_no_video_url(self):
        self.assertIsNone(T.normalize_task({"status": "PROGRESS"})["content"]["video_url"])

    def test_unknown_status_does_not_crash(self):
        self.assertEqual(T.normalize_task({"status": "WHATEVER"})["status"], "running")

    def test_empty_input(self):
        self.assertEqual(T.normalize_task(None)["status"], "queued")


class TestOfficialClientGuard(unittest.TestCase):
    """安全带必须在发起网络请求之前生效。"""

    def test_create_without_cap_is_refused(self):
        c = OfficialClient("ak_not-a-real-key", base_url=DEAD_UPSTREAM)
        with self.assertRaises(BudgetUnsetError) as ctx:
            c.create("seedance20", {"prompt": "x", "duration": 5, "resolution": 480})
        self.assertEqual(ctx.exception.code, "BUDGET_UNSET")
        self.assertIn("AVM_OFFICIAL_MAX_CREDITS", str(ctx.exception))

    def test_refuses_to_construct_without_a_key(self):
        with self.assertRaises(ValueError):
            OfficialClient("")

    def test_network_error_is_wrapped(self):
        # trust_env=False：绕开系统代理，否则 127.0.0.1 会被代理走并拿到网关的 502
        c = OfficialClient("ak_test", base_url=DEAD_UPSTREAM, timeout=1.0, trust_env=False)
        with self.assertRaises(OfficialApiError) as ctx:
            c.account()
        self.assertEqual(ctx.exception.code, "NETWORK_ERROR")


# ============================================================ HTTP 层 =========


class TestHttpLayer(unittest.TestCase):
    """真实 ASGI 往返。上游指向死端口，dry-run 不触网。"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings())
        cls.client = TestClient(cls.app)

    def test_dry_run_translates_without_submitting(self):
        r = self.client.post(TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["dry_run"])
        self.assertEqual(j["official_model"], "seedance20")
        self.assertEqual(j["official_payload"]["resolution"], 480)
        self.assertTrue(j["effective"]["billed"])

    def test_dry_run_header_also_works(self):
        r = self.client.post(TASKS_PATH, json=body(), headers={"X-Avm-Dry-Run": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])

    def test_real_submit_without_a_cap_is_refused(self):
        r = self.client.post(TASKS_PATH, json=body())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "BudgetGuardRequired")

    def test_invalid_parameter_maps_to_400(self):
        r = self.client.post(TASKS_PATH, json=body(ratio="5:4", extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")

    def test_invalid_json_body(self):
        r = self.client.post(TASKS_PATH, content=b"{not json", headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "InvalidParameter")

    def test_unknown_task_is_404(self):
        r = self.client.get(f"{TASKS_PATH}/cgt-nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TaskNotFound")

    def test_unknown_route_is_404(self):
        self.assertEqual(self.client.get("/api/v3/nope").status_code, 404)

    def test_list_defaults(self):
        r = self.client.get(TASKS_PATH)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["items"], [])
        self.assertEqual(j["total"], 0)

    def test_healthz_is_local_by_default(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["upstream"], "official")
        self.assertEqual(j["gate"], "open")
        self.assertIn("seedance20", j["supported_models"])
        self.assertNotIn("balance", j)  # 默认不深探上游

    def test_request_id_is_echoed(self):
        r = self.client.get("/healthz", headers={"x-request-id": "rid-123"})
        self.assertEqual(r.headers.get("x-request-id"), "rid-123")

    def test_query_payload_reports_the_declared_request(self):
        plan = self.client.post(TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True})).json()
        self.assertEqual(plan["requested"]["model"], ARK_MODEL)


class TestHttpGate(unittest.TestCase):
    """设了 AVM_GATE_KEY 时，未授权请求必须被挡在翻译之前。"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(gate_key="sk-secret"))
        cls.client = TestClient(cls.app)

    def test_missing_token_is_401(self):
        r = self.client.post(TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AuthenticationError")

    def test_wrong_token_is_401(self):
        r = self.client.post(
            TASKS_PATH,
            json=body(extra_body={"aivideomaker_dry_run": True}),
            headers={"Authorization": "Bearer nope"},
        )
        self.assertEqual(r.status_code, 401)

    def test_correct_token_passes_the_gate(self):
        r = self.client.post(
            TASKS_PATH,
            json=body(extra_body={"aivideomaker_dry_run": True}),
            headers={"Authorization": "Bearer sk-secret"},
        )
        # 通过闸门后应到达翻译层并返回 dry-run 结果，而不是 401/404
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])

    def test_healthz_stays_open(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)


class TestPassthroughMode(unittest.TestCase):
    """AVM_PASSTHROUGH_KEY=1：调用方的 Bearer token 直接当上游 key。"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(upstream_key="", passthrough_key=True))
        cls.client = TestClient(cls.app)

    def test_requires_a_bearer_token_for_submission(self):
        r = self.client.post(TASKS_PATH, json=body())
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AuthenticationError")

    def test_dry_run_works_without_upstream_credentials(self):
        r = self.client.post(
            TASKS_PATH,
            json=body(extra_body={"aivideomaker_dry_run": True}),
            headers={"Authorization": "Bearer ak_caller"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])


class TestConfig(unittest.TestCase):
    def test_settings_from_env(self):
        s = Settings.from_env(
            {
                "AVM_KEY": " ak_x ",
                "AVM_GATE_KEY": "g",
                "AVM_OFFICIAL_MAX_CREDITS": "42",
                "AVM_OFFICIAL_MODEL": "t2v",
                "AVM_DISABLE_LOGFIRE": "1",
            }
        )
        self.assertEqual(s.upstream_key, "ak_x")
        self.assertEqual(s.gate_key, "g")
        self.assertEqual(s.max_credits, 42)
        self.assertEqual(s.default_model, "t2v")
        self.assertFalse(s.enable_logfire)
        self.assertEqual(s.translate_env(), {"AVM_OFFICIAL_MODEL": "t2v", "AVM_OFFICIAL_MAX_CREDITS": "42"})

    def test_validate_requires_a_key(self):
        with self.assertRaises(ValueError):
            Settings(upstream_key="", passthrough_key=False).validate()
        Settings(upstream_key="", passthrough_key=True).validate()  # 不应抛

    def test_bad_max_credits_is_rejected(self):
        with self.assertRaises(ValueError):
            Settings.from_env({"AVM_OFFICIAL_MAX_CREDITS": "abc"})


class TestObservability(unittest.TestCase):
    """logfire 装配必须可失败降级 —— 追踪装不上不能拖倒服务。"""

    def test_disabled_logfire_still_serves(self):
        app = create_app(settings(enable_logfire=False))
        c = TestClient(app)
        self.assertEqual(c.get("/healthz").status_code, 200)
        self.assertFalse(c.get("/healthz").json()["logfire"])

    def test_logfire_path_is_exercised_without_sending_data(self):
        # send_to_logfire=False：装配真实走一遍，但绝不外发
        app = create_app(settings(enable_logfire=True, logfire_send=False))
        c = TestClient(app)
        r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["logfire"])
        # 装配好了之后，dry-run 路径上的 span 也必须照常工作
        r2 = c.post(TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}))
        self.assertEqual(r2.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
