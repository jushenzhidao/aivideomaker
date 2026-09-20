#!/usr/bin/env python3
"""`/v1/videos` **只跑免费线**：调用方写什么模型名都不参与选路（2026-09-20 用户口径）。

用户原话：「`/v1/videos` 不走付费模型 全部用免费的兜底」，随后对 1080p 补了「8s 720p」。
落成两条互不替代的约束：

| # | 约束 | 唯一实现点 |
|---|---|---|
| 1 | 上游槽位**恒为** `FREE_ONLY_SLOT`（免费线），未认识的名字**不再 400**、渠道映射也不生效 | `translate_create(force_slot=…)`（app 层按面传入）|
| 2 | 分辨率落在免费档内：`_1080p` 降级为 720p，时长随后按表钉死 | `ark_body_from_openai` |

为什么单开门禁：这两条都是**钱**的约束，而它们的失效方式极其安静 —— 请求照常 200、
成片照常出，只是账单在涨（站点侧 1080p 按秒计价、其余槽位计费口径未知）。所以每条断言都
瞄准"政策没生效"那一侧，并且都能被变异证伪：

  ① `force_slot` 没传进 `resolve_model`      ⇒ `test_a_known_paid_slot...` 红（照名字发出去）
  ② `force_slot` 判定被放到映射表之后        ⇒ `test_a_channel_map...` 红（渠道映射把面级策略覆盖掉）
  ③ 1080p 降级被删                          ⇒ `test_1080p_...` 与 `test_every_request_is_predicted_free` 红
  ④ 顺手把方舟线也强制了                    ⇒ `test_the_ark_line_is_untouched` 红
  ⑤ 免费槽位被改成未实测的槽位               ⇒ `test_the_free_slot_is_the_verified_one` 红
  ⑥ 证据字段被改写（丢调用方原值）           ⇒ `test_evidence_keeps_the_requested_name` 红

纪律：零外发（`FakeSite` 替身，不创建任何真实任务）；方舟线的行为**刻意不在本文件里放宽**。

运行：python3 -m unittest tests.test_videos_free_only
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.channel_options import CHANNEL_OPTIONS_HEADER, VERIFIED_SLOTS, procedure_for_slot  # noqa: E402
from ark_compat.openai_videos import FREE_ONLY_SLOT, OPENAI_VIDEOS_PATH  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402
from test_web_upstream import FakeSite, ark_body, make_client, web_settings  # noqa: E402

#: 站点上另外两个**已知槽位名**（`SITE_MODEL_KEYS` 里就有）。它们在本面的旧行为是
#: **透传**到各自的 procedure —— 正是"会走付费模型"的那条路，所以必须被兜底掉。
PAID_ISH_SLOTS = ("seedance20", "wan27")


class FreeOnlyCase(unittest.TestCase):
    """真 app + 站点替身（记录 realmente 打到哪个 procedure）。"""

    def setUp(self):
        self.site = FakeSite()
        self.site.set("ai.minimaxH3", "t-free")
        for slot in PAID_ISH_SLOTS:
            self.site.set(procedure_for_slot(slot), f"t-{slot}")
        self.site.set("ai.seedance25", "t-mapped")
        # 🔴 必须给一个**终态**的任务记录：`WebSubmitQueue` 的盯梢线程要盯到终态才放槽位，
        #    而本类多个用例会连续建好几条任务（max_concurrent 只有 1~2）⇒ 不给终态的话
        #    第二条就卡在信号量上，测试表现为**挂死**（沙箱 137）而不是失败。
        self.site.set(
            "model.getModel",
            {
                "id": "t-free",
                "taskStatus": "succeed",
                "aiModel": "minimax-h3",
                "url": "https://cdn.test/a.mp4",
                "kelingKeyId": "480",
                "credits": 0,
                "paid": False,
            },
        )
        c = make_client(self.site)
        app = create_app(web_settings())
        app.state.upstreams = {
            "web": WebUpstream(c, WebSubmitQueue(c, max_concurrent=2, poll_interval=0.01))
        }
        self.app = app
        self.client = TestClient(app)

    # ---- helpers ----
    def procedures_hit(self) -> list:
        """本次实际发出的**创建** procedure（排序去重）—— 政策只在"真发出去的那一条"上证伪。"""
        return sorted({q.url.path for q in self.site.requests if q.method == "POST"})

    def post_videos(self, **fields) -> str:
        fields.setdefault("prompt", "a cat")
        r = self.client.post(OPENAI_VIDEOS_PATH, json=fields)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def post_ark(self, model: str, header: str | None = None) -> object:
        headers = {CHANNEL_OPTIONS_HEADER: header} if header else {}
        return self.client.post(TASKS_PATH, json=ark_body(model=model), headers=headers)

    def entry(self, tid: str) -> dict:
        rec = self.app.state.tasks.get(tid)
        self.assertIsNotNone(rec, f"任务记录不见了：{tid}")
        return rec


class TestSlotIsAlwaysTheFreeOne(FreeOnlyCase):
    def test_the_free_slot_is_the_verified_one(self):
        """🔴 免费档不是"随便挑一个槽位"：必须是**站点实测存在 procedure** 的那一条。

        否则"兜底"会把所有请求发到一个按命名形态推断出来的 procedure 上（对未实测槽位，
        上游是 NOT_FOUND 还是**照常计费**都还不知道）。
        """
        self.assertIn(FREE_ONLY_SLOT, VERIFIED_SLOTS, "免费槽位必须是已实测槽位")
        self.assertEqual(procedure_for_slot(FREE_ONLY_SLOT), "ai.minimaxH3")

    def test_an_unknown_name_lands_on_the_free_slot(self):
        """不认识的模型名：**不再 400**（旧行为），落到免费槽位。"""
        self.post_videos(model="sora-2")
        self.assertEqual(self.procedures_hit(), ["/api/ai.minimaxH3"])

    def test_a_known_paid_slot_lands_on_the_free_slot_too(self):
        """★ 核心：**认识的**槽位名（旧行为是透传到它自己的 procedure）同样被兜底。"""
        for slot in PAID_ISH_SLOTS:
            with self.subTest(model=slot):
                self.site.requests.clear()
                self.post_videos(model=slot)
                self.assertEqual(
                    self.procedures_hit(), ["/api/ai.minimaxH3"],
                    f"`{slot}` 照名字发出去了 —— 那正是「走付费模型」这条路",
                )

    def test_a_channel_map_cannot_override_the_face_policy(self):
        """渠道 `model_map` 指向付费槽位也没用：**面级策略优先于渠道映射**，且覆盖要留痕。"""
        header = '{"model_map": {"*": "seedance25"}}'
        r = self.client.post(
            OPENAI_VIDEOS_PATH,
            json={"model": "minimaxH3", "prompt": "a cat"},
            headers={CHANNEL_OPTIONS_HEADER: header},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.minimaxH3"], "渠道映射把面级策略覆盖了")
        warns = self.entry(r.json()["id"])["warnings"]
        self.assertTrue(
            any("only runs one upstream slot" in w for w in warns),
            f"覆盖没有留痕（不静默换模型是这条政策的前提）：{warns}",
        )

    def test_evidence_keeps_the_requested_name(self):
        """改写槽位**不许**抹掉"调用方写了什么"—— 出问题时全靠这一条复盘。"""
        tid = self.post_videos(model="sora-2")
        eff = self.entry(tid)
        self.assertEqual(eff["requested"]["model"], "sora-2", "调用方原值必须留在 requested 里")
        self.assertEqual(eff["effective"]["model"], FREE_ONLY_SLOT)
        self.assertEqual(eff["effective"]["model_source"], "free_only", "来源要说清是面级策略改的")
        self.assertIs(eff["effective"]["model_verified"], True)
        self.assertEqual(
            self.entry(tid)["effective"].get("tier", "turbo"), "turbo", "免费档只走 turbo"
        )


class TestResolutionStaysInsideTheFreeTier(FreeOnlyCase):
    def test_1080p_is_downgraded_and_pinned(self):
        """★ 1080p → 720p + 钉 8s（站点对 1080p **没有**实测免费线）。"""
        tid = self.post_videos(model="minimaxH3_1080p", seconds=20)
        eff = self.entry(tid)["effective"]
        self.assertEqual(eff["resolution"], "720p", "1080p 没被降级")
        self.assertEqual(eff["duration"], 8, "降级后必须按 720p 钉死在免费区最长档")

    def test_every_request_is_predicted_free(self):
        """🔴 这条政策的**目的**：本面上任何请求的计费预测都是"免费"。

        只测"落到了免费槽位"会漏掉另一半 —— 同一个免费模型在 1080p / 超长时长下**照样扣钱**
        （站点按秒计价）。所以要按 `effective.billed` 断言，而不是按槽位名断言。
        """
        cases = [
            ("minimaxH3_480p", None), ("minimaxH3_480p", 20), ("minimaxH3_720p", 20),
            ("minimaxH3_1080p", 20), ("minimaxH3", 20), ("sora-2", 20),
        ]
        for model, seconds in cases:
            with self.subTest(model=model, seconds=seconds):
                self.site.requests.clear()
                fields = {"model": model, "prompt": "a cat"}
                if seconds is not None:
                    fields["seconds"] = seconds
                r = self.client.post(OPENAI_VIDEOS_PATH, json=fields)
                self.assertEqual(r.status_code, 200, r.text)
                eff = self.entry(r.json()["id"])["effective"]
                self.assertFalse(
                    eff["billed"],
                    f"{model} / {seconds}s 被预测为计费（res={eff['resolution']} "
                    f"dur={eff['duration']}）—— 本面承诺只跑免费档",
                )


class TestTheArkLineIsUntouched(FreeOnlyCase):
    """对照：方舟线**不套用**这条政策（它的调用方可以自己点档、也自己承担费用）。

    没有这一组，把 `force_slot` 写成"无条件传"也能让上面全绿 —— 那是把一条线的政策
    悄悄扩散到另一条线，属于契约变更而不是修 bug。
    """

    def test_a_known_slot_still_passes_through_on_the_ark_line(self):
        self.site.requests.clear()
        r = self.post_ark("seedance20")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.seedance20"], "方舟线的透传被改掉了")

    def test_an_unknown_name_is_still_refused_on_the_ark_line(self):
        self.site.requests.clear()
        r = self.post_ark("sora-2")
        self.assertEqual(r.status_code, 400, "方舟线不认识的名字必须仍然 400")
        self.assertEqual(r.json()["error"]["param"], "model")
        self.assertEqual(self.procedures_hit(), [], "拒绝必须发生在上游调用之前")

    def test_a_channel_map_still_works_on_the_ark_line(self):
        self.site.requests.clear()
        r = self.post_ark("doubao-x", header='{"model_map": {"*": "wan27"}}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.procedures_hit(), ["/api/ai.wan27"], "方舟线的映射被面级策略污染了")


if __name__ == "__main__":
    unittest.main()
