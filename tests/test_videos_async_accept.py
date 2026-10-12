#!/usr/bin/env python3
"""**受理/推进分离**的时序门禁（两条线各一组）。

`POST` 毫秒级返回 id，素材转存 / 取 token / 抢上游槽位 / 上游提交在 daemon 后台线程完成。
起初只作用于 `/v1/videos`（2026-09-20）；方舟线 `/tasks` 2026-10-12 并入
（用户口径「优化 minimax 服务，支持后端排队【提交立即返回任务 id】」）。

为什么要单开门禁：这条改动改变的是**时序契约**，而时序恰恰是"所有功能测试都绿、只有体感
不对"的那类缺陷。⚠️ 本文件是**全仓唯一**在真实异步模式下跑的地方 —— 其余测试基座一律走
`submit_inline`（同步推进，理由见 `test_web_upstream.web_settings`），那边考的是映射与契约。
本文件每条断言钉住一个时序事实：

  `/v1/videos`：
  ① POST 返回时提交**尚未完成**（甚至尚未发生）        ⇒ test_post_returns_before... 红
  ② 未提交阶段轮询：对外仍是 queued、**不打上游**      ⇒ test_pending_poll_never_touches... 红
  ③ 提交成功 ⇒ 记录补上 taskId、轮询走到 completed    ⇒ test_a_successful_submit... 红
  ④ 提交失败 ⇒ 记录转 failed、轮询可见、原因不出响应体 ⇒ test_a_failed_submit... 红
  ⑤ 提交线程死了（租约超时）⇒ 轮询把它判成 failed     ⇒ test_a_stale_submission... 红

  方舟线（本组的前身是"方舟线**保持同步**"那条反向锚；行为按用户口径反转后，锚**跟着
  反转**而不是删掉 —— 否则"方舟线又变回同步"这件事将再也无人能发现）：
  ⑥ POST 不等提交完成就返回，形状**仍旧只有一个 id** ⇒ test_the_ark_line_returns_before... 红
  ⑦ 未提交阶段轮询 queued、且**零上游调用**            ⇒ test_a_pending_ark_poll... 红
  ⑧ 提交成功 ⇒ taskId 落库、轮询走到 succeeded         ⇒ test_a_successful_ark_submit... 红
  ⑨ 提交失败 ⇒ failed 且 `error` 是**对象**（不是裸串）⇒ test_a_failed_ark_submit... 红
  ⑩ 租约超时 ⇒ failed（与 OpenAI 面同一套判定）        ⇒ test_a_stale_ark_submission... 红

纪律：零外发（替身客户端）、`create()` 可阻塞的替身让时序**确定性**可断言（不靠 sleep 猜）。

运行：python3 -m unittest tests.test_videos_async_accept
"""

import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import (  # noqa: E402
    OPENAI_VIDEOS_PATH,
    SUBMISSION_FAILED_MESSAGE,
    TASKS_PATH,
    _SUBMIT_LEASE_SECONDS,
    create_app,
)
from ark_compat.errors import WebApiError  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402
from test_web_upstream import ark_body, web_settings  # noqa: E402

GATE = "gate-secret-123"
# 六字段契约（Chatfire 查询响应）—— 每次轮询断言都顺手核一遍键集
QUERY_FIELDS = {"id", "object", "status", "progress", "video_url", "created_at"}


class BlockingSubmitClient:
    """`create()` 可阻塞的替身：让"受理已返回、提交未完成"成为**可断言**的状态。

    `entered` / `release` 两个事件把时序变成确定性的：测试先拿到 POST 响应，
    再等 `entered` 证明提交线程已被调起，此时 `taskId` 必然还没落库。
    """

    kind = "web"

    def __init__(self):
        self.created: list = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False
        self.get_task_calls = 0

    def create(self, params, token=None):
        self.created.append(dict(params))
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("测试 10s 内没有放行 create() —— 时序断言失效")
        if self.fail:
            raise WebApiError("site", "boom", code="SITE_INTERNAL_42", http_status=500)
        return "t-upstream-1"

    def wait_for_task(self, task_id, *, timeout=600.0, interval=10.0, on_poll=None):
        if on_poll is not None:
            on_poll("succeed")
        return {"done": True, "ok": True, "status": "succeed", "task": {}, "ms": 1}

    def get_task(self, task_id):
        self.get_task_calls += 1
        return {
            "id": task_id,
            "taskStatus": "succeed",
            "aiModel": "minimax-h3",
            "url": "https://cdn.test/a.mp4",
            "kelingKeyId": "480",
            "credits": 0,
            "paid": False,
            "createdAt": "2026-09-20T03:00:00.000Z",
            "completedAt": "2026-09-20T03:01:00.000Z",
        }

    def get_credits(self):
        return 796


class AsyncAcceptCase(unittest.TestCase):
    """真实异步模式（显式 `submit_inline=False`）+ 可阻塞替身。"""

    def setUp(self):
        self.api = BlockingSubmitClient()
        # 🔴 显式**关掉**同步推进 —— 本文件是全仓唯一在真异步模式下跑的门禁（其余测试基座
        #    一律 `submit_inline=True`，见 `test_web_upstream.web_settings`）。漏写这一行，
        #    下面所有时序断言都会退化成"同步模式下也成立"的空话（2026-10-12 实测踩到）。
        app = create_app(web_settings(gate_key=GATE, submit_inline=False))
        app.state.upstreams = {
            "web": WebUpstream(self.api, WebSubmitQueue(self.api, max_concurrent=1, poll_interval=0.01))
        }
        self.app = app
        self.client = TestClient(app)

    # ---- helpers ----
    def post_videos(self, **kw):
        fields = {"model": "minimaxH3_480p", "prompt": "a cat"}
        fields.update(kw)
        return self.client.post(
            OPENAI_VIDEOS_PATH, json=fields, headers={"Authorization": f"Bearer {GATE}"}
        )

    def poll(self, tid):
        return self.client.get(
            f"{OPENAI_VIDEOS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}
        )

    def post_ark(self):
        return self.client.post(
            TASKS_PATH, json=ark_body(), headers={"Authorization": f"Bearer {GATE}"}
        )

    def poll_ark(self, tid):
        return self.client.get(
            f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}
        )

    def entry(self, tid):
        return self.app.state.tasks.get(tid)

    def wait_until(self, pred, *, timeout=5.0, what="条件"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError(f"等待超时（{timeout:.0f}s）：{what}")


class TestAcceptThenAdvance(AsyncAcceptCase):
    def test_post_returns_before_the_upstream_submit_finishes(self):
        """★ 核心时序：POST 已经拿到 200/queued，而上游提交还卡在替身里没被放行。"""
        r = self.post_videos()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(r.json()), {"id", "object", "status", "created_at"})
        self.assertEqual(r.json()["status"], "queued", "受理语义就是 queued（已受理未开始）")
        tid = r.json()["id"]

        self.assertTrue(self.api.entered.wait(5), "后台提交线程没有被调起")
        self.assertIsNone(
            self.entry(tid).get("taskId"),
            "受理返回时任务**还没**提交到上游（taskId 此刻必须缺席）",
        )
        # 轮询与上游互不干扰：等待期间不打上游、对外仍是 queued
        p = self.poll(tid)
        self.assertEqual(p.json()["status"], "queued")

        self.api.release.set()
        self.wait_until(
            lambda: (self.entry(tid) or {}).get("taskId") == "t-upstream-1",
            what="后台提交完成（taskId 落库）",
        )
        # 对照：业务照常 —— 别把"不阻塞"做成"不提交"
        self.assertEqual(len(self.api.created), 1, "后台线程必须真的把任务提交出去")

    def test_pending_poll_never_touches_the_upstream(self):
        """未提交阶段的轮询：**零上游调用**（空 taskId 打上游 = 拿回错误空壳 + 假跃迁）。"""
        r = self.post_videos()
        tid = r.json()["id"]
        self.assertTrue(self.api.entered.wait(5))
        for _ in range(3):
            p = self.poll(tid)
            self.assertEqual(p.status_code, 200, p.text)
            self.assertEqual(set(p.json()), QUERY_FIELDS, "六字段契约一个不多一个不少")
            self.assertEqual(p.json()["status"], "queued")
            self.assertIsNone(p.json()["video_url"])
        self.assertEqual(
            self.api.get_task_calls, 0,
            "没提交就查上游 ⇒ 那条轮询根本是在查一个不存在的站点任务",
        )
        self.api.release.set()

    def test_a_successful_submit_becomes_completed_on_poll(self):
        """放行后：taskId 落库 ⇒ 轮询照常走到 completed（受理分离不改变后续管线）。"""
        r = self.post_videos()
        tid = r.json()["id"]
        self.api.entered.wait(5)
        self.api.release.set()
        self.wait_until(
            lambda: (self.entry(tid) or {}).get("taskId") == "t-upstream-1",
            what="taskId 落库",
        )
        self.wait_until(lambda: self.poll(tid).json().get("status") == "completed",
                        what="轮询走到 completed")
        p = self.poll(tid)
        self.assertEqual(p.json()["video_url"], "https://cdn.test/a.mp4")
        self.assertEqual(p.json()["progress"], 100)

    def test_a_failed_submit_becomes_failed_on_poll(self):
        """🔴 提交失败必须**可见**：记录转 failed（否则客户端对着 queued 等到天荒地老）。"""
        self.api.fail = True
        r = self.post_videos()
        tid = r.json()["id"]
        self.api.entered.wait(5)
        self.api.release.set()
        self.wait_until(
            lambda: (self.entry(tid) or {}).get("submit_error"),
            what="失败归因落库（submit_error）",
        )
        self.assertIn("SITE_INTERNAL_42", self.entry(tid)["submit_error"], "完整归因留在记录里")
        self.wait_until(lambda: self.poll(tid).json().get("status") == "failed",
                        what="轮询看到 failed")
        p = self.poll(tid)
        self.assertEqual(set(p.json()), QUERY_FIELDS)
        self.assertIsNone(p.json()["video_url"])
        # 🔴 失败原因不出响应体：六字段契约没这个键，内部归因只进日志/span
        self.assertNotIn("SITE_INTERNAL_42", json.dumps(p.json()))


class TestCrashSafety(AsyncAcceptCase):
    def test_a_stale_submission_is_marked_failed_by_the_lease(self):
        """提交线程死了（重启/崩溃）⇒ 轮询路径的**租约**判定把记录标成 failed。

        没有这一条，"受理成功、提交线程消失"的任务会永远 queued —— 客户端无从得知。
        """
        r = self.post_videos()
        tid = r.json()["id"]
        self.assertTrue(self.api.entered.wait(5))
        rec = self.entry(tid)
        # 把租约起点拨回到"早已超时"（模拟：受理之后进程重启，提交线程没了）
        rec["submit_started_at_ms"] = int(time.time() * 1000) - int(_SUBMIT_LEASE_SECONDS * 1000) - 1000
        self.app.state.tasks.put(rec)

        p = self.poll(tid)
        self.assertEqual(p.json()["status"], "failed", "租约超时必须判成 failed，不许永远 queued")
        self.assertIn("interrupted", self.entry(tid).get("submit_error") or "")
        # 提交线程还卡着（本用例不放行）⇒ 它之后再补的 taskId 不该把 failed 又改回正常
        self.assertIsNone(self.entry(tid).get("taskId"))


class TestTheArkLineAlsoAcceptsEarly(AsyncAcceptCase):
    """★ 方舟线的受理/推进分离（2026-10-12 用户口径「提交立即返回任务 id」）。

    这个类的前身叫 `TestTheArkLineIsUntouched`，断言的是"方舟线**保持同步**" —— 它存在的
    意义正是拦住"受理分离被顺手扩散"。行为按用户口径反转之后，那条锚**跟着反转**而不是
    删掉：否则"方舟线又变回同步"将没有任何门禁能发现（这正是它当初被写出来的理由）。
    """

    def test_the_ark_line_returns_before_the_submit_finishes(self):
        """★ 核心时序（旧锚的反面）：提交还卡在替身里，POST 已经返回，且形状仍是单字段。"""
        result: dict = {}

        def run():
            r = self.post_ark()
            result["status"] = r.status_code
            result["body"] = r.json()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.assertTrue(self.api.entered.wait(5), "方舟线的提交线程没被调起")
        th.join(3)
        self.assertFalse(
            th.is_alive(),
            "POST 仍在等上游提交完成 ⇒ 受理分离没有落到方舟线上",
        )
        self.assertEqual(result.get("status"), 200)
        # 🔴 契约形状**一个键都不能动**：这个端点的调用方是官方 SDK 与直连客户，
        #    受理分离只改"什么时候返回"，不改"返回什么"。
        self.assertEqual(set(result["body"]), {"id"}, "方舟线维持旧契约（只有 id）")
        self.assertIsNone(
            self.entry(result["body"]["id"]).get("taskId"),
            "受理返回时任务**还没**提交到上游（taskId 此刻必须缺席）",
        )
        self.api.release.set()

    def test_a_pending_ark_poll_is_queued_and_touches_nothing(self):
        """未提交阶段的方舟线轮询：`status=queued` + **零上游调用**（空 taskId 打上游没有意义）。"""
        r = self.post_ark()
        tid = r.json()["id"]
        self.assertTrue(self.api.entered.wait(5))
        p = self.poll_ark(tid)
        self.assertEqual(p.status_code, 200, p.text)
        self.assertEqual(p.json()["status"], "queued")
        self.assertIsNone(p.json().get("content"), "还没出片，content 不该出现")
        self.assertEqual(
            self.api.get_task_calls, 0,
            "没提交就查上游 ⇒ 那条轮询是在查一个并不存在的站点任务",
        )
        self.api.release.set()

    def test_a_successful_ark_submit_lands_the_task_id(self):
        """放行后：taskId 落库 ⇒ 方舟线轮询照常走到 succeeded（受理分离不改变后续管线）。"""
        r = self.post_ark()
        tid = r.json()["id"]
        self.api.entered.wait(5)
        self.api.release.set()
        self.wait_until(
            lambda: (self.entry(tid) or {}).get("taskId") == "t-upstream-1",
            what="taskId 落库",
        )
        self.wait_until(
            lambda: self.poll_ark(tid).json().get("status") == "succeeded",
            what="方舟线轮询走到 succeeded",
        )

    def test_a_failed_ark_submit_is_failed_with_a_structured_error(self):
        """🔴 提交失败必须**可见且可读**：记录转 failed，且 `error` 是方舟 schema 的**对象**。

        给裸字符串的话 `ark_task_view` 会把它丢成 `null` ⇒ 调用方只看到 failed、一句原因都
        没有（那正是受理分离推广到方舟线时一并修掉的缺陷，这条门禁钉住它别回退）。
        """
        self.api.fail = True
        r = self.post_ark()
        tid = r.json()["id"]
        self.api.entered.wait(5)
        self.api.release.set()
        self.wait_until(
            lambda: (self.entry(tid) or {}).get("submit_error"),
            what="失败归因落库（submit_error）",
        )
        self.assertIn("SITE_INTERNAL_42", self.entry(tid)["submit_error"], "完整归因留在记录里")
        self.wait_until(
            lambda: self.poll_ark(tid).json().get("status") == "failed",
            what="轮询看到 failed",
        )
        body = self.poll_ark(tid).json()
        self.assertEqual(
            body["error"],
            {"code": "SubmissionFailed", "message": SUBMISSION_FAILED_MESSAGE},
            "error 必须是对象（裸字符串会被 ark_task_view 丢成 null）",
        )
        # 🔴 脱敏：上游原文（procedure 名 / 站点 code / 主机）绝不进响应体
        self.assertNotIn("SITE_INTERNAL_42", json.dumps(body))

    def test_a_stale_ark_submission_is_marked_failed_by_the_lease(self):
        """提交线程死了（重启/崩溃）⇒ 方舟线同样靠**租约**判定把它标成 failed。"""
        r = self.post_ark()
        tid = r.json()["id"]
        self.assertTrue(self.api.entered.wait(5))
        rec = self.entry(tid)
        # 把租约起点拨回到"早已超时"（模拟：受理之后进程重启，提交线程没了）
        rec["submit_started_at_ms"] = (
            int(time.time() * 1000) - int(_SUBMIT_LEASE_SECONDS * 1000) - 1000
        )
        self.app.state.tasks.put(rec)

        p = self.poll_ark(tid)
        self.assertEqual(p.json()["status"], "failed", "租约超时必须判成 failed，不许永远 queued")
        self.assertIn("interrupted", self.entry(tid).get("submit_error") or "")
        self.assertIsNone(self.entry(tid).get("taskId"))


if __name__ == "__main__":
    unittest.main()
