#!/usr/bin/env python3
"""`ratio` 映射层的通用性门禁（2026-09-18）。

这一层存在的理由就是"以后形态会变多"，所以本文件锁的是**可替换性**本身，而不是
某个具体取值：

  1. **档位表可换** —— 换一个 `RatioSpec`，吸附落点必须跟着变。若哪天有人把档位
     写回成全局常量，本文件立刻红。
  2. **输入形态可插** —— `presets` 表加一项就多认一种写法，不给表就不改变行为。
  3. **像素只认实测** —— 没实测过的组合返回 None，**绝不按比例推算**。

运行：python3 tests/test_ratio_resolve.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import ratio as R  # noqa: E402
from ark_compat.translate import SITE_UI_SPEC  # noqa: E402


class TestSpecIsSwappable(unittest.TestCase):
    """档位表必须真的能换 —— 这是"多上游/多模型"的立足点。"""

    def test_a_narrower_spec_snaps_differently(self):
        """换成只有三档的 spec，同一个输入必须落到**不同**的档位。

        这条是"档位表不是全局常量"的硬证据：若吸附写死读某个模块级清单，
        换 spec 不会改变结果 ⇒ 本测试红。
        """
        wide = R.SITE_UI_SPEC
        narrow = R.RatioSpec(order=("16:9", "1:1", "9:16"), fallback="1:1", name="narrow")
        # 1.25 在六档里最近 4:3；三档里最近 1:1
        self.assertEqual(wide.snap(1.25), "4:3")
        self.assertEqual(narrow.snap(1.25), "1:1")

    def test_fallback_travels_with_the_spec(self):
        """兜底档位是 spec 的一部分 —— 换 spec 就换兜底，不是另一处常量。"""
        other = R.RatioSpec(order=("1:1",), fallback="1:1", name="square-only")
        self.assertEqual(other.fallback, "1:1")
        # ⚠️ 故意用**非** `16:9` 的目标值：若 `with_fallback` 把兜底写死成 16:9
        #    （站点默认），用 16:9 做断言会"巧合通过" —— 这条正是防那种假绿。
        self.assertEqual(other.with_fallback("9:16").fallback, "9:16")
        # with_fallback 只改兜底，其余不变
        self.assertEqual(other.with_fallback("9:16").order, ("1:1",))

    def test_extra_values_are_not_snapping_targets(self):
        """`extra`（adaptive 一类非比例枚举）不参与吸附比较。"""
        spec = R.RatioSpec(order=("16:9",), extra=("adaptive",))
        self.assertIn("adaptive", spec.all_values)
        self.assertNotIn("adaptive", spec.order)
        self.assertEqual(spec.snap(0.3), "16:9")  # 只有一档可落


class TestResolveModes(unittest.TestCase):
    """resolver 链的每个出口都要能被单独验证（mode 是契约的一部分）。"""

    def test_absent_is_its_own_mode(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                got = R.resolve(raw)
                self.assertEqual(got.mode, "absent")
                self.assertEqual(got.ratio, "")

    def test_exact_modes_are_silent(self):
        for raw in ("16:9", "21:9", "adaptive"):
            with self.subTest(raw=raw):
                got = R.resolve(raw)
                self.assertEqual(got.mode, "exact")
                self.assertTrue(got.silent, "精确命中不该产生噪声")

    def test_dimension_reduces_to_the_exact_bucket(self):
        got = R.resolve("1920x1080")
        self.assertEqual((got.ratio, got.mode), ("16:9", "dimension"))
        self.assertFalse(got.silent)

    def test_snapped_reports_the_drift(self):
        got = R.resolve("1024x1792")
        self.assertEqual((got.ratio, got.mode), ("9:16", "snapped"))
        self.assertIn("differs by", got.note)
        self.assertIn("4:7", got.note)

    def test_proportion_is_accepted_by_default(self):
        """`W:H` 两线都认（用户口径"等比 或者按比例 传都可以"）。"""
        got = R.resolve("5:4")
        self.assertEqual((got.ratio, got.mode), ("4:3", "proportion"))

    def test_fallback_only_when_not_strict(self):
        strict = R.ResolveOptions(strict=True)
        loose = R.ResolveOptions(strict=False)
        with self.assertRaises(R.UnknownRatioError):
            R.resolve("banana", options=strict)
        got = R.resolve("banana", options=loose)
        self.assertEqual((got.ratio, got.mode), ("16:9", "fallback"))
        self.assertIn("fell back", got.note)


class TestPresetsArePluggable(unittest.TestCase):
    """加一种语义写法 = 往表里加一项，不动判定逻辑。"""

    def test_preset_works_once_supplied(self):
        opt = R.ResolveOptions(presets={"portrait": "9:16", "square": "1:1"})
        self.assertEqual(R.resolve("portrait", options=opt).ratio, "9:16")
        self.assertEqual(R.resolve("square", options=opt).mode, "preset")

    def test_empty_preset_table_changes_nothing(self):
        """不给表 ⇒ `portrait` 仍走兜底/拒绝 —— 新能力默认关闭，不悄悄生效。"""
        opt = R.ResolveOptions(strict=False, presets={})
        self.assertEqual(R.resolve("portrait", options=opt).mode, "fallback")

    def test_aliases_are_also_a_table(self):
        opt = R.ResolveOptions(aliases={"keep_ratio": "adaptive"})
        got = R.resolve("keep_ratio", options=opt)
        self.assertEqual((got.ratio, got.mode), ("adaptive", "alias"))


class TestPixelsAreMeasuredOnly(unittest.TestCase):
    """像素表只认实测 —— 这条专门防"按比例推算补表"。"""

    def test_measured_entries_are_returned(self):
        for (res, ratio_), (w, h) in [
            (("480p", "16:9"), (864, 480)),
            (("480p", "4:3"), (640, 480)),
            (("480p", "3:4"), (480, 640)),
            (("480p", "21:9"), (1120, 480)),
            (("720p", "16:9"), (1248, 704)),
            (("720p", "4:3"), (928, 704)),
            (("720p", "1:1"), (704, 704)),
            (("720p", "3:4"), (704, 928)),
            (("720p", "9:16"), (704, 1248)),
            (("1080p", "16:9"), (1904, 1080)),
            (("1080p", "1:1"), (1080, 1080)),
            (("1080p", "9:16"), (1080, 1904)),
        ]:
            with self.subTest(res=res, ratio=ratio_):
                self.assertEqual(R.pixels_for(ratio_, res), (w, h))

    def test_the_720p_4_3_entry_proves_extrapolation_would_be_wrong(self):
        """本表存在的理由：480p 的 4:3 是 `640x480`，按比例放大算 `960x720`，
        而站点实测是 **`928x704`** —— 短边对齐后取整，推算必错一档。

        ⚠️ 这条同时是"不要顺手加推算逻辑"的哨兵：哪天有人把推算写进去，
        实测值就会变成推算值，本断言立刻红。
        """
        w480, h480 = R.pixels_for("4:3", "480p")
        measured = R.pixels_for("4:3", "720p")
        naive = (round(w480 * 720 / h480 / 2) * 2, 720)   # 按比例 + 偶数对齐
        self.assertEqual(measured, (928, 704))
        self.assertNotEqual(measured, naive, "实测值不该等于推算值")

    def test_the_1080p_16_9_entry_is_also_not_the_exact_ratio(self):
        """第二个反例：16:9 @1080p 按精确比例是 `1920x1080`，实测 `1904x1080`
        （边长对齐到 16 的倍数）。连"同名比例"都不能照抄标准分辨率。
        """
        self.assertEqual(R.pixels_for("16:9", "1080p"), (1904, 1080))
        self.assertNotEqual(R.pixels_for("16:9", "1080p"), (1920, 1080))

    def test_unmeasured_is_none_and_not_extrapolated(self):
        """720p 各档**没实测** ⇒ 必须返回 None。

        ⚠️ 这条是防"顺手按比例算一下"：480p 的 16:9 是 864x480，按比例推算 720p
        会给出 1280x720 —— 看着合理，但站点常取整/对齐，推算会**稳定地错一档**。
        宁可不给，也不给错的。
        """
        # 用**当前确实没测过**的组合（480p 的 1:1 / 9:16）。已实测的会随补测增加，
        # 断言别挑那些会变的 —— 本测试要钉的是"未测 ⇒ None"这条规则本身。
        self.assertIsNone(R.pixels_for("1:1", "480p"))
        self.assertIsNone(R.pixels_for("9:16", "480p"))

    def test_table_holds_no_placeholder_values(self):
        """表里不允许出现 0 / 负数这类占位值 —— 有就是"编造"。"""
        for (res, ratio_), (w, h) in R.MEASURED_PIXELS.items():
            with self.subTest(res=res, ratio=ratio_):
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)


class TestRatioOfPixels(unittest.TestCase):
    """像素 → 档位（反方向）。"""

    def test_exact_dimensions_keep_their_bucket(self):
        for (w, h), want in [
            ((1920, 1080), "16:9"),
            ((1080, 1920), "9:16"),
            ((1024, 1024), "1:1"),
            ((768, 1024), "3:4"),
        ]:
            with self.subTest(w=w, h=h):
                self.assertEqual(R.ratio_of(w, h), want)

    def test_off_dimensions_snap(self):
        self.assertEqual(R.ratio_of(1024, 1792), "9:16")   # 4:7 → 最近 9:16
        self.assertEqual(R.ratio_of(3000, 1000), "21:9")   # 3:1 → 最宽档

    def test_zero_or_negative_is_rejected(self):
        for w, h in ((0, 100), (100, 0), (-1, 100)):
            with self.subTest(w=w, h=h):
                with self.assertRaises(ValueError):
                    R.ratio_of(w, h)


class TestPresetsReachOnlyTheFaceThatAsks(unittest.TestCase):
    """预设词是**调用方点名要认**才认的 —— 不传表就不生效。"""

    def test_openai_face_accepts_the_preset_words(self):
        from ark_compat.openai_videos import size_to_ratio

        for word, want in (("portrait", "9:16"), ("landscape", "16:9"),
                           ("square", "1:1"), ("ultrawide", "21:9")):
            with self.subTest(word=word):
                ratio, notes = size_to_ratio(word)
                self.assertEqual(ratio, want)
                self.assertTrue(notes, "预设映射改变了调用方的写法 ⇒ 必须留痕")
                self.assertIn(word, notes[0])

    def test_ark_face_does_not_accept_them(self):
        """方舟线是官方契约，不收非标准值 —— 没传 presets 就是认不出。"""
        from ark_compat.errors import ParamError
        from ark_compat.translate import normalize_ratio

        with self.assertRaises(ParamError):
            normalize_ratio("portrait")                      # 严格：400
        # 宽松线也没认（没传表）⇒ 落到兜底档，而不是"偷偷生效"
        self.assertEqual(normalize_ratio("portrait", fallback="16:9")[0], "16:9")

    def test_preset_table_is_data_not_logic(self):
        """加一个词只需往表里加一项 —— 这里锁表的形态，免得有人改成 if/elif。"""
        self.assertIsInstance(R.SIZE_PRESETS, dict)
        self.assertEqual(R.SIZE_PRESETS["portrait"], "9:16")
        for v in R.SIZE_PRESETS.values():
            self.assertIn(v, R.SITE_UI_SPEC.order, "预设必须落在真实档位上")


class TestSiteSpecMatchesTheUi(unittest.TestCase):
    """站点那份 spec 必须与 UI 选择器一致（换档位要先改这里）。"""

    def test_order_is_the_six_ui_options(self):
        self.assertEqual(R.SITE_UI_SPEC.order, ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9"))

    def test_fallback_is_the_ui_default(self):
        self.assertEqual(R.SITE_UI_SPEC.fallback, "16:9")

    def test_translate_reexports_the_same_spec(self):
        """翻译层用的就是这一份 —— 两份 spec 会立刻漂移。"""
        self.assertIs(SITE_UI_SPEC, R.SITE_UI_SPEC)


if __name__ == "__main__":
    unittest.main()
