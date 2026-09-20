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
        """`{"*": ...}` 兜底：调用方写任何表外名字都落到那一档（也是旧行为的迁移路）。"""
        r = self.post(ark_body("doubao-seedance-2-5-260628"), '{"model_map": {"*": "wan27"}}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"])

    def test_non_catchall_wildcard_is_refused_before_any_upstream_call(self):
        """**非 `*` 的通配模式已被拆掉**（2026-09-17 降级）⇒ 渠道配置错误，一个上游请求都不发。

        这条钉的是"校验发生在**配置层**而不是请求命中层"：多模式机制拆掉之后，
        "多命中怎么排"这一整类问题在**解析配置时**就不可能出现。
        """
        r = self.post(ark_body("doubao-x"), '{"model_map": {"doubao-*": "wan27"}}')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(
            r.json()["error"]["param"], CHANNEL_OPTIONS_HEADER,
            "这是渠道配置错误，不能指向调用方的 model",
        )
        self.assertEqual(self.procedures_hit(), [], "拒绝必须发生在上游调用之前")

    def test_removed_pin_key_is_refused_not_ignored(self):
        """`model` 钉住键**已撤除**（2026-09-17）⇒ 响亮失败，不静默忽略、不发上游请求。

        静默忽略是更坏的选择：那个键的语义是"本渠道只服务某个槽位"，运维据此以为
        "调用方传错名字会被拦住" —— 一旦它不再生效却仍被接受，那层保护就无声消失了。
        """
        r = self.post(ark_body("wan27"), '{"model": "wan27"}')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["param"], CHANNEL_OPTIONS_HEADER)
        self.assertIn("removed", r.json()["error"]["message"], "报文要说清这是被撤除的键")
        self.assertEqual(self.procedures_hit(), [], "拒绝必须发生在上游调用之前")

    def test_unknown_model_is_refused_before_any_upstream_call(self):
        """表外名字且没有兜底 ⇒ 400，且**一个上游请求都不发**（带上凭据的请求不浪费）。"""
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
        """撤除后的 `model` 键：**任何**取值都被拒（连"与解析结果一致"也不例外）。

        与上一版不同 —— 那时它只在**冲突**时报错、一致时放行；撤除后它整体不再有语义，
        所以"一致"也不再是免死牌（否则运维会以为它还在生效）。
        """
        for value in ("wan27", "minimaxH3"):
            with self.subTest(pin=value):
                r = self.post(ark_body(value), f'{{"model": "{value}"}}')
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

    def test_catchall_overrides_even_a_slot_name(self):
        """**兜底覆盖一切**（2026-09-17 用户裁定）：连调用方写出的真槽位名也改写。

        理由：兜底＝渠道声明"除我明确列出的以外，一律落这一档"；不覆盖的话，运维
        "这个渠道只跑某一档"的意图就会落空 —— 而 `model` 钉住键已撤除，
        兜底是**唯一**的强制手段。代价由证据兜住：改写进 `warnings`，
        证据字段标 `model_source=model_map`，**不静默**。
        """
        j = self.dry_run(ark_body("minimaxH3"), '{"model_map": {"*": "seedance25"}}')
        self.assertEqual(j["effective"]["model"], "seedance25", "兜底没覆盖槽位名？那是规则变了")
        self.assertEqual(j["effective"]["model_source"], "model_map")
        self.assertTrue(
            any("fallback" in w for w in j["warnings"]), "改写必须留痕（不静默换模型）"
        )

    def test_resolution_is_warned_not_silent(self):
        """命中映射/钉住都要有告警 —— 静默改模型的账单差异是本项目最贵的一类缺陷。"""
        j = self.dry_run(ark_body("doubao-seedance-2-0-260128"), '{"model_map": {"*": "minimaxH3"}}')
        self.assertTrue(any("model_map" in w for w in j["warnings"]), j["warnings"])


class TestOpenAiFaceCarriesTheSameSemantics(unittest.TestCase):
    """两条入口共用**同一套机制**（同一个解析函数、同一份翻译、同一个 procedure）。

    ⚠️ 但**策略不同**（2026-09-20）：方舟线按判定序走（映射表 → 透传 → 400），
    `/v1/videos` 则强制落免费档（`translate_create(force_slot=…)`）。机制同源、策略不同 ——
    本类钉的是"OpenAI 面确实走到同一条管线上"（procedure 恒为 `ai.minimaxH3`）。
    """

    def setUp(self):
        self.site = FakeSite()
        self.site.set("ai.minimaxH3", "t-h3")
        c = make_client(self.site)
        app = create_app(web_settings())
        # 映射/接线类门禁用**同步推进**（submit_inline，见 _schedule_videos_submit 的说明）
        app.state.submit_inline = True
        app.state.upstreams = {
            "web": WebUpstream(c, WebSubmitQueue(c, max_concurrent=1, poll_interval=0.01))
        }
        self.app = TestClient(app)

    def test_the_suffix_still_drives_resolution_and_the_slot_is_the_free_one(self):
        """`minimaxH3_480p`：后缀决定**分辨率**，而槽位由本面的免费档策略固定。

        ⚠️ 旧版断言的是"后缀不参与槽位比对"—— 那条规则现在只对**方舟线**还有意义
        （本面上槽位恒为 `FREE_ONLY_SLOT`，后缀唯一还起作用的地方就是分辨率）。
        """
        r = self.app.post(
            OPENAI_VIDEOS_PATH, json={"model": "minimaxH3_480p", "prompt": "a cat", "seconds": 10}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            sorted({q.url.path for q in self.site.requests if q.method == "POST"}),
            ["/api/ai.minimaxH3"],
            "本面上槽位恒为免费档（后缀只决定分辨率）",
        )

    def test_unknown_model_on_the_openai_face_lands_on_the_free_slot(self):
        """🔴 本面**不再因模型名不认识而 400**（2026-09-20 用户口径：「不走付费模型，
        全部用免费的兜底」）：名字照收下，槽位一律落免费档。

        旧行为（未命中 ⇒ 400）已作废 —— 本面的调用方是 OpenAI SDK 用户，他们能拿到的信息
        只有"请求失败了"。⚠️ 方舟线**不变**，不认识的名字仍然 400（见
        `TestModelResolutionIsWired` 里那条"拒绝必须发生在上游调用之前"）。
        """
        r = self.app.post(OPENAI_VIDEOS_PATH, json={"model": "sora-2", "prompt": "a cat"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            sorted({q.url.path for q in self.site.requests if q.method == "POST"}),
            ["/api/ai.minimaxH3"],
            "不认识的模型名也必须落到免费槽位（既不该拒，更不该照名字发出去）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
