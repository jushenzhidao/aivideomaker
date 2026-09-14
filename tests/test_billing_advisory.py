#!/usr/bin/env python3
"""`duration` 越过免费线**必须**有声音（报告 AVM12-OPEN-ADVISORY / AVM12-OPEN-DEAD）。

两个发现，一个修法：

* **AVM12-OPEN-ADVISORY**：`duration=15` 会**静默**进计费区 —— `effective.billed=true`
  而 `warnings` 为空。计费口径是本项目最贵的一类缺陷（花了就回不来），调用方却只能靠
  自己去读 `billed` 字段才知道要花钱；而 `tier` 默认是 `turbo`，看起来就像免费档。
* **AVM12-OPEN-DEAD**：`translate_create` 里原本有一支"吸附把请求带进了计费区"的提醒，
  但它**不可达**（合法时长是连续区间 `[5, 20]`，吸附只在越界时发生 ⇒
  `duration > FREE_MAX_DURATION >= requested_duration` 永不成立）。留着死分支比删掉更坏：
  读代码的人会以为这件事已经有人管了。

修法：提醒统一在 `billing_view()` 渲染（只有那里同时拿到**最终** tier 与时长，且对
**未被吸附**的越线请求同样生效），死分支删除。

运行：python3 tests/test_billing_advisory.py
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat.translate import (  # noqa: E402
    FREE_MAX_DURATION,
    billing_view,
    translate_create,
)

MODEL = "doubao-seedance-2-5-260628"
TRANSLATE_PY = ROOT / "src" / "ark_compat" / "translate.py"


def plan(duration=15, resolution="720p", tier=None, files=()):
    body = {
        "model": MODEL,
        "content": [{"type": "text", "text": "a red balloon"}],
        "resolution": resolution,
        "duration": duration,
    }
    if tier:
        body["extra_body"] = {"aivideomaker_tier": tier}
    return translate_create(body)


def shots(warnings):
    return [w for w in warnings if "WILL BE BILLED" in w]


class TestBillingAdvisory(unittest.TestCase):
    def test_crossing_the_line_is_announced(self):
        eff, warns = billing_view(plan(duration=15))
        self.assertTrue(eff["billed"])
        found = shots(warns)
        self.assertEqual(len(found), 1, f"越线必须恰好有一条计费提醒：{warns}")
        self.assertIn(f"{FREE_MAX_DURATION}s", found[0], "提醒里必须写清免费上界")
        self.assertIn("prefer_free", found[0], "提醒必须给出省钱开关的名字")

    def test_free_durations_stay_silent(self):
        for duration in (5, 8, 10):
            with self.subTest(duration=duration):
                eff, warns = billing_view(plan(duration=duration))
                self.assertFalse(eff["billed"])
                self.assertEqual(shots(warns), [], "免费档不该报计费")

    def test_the_boundary_follows_the_constant_not_a_hardcoded_number(self):
        """上界两侧各测一格 —— 防止有人在提醒里把 10 写成别的数。"""
        lo, hi = FREE_MAX_DURATION, FREE_MAX_DURATION + 1
        self.assertFalse(billing_view(plan(duration=lo))[0]["billed"])
        self.assertEqual(shots(billing_view(plan(duration=lo))[1]), [])
        self.assertTrue(billing_view(plan(duration=hi))[0]["billed"])
        self.assertEqual(len(shots(billing_view(plan(duration=hi))[1])), 1)

    def test_base_tier_is_not_shouted_about(self):
        """`tier=base` 是调用方**显式点名**的选择，不是"悄悄变贵" ⇒ 不重复喊。"""
        eff, warns = billing_view(plan(duration=15, tier="base"))
        self.assertTrue(eff["billed"])
        self.assertEqual(shots(warns), [])

    def test_snapping_to_the_ceiling_also_warns(self):
        """被吸附到 20s（越界钳制）时，计费提醒同样要有 —— 这正是原死分支想覆盖的场景。"""
        p = plan(duration=99)
        eff, warns = billing_view(p)
        self.assertEqual(eff["duration"], 20)
        self.assertEqual(len(shots(warns)), 1)
        # 同时仍要有"被吸附了"的留痕（两者是不同的事，缺一不可）
        self.assertTrue(any("snapped to 20s" in w for w in warns), warns)

    def test_the_advisory_only_fires_once_even_after_repeated_views(self):
        """`billing_view` 每次调用都会重新渲染 ⇒ 不能把提醒累加进 plan["warnings"]。"""
        p = plan(duration=15)
        first = billing_view(p)[1]
        second = billing_view(p)[1]
        self.assertEqual(shots(first), shots(second))
        self.assertEqual(len(shots(second)), 1)


class TestDeadBranchIsGone(unittest.TestCase):
    """★ AVM12-OPEN-DEAD：不可达的分支必须真的删掉，而不是留在那里误导读者。"""

    def test_the_unreachable_crossing_branch_no_longer_exists(self):
        src = TRANSLATE_PY.read_text(encoding="utf-8")
        self.assertNotIn(
            "crosses into the billed range", src,
            "那条不可达的『跨入计费区』分支又回来了 —— 吸附在连续区间上永远不会跨档",
        )

    def test_translate_create_does_not_render_billing_wording(self):
        """计费措辞只在 `billing_view` 一处渲染（否则两处必然漂移）。"""
        for duration in (5, 15, 99):
            with self.subTest(duration=duration):
                p = translate_create({
                    "model": MODEL,
                    "content": [{"type": "text", "text": "x"}],
                    "resolution": "720p",
                    "duration": duration,
                })
                for w in p["warnings"]:
                    self.assertNotIn("WILL BE BILLED", w)
                    self.assertNotIn("billed range", w)

    def test_snapping_still_leaves_a_trace(self):
        """删的是**计费**那半句，不是"被改过时长"这件事的留痕。"""
        p = translate_create({
            "model": MODEL, "content": [{"type": "text", "text": "x"}],
            "resolution": "720p", "duration": 3,
        })
        self.assertTrue(any("snapped to 5s" in w for w in p["warnings"]), p["warnings"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
