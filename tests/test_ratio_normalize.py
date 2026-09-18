#!/usr/bin/env python3
"""`ratio` / `size` 的归一化、就近吸附与兜底（2026-09-18）。

背景：上游只认站点 UI `Aspect Ratio` 选择器里的六档 —— `21:9 / 16:9 / 4:3 / 1:1 /
3:4 / 9:16`（截图取证，顺序即 UI 顺序）。调用方却会按 OpenAI / 图像 API 的习惯写成
`WxH`（`1024x1792`）或别家的比例串（`5:4`、`2.35:1`）。**这些都不该让整请求失败** ——
那会让"要个竖屏"这类完全合理的请求直接 400；也不能静默改（改了不说 = 本层最贵的缺陷）。
本文件锁定第三条路：**就近吸附 + 必留痕 + 最后兜底 16:9**。

🔴 关键边界：**兜底只属于 `/v1/videos`**。本服务的 Ark 线（`/tasks`）沿用既有对外契约，
认不出仍然 400（TESTCASES §A3 第 4 条）—— 两条线共用一个 `normalize_ratio`，差别只在
`fallback` 参数上，本文件的 `TestTwoLinesDifferOnlyByFallback` 专门锁这一点。

四条边界，一条都不能退：
  1. 枚举原样值**零 warning**（`notes == []`）—— 否则健康请求会被噪声淹没；
  2. 凡是改写（约分 / 吸附 / 兜底）**必进 warnings**，`requested.ratio` 回显调用方原值；
  3. Ark 线认不出的值**继续 400**（`5:4`、`abc`）—— 放宽没有漫过这条线；
  4. `adaptive` / `keep_ratio` **不被归一化吃掉**：它是"跟随输入图"，不是"比例"。

运行：python3 tests/test_ratio_normalize.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import translate as T  # noqa: E402
from ark_compat.errors import ParamError  # noqa: E402
from ark_compat.openai_videos import _SIZE_FALLBACK, ark_body_from_openai  # noqa: E402

ARK_MODEL = "minimaxH3"

#: 站点 UI 的六档（截图取证）—— 也是 `RATIO_ORDER`，本文件多处用它做对照。
SITE_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")


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
        for v in SITE_RATIOS + ("adaptive",):
            with self.subTest(v=v):
                self.assertEqual(T.normalize_ratio(v), (v, []))

    def test_snapping_target_set_is_exactly_the_site_ui_six(self):
        """六档真源只有一个：`RATIO_ORDER` 必须与站点 UI 选择器逐项一致。

        一旦有人往这里加第七档（或改顺序），`nearest_ratio` 的落点范围就跟着变 ——
        锁死它，是为了让"就近"永远落在站点真能出的比例上。
        """
        self.assertEqual(T.RATIO_ORDER, SITE_RATIOS)
        self.assertEqual(T.ARK_RATIOS, frozenset(SITE_RATIOS) | {"adaptive"})

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
        """换个尺寸，百分比必须跟着变 —— 防"写死一个百分数"糊过去。

        用 `2160x1080`（2:1 → 16:9，12.5%）：必须仍在 snap 上限内，否则这条就变成
        在测 400 而不是在测偏差计算（见 `TestSnapLimit`）。
        """
        ratio, notes = T.normalize_ratio("2160x1080")
        self.assertEqual(ratio, "16:9")
        self.assertIn("12.5%", notes[0], "偏差应以**落点**为分母：比 16:9 宽 12.5%")

    def test_snapping_picks_the_nearest_not_the_first(self):
        for raw, want in [
            ("1440x900", "16:9"),   # 8:5 = 1.6：离 16:9 近，离 4:3 远
            ("2160x1080", "16:9"),  # 2:1：比起 4:3，更接近 16:9
            ("1024x1792", "9:16"),  # 4:7：比起 3:4，更接近 9:16
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


class TestSnapIsNeverRejected(unittest.TestCase):
    """🔴 吸附**永远不拒绝** —— 偏得再远也是"留有痕地吸附"（用户 2026-09-18 口径：
    能提交成功就行）。

    曾经按"偏差 > 15% 就 400"实现过一轮，被该口径推翻。本类就是防它复辟：任何
    "因为比例偏得远而拒绝"的实现，下面第一条立刻会红。
    """

    def test_far_off_ratios_are_still_snapped(self):
        for raw, want in [("1170x2532", "9:16"), ("1284x2778", "9:16"),
                          ("3000x1000", "21:9"), ("100x300", "9:16")]:
            with self.subTest(raw=raw):
                self.assertEqual(T.normalize_ratio(raw)[0], want)

    def test_warning_gets_louder_past_the_soft_threshold(self):
        """软阈值只是**措辞升级**，不是拒绝 —— 且必须给出能直接用的替代写法。"""
        _, notes = T.normalize_ratio("3000x1000")
        self.assertIn("28.6%", notes[0])
        self.assertIn("noticeably different", notes[0])
        self.assertIn(T.RATIO_EXAMPLES["21:9"], notes[0])

    def test_below_the_threshold_keeps_the_milder_wording(self):
        _, notes = T.normalize_ratio("1024x1792")
        self.assertNotIn("noticeably different", notes[0])
        self.assertIn("1.6%", notes[0])

    def test_openai_line_is_equally_permissive(self):
        from ark_compat.openai_videos import size_to_ratio
        self.assertEqual(size_to_ratio("3000x1000")[0], "21:9")
        self.assertEqual(size_to_ratio("1024x1792")[0], "9:16")


class TestProportionParsing(unittest.TestCase):
    """`W:H` 比例串的识别边界 —— 认什么、不认什么，都要能被证伪。"""

    def test_numeric_proportions_are_read(self):
        for raw, want in [("5:4", 1.25), ("9:21", 3 / 7), ("2.35:1", 2.35), (" 16 : 9 ", 16 / 9)]:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(T.parse_proportion(raw), want, places=6)

    def test_non_proportions_are_not_read(self):
        """认不出的形态必须返回 None（而不是抛/给个瞎猜的值）。"""
        for raw in ("abc", "16", "16:9:1", "16/9", "0x100", "", None):
            with self.subTest(raw=raw):
                self.assertIsNone(T.parse_proportion(raw))

    def test_zero_and_negative_sides_are_rejected(self):
        """`0:9` / `16:0` 不是比例：放行会让 `inf` 的最近邻恒为 `21:9`（把任意值吸成一档）。"""
        for raw in ("0:9", "16:0", "0:0"):
            with self.subTest(raw=raw):
                self.assertIsNone(T.parse_proportion(raw))


class TestArkLineStillRejectsUnknown(unittest.TestCase):
    """兜底**不等于**什么都收：**Ark 线**（`fallback=None`）认不出的形态继续 400。"""

    def test_unknown_shapes_raise(self):
        """兜底不等于什么都收 —— 连形态都认不出的，仍然 400。"""
        for v in ("abc", "16/9", "0x100", "16", "16:9:1"):
            with self.subTest(v=v):
                with self.assertRaises(ParamError):
                    T.normalize_ratio(v)

    def test_proportion_strings_are_snapped_on_both_lines(self):
        """`W:H` 比例串与 `WxH` **同等对待**（用户口径："等比 或者按比例 传都可以"）。

        ⚠️ 这里**曾经**断言 Ark 线必须拒绝 `5:4`（依据是 TESTCASES §A3 记的既有契约），
        已被上述口径推翻 —— 那是本层自己加的门槛，不是上游的约束；调用方写哪种形态
        不该由我们挑，两种都收、都吸附、都留痕。
        """
        for v, want in [("5:4", "4:3"), ("9:21", "9:16"), ("2.35:1", "21:9")]:
            with self.subTest(v=v):
                ratio, notes = T.normalize_ratio(v)
                self.assertEqual(ratio, want)
                self.assertEqual(len(notes), 1, "吸附必须留痕")

    def test_translate_create_accepts_a_proportion_string(self):
        """端到端：`ratio: "5:4"` 现在能被提交出去（吸到 4:3），不再是 400。"""
        plan = T.translate_create(body(ratio="5:4"))
        self.assertEqual(plan["web_params"]["aspectRatio"], "4:3")
        self.assertTrue(any("5:4" in w and "4:3" in w for w in plan["warnings"]))


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
        """`adaptive` 的特殊语义不能被归一化吃掉：它**不设** aspectRatio。

        站点 UI 的六档是"显式指定比例"时的选项；`adaptive` 是"跟随输入图"这条另一维度的
        语义，两者不冲突 —— 砍掉它会打断首帧 / 尾帧 / omni-edit 三条路径。
        """
        plan = T.translate_create(body(ratio="adaptive"))
        self.assertNotIn("aspectRatio", plan["web_params"])
        self.assertEqual(plan["warnings"], [])

    def test_plain_enum_stays_silent(self):
        plan = T.translate_create(body(ratio="9:16"))
        self.assertEqual(plan["web_params"]["aspectRatio"], "9:16")
        self.assertEqual(plan["warnings"], [])


class TestOpenAIVideosSize(unittest.TestCase):
    """`/v1/videos` 的 `size` 走**同一份**归一化（两处各写一份必然漂移）。"""

    def test_site_ui_six_pass_through_silently(self):
        for v in SITE_RATIOS:
            with self.subTest(v=v):
                b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": v})
                self.assertEqual(b["ratio"], v)
                self.assertEqual(notes, [])

    def test_size_wxh_snaps_too(self):
        # 1080p 载具：把 size 映射与「写死时长」隔离（否则 notes 里会多一条时长说明）
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "1024x1792"})
        self.assertEqual(b["ratio"], "9:16")
        self.assertTrue(any("1024x1792" in n and "9:16" in n for n in notes))

    def test_proportion_string_is_snapped_not_rejected(self):
        """`5:4` 在**这条线**上就近落到 `4:3` 并留痕 —— 与 Ark 线的 400 形成对照。"""
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "5:4"})
        self.assertEqual(b["ratio"], "4:3")
        self.assertTrue(any("5:4" in n and "4:3" in n and "differs by" in n for n in notes),
                        f"就近吸附必须留痕并给出偏差：{notes}")


class TestOpenAIVideosFallback(unittest.TestCase):
    """兜底：**认不出**的 `size` 落到 16:9，而不是让整请求 400。

    这是 `/v1/videos` 与 Ark 线的分界，也是本次改动的核心。兜底**必须留痕** ——
    否则就从"降级"退化成了本层最贵的缺陷（静默改写）。
    """

    def test_unreadable_size_falls_back_to_16_9(self):
        for v in ("abc", "16/9", "0x100", "16", "16:9:1", "16:9:1:2"):
            with self.subTest(v=v):
                b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": v})
                self.assertEqual(b["ratio"], "16:9", f"{v} 应兜底到 16:9")
                self.assertTrue(any(v in n and "16:9" in n for n in notes),
                                f"{v} 的兜底没有留痕：{notes}")

    def test_fallback_choice_matches_the_site_default(self):
        """兜底档位与站点 UI 默认选中项一致（都是 16:9）—— 换掉它必须先改这里。"""
        self.assertEqual(T.RATIO_FALLBACK, "16:9")
        self.assertEqual(_SIZE_FALLBACK, T.RATIO_FALLBACK)

    def test_fallback_is_never_silent(self):
        """兜底一定要有 note —— 这条是防"有人把留痕那句删了让它闭嘴"。"""
        _, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "abc"})
        self.assertEqual(len(notes), 1)
        self.assertIn("fell back", notes[0])

    def test_blank_size_is_still_absent_not_fallback(self):
        """省略 `size` ≠ 认不出：前者是"没指定"，仍交给上游默认，**不**报兜底。"""
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p"})
        self.assertNotIn("ratio", b)
        self.assertEqual(notes, [])

    def test_keep_ratio_still_means_derive_from_image(self):
        """别名不能被兜底吃掉：`keep_ratio` → `adaptive`（跟随输入图），不是 16:9。"""
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "keep_ratio"})
        self.assertEqual(b["ratio"], "adaptive")
        self.assertTrue(any("keep_ratio" in n and "adaptive" in n for n in notes))

    def test_adaptive_is_never_rewritten_on_either_line(self):
        """`adaptive` 在两条线上都必须是它自己 —— 兜底只针对**认不出**的值。

        ⚠️ 大小写**不**认（`Adaptive` 会走到兜底）：别名表是小写精确匹配，而兜底会
        把它变成 16:9。这条断言把该行为显式钉住，免得日后有人当成 bug 顺手改掉。
        """
        b, notes = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "adaptive"})
        self.assertEqual(b["ratio"], "adaptive")
        self.assertEqual(notes, [])

        b2, _ = ark_body_from_openai({"model": "minimaxH3_1080p", "prompt": "p", "size": "Adaptive"})
        self.assertEqual(b2["ratio"], "16:9", "非精确匹配的写法落兜底，不静默当 adaptive")


class TestTwoLinesDifferOnlyByFallback(unittest.TestCase):
    """两条线共用一份归一化，**唯一**的分歧点是 `fallback` —— 逐值对照锁定它。"""

    CASES = [
        # (size, Ark 线期望, /v1/videos 线期望)
        ("16:9", "16:9", "16:9"),          # 枚举：两边相同
        ("1024x1792", "9:16", "9:16"),     # WxH 吸附：两边相同（既有行为）
        ("5:4", "4:3", "4:3"),             # 比例串：自 2026-09-18 起两边都就近（不再 400）
        ("abc", ParamError, "16:9"),       # 认不出：Ark 线 400，videos 线兜底
        ("16", ParamError, "16:9"),
    ]

    def test_matrix(self):
        for raw, ark_want, videos_want in self.CASES:
            with self.subTest(raw=raw):
                if ark_want is ParamError:
                    with self.assertRaises(ParamError):
                        T.normalize_ratio(raw)
                else:
                    self.assertEqual(T.normalize_ratio(raw)[0], ark_want)
                b, _ = ark_body_from_openai(
                    {"model": "minimaxH3_1080p", "prompt": "p", "size": raw}
                )
                self.assertEqual(b["ratio"], videos_want)


if __name__ == "__main__":
    unittest.main()
