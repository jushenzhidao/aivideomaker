#!/usr/bin/env python3
"""内容审核错误的**有区分透传**（2026-10-23 用户口径「上游错误透传一下 尤其是内容审核」）。

为什么要专门开门禁：这条改动**放松了出口脱敏**（此前一律通用句），是**有意的契约变更**，
所以必须同时钉住两件事 —— 该透传的透传、不该透传的一个字都不许露：

  ① 审核失败 ⇒ `code=ContentFiltered` + **上游原话**（`Prompt or Image Sensitive` /
     `...flagged by a content checker.`）—— 调用方据此换素材/文案，且知道**不该重试**；
  ② 其它失败 ⇒ 仍是 `SubmissionFailed` + 通用句（原文含 procedure / 铸造主机 / 上游域名）；
  ③ 上游原话里**绝不带 procedure 前缀**（`ai.minimaxH3:` 是站点内部路由细节）；
  ④ 老记录（没有新键）⇒ 回落到旧行为，不猜。

生产依据（2026-10-23）：59 条审核失败里 **34 次是调用方盲目重发**（同一条重发 10 次），
把失败率从 6.6% 抬到 15.7%。`ContentFiltered` 就是给下游的那句"别再试了"。

运行：python3 -m unittest tests.test_content_rejection_passthrough
"""

import json
import sys
import time
import unittest
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from ark_compat.app import create_app  # noqa: E402
from ark_compat.errors import WebApiError  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402
from test_web_upstream import ark_body, web_settings  # noqa: E402

GATE = "gate-secret-123"
TASKS_PATH = "/api/v3/contents/generations/tasks"


class _FailingApi:
    """`create()` 直接抛指定异常（不需要阻塞：本文件只考失败后的**报文**）。"""

    kind = "web"

    def __init__(self, exc: BaseException):
        self.exc = exc

    def create(self, params, token=None):
        raise self.exc

    def wait_for_task(self, task_id, *, timeout=600.0, interval=10.0, on_poll=None):
        return {"done": True, "ok": True, "status": "succeed", "task": {}, "ms": 1}

    def get_task(self, task_id):
        return {"id": task_id, "taskStatus": "succeed", "aiModel": "minimax-h3",
                "url": "https://cdn.test/a.mp4", "credits": 0, "paid": False}

    def get_credits(self):
        return 100


class PassthroughCase(unittest.TestCase):
    def _submit_and_fail(self, exc: BaseException) -> tuple:
        api = _FailingApi(exc)
        app = create_app(web_settings(gate_key=GATE, submit_inline=False))
        app.state.upstreams = {
            "web": WebUpstream(api, WebSubmitQueue(api, max_concurrent=1, poll_interval=0.01))
        }
        client = TestClient(app)
        r = client.post(TASKS_PATH, json=ark_body(), headers={"Authorization": f"Bearer {GATE}"})
        assert r.status_code == 200, r.text[:200]
        tid = r.json()["id"]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (app.state.tasks.get(tid) or {}).get("submit_error"):
                break
            time.sleep(0.02)
        return app, client, tid

    def _error(self, exc) -> dict:
        app, client, tid = self._submit_and_fail(exc)
        body = client.get(
            f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}
        ).json()
        self.assertIn("error", body, f"失败任务必须带 error：{body}")
        return body["error"], body


class TestContentRejectionIsPassedThrough(PassthroughCase):
    def test_prompt_or_image_sensitive(self):
        exc = WebApiError("ai.minimaxH3", "Prompt or Image Sensitive",
                          code="INTERNAL_SERVER_ERROR", http_status=500)
        err, body = self._error(exc)
        self.assertEqual(err["code"], "ContentFiltered", "审核必须给可区分的 code")
        self.assertIn("Prompt or Image Sensitive", err["message"], "上游原话必须透传")
        self.assertIn("will fail again", err["message"], "必须说明重试无用")
        # 🔴 procedure 名是站点内部路由细节，绝不能跟着原话泄出去
        self.assertNotIn("ai.minimaxH3", json.dumps(body), "procedure 名不许进响应体")

    def test_content_checker_variant(self):
        exc = WebApiError(
            "ai.minimaxH3",
            "The content could not be processed because it contained material flagged by a content checker.",
            code="INTERNAL_SERVER_ERROR", http_status=500,
        )
        err, body = self._error(exc)
        self.assertEqual(err["code"], "ContentFiltered")
        self.assertIn("flagged by a content checker", err["message"])
        self.assertNotIn("ai.minimaxH3", json.dumps(body))

    def test_error_stays_an_object_with_only_two_keys(self):
        """方舟 schema 的 `error` 只有 `{code, message}` —— 多一个键就是坏一个客户端。"""
        exc = WebApiError("ai.minimaxH3", "Prompt or Image Sensitive", http_status=500)
        err, _ = self._error(exc)
        self.assertEqual(set(err), {"code", "message"})


class TestOtherFailuresStayRedacted(PassthroughCase):
    def test_unknown_upstream_error_is_generic(self):
        exc = WebApiError("site", "boom", code="SITE_INTERNAL_42", http_status=500)
        err, body = self._error(exc)
        self.assertEqual(err["code"], "SubmissionFailed", "非审核保持原码 ⇒ 老调用方零感知")
        self.assertEqual(err["message"], "the task could not be submitted to the upstream")
        self.assertNotIn("SITE_INTERNAL_42", json.dumps(body), "站点内部码不许透传")

    def test_read_timeout_is_generic(self):
        exc = WebApiError("ai.minimaxH3", "ReadTimeout: The read operation timed out",
                          transport="ReadTimeout")
        err, body = self._error(exc)
        self.assertEqual(err["code"], "SubmissionFailed")
        self.assertNotIn("ReadTimeout", json.dumps(body), "传输层细节不进对外报文")

    def test_legacy_record_without_new_keys_falls_back(self):
        """老记录（新键落库之前建的）⇒ 回落成旧行为，不猜。"""
        exc = WebApiError("ai.minimaxH3", "Prompt or Image Sensitive", http_status=500)
        app, client, tid = self._submit_and_fail(exc)
        # 模拟"这条记录是在旧版本里写下的"：把新键抹掉。
        # ⚠️ 必须直接改 store 的行 —— `get()` 返回的是**副本**，pop 它不会写回。
        rows = app.state.tasks._rows  # noqa: SLF001 本用例就是要伪造"老记录"
        rows[tid].pop("submit_error_code", None)
        rows[tid].pop("submit_error_quote", None)
        err = client.get(f"{TASKS_PATH}/{tid}",
                         headers={"Authorization": f"Bearer {GATE}"}).json()["error"]
        self.assertEqual(err["code"], "SubmissionFailed")
        self.assertEqual(err["message"], "the task could not be submitted to the upstream")


class TestOpenaiFaceUnchanged(PassthroughCase):
    def test_six_field_contract_has_no_error_key(self):
        """`/v1/videos` 的六字段契约**不带** error ⇒ 透传只作用于方舟线的查询。"""
        exc = WebApiError("ai.minimaxH3", "Prompt or Image Sensitive", http_status=500)
        api = _FailingApi(exc)
        app = create_app(web_settings(gate_key=GATE, submit_inline=False))
        app.state.upstreams = {
            "web": WebUpstream(api, WebSubmitQueue(api, max_concurrent=1, poll_interval=0.01))
        }
        client = TestClient(app)
        r = client.post("/v1/videos", json={"model": "minimaxH3_480p", "prompt": "a cat"},
                        headers={"Authorization": f"Bearer {GATE}"})
        tid = r.json()["id"]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            body = client.get(f"/v1/videos/{tid}",
                              headers={"Authorization": f"Bearer {GATE}"}).json()
            if body.get("status") == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(set(body), {"id", "object", "status", "progress", "video_url", "created_at"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
