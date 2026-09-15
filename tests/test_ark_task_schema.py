#!/usr/bin/env python3
"""`GET /api/v3/contents/generations/tasks/{id}` 的响应体纪律（报告 AVM12-OPEN-SCHEMA，2026-09-15）。

两轮口径（用户）：
  第一轮 —— 响应体必须对齐官方「查询视频生成任务」schema，官方没有的键一个都不能有；
  第二轮 —— **再收窄**：不要 `model` / `resolution`；凡**值不确定或取不到**的字段一并删掉，
            只留必要字段（`id` / `status` / 出片地址为核心）。

因此对外响应体 = 官方字段集的**真子集**，且只含"我们真知道值"的字段：

    id · status · error · content{video_url[,last_frame_url]} · duration · ratio
    · created_at · updated_at

被拿掉的两类，处理方式不同（别混为一谈）：

  - **官方压根没有的键**（`model` 之外的 `upstream_model` / `upstream_record` /
    `requested` / `effective` / `warnings` / `unsupported` / `usage.credits` /
    `usage.paid`）：官方 SDK（Java/Go）对 unknown field 是**报错**而非忽略 ⇒ 不进响应体，
    改由 `ark.task.fetch` span 承载（**证据换通道，不是消失**，见 test_trace_contract.py）。
  - **官方有、但我们的值只能靠编**（`seed:-1` / `framespersecond:24` /
    `service_tier:"default"` / `execution_expires_after:172800` / `draft:false` /
    `usage.completion_tokens:0`）：归一化层已**停止产出**它们 —— 编一个看起来合理的常量
    比"不给"更糟，调用方会把它当成事实。

运行：python3 tests/test_ark_task_schema.py
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat.translate import (  # noqa: E402
    ARK_TASK_FIELDS,
    ark_task_view,
    normalize_web_task,
)

# 🔴 两个字段集都**硬编码**，绝不引用实现的常量：门禁与实现共用同一个常量时，
#    把白名单改宽会让"多出字段"的断言一起放水 —— 自引用的门禁不可证伪（实测踩过）。
#
# 官方全集：docs 82379/1521309「响应参数」表（2026-09-15 取证）。
OFFICIAL_TASK_FIELDS = frozenset({
    "id", "model", "status", "error", "content", "usage", "seed", "resolution",
    "ratio", "duration", "frames", "framespersecond", "generate_audio", "draft",
    "draft_task_id", "service_tier", "execution_expires_after", "created_at", "updated_at",
})

# 我们**实际允许**输出的字段（用户第二轮口径：只要必要且有真值的）。
ARK_BODY_FIELDS = frozenset({
    "id", "status", "error", "content", "duration", "ratio", "created_at", "updated_at",
})

# 官方有、我们刻意不输出的：要么值只能编，要么用户明确不要
NOT_EMITTED = OFFICIAL_TASK_FIELDS - ARK_BODY_FIELDS

# 站点任务记录的原样（字段名与实测一致：`taskStatus` / `aiModel` / `kelingKeyId`…）
SITE_TASK = {
    "id": "upioiypwxrxyuq4",
    "userId": "cmtv0dy8o0000t4vguhjcpe61",
    "taskStatus": "succeed",
    "taskStatusMsg": None,
    "duration": "5",
    "url": "https://static.img2video.ai/x-1634929_0_minimax_h3_1634929.mp4",
    "cover": "https://static.img2video.ai/cover.jpg",
    "content": "a detective walks into a dim room --ratio 16:9",
    "aspectRatio": "16:9",
    "aiModel": "minimax-h3",
    "credits": 1,
    "kelingKeyId": "704",
    "paid": False,
    "visitorId": "f29ee26edcb4e8b96ee17e277a384f6f",
    "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "createdAt": "2026-09-15T13:10:18.405Z",
    "completedAt": "2026-09-15T13:12:39.538Z",
}


def view(raw=None):
    return ark_task_view(normalize_web_task(SITE_TASK if raw is None else raw))


class TestFieldSet(unittest.TestCase):
    def test_body_is_a_subset_of_the_official_schema(self):
        """官方 SDK 对 unknown field 是**报错**而非忽略 —— 多一个键就是坏一个客户端。"""
        extra = set(view()) - OFFICIAL_TASK_FIELDS
        self.assertEqual(extra, set(), f"响应体出现官方 schema 里没有的键：{sorted(extra)}")

    def test_body_stays_inside_the_narrowed_whitelist(self):
        extra = set(view()) - ARK_BODY_FIELDS
        self.assertEqual(extra, set(), f"响应体多出白名单之外的键：{sorted(extra)}")

    def test_implementation_whitelist_matches_the_narrowed_set(self):
        """防漂移：实现的白名单必须与这里**硬编码的**集合精确相等。

        少了 -> 该给的字段被吞掉；多了 -> 上面两条断言里的任一条会被悄悄放水。
        """
        self.assertEqual(set(ARK_TASK_FIELDS), set(ARK_BODY_FIELDS))

    def test_fields_the_user_dropped_are_really_gone(self):
        """点名钉死：`model` / `resolution` 及所有"值只能编"的官方字段都不输出。"""
        j = view()
        for gone in sorted(NOT_EMITTED):
            self.assertNotIn(gone, j, f"{gone} 属于已收窄掉的字段")
        self.assertIn("model", NOT_EMITTED, "model 必须真的被丢弃（第二轮用户口径）")
        self.assertIn("resolution", NOT_EMITTED, "resolution 必须真的被丢弃（第二轮用户口径）")
        self.assertEqual(set(j["content"]), {"video_url"}, "没设 return_last_frame 就不该有尾帧键")
        self.assertEqual(set(j), set(ARK_BODY_FIELDS), "站点记录完整时，白名单字段应当全部到齐")

    def test_internal_evidence_never_leaks_into_the_body(self):
        j = view()
        for gone in ("upstream_model", "upstream", "upstream_record", "requested",
                     "effective", "warnings", "unsupported", "cover", "output_format",
                     "incompatible", "web_params", "credits", "paid", "file_url"):
            self.assertNotIn(gone, j, f"{gone} 不属于官方 schema")
        # 站点记录里的上游账号标识与浏览器指纹同样不该顺着嵌套结构漏出去
        blob = repr(j)
        for leak in ("cmtv0dy8o0000t4vguhjcpe61", "f29ee26edcb4e8b96ee17e277a384f6f", "Mozilla/5.0"):
            self.assertNotIn(leak, blob, "上游账号标识 / 浏览器指纹不该进对外响应")


class TestOnlyRealValues(unittest.TestCase):
    def test_duration_is_an_integer_not_a_string(self):
        """★ 这就是"对不上"的元凶：站点存的是 `"5"`，官方要 `integer`。"""
        self.assertIsInstance(view()["duration"], int)
        self.assertEqual(view()["duration"], 5)

    def test_unknown_values_are_omitted_not_faked(self):
        """值取不到的字段**不给键**，而不是给 null / 0 / 默认值。"""
        j = view({"taskStatus": "processing"})
        for absent in ("duration", "ratio", "created_at", "updated_at", "content"):
            self.assertNotIn(absent, j, f"{absent} 没有可信值就不该出现")

    def test_error_is_always_present_even_when_null(self):
        """`error` 是唯一恒给的可空字段：官方规定成功时显式返回 `null`（那是**确定**的信息）。"""
        self.assertIn("error", view())
        self.assertIsNone(view()["error"])

    def test_content_appears_only_when_a_video_url_exists(self):
        self.assertEqual(view()["content"]["video_url"], SITE_TASK["url"])
        self.assertNotIn("content", view({"taskStatus": "processing"}))

    def test_last_frame_url_requires_a_truthy_value(self):
        base = normalize_web_task(SITE_TASK)
        base["content"] = {**base["content"], "last_frame_url": "https://cdn/last.png"}
        self.assertEqual(ark_task_view(base)["content"]["last_frame_url"], "https://cdn/last.png")

    def test_a_queued_task_returns_a_minimal_body(self):
        """排队中：只有 `status` + `error` 两项 —— 没有 URL、没有时长、没有编造值。"""
        j = view({"taskStatus": "queueing"})
        self.assertEqual(set(j), {"status", "error"})
        self.assertEqual(j["status"], "queued")

    def test_failed_task_carries_a_reason(self):
        j = view({"taskStatus": "failed", "taskStatusMsg": "boom"})
        self.assertEqual(j["status"], "failed")
        self.assertEqual(set(j["error"]), {"code", "message"})
        self.assertEqual(j["error"]["message"], "boom")


class TestNormalizerStopsFabricating(unittest.TestCase):
    """归一化层不再产出"看起来合理但其实是编的"常量。

    它们曾经让响应体看起来很"完整"（`seed:-1`、`service_tier:"default"`、
    `usage.completion_tokens:0`…），代价是调用方会把它们当成事实。
    """

    def test_fabricated_official_constants_are_gone(self):
        t = normalize_web_task(SITE_TASK)
        for gone in ("seed", "framespersecond", "service_tier", "execution_expires_after",
                     "draft", "draft_task_id", "generate_audio", "frames", "output_format",
                     "cover", "file_url", "last_frame_url"):
            self.assertNotIn(gone, t, f"{gone} 是编造/无来源的字段，不该再从归一化层产出")

    def test_usage_keeps_only_the_real_station_fields(self):
        """站点真实给的是 `credits` / `paid`；token 用量站点没有，不许伪造 0。"""
        usage = normalize_web_task(SITE_TASK)["usage"]
        self.assertEqual(set(usage), {"credits", "paid"})
        self.assertEqual(usage["credits"], 1)
        self.assertFalse(usage["paid"])

    def test_paid_flag_not_credits_decides_billing(self):
        free = normalize_web_task({"taskStatus": "succeed", "url": "u", "credits": 1, "paid": False})
        self.assertFalse(free["usage"]["paid"])
        self.assertEqual(free["usage"]["credits"], 1)

    def test_the_evidence_fields_are_still_there_for_the_trace(self):
        """收窄的是**响应体**，不是内部视图 —— 证据必须留给 span。"""
        t = normalize_web_task(SITE_TASK)
        self.assertEqual(t["upstream_model"], "minimax-h3")
        self.assertEqual(t["resolution"], "704p", "实际产出档位（kelingKeyId）内部仍要能读")
        self.assertEqual(t["upstream_record"]["aiModel"], "minimax-h3")


class TestStatusVocabularyCoversAllSixOfficialStates(unittest.TestCase):
    def test_expired_is_a_reachable_state(self):
        """官方 6 态里的 `expired` 曾经完全没有分支 —— 超时任务会被默认成 `queued`，
        对调用方来说"还在排队"和"已经超时"是相反的两个结论。"""
        for word in ("expired", "timeout", "timedout", "timed_out", "expire"):
            self.assertEqual(
                normalize_web_task({"taskStatus": word})["status"], "expired",
                f"站点状态 {word!r} 应落到官方的 expired",
            )

    def test_the_other_five_states_still_map(self):
        cases = {
            "queueing": "queued", "processing": "running",
            "failed": "failed", "cancelled": "cancelled",
        }
        for site_word, ark_word in cases.items():
            self.assertEqual(normalize_web_task({"taskStatus": site_word})["status"], ark_word)
        self.assertEqual(
            normalize_web_task({"taskStatus": "succeed", "url": "u"})["status"], "succeeded"
        )

    def test_succeed_without_a_url_becomes_a_failure(self):
        t = normalize_web_task({"taskStatus": "succeed", "url": None, "taskStatusMsg": "not found url"})
        self.assertEqual(t["status"], "failed")
        self.assertNotIn("content", ark_task_view(t), "没出片就不该给 content")


class TestDegenerateInputsDoNotRaise(unittest.TestCase):
    def test_empty_and_none(self):
        for raw in (None, {}, {"taskStatus": None}):
            j = ark_task_view(normalize_web_task(raw))
            self.assertIsInstance(j, dict)
            self.assertEqual(set(j) - ARK_BODY_FIELDS, set())

    def test_unparsable_duration_is_dropped_not_faked(self):
        j = ark_task_view(normalize_web_task({**SITE_TASK, "duration": "not-a-number"}))
        self.assertNotIn("duration", j)

    def test_ark_task_view_tolerates_a_view_that_is_missing_keys(self):
        j = ark_task_view({"id": "cgt-x", "status": "queued"})
        self.assertEqual(j["id"], "cgt-x")
        self.assertEqual(j["status"], "queued")
        self.assertEqual(set(j) - ARK_BODY_FIELDS, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
