#!/usr/bin/env python3
"""`ratio` / `size` 的归一化与就近吸附（2026-09-18）。

背景：调用方按 OpenAI / 图像 API 的习惯把比例写成 `WxH`（`1024x1792`），而上游只认
有限几档（`16:9` … `21:9` / `adaptive`）。**约不进枚举的值不能整请求 400** —— 那会让
"要个竖屏"这类完全合理的请求直接失败；也不能静默改（改了不说 = 本层最贵的缺陷）。
本文件锁定的就是第三条路：**就近吸附 + 必留痕**。

三条边界，一条都不能退：
  1. 认不出的字符串（`5:4`、`abc`）**仍然 400** —— 兜底不是"什么都收"；
  2. 枚举原样值**零 warning**（`notes == []`）—— 否则健康请求会被噪声淹没；
  3. 凡是改写（约分 / 吸附）**必进 warnings**，且 `requested.ratio` 回显调用方原值。

运行：python3 tests/test_ratio_normalize.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import translate as T  # noqa: E402
from ark_compat.errors import ParamError  # noqa: E402
from ark_compat.openai_videos import ark_body_from_openai  # noqa: E402

ARK_MODEL = "minimaxH3"


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


class TestEnumPassthrough(unittest.TestCase):
    """合法枚举值必须**原样**通过，且**不带**任何 warning。"""

    def test_every_enum_value_is_returned_verbatim_and_silent(self):
        for v in ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"):
            with self.subTest(v=v):
                self.assertEqual(T.normalize_ratio(v), (v, []))

    def test_blank_means_absent(self):
        self.assertEqual(T.normalize_ratio(None), ("", []))
        self.assertEqual(T.normalize_ratio(""), ("", []))
        self.assertEqual(T.normalize_ratio("   "), ("", []))


class TestWxHNormalization(unittest.TestCase):
    """`WxH` 约分后**正好在**枚举里 ⇒ 直接用，但仍要留痕（比例被改写过）。"""

    def test_exact_reductions(self):
        for raw, want in [
            ("1920x1080", "16:9"),
            ("1080x1920", "9:16"),
            ("1024x1024", "1:1"),
            ("768x1024", "3:4"),
            ("1024x768", "4:3"),
            ("16x9", "16:9"),   # 只有一位数的 WxH 也要认（旧正则要求 2-5 位，认不出）
            ("16X9", "16:9"),   # 大写
            ("16×9", "16:9"),   # 全角乘号
        ]:
            with self.subTest(raw=raw):
                ratio, notes = T.normalize_ratio(raw)
                self.assertEqual(ratio, want)
                self.assertEqual(len(notes), 1, "改写必须留痕")
                self.assertIn(raw, notes[0])
                self.assertIn(want, notes[0])


class TestWxHSnapping(unittest.TestCase):
    """约不进枚举 ⇒ **就近吸附**并留痕。这是本次要兜住的那条路径。"""

    def test_1024x1792_snaps_to_9_16(self):
        """报错现场：`ParamError: ratio: invalid enum value "1024x1792"`（4:7 竖屏）。"""
        ratio, notes = T.normalize_ratio("1024x1792")
        self.assertEqual(ratio, "9:16")
        self.assertEqual(len(notes), 1)
        self.assertIn("1024x1792", notes[0])
        self.assertIn("4:7", notes[0], "要说清约出来的比例，调用方才知道差多少")
        self.assertIn("9:16", notes[0])

    def test_deviation_is_reported_so_the_caller_can_judge(self):
        """吸附是**有损**的 ⇒ warning 必须写明偏了多少，调用方才知道能不能接受。"""
        _, notes = T.normalize_ratio("1024x1792")
        self.assertIn("differs by", notes[0])
        self.assertIn("1.6%", notes[0])  # 4:7 vs 9:16

    def test_deviation_is_computed_not_hardcoded(self):
        """换个差得多的尺寸，百分比必须跟着变 —— 防"写死一个百分数"糊过去。"""
        _, notes = T.normalize_ratio("1170x2532")  # iPhone 竖屏，约 195:422
        self.assertEqual(T.normalize_ratio("1170x2532")[0], "9:16")
        self.assertIn("17.9%", notes[0], "偏差应以**落点**为分母：比 9:16 窄 17.9%")

    def test_snapping_picks_the_nearest_not_the_first(self):
        for raw, want in [
            ("1440x900", "16:9"),   # 8:5 = 1.6：离 16:9 近，离 4:3 远
            ("3000x1000", "21:9"),  # 3:1 = 3.0：超宽，只能落到最宽那档
            ("100x300", "9:16"),    # 1:3：比 9:16 还窄，落到最窄那档
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(T.normalize_ratio(raw)[0], want)

    def test_extreme_ratios_land_on_the_edge_buckets(self):
        """超宽 / 超窄没有对应档位 ⇒ 落到最宽 / 最窄那档，而不是 400。"""
        self.assertEqual(T.nearest_ratio(8 / 3), "21:9")   # 2.67，比 21:9 (2.33) 还宽
        self.assertEqual(T.nearest_ratio(3 / 8), "9:16")   # 0.375，比 9:16 (0.5625) 还窄

    def test_distance_is_relative_not_absolute(self):
        """吸附用**相对**距离（对数差），不是绝对差 —— 这条断言专门用来证伪后者。

        `1.16` 落在 `1:1` 与 `4:3` 之间：绝对差会判给 `1:1`（0.160 < 0.173），
        相对距离判给 `4:3`（0.139 < 0.148）。竖屏侧同理取 `0.87`（互为倒数区间）。
        ⚠️ 这两条是**刻意挑在两种度量分歧区间内**的值：换成绝对差，本测试立刻变红。
        """
        self.assertEqual(T.nearest_ratio(1.16), "4:3")   # 绝对差 ⇒ 1:1
        self.assertEqual(T.nearest_ratio(0.87), "1:1")   # 绝对差 ⇒ 3:4


class TestUnknownValuesStillRejected(unittest.TestCase):
    """兜底**不等于**什么都收：认不出的形态必须继续 400。"""

    def test_unknown_shapes_raise(self):
        for v in ("5:4", "abc", "16/9", "0x100", "16", "16:9:1"):
            with self.subTest(v=v):
                with self.assertRaises(ParamError):
                    T.normalize_ratio(v)

    def test_translate_create_still_400s_on_5_4(self):
        """既有契约（TESTCASES §A3 第 4 条）：`ratio: "5:4"` → 400。"""
        with self.assertRaises(ParamError):
            T.translate_create(body(ratio="5:4"))


class TestTranslateCreateWiring(unittest.TestCase):
    """归一化必须真接进 `translate_create`，而不只是有个孤立函数。"""

    def test_wxh_ratio_reaches_the_upstream_aspect_ratio(self):
        plan = T.translate_create(body(ratio="1024x1792"))
        self.assertEqual(plan["web_params"]["aspectRatio"], "9:16")
        self.assertTrue(
            any("1024x1792" in w and "9:16" in w for w in plan["warnings"]),
            f"改写没进 warnings：{plan['warnings']}",
        )

    def test_requested_echoes_what_the_caller_wrote(self):
        """`requested` 回显**原值**，归一化结果在 `effective` —— 两处并列才看得出被改过。"""
        plan = T.translate_create(body(ratio="1024x1792"))
        self.assertEqual(plan["requested"]["ratio"], "1024x1792")
        self.assertEqual(plan["effective"]["aspectRatio"], "9:16")

    def test_adaptive_still_means_derive_from_image(self):
        """`adaptive` 的特殊语义不能被归一化吃掉：它**不设** aspectRatio。"""
        plan = T.translate_create(body(ratio="adaptive"))
        self.assertNotIn("aspectRatio", plan["web_params"])
        self.assertEqual(plan["warnings"], [])

    def test_plain_enum_stays_silent(self):
        plan = T.translate_create(body(ratio="9:16"))
        self.assertEqual(plan["web_params"]["aspectRatio"], "9:16")
        self.assertEqual(plan["warnings"], [])


class TestOpenAIVideosSize(unittest.TestCase):
    """`/v1/videos` 的 `size` 走**同一份**归一化（两处各写一份必然漂移）。"""

    def test_size_wxh_snaps_too(self):
        # 1080p 载具：把 size 映射与「写死时长」隔离（否则 notes 里会多一条时长说明）
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "1024x1792"})
        self.assertEqual(b["ratio"], "9:16")
        self.assertTrue(any("1024x1792" in n and "9:16" in n for n in notes))

    def test_unknown_size_still_falls_through_to_translate(self):
        """既有契约：认不出的 size **原样透传**，由 translate 的枚举校验给 400。"""
        b, _ = ark_body_from_openai({"model": "minimaxH3", "prompt": "p", "size": "5:4"})
        self.assertEqual(b["ratio"], "5:4")
        with self.assertRaises(ParamError):
            T.translate_create(b)


if __name__ == "__main__":
    unittest.main()
