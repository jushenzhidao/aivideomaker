#!/usr/bin/env python3
"""门禁：`X-Channel-Options` 的模型解析**真的接到了上游请求上**（不是只在纯函数里对）。

## 为什么单独一条

纯函数解析（槽位/来源/告警）由 `tests/test_channel_model_map_wildcard.py` 覆盖；
本文件只钉**接线**这一层 —— 项目自己的教训：**「实现了」不等于「接线了」**。
三个断点各有一个真实失败模式：

| 断点 | 不接线时的症状 | 本文件怎么抓 |
| --- | --- | --- |
| app 读头 → `translate_create` | 头被静默忽略，永远走透传 | 带头的请求必须改变上游 URL |
| `translate_create` → `web_params.procedure` | 解析对了，但 procedure 仍是常量 | 断言 `web_params["procedure"]` |
| `web_params.procedure` → `WebClient.create` | 请求仍打到 `ai.minimaxH3` | **用真 WebClient + 站点替身**看 URL 路径 |

⚠️ 第三条**必须**用真 `WebClient`（`FakeWebClient` 整个把客户端换掉，
解析结果根本到不了"发请求"这一步 —— 用它会得到假绿）。
站点替身是 `FakeSite`（`httpx.MockTransport`）：**零网络、零计费、不创建任何真实任务**。

运行：python3 tests/test_channel_model_wiring.py
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.channel_options import CHANNEL_OPTIONS_HEADER  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402
from test_web_upstream import FakeSite, make_client, web_settings  # noqa: E402

OPENAI_VIDEOS_PATH = "/v1/videos"


def ark_body(model: str = "minimaxH3", **kw) -> dict:
    body = {
        "model": model,
        "content": [{"type": "text", "text": "a cat"}],
        "ratio": "16:9",
        "resolution": "480p",
        "duration": 5,
    }
    body.update(kw)
    return body


class TestModelResolutionIsWired(unittest.TestCase):
    """**真客户端 + 站点替身**：解析结论必须出现在真正发出去的请求里。"""

    def setUp(self):
        self.site = FakeSite()
        # 这两条 procedure 都预先应答，好让"打到了哪条"成为唯一变量
        self.site.set("ai.minimaxH3", "t-h3")
        self.site.set("ai.wan27", "t-wan27")
        self.client = make_client(self.site)
        app = create_app(web_settings())
        app.state.upstreams = {
            "web": WebUpstream(
                self.client,
                WebSubmitQueue(self.client, max_concurrent=2, poll_interval=0.01),
            )
        }
        self.app = TestClient(app)

    def post(self, body: dict, header: str | None = None):
        headers = {CHANNEL_OPTIONS_HEADER: header} if header is not None else {}
        return self.app.post(TASKS_PATH, json=body, headers=headers)

    def procedures_hit(self) -> list[str]:
        return sorted({r.url.path for r in self.site.requests if r.method == "POST"})

    def test_passthrough_is_the_default_and_reaches_the_site(self):
        """不配任何键：名字本身是槽位 ⇒ 逐字透传，请求打到 `ai.<名字>`。"""
        r = self.post(ark_body("wan27"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"])

    def test_channel_map_retargets_the_procedure(self):
        """★ 核心：映射命中必须**换掉上游 procedure**（解析对了但没接线就会红在这）。"""
        r = self.post(
            ark_body("doubao-seedance-2-0-260128"),
            '{"model_map": {"doubao-seedance-2-0-260128": "wan27"}}',
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"])
        self.assertEqual(self.site.calls("ai.minimaxH3"), [], "落回了默认 procedure ⇒ 接线断了")

    def test_wildcard_catch_all_retargets_the_procedure(self):
        """`{"*": ...}` 兜底：调用方写任何名字都落到钉住的槽位（这也是旧行为的迁移路）。"""
        r = self.post(ark_body("doubao-seedance-2-5-260628"), '{"model_map": {"*": "wan27"}}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"])

    def test_pinned_slot_is_used_when_nothing_matches(self):
        """渠道钉住 + 调用方写了个表外的名字 ⇒ 落钉住值。"""
        r = self.post(ark_body("doubao-seedance-2-5-260628"), '{"model": "wan27"}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"])

    def test_unknown_model_is_refused_before_any_upstream_call(self):
        """未命中且没有钉住 ⇒ 400，且**一个上游请求都不发**（带上凭据的请求不浪费）。"""
        r = self.post(ark_body("doubao-seedance-2-5-260628"))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["param"], "model", "调用方的错，param 必须指向 model")
        self.assertIn("model_map", r.json()["error"]["message"], "报文要给出下一步怎么改")
        self.assertEqual(self.procedures_hit(), [], "拒绝必须发生在上游调用之前")

    def test_broken_header_is_refused_and_attributed_to_the_channel(self):
        """坏掉的头**不许**当成"没配"（那样会静默按透传跑掉，而运维以为映射生效了）。"""
        for bad in ("{not json", '["a"]'):
            with self.subTest(header=bad):
                r = self.post(ark_body("wan27"), bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(
                    r.json()["error"]["param"], CHANNEL_OPTIONS_HEADER,
                    "渠道配置错误要指向头名，不能指向调用方的 model",
                )
                self.assertEqual(self.procedures_hit(), [])

    def test_pin_conflict_is_a_channel_configuration_error(self):
        """渠道钉 `wan27` 而请求明确指名 `minimaxH3` ⇒ 报错，**不静默改模型**。"""
        r = self.post(ark_body("minimaxH3"), '{"model": "wan27"}')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["param"], CHANNEL_OPTIONS_HEADER)
        self.assertEqual(self.procedures_hit(), [])


class TestResolutionEvidence(unittest.TestCase):
    """证据字段：dry-run 与内部视图必须能回答"请求了什么 vs 实际跑什么"。"""

    def setUp(self):
        self.site = FakeSite()
        self.site.set("ai.minimaxH3", "t-h3")
        c = make_client(self.site)
        app = create_app(web_settings())
        app.state.upstreams = {
            "web": WebUpstream(c, WebSubmitQueue(c, max_concurrent=1, poll_interval=0.01))
        }
        self.app = TestClient(app)

    def dry_run(self, body: dict, header: str | None = None) -> dict:
        headers = {CHANNEL_OPTIONS_HEADER: header} if header is not None else {}
        r = self.app.post(
            TASKS_PATH, json={**body, "extra_body": {"aivideomaker_dry_run": True}}, headers=headers
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_dry_run_exposes_slot_source_and_verified(self):
        j = self.dry_run(ark_body("minimaxH3"))
        self.assertEqual(j["requested"]["model"], "minimaxH3", "请求值**不被改写**")
        self.assertEqual(j["effective"]["model"], "minimaxH3")
        self.assertEqual(j["effective"]["model_source"], "passthrough")
        self.assertIs(j["effective"]["model_verified"], True, "已实测槽位")
        self.assertEqual(j["web_params"]["procedure"], "ai.minimaxH3")

    def test_unverified_slot_is_allowed_but_announced(self):
        """站点有、procedure 未实测的槽位：**允许**（否则映射表没法配）但必须留痕。"""
        j = self.dry_run(ark_body("doubao-seedance-2-5-260628"), '{"model_map": {"*": "seedance25"}}')
        self.assertEqual(j["effective"]["model"], "seedance25")
        self.assertIs(j["effective"]["model_verified"], False)
        self.assertEqual(j["web_params"]["procedure"], "ai.seedance25", "按命名形态推断，非实测")
        self.assertTrue(
            any("no verified procedure" in w for w in j["warnings"]),
            "未验证槽位不告警 = 允许但不静默",
        )

    def test_known_slot_name_is_not_overridden_by_a_wildcard(self):
        """调用方**指名**了一个真槽位时，`"*"` 兜底不许改写它（② 优先于 ③，刻意）。

        代价是：想"这个渠道一律跑某一档"不能靠 `"*"` 覆盖槽位名，要用 `model` 钉住
        （钉住与指名冲突会报渠道配置错误 —— 见 `TestModelResolutionIsWired`）。
        """
        j = self.dry_run(ark_body("minimaxH3"), '{"model_map": {"*": "seedance25"}}')
        self.assertEqual(j["effective"]["model"], "minimaxH3")
        self.assertEqual(j["effective"]["model_source"], "passthrough")

    def test_resolution_is_warned_not_silent(self):
        """命中映射/钉住都要有告警 —— 静默改模型的账单差异是本项目最贵的一类缺陷。"""
        j = self.dry_run(ark_body("doubao-seedance-2-0-260128"), '{"model_map": {"*": "minimaxH3"}}')
        self.assertTrue(any("model_map" in w for w in j["warnings"]), j["warnings"])


class TestOpenAiFaceCarriesTheSameSemantics(unittest.TestCase):
    """两条入口（方舟面 / OpenAI 面）必须走**同一套**模型语义与同一个 procedure。"""

    def setUp(self):
        self.site = FakeSite()
        self.site.set("ai.minimaxH3", "t-h3")
        c = make_client(self.site)
        app = create_app(web_settings())
        app.state.upstreams = {
            "web": WebUpstream(c, WebSubmitQueue(c, max_concurrent=1, poll_interval=0.01))
        }
        self.app = TestClient(app)

    def test_suffix_is_stripped_before_matching_and_the_procedure_follows(self):
        """`minimaxH3_1080p`：后缀决定分辨率、**不参与槽位比对**，否则合法请求会被拒。"""
        r = self.app.post(
            OPENAI_VIDEOS_PATH, json={"model": "minimaxH3_1080p", "prompt": "a cat"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            sorted({q.url.path for q in self.site.requests if q.method == "POST"}),
            ["/api/ai.minimaxH3"],
        )

    def test_unknown_model_on_the_openai_face_is_refused_too(self):
        r = self.app.post(OPENAI_VIDEOS_PATH, json={"model": "sora-2", "prompt": "a cat"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
