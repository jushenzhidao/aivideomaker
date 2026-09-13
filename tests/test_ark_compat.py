#!/usr/bin/env python3
"""ark_compat 的测试（纯翻译层 + HTTP 层）。

两条纪律：
  1. **零额度消耗**：所有提交路径都走 dry-run；本文件**不会**产生任何真实生成请求。
  2. **零外发**：上游 base_url 指向一个没人监听的本地端口（死端口），任何"手滑
     发出真实请求"都会立刻连接失败；Logfire 用 `send_to_logfire=False`。

运行：python3 tests/test_ark_compat.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat import translate as T  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.errors import ParamError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402

ARK_MODEL = "doubao-seedance-2-5-260628"
DEAD_UPSTREAM = "http://127.0.0.1:9"  # discard 端口，保证不出网


def settings(**kw) -> Settings:
    base = dict(
        cookie="auth_session=deadbeef",
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
        self.assertEqual(plan["web_params"]["content"], "a red balloon")
        self.assertEqual(plan["web_params"]["resolution"], "480p")
        # 裸翻译按 web 口径给默认值（turbo / 5s → 落在免费区）。
        # 最终口径由 billing_view 统一渲染，见 TestBillingView。
        self.assertFalse(plan["effective"]["billed"])
        self.assertIn("web_params", plan)
        # 官方线的产物不该再出现在 plan 里
        for gone in ("official_model", "official_payload", "official_warnings", "max_credits"):
            self.assertNotIn(gone, plan, f"{gone} 属于已移除的 official 线")

    def test_effective_resolution_is_the_requested_one(self):
        plan = T.translate_create(body(resolution="1080p"))
        self.assertEqual(plan["requested"]["resolution"], "1080p")
        # 站点支持 1080p，翻译层不该擅自降级（实际产出的档位以任务记录的
        # kelingKeyId 为准，见 test_web_upstream.py 的 normalize 用例）
        self.assertEqual(plan["effective"]["resolution"], "1080p")

    def test_raw_translation_leaves_billing_wording_to_the_view(self):
        """翻译层只给事实，计费口径的措辞由 `billing_view` 渲染。"""
        plan = T.translate_create(body(resolution="480p", duration=5))
        self.assertFalse(any("free window" in w or "billed range" in w for w in plan["warnings"]))
        eff, _ = T.billing_view(plan)
        self.assertIn("free up to 8s", eff["billing_note"])


class TestBillingView(unittest.TestCase):
    """计费口径只有一个判据：`tier=base` 一律计费、`turbo` 只在 ≤8s 免费。"""

    def plan(self, **kw):
        return T.translate_create(body(**kw))

    def test_free_window_is_reported(self):
        eff, _ = T.billing_view(self.plan(resolution="480p", duration=5))
        self.assertFalse(eff["billed"])
        self.assertIn("free up to 8s", eff["billing_note"])
        self.assertEqual(eff["tier"], "turbo")

    def test_base_tier_is_billed_regardless_of_duration(self):
        plan = self.plan(resolution="480p", duration=5)
        plan["web_params"]["tier"] = "base"
        eff, _ = T.billing_view(plan)
        self.assertTrue(eff["billed"])

    def test_long_turbo_leaves_the_free_window(self):
        eff, _ = T.billing_view(self.plan(resolution="720p", duration=10))
        self.assertTrue(eff["billed"])

    def test_eight_seconds_is_still_free(self):
        eff, _ = T.billing_view(self.plan(resolution="720p", duration=8))
        self.assertFalse(eff["billed"])

    def test_nine_seconds_crosses_the_line(self):
        eff, _ = T.billing_view(self.plan(resolution="720p", duration=9))
        self.assertTrue(eff["billed"])

    def test_billing_note_names_the_line(self):
        self.assertIn("web", T.billing_note())

    def test_1080p_survives_the_round_trip(self):
        """站点支持 1080p —— 没有任何上游限制该把它降级。"""
        eff, warns = T.billing_view(self.plan(resolution="1080p", duration=5))
        self.assertEqual(eff["resolution"], "1080p")
        self.assertFalse(any("downgraded" in w for w in warns))

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
        self.assertEqual(plan["web_params"]["imageUrl"], "https://x/f.png")

    def test_reference_roles_are_collected(self):
        ref = {"type": "image_url", "role": "reference_image", "image_url": {"url": "https://x/r.png"}}
        plan = T.translate_create(body(content=[text(), ref]))
        self.assertEqual(plan["web_params"]["referenceImageUrls"], ["https://x/r.png"])

    def test_frame_and_reference_are_mutually_exclusive(self):
        first = {"type": "image_url", "role": "first_frame", "image_url": {"url": "https://x/f.png"}}
        ref = {"type": "image_url", "role": "reference_image", "image_url": {"url": "https://x/r.png"}}
        with self.assertRaises(ParamError):
            T.translate_create(body(content=[text(), first, ref]))

    def test_multiple_texts_are_joined(self):
        plan = T.translate_create(body(content=[text("第一段"), text("第二段")]))
        self.assertEqual(plan["web_params"]["content"], "第一段\n第二段")

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
        self.assertEqual(j["web_params"]["content"], "a red balloon")
        self.assertEqual(j["effective"]["resolution"], "480p")
        self.assertFalse(j["effective"]["billed"])  # 480p/5s/turbo 在免费窗口内

    def test_dry_run_header_also_works(self):
        r = self.client.post(TASKS_PATH, json=body(), headers={"X-Avm-Dry-Run": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])

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
        self.assertEqual(j["upstream"], "web")
        self.assertEqual(j["available_upstreams"], ["web"])
        self.assertEqual(j["gate"], "open")
        self.assertNotIn("balance", j)  # 默认不深探上游
        # 已移除的官方线不该留下任何字段
        for gone in ("switch_via", "max_credits", "default_model", "passthrough_key", "supported_models"):
            self.assertNotIn(gone, j, f"{gone} 属于已移除的 official 线")

    def test_supports_cancel_is_reported_honestly(self):
        """站点没有取消端点 —— 健康检查必须如实说 false。"""
        self.assertFalse(self.client.get("/healthz").json()["supports_cancel"]["web"])

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


class TestConfig(unittest.TestCase):
    def test_settings_from_env(self):
        s = Settings.from_env(
            {
                "AVM_COOKIE": "auth_session=x",
                "AVM_GATE_KEY": "g",
                "AVM_DISABLE_LOGFIRE": "1",
            }
        )
        self.assertEqual(s.cookie, "auth_session=x")
        self.assertEqual(s.gate_key, "g")
        self.assertFalse(s.enable_logfire)

    def test_validate_requires_a_cookie(self):
        with self.assertRaises(ValueError):
            Settings(cookie="").validate()
        Settings(cookie="auth_session=x").validate()  # 不应抛

    def test_legacy_upstream_official_is_rejected(self):
        # 旧配置写着已移除的 official 线时必须报错，而不是静默按 web 跑
        with self.assertRaises(ValueError) as ctx:
            Settings(cookie="auth_session=x", legacy_upstream="official").validate()
        self.assertIn("official", str(ctx.exception))

    def test_legacy_upstream_web_is_tolerated(self):
        Settings(cookie="auth_session=x", legacy_upstream="web").validate()


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
