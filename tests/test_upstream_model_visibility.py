#!/usr/bin/env python3
"""上游**实际执行**的模型必须能在契约里读到（报告 AVM12-OPEN-UPSTREAM）。

历轮三次真实提交的结论一致：请求的是 `doubao-seedance-2-5-260628`，成片 URL 里却是
`minimax_h3`（web 线只有 `ai.minimaxH3` 一条 tRPC 程序，站点对请求的模型名只是回显）。
但对外任务视图里 `model` 被覆盖成**请求值**、嵌套的原始记录又被 `upstream`（上游种类）
覆盖掉 ⇒ "上游到底跑了什么"这个事实**在最后一跳消失**，契约里读不到。

修法两条（都在 `translate.py`）：
  1. `normalize_web_task` 增加平行的 `upstream_model`（= 站点记录里的 `aiModel`），
     与 `model`（请求值）**并列**暴露 —— 谁都不改写谁；
  2. 原始记录换到不会被覆盖的键 `upstream_record`（`app._task_view` 会用 `upstream`
     表示上游种类）—— 证据链不再在最后一跳断掉。

运行：python3 tests/test_upstream_model_visibility.py
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat.translate import normalize_web_task  # noqa: E402

REQUESTED = "doubao-seedance-2-5-260628"


def site_record(**kw):
    base = {
        "id": "t1",
        "taskStatus": "succeed",
        "url": "https://static.img2video.ai/x-1620612_0_minimax_h3_1620612.mp4",
        "aiModel": "minimax_h3",
        "kelingKeyId": "480",
    }
    base.update(kw)
    return base


class TestUpstreamModelIsVisible(unittest.TestCase):
    def test_upstream_model_is_exposed(self):
        self.assertEqual(normalize_web_task(site_record())["upstream_model"], "minimax_h3")

    def test_requested_and_actual_models_are_both_present_and_distinct(self):
        """★ 核心：`model` 不被改写、`upstream_model` 单独给 —— 两个事实都要在。"""
        view = normalize_web_task(site_record())
        self.assertEqual(view["upstream_model"], "minimax_h3")
        # 归一化层里 `model` 仍是上游记录里的值；**请求值**由 app 层另给（见
        # tests/test_web_upstream.py 的 app 层用例）。
        self.assertEqual(view["model"], "minimax_h3")
        self.assertIn("upstream_model", view, "字段名被改掉了？调用方按它读")

    def test_missing_model_is_none_never_fabricated(self):
        """记录里没有这个字段时如实给 None —— 拿请求值顶上等于"看不到"变"看到假的"。"""
        view = normalize_web_task(site_record(aiModel=None))
        self.assertIsNone(view["upstream_model"])

    def test_empty_record_does_not_raise(self):
        self.assertIsNone(normalize_web_task(None)["upstream_model"])
        self.assertIsNone(normalize_web_task({})["upstream_model"])


class TestRawRecordSurvivesTheLastHop(unittest.TestCase):
    def test_the_raw_site_record_is_under_a_key_app_does_not_clobber(self):
        """`upstream` 这个键在 app 层表示**上游种类**（`"web"`）且会被覆盖 ⇒
        原始记录必须放别的键，否则证据在最后一跳消失。"""
        view = normalize_web_task(site_record())
        self.assertIn("upstream_record", view)
        self.assertEqual(view["upstream_record"]["aiModel"], "minimax_h3")
        self.assertNotIn(
            "upstream", view,
            "`upstream` 是 app 层的上游**种类**字段，归一化层不该占用它（会被覆盖）",
        )

    def test_app_layer_assigns_upstream_after_and_therefore_needs_the_other_key(self):
        """把"谁覆盖谁"这件事钉成契约：app 层的赋值必须发生在 `upstream` 上。"""
        app_src = (ROOT / "src" / "ark_compat" / "app.py").read_text(encoding="utf-8")
        self.assertIn('view["upstream"] = entry["upstream"]', app_src)
        self.assertIn("upstream_record", (ROOT / "src" / "ark_compat" / "translate.py")
                      .read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
