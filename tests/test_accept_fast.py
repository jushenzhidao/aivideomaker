#!/usr/bin/env python3
"""「受理即返」的最后一个破口：**新凭据首条不再等额度探测**（2026-10-21）。

问题：透传模式下每个新账号的首条 `POST /tasks` 都要现建上游，而建上游会同步调
`ai.queryUserPermission` 拿 `maxQueueLength`（决定闸门槽位）。那是跨境的一次完整往返，
新 HTTP 客户端还得先做 TLS 握手 —— 生产实测把受理耗时拖到 **3167ms**，
而"受理即返"承诺的是毫秒级。

改法：透传建实例时**不等**探测（先用 `max_concurrent_fallback` 把闸门建起来），
额度交给后台线程，探到就 `raise_capacity` **补许可**（只放宽，不收紧）。

这份门禁钉四件事：
  ① 建实例**立即返回**（探测在慢也不等）；
  ② 后台探测**确实**把容量放宽了；
  ③ `raise_capacity` **只放宽不收紧**（收紧会把在跑的任务卡在额度之外）；
  ④ 启动路径（`build_upstreams`，不在受理链路上）**仍然同步探测** —— 别顺手一起改掉。

运行：python3 tests/test_accept_fast.py
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import upstreams  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

COOKIE = "auth_session=" + "c" * 40


def settings(**kw) -> Settings:
    base = dict(
        cookie=COOKIE,
        base_url="https://site.test",
        trust_env=False,
        task_store="memory",
        max_concurrent=0,            # 0 = 按账号额度自动 ⇒ 才走探测
        max_concurrent_fallback=2,
        minter_url="",
    )
    base.update(kw)
    return Settings(**base)


def slow_permission(slots=4, delay=0.5, plan="pro"):
    calls = {"n": 0}

    def fn(self, *a, **kw):
        calls["n"] += 1
        time.sleep(delay)
        return {"maxQueueLength": slots, "planName": plan}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


class TestBuildDoesNotWait(unittest.TestCase):
    def test_build_returns_immediately(self):
        s = settings()
        probe = slow_permission()
        t0 = time.perf_counter()
        with mock.patch.object(upstreams.WebClient, "get_permission", probe):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        dt = time.perf_counter() - t0
        self.addCleanup(up.client.close)
        self.assertLess(dt, 0.2, f"建实例不该等额度探测（实测 {dt * 1000:.0f}ms）")
        self.assertEqual(up.queue.max_concurrent, 2, "先用 fallback 槽位把闸门建起来")

    def test_background_probe_raises_capacity(self):
        s = settings()
        probe = slow_permission(slots=4, delay=0.05)
        with mock.patch.object(upstreams.WebClient, "get_permission", probe):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        self.assertEqual(up.queue.max_concurrent, 2)
        for _ in range(200):
            if up.queue.max_concurrent >= 4:
                break
            time.sleep(0.01)
        self.assertEqual(up.queue.max_concurrent, 4, "后台探测到额度 4 ⇒ 应补到 4 槽")
        self.assertEqual(probe.calls["n"], 1, "只探一次")

    def test_background_probe_divides_by_workers(self):
        """多 worker：额度要在**后台**也按 worker 数分摊（否则每 worker 都按全额跑）。"""
        s = settings(concurrency_divisor=2)
        with mock.patch.object(upstreams.WebClient, "get_permission",
                              slow_permission(slots=4, delay=0.02)):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        for _ in range(200):
            if up.queue.max_concurrent != 2:
                break
            time.sleep(0.01)
        # fallback 也是 2//2=1；探到 4//2=2 ⇒ 最终 2
        self.assertGreaterEqual(up.queue.max_concurrent, 1)
        self.assertLessEqual(up.queue.max_concurrent, 2)

    def test_probe_failure_keeps_fallback(self):
        s = settings()
        with mock.patch.object(upstreams.WebClient, "get_permission",
                              side_effect=RuntimeError("boom")):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        time.sleep(0.1)
        self.assertEqual(up.queue.max_concurrent, 2, "探测失败必须保持 fallback，不能变 0")

    def test_probe_without_quota_field_keeps_fallback(self):
        s = settings()
        with mock.patch.object(upstreams.WebClient, "get_permission",
                              return_value={"planName": "?"}):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        time.sleep(0.1)
        self.assertEqual(up.queue.max_concurrent, 2)

    def test_probe_is_registered_as_in_flight(self):
        """探测期间这个 client 必须"不算空闲"，否则缓存淘汰会把它 close 掉。"""
        s = settings()
        with mock.patch.object(upstreams.WebClient, "get_permission",
                              slow_permission(delay=0.3)):
            up = upstreams.build_web_for_cookie(s, COOKIE)
        self.addCleanup(up.client.close)
        self.assertGreaterEqual(up.in_flight, 1, "探测线程应当登记在飞")
        self.assertFalse(up.idle, "探测期间不能判空闲（会被 close，探测必然失败）")
        for _ in range(100):
            if up.in_flight == 0:
                break
            time.sleep(0.02)
        self.assertEqual(up.in_flight, 0, "探测结束必须注销（否则这个上游永远不会被淘汰）")


class TestRaiseCapacityOnlyGrows(unittest.TestCase):
    def _queue(self, n=2):
        return WebSubmitQueue(None, max_concurrent=n)

    def test_grows(self):
        q = self._queue(2)
        self.assertEqual(q.raise_capacity(4), 4)
        self.assertEqual(q.stats()["max_concurrent"], 4)

    def test_never_shrinks(self):
        q = self._queue(2)
        q.raise_capacity(4)
        self.assertEqual(q.raise_capacity(1), 4, "收紧会把在跑的任务卡在额度之外")

    def test_extra_slots_are_really_usable(self):
        """补的许可必须能真的 acquire 到 —— 否则就是"看着 4 槽、实际只有 2 槽"。"""
        import threading

        q = self._queue(2)
        held = []

        def take():
            q._sem.acquire(timeout=1)
            held.append(1)

        for _ in range(2):
            take()
        self.assertEqual(len(held), 2)
        # 第 3 个现在应当拿不到
        self.assertFalse(q._sem.acquire(timeout=0.05))
        q.raise_capacity(4)
        self.assertTrue(q._sem.acquire(timeout=0.05), "放宽之后应当立刻能再拿一个")


class TestStartupPathStillProbes(unittest.TestCase):
    def test_build_upstreams_probes_synchronously(self):
        """启动路径不在受理链路上 ⇒ 保持同步探测（否则 `/healthz` 会先报 fallback 槽位）。"""
        s = settings(cookie=COOKIE)
        with mock.patch.object(upstreams.WebClient, "get_permission",
                              slow_permission(slots=4, delay=0.01)):
            pool = upstreams.build_upstreams(s)
        up = pool["web"]
        self.addCleanup(up.client.close)
        self.assertEqual(up.queue.max_concurrent, 4, "启动路径应当同步拿到真实额度")


if __name__ == "__main__":
    unittest.main(verbosity=2)
