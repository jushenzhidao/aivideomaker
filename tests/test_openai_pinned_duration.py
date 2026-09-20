#!/usr/bin/env python3
"""`/v1/videos` 的**写死时长**（分辨率 → 时长，无条件覆盖调用方传的 `seconds`）。

`openai_videos.PINNED_DURATIONS` 等价于"把本端点钉死在免费区"：480p 一律 10 秒、
720p 一律 8 秒 —— 两条都是站点**已实测 `paid=false`** 的组合（判据是任务记录的
`paid` 字段，不是页面文案）。

只断言"时长被改写"是不够的：把时长钉到一个**仍在计费区**的秒数上，改写看着完全正确，
钱照花。所以这里两条一起钉 —— ① 改写确实发生且留痕；② 最终 `billed=false`。

另一条最容易漏的口径：**model 名不带分辨率后缀时它同样是 720p**（下游按
`DEFAULT_RESOLUTION` 兜底）。它也必须被钉住，否则同一个 720p 请求会因
"model 写没写后缀"分成免费与计费两种结果，而调用方从报文里看不出差别。

运行：python3 tests/test_openai_pinned_duration.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.channel_options import parse_channel_options  # noqa: E402
from ark_compat.openai_videos import PINNED_DURATIONS, ark_body_from_openai  # noqa: E402
from ark_compat.translate import DEFAULT_RESOLUTION, billing_view, translate_create  # noqa: E402

#: 站点已实测免费的两个组合。表本身即口径 ⇒ 改它必须是有意识的决定（见第一条用例）。
FREE_COMBINATIONS = {"480p": 10, "720p": 8}

# model 名要能过模型解析：`minimaxH3_480p` 会先剥后缀再查槽位表；这条兜底让所有
# 槽位名都可用（真实渠道用精确表或唯一 `*` 兜底，见 channel_options 的判定顺序）。
_CHANNEL = '{"model_map": {"*": "minimaxH3"}}'


def _plan(fields):
    body, notes = ark_body_from_openai(fields)
    plan = translate_create(body, channel_options=parse_channel_options(_CHANNEL))
    eff, _ = billing_view(plan)
    return body, notes, eff


class TestPinnedDuration(unittest.TestCase):
    def test_the_table_is_the_two_known_free_combinations(self):
        self.assertEqual(PINNED_DURATIONS, FREE_COMBINATIONS)

    def test_pinning_overrides_an_explicit_seconds_value(self):
        for res, want in FREE_COMBINATIONS.items():
            for given in (5, 9, 15, 20):
                with self.subTest(res=res, given=given):
                    body, notes, _ = _plan({"model": f"minimaxH3_{res}", "prompt": "p", "seconds": given})
                    self.assertEqual(body["duration"], want)
                    self.assertTrue(
                        any("overridden" in n for n in notes),
                        f"{res} 传 {given}s 被改写成 {want}s，却没有任何说明：{notes}",
                    )

    def test_omitted_seconds_is_filled_from_the_table(self):
        body, notes, _ = _plan({"model": "minimaxH3_480p", "prompt": "p"})
        self.assertEqual(body["duration"], 10)
        self.assertTrue(any("omitted" in n for n in notes), notes)

    def test_only_a_real_rewrite_is_announced(self):
        """传的值与表值一致时不该有告警 —— 否则每次请求都带一条，告警会被读成噪声。"""
        body, notes, _ = _plan({"model": "minimaxH3_720p", "prompt": "p", "seconds": 8})
        self.assertEqual(body["duration"], 8)
        self.assertEqual(notes, [])

    def test_a_model_without_a_resolution_suffix_is_pinned_too(self):
        body, _, eff = _plan({"model": "minimaxH3", "prompt": "p", "seconds": 15})
        self.assertNotIn("resolution", body)
        self.assertEqual(eff["resolution"], DEFAULT_RESOLUTION)
        self.assertEqual(body["duration"], PINNED_DURATIONS[DEFAULT_RESOLUTION])

    def test_1080p_is_downgraded_into_the_free_window(self):
        """★ 2026-09-20：`1080p` **不再**"按原值透传给调用方"——它先被降级成 720p，再钉到 8s。

        旧口径（"表外分辨率原值透传、该请求仍可能计费"）与"本面只跑免费线"直接矛盾：
        站点对 1080p **没有**实测免费线（文案口径 4 积分/秒）⇒ 只要放行 15s 的 1080p，
        这个面就会真花钱。降级方向是"往免费档收"，且两步都留痕（降级一条 + 覆盖一条）。
        """
        body, notes, eff = _plan({"model": "minimaxH3_1080p", "prompt": "p", "seconds": 15})
        self.assertEqual(body["resolution"], "720p", "1080p 必须降到免费档内的分辨率")
        self.assertEqual(body["duration"], 8, "降级后按 720p 钉到免费区最长档")
        self.assertFalse(eff["billed"], "降级 + 钉死之后仍落在计费区 —— 那这条政策就没生效")
        self.assertTrue(any("downgraded" in n for n in notes), f"降级没留痕：{notes}")
        self.assertTrue(any("overridden" in n for n in notes), f"时长改写没留痕：{notes}")
        # 两条 note 缺一不可：只留"降级"会让"15s 被改成 8s"变成静默改档（反向同理）
        self.assertEqual(len(notes), 2, f"降级与时长改写各该有一条说明：{notes}")

    def test_pinned_combinations_are_actually_free(self):
        """钉住的**目的**是不计费：只测时长会漏掉"钉了但仍在计费区"。"""
        for res in FREE_COMBINATIONS:
            for given in (None, 20):
                fields = {"model": f"minimaxH3_{res}", "prompt": "p"}
                if given is not None:
                    fields["seconds"] = given
                with self.subTest(res=res, given=given):
                    _, _, eff = _plan(fields)
                    self.assertFalse(
                        eff["billed"],
                        f"{res} 被钉到 {eff['duration']}s，计费结论却是 billed=true",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
