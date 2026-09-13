#!/usr/bin/env python3
"""免费窗口的边界（2026-09-14 更正为 **≤10s**）。

判据来自站点自己的**终态记录**，不是文案：用户在 480p/turbo 下提交的 **10s** 任务
`taskStatus=succeed` 且 **`paid=False`**（余额未动）⇒ 10s 仍在免费区。
原常量写的是 8s，会让适配层**多报计费**（并让 `prefer_free` 把 10s 无谓拉短）。

`480p` 的合法时长只有 5/10/15/20 ⇒ 免费档是 5s 与 10s；`720p` 连续 5~20 ⇒ 免费档 5~10s。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.translate import (  # noqa: E402
    FREE_MAX_DURATION,
    billing_note,
    billing_view,
    snap_duration,
)


class TestFreeWindow(unittest.TestCase):
    @staticmethod
    def _plan(duration, tier="turbo", resolution="720p"):
        # billing_view 读 plan["effective"].duration 与 plan["web_params"].tier
        return {"effective": {"duration": duration},
                "web_params": {"tier": tier, "resolution": resolution}}

    def test_constant_is_ten(self):
        self.assertEqual(FREE_MAX_DURATION, 10)

    def test_ten_seconds_is_free_eleven_is_billed(self):
        for duration, expected in ((5, False), (8, False), (10, False), (11, True), (15, True)):
            eff, _ = billing_view(self._plan(duration))
            self.assertIs(eff["billed"], expected, f"turbo/{duration}s 的 billed 判断错了")

    def test_base_is_always_billed(self):
        eff, _ = billing_view(self._plan(5, tier="base"))
        self.assertTrue(eff["billed"])

    def test_prefer_free_snaps_to_ten_not_eight(self):
        # 15s 合法但越过免费区 ⇒ prefer_free 拉到免费上界（10），不是旧上界 8
        self.assertEqual(snap_duration(15, "720p", prefer_free=True), 10)

    def test_billing_note_uses_the_constant(self):
        self.assertIn("10s", billing_note(), "计费提示没跟着常量走")


if __name__ == "__main__":
    unittest.main(verbosity=2)
