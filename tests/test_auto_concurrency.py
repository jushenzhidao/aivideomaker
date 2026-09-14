#!/usr/bin/env python3
"""提交闸门槽位：按**账号额度**定（`ai.queryUserPermission.maxQueueLength`）。

为什么要有这个：槽位过去是写死的（`AVM_MAX_CONCURRENT=2`），而账号真实额度是
**premium 2 / pro 4**（站点 API 字段 + 站点原文同口径）⇒ **pro 账号被我们自己卡在 2**，
4 并发额度用不满。`AVM_MAX_CONCURRENT=0` 现在表示"按账号额度自动"。

三条必须钉住的属性：
1. **显式配置优先**，且显式时**不该**去问上游（别把"配了就按我配的"变成一次额外请求）；
2. 自动模式下**读到了就用它**（premium→2 / pro→4）；
3. 读不到/读失败 ⇒ 回落到 fallback，**绝不能**当成"没限制"（那会直接撞满上游配额），
   也不能把异常抛出去把"建上游"搞失败。

运行：python3 tests/test_auto_concurrency.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.settings import Settings  # noqa: E402
from ark_compat.upstreams import resolve_max_concurrent  # noqa: E402

COOKIE = "auth_session=" + "c" * 40


class FakeClient:
    def __init__(self, perm=None, boom=False):
        self.perm = perm
        self.boom = boom
        self.calls = 0

    def get_permission(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("permission endpoint down")
        return self.perm


def settings(max_concurrent: int, fallback: int = 2) -> Settings:
    return Settings(
        cookie=COOKIE, base_url="http://127.0.0.1:9", trust_env=False,
        task_store="memory", max_concurrent=max_concurrent, max_concurrent_fallback=fallback,
    )


class TestExplicitWins(unittest.TestCase):
    def test_explicit_value_is_used_without_probing(self):
        c = FakeClient(perm={"maxQueueLength": 4, "planName": "pro"})
        self.assertEqual(resolve_max_concurrent(settings(2), c), 2)
        self.assertEqual(c.calls, 0, "显式配置时不该再去问上游额度")

    def test_explicit_value_can_be_larger_than_plan(self):
        # 显式优先是刻意的：调用方可能自己就想更保守/更激进（但更激进会撞上游，见 README）
        c = FakeClient(perm={"maxQueueLength": 2})
        self.assertEqual(resolve_max_concurrent(settings(1), c), 1)


class TestAutoFromAccountQuota(unittest.TestCase):
    def test_pro_account_gets_four_slots(self):
        c = FakeClient(perm={"maxQueueLength": 4, "planName": "pro"})
        self.assertEqual(resolve_max_concurrent(settings(0), c), 4)
        self.assertEqual(c.calls, 1)

    def test_premium_account_gets_two_slots(self):
        c = FakeClient(perm={"maxQueueLength": 2, "planName": "premium"})
        self.assertEqual(resolve_max_concurrent(settings(0), c), 2)

    def test_zero_quota_falls_back(self):
        c = FakeClient(perm={"maxQueueLength": 0})
        self.assertEqual(resolve_max_concurrent(settings(0), c), 2, "额度为 0 是异常值 ⇒ 回落")

    def test_missing_field_falls_back(self):
        c = FakeClient(perm={"planName": "pro"})
        self.assertEqual(resolve_max_concurrent(settings(0), c), 2)

    def test_probe_failure_falls_back_and_does_not_raise(self):
        c = FakeClient(boom=True)
        self.assertEqual(resolve_max_concurrent(settings(0), c), 2, "探测失败不能把建上游搞失败")

    def test_fallback_is_configurable(self):
        c = FakeClient(perm=None)
        self.assertEqual(resolve_max_concurrent(settings(0, fallback=1), c), 1)


class TestSettings(unittest.TestCase):
    def test_zero_means_auto(self):
        self.assertEqual(Settings.from_env({"AVM_MAX_CONCURRENT": "0"}).max_concurrent, 0)
        self.assertEqual(Settings.from_env({}).max_concurrent, 2, "默认仍是显式 2，行为不变")
        self.assertEqual(Settings.from_env({"AVM_MAX_CONCURRENT": "4"}).max_concurrent, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
