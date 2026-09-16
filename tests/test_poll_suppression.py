#!/usr/bin/env python3
"""闸门 1（产生层）：轮询不产生 span，状态跃迁才留痕。

为什么值得单开一个门禁文件：这一轮改的是**产生量**。"到底少了多少条 span"必须能被
量化断言 —— 只断言"业务还对"完全看不见它（一个任务发 60 条 span 的版本，所有功能测试
都是绿的）。反过来，本文件每条断言都应当**能被变异证伪**：

  ① 把闸门摘掉（每次都发 span）        ⇒ TestInboundBudget 的"重复查询不产 span"红
  ② 首见也静音（把跃迁一起去重）        ⇒ ..._every_status_transition_... 红
  ③ 把失败也当成"状态没变"              ⇒ ..._failure_always_leaves_a_span 红
  ④ 去掉 excluded_urls 里的轮询表       ⇒ test_logfire_probe_exclusion.TestWiring 红
  ⑤ 去掉 wait_for_task 里的 suppress_http ⇒ test_each_poll_get_is_wrapped_... 红
  ⑥ 让 on_poll 的异常冒出去             ⇒ ..._a_broken_on_poll_callback_... 红
  ⑦ 把 on_poll 的失败也算成"盯梢失败"    ⇒ ..._a_broken_on_poll_callback_... 红

四层，从机制到接线到底线：

  1. **机制**：`suppress_instrumentation()` 到底压得住什么（出站 httpx 压得住、入站 ASGI
     压不住、且会连带压掉我们自己显式开的 span）。这是"为什么入站只能用 excluded_urls"
     的判据，也是本轮最容易想当然的地方 —— 全部实测。
  2. **入站**：同状态重复查询 ⇒ 0 条新 span；每次跃迁 ⇒ 1 条；失败 ⇒ 照发。
  3. **出站**：覆盖全程的**唯一** span + 跃迁事件；轮询 GET 被压制；盯梢坏掉也要放槽位。
  4. **指标与日志**：不产生 span 的查询仍被计数；成功的轮询不上报 logfire，失败的照报。

运行：python3 -m unittest tests.test_poll_suppression
"""

import http.server
import json
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import logfire  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from logfire.testing import (  # noqa: E402
    IncrementalIdGenerator,
    SimpleSpanProcessor,
    TestExporter,
    TimeGenerator,
)
from loguru import logger  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402

from ark_compat import observability as O  # noqa: E402
from ark_compat import web_client  # noqa: E402
from ark_compat.app import TASKS_PATH, PollReportGate, create_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

# 复用 web 上游那套**真站点替身**（httpx.MockTransport），不另造一个
from test_web_upstream import (  # noqa: E402
    FakeSite,
    TrpcError,
    ark_body,
    make_client,
    web_settings,
)

GATE = "gate-secret-123"

# 站点侧任务记录。`url` 必须有：站点会把没有产出 URL 的 `succeed` 判成失败
# （translate.normalize_web_task 里那条"自相矛盾状态"的修正），没有它就测不到终态。
SITE_TASK = {
    "id": "t1",
    "taskStatus": "succeed",
    "aiModel": "minimax-h3",
    "url": "https://cdn.test/a.mp4",
    "kelingKeyId": "704",
    "credits": 0,
    "paid": False,
}


def site_task(**kw) -> dict:
    rec = dict(SITE_TASK)
    rec.update(kw)
    return rec


def configure_tracing(exporter: TestExporter) -> None:
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=False,
        advanced=logfire.AdvancedOptions(
            id_generator=IncrementalIdGenerator(),
            ns_timestamp_generator=TimeGenerator(),
        ),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )


# ======================================================================== 1 机制 ----


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音 stdlib 的 access log
        pass


class TestSuppressionMechanism(unittest.TestCase):
    """`suppress_instrumentation()` 的边界。**全部实测**，不引用文档。

    ⚠️ 必须走**真实传输**：出站 httpx 埋点打在 `HTTPTransport.handle_request` 上，
    用 `MockTransport` 的测试永远看不到它（这正是"httpx 自动子 span"在项目里
    长期只存在于注释、从没被断言过的原因）。所以这里起一个**回环**服务 —— 不出网。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.exporter = TestExporter()
        configure_tracing(self.exporter)
        O._LOGFIRE_READY = True
        logfire.instrument_httpx(capture_headers=False)
        self.client = httpx.Client(trust_env=False)
        self.addCleanup(self.client.close)

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    def names(self) -> list:
        logfire.force_flush()
        return [s["name"] for s in self.exporter.exported_spans_as_dict()]

    def get(self):
        return self.client.get(f"http://127.0.0.1:{self.port}/api/model.getModel")

    def test_control_an_unsuppressed_get_does_produce_a_span(self):
        """对照组：不压制时它**确实**产 span —— 否则下面那条断言是空跑。"""
        self.get()
        self.assertEqual(len(self.names()), 1, f"对照组失效：{self.names()}")

    def test_a_suppressed_get_produces_nothing(self):
        with O.suppress_http():
            self.get()
        self.assertEqual(self.names(), [], "轮询 GET 必须一条 span 都不产生")

    def test_our_own_span_survives_when_only_the_get_is_suppressed(self):
        """🔴 **顺序契约**：压制按 contextvar 在 **span 处理器层**生效，会连带压掉我们
        自己显式开的 `span()` ⇒ 必须"先开 span、再用它只包 GET"。

        把顺序写反，覆盖全程的那条 span 会**静默消失** —— 业务照跑、测试照绿，
        只是 trace 里再也找不到这个任务。这条断言就是钉住顺序的。
        """
        with O.span("ark.task.watch", upstream="web"):
            with O.suppress_http():
                self.get()
        self.assertEqual(self.names(), ["ark.task.watch"])

    def test_suppression_is_thread_scoped(self):
        """压制是**按上下文**的：盯梢线程里的压制不许影响同进程的在飞请求
        （两者共用同一个 httpx 客户端，靠的只是 contextvar 隔离）。"""
        entered, release = threading.Event(), threading.Event()

        def holder():
            with O.suppress_http():
                entered.set()
                release.wait(5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(entered.wait(5), "压制线程没起来，断言会变成空跑")
        try:
            self.get()  # 主线程：压制不生效 ⇒ 照旧有 span
        finally:
            release.set()
            t.join(5)
        self.assertEqual(len(self.names()), 1, "别的线程被压制不该影响本线程")


# ======================================================================== 2 入站 ----


class InboundBudgetCase(unittest.TestCase):
    """真 app + 真站点替身 + 内存 exporter：入站轮询的 span 预算。"""

    TTL = 15.0  # 与生产默认一致

    def setUp(self):
        self.exporter = TestExporter()
        configure_tracing(self.exporter)
        self.site = FakeSite()
        self.site.set("model.getModel", site_task())
        self.site.set("ai.minimaxH3", "t1")
        self.ttl = self.TTL
        self.boot()
        O._LOGFIRE_READY = True

    def boot(self):
        """按当前 `self.ttl` 重建 app + TestClient（要改 TTL 的用例重建一次即可）。"""
        app = create_app(web_settings(gate_key=GATE, task_cache_ttl=self.ttl))
        client = make_client(self.site)
        app.state.upstreams = {
            "web": WebUpstream(
                client, WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01)
            )
        }
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    # ---- helpers ----
    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def post_task(self) -> str:
        r = self.client.post(
            TASKS_PATH, json=ark_body(), headers={"Authorization": f"Bearer {GATE}"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def get_task(self, tid: str):
        return self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})


class TestInboundBudget(InboundBudgetCase):
    def test_repeat_poll_produces_no_new_span(self):
        """★★ 本轮的主断言：10 次同状态轮询 = 1 条 span（不是 10 条）。

        量化口径：一个 300s 任务被按 5s 轮询 = 60 次请求。改前是 60 条
        `ark.task.fetch` + 60 条 ASGI span；改后只剩"跃迁"那几条。
        """
        tid = self.post_task()
        self.get_task(tid)
        first = len(self.spans("ark.task.fetch"))
        self.assertEqual(first, 1, "首次观测留一条")
        for _ in range(9):
            self.get_task(tid)
        self.assertEqual(
            len(self.spans("ark.task.fetch")), first,
            "同状态重复查询必须**零新增** span（闸门 1：产生层）",
        )
        # 对照：请求本身照常成功 —— 别把"不埋点"做成"不改动业务"
        self.assertEqual(self.get_task(tid).status_code, 200)

    def test_every_status_transition_emits_exactly_one_span(self):
        """跃迁才留痕：queued→running→succeeded 每一步一条，重复的不要。

        用 `task_cache_ttl=0` 关掉节流缓存，好让每次轮询都真的回上游读一次 ——
        否则命中缓存根本走不到"观测到新状态"那一步。
        """
        self.ttl = 0.0
        self.boot()
        self.site.set("model.getModel", site_task(taskStatus="queueing"))
        tid = self.post_task()

        self.get_task(tid)  # first: queued
        self.get_task(tid)  # 同状态：不留痕
        self.site.set("model.getModel", site_task(taskStatus="processing"))
        self.get_task(tid)  # transition: queued -> running
        self.site.set("model.getModel", site_task(taskStatus="succeed"))
        self.get_task(tid)  # transition: running -> succeeded
        self.get_task(tid)  # 同状态：不留痕

        spans = self.spans("ark.task.fetch")
        self.assertEqual(len(spans), 3, f"5 次查询只该有 3 条（首见 + 2 次跃迁）：{len(spans)}")
        reasons = [s["attributes"]["transition_reason"] for s in spans]
        self.assertEqual(reasons, ["first", "transition", "transition"])
        self.assertEqual(
            (spans[1]["attributes"]["transition_from"], spans[1]["attributes"]["transition_to"]),
            ("queued", "running"),
        )
        self.assertEqual(spans[2]["attributes"]["status"], "succeeded")
        # 跃迁也留下了事件（片段里"状态跃迁用事件"的语义）
        events = [e["name"] for e in spans[2].get("events") or []]
        self.assertIn("status_change", events)

    def test_failure_always_leaves_a_span(self):
        """🔴 **失败不去重**：失败时上游状态未知，不能据此认定"没变化"。

        两次同样的失败必须留两条 —— 静音掉就等于把"调用方到底看到了什么"删了。
        """
        tid = self.post_task()
        self.site.set("model.getModel", error=TrpcError("task not found", code="NOT_FOUND"))
        self.get_task(tid)
        self.get_task(tid)

        spans = self.spans("ark.task.fetch")
        self.assertEqual(len(spans), 2, "失败必须每次都留痕（不许被闸门当成'状态没变'）")
        for s in spans:
            self.assertEqual(s["attributes"]["transition_reason"], "error")
            self.assertIn("NOT_FOUND", s["attributes"]["error"])

    def test_first_observation_from_cache_carries_the_full_evidence(self):
        """缓存命中那条上报路径**仍然**要能带齐证据 —— 只是触发条件变窄了。

        原来"每次命中缓存"都发 span；现在只有**本进程第一次看到这个任务**才发
        （闸门的判据是"状态跃迁"，而缓存里的状态按定义就是上次上报过的那个）。
        这里换一个空闸门来模拟"另一个 worker / 重启后的第一次查询"。
        """
        tid = self.post_task()
        self.get_task(tid)  # 非缓存路径：建缓存 + 首次上报
        self.assertEqual(len(self.spans("ark.task.fetch")), 1)

        self.get_task(tid)  # 缓存命中 + 状态没变 ⇒ 不再上报
        self.assertEqual(len(self.spans("ark.task.fetch")), 1, "重复查询不该新增")

        self.app.state.poll_gate = PollReportGate()  # 空闸门 = 另一个进程第一次看到它
        self.get_task(tid)
        spans = self.spans("ark.task.fetch")
        self.assertEqual(len(spans), 2)
        attrs = spans[-1]["attributes"]
        self.assertTrue(attrs["cached"], "这条来自缓存（本次没有上游调用）")
        for required in ("ark_id", "upstream_task_id", "status", "paid",
                         "warnings", "unsupported", "upstream_response"):
            self.assertIn(required, attrs, f"缓存路径缺了 {required}（与非缓存路径不一致）")
        self.assertEqual(json.loads(attrs["upstream_response"])["resolution"], "704p")


class TestPollGateUnit(unittest.TestCase):
    """闸门本身：判据、计数、以及"失败不许污染已知状态"这条边界。"""

    def test_reason_sequence_and_poll_count(self):
        g = PollReportGate()
        self.assertEqual(g.observe("cgt-1", "queued")["reason"], "first")
        self.assertIsNone(g.observe("cgt-1", "queued"))
        self.assertIsNone(g.observe("cgt-1", "queued"))
        hit = g.observe("cgt-1", "running")
        self.assertEqual((hit["reason"], hit["from"], hit["polls"]), ("transition", "queued", 4))
        self.assertIsNone(g.observe("cgt-1", "running"))

    def test_a_failure_does_not_overwrite_the_known_status(self):
        """🔴 失败时状态未知 ⇒ 不能用空串覆盖已知状态。

        覆盖了的话，**下一次成功查询会被读成"跃迁"**：凭空多一条 span，还给对账一个
        假的跃迁方向（`from` 是空串）。
        """
        g = PollReportGate()
        g.observe("cgt-1", "running")
        g.observe("cgt-1", "", error="WebApiError[NOT_FOUND]: boom")
        self.assertIsNone(
            g.observe("cgt-1", "running"),
            "失败没有改变状态 ⇒ 下一次同状态查询不该被读成跃迁",
        )

    def test_gate_is_bounded(self):
        """有界：任务不是无限的，闸门不能无限长（超限扔最早的）。"""
        g = PollReportGate(max_entries=3)
        for i in range(10):
            g.observe(f"cgt-{i}", "queued")
        self.assertEqual(len(g._seen), 3)
        self.assertEqual(g.observe("cgt-9", "queued"), None, "最近看到的那条必须还在")


class TestPollMetrics(InboundBudgetCase):
    """不产生 span 的那些查询**仍然被计数**（量在、明细不要）。"""

    TTL = 0.0

    def setUp(self):
        O.reset_gauge_cache()
        self.reader = InMemoryMetricReader()
        self.exporter = TestExporter()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(),
                ns_timestamp_generator=TimeGenerator(),
            ),
            additional_span_processors=[SimpleSpanProcessor(self.exporter)],
            metrics=logfire.MetricsOptions(additional_readers=[self.reader]),
        )
        self.site = FakeSite()
        self.site.set("model.getModel", site_task(taskStatus="processing"))
        self.site.set("ai.minimaxH3", "t1")
        self.ttl = self.TTL
        self.boot()
        O._LOGFIRE_READY = True
        self.addCleanup(O.reset_gauge_cache)
        self._points = None

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    def points(self) -> list:
        """(指标名, 值, 标签)。⚠️ 读之前**不要** force_flush —— 它会先 collect 一次，
        把数据消费掉（照抄 test_pool_metrics 踩过的坑）。"""
        if self._points is None:
            data = self.reader.get_metrics_data()
            out = []
            for rm in (data.resource_metrics if data is not None else []):
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        for dp in m.data.data_points:
                            value = getattr(dp, "value", None)
                            if value is None:  # HistogramDataPoint 没有 .value
                                value = (getattr(dp, "sum", None), getattr(dp, "count", None))
                            out.append((m.name, value, dict(dp.attributes)))
            self._points = out
        return self._points

    def named(self, name: str) -> list:
        return [p for p in self.points() if p[0] == name]

    def test_every_query_is_counted_and_reported_splits_the_two_cases(self):
        """3 次同状态查询：1 条 span + 3 次计数（reported 把两类分开）。"""
        tid = self.post_task()
        for _ in range(3):
            self.get_task(tid)

        # ⚠️ 必须按 `source` 分开：后台盯梢那一路**每次都查上游**（它不看闸门），
        #    混在一起会把两件事加成一个数（本次就是这么踩到的：`reported=False` 多出 1）。
        all_rows = self.named("avm.task.poll_requests")
        polls = [p for p in all_rows if p[2].get("source") == "poll"]
        self.assertTrue(polls, "没采到轮询计数 —— 不产生 span 的查询必须仍然被计数")
        counted = {}
        for _name, value, attrs in polls:
            counted[attrs.get("reported")] = counted.get(attrs.get("reported"), 0) + value
        self.assertEqual(counted.get(True), 1, "首见那一次是留了 span 的")
        self.assertEqual(counted.get(False), 2, "重复两次只计数、不产 span")
        for _name, _value, attrs in all_rows:
            self.assertEqual(attrs.get("upstream"), "web")
            self.assertNotIn(
                "ark_id", attrs,
                "🔴 指标标签不许带任务 id（高基数会把时间序列打成一任务一条，比 span 还贵）",
            )
            if attrs.get("source") == "watcher":
                self.assertIs(attrs.get("reported"), False, "盯梢从不单独开 span（全程只有一条）")

    def test_terminal_transition_records_the_wait(self):
        self.site.set("model.getModel", site_task(taskStatus="processing"))
        tid = self.post_task()
        self.get_task(tid)
        self.site.set("model.getModel", site_task(taskStatus="succeed"))
        self.get_task(tid)
        self.get_task(tid)  # 同状态：不许再记一次

        # ⚠️ 必须**只取 poll 那一路**：`post_task` 会起盯梢线程，它也可能观测到终态并记一次
        #    （`source=watcher`）。本用例测的是**轮询路径**的跃迁记账；不筛 source 就等于
        #    依赖"谁先记"这个竞态（CI 2026-09-17 就是这样红的：读到了盯梢那一条）。
        waits = [r for r in self.named("avm.task.wait_seconds")
                 if r[2].get("source") == "poll"]
        self.assertTrue(waits, "轮询看到跃迁进终态必须记一次等待时长")
        _name, (total, count), attrs = waits[0]
        self.assertEqual(count, 1, "只记一次（同状态轮询不许重复计数）")
        self.assertGreaterEqual(total, 0.0)
        self.assertEqual(attrs.get("final_status"), "succeeded", "指标标签用归一化词")
        self.assertEqual(attrs.get("source"), "poll")


# ======================================================================== 3 出站 ----


class StagedWatchClient:
    """只实现 `wait_for_task` 的替身 —— 闸门 1 在盯梢这一侧关心的就是它。"""

    def __init__(self, statuses, *, boom=None):
        self.statuses = list(statuses)
        self.boom = boom
        self.polls = 0

    def wait_for_task(self, task_id, *, timeout=600.0, interval=10.0, on_poll=None):
        if self.boom is not None:
            raise self.boom
        for st in self.statuses:
            self.polls += 1
            if on_poll is not None:
                on_poll(st)
        return {"done": True, "ok": True, "status": self.statuses[-1], "task": {}, "ms": 1500}


class TestWatcherBudget(unittest.TestCase):
    """盯梢：**覆盖全程的一条 span**（片段里 `watch_task` 在本服务的对应物）。"""

    def setUp(self):
        self.exporter = TestExporter()
        configure_tracing(self.exporter)
        O._LOGFIRE_READY = True

    def tearDown(self):
        O._LOGFIRE_READY = False
        logfire.force_flush()

    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def watch(self, statuses, *, boom=None) -> WebSubmitQueue:
        q = WebSubmitQueue(StagedWatchClient(statuses, boom=boom), max_concurrent=1, upstream="web")
        q._running["t1"] = time.time()
        q._watch("t1")
        return q

    def test_five_polls_produce_exactly_one_span(self):
        """5 次轮询 = 1 条 span（改前是 5 条，每条还各自带着上游请求的原文）。"""
        self.watch(["queueing", "queueing", "processing", "processing", "succeed"])
        spans = self.spans("ark.task.watch")
        self.assertEqual(len(spans), 1, f"盯梢全程只许一条 span，实际 {len(spans)}")
        attrs = spans[0]["attributes"]
        self.assertEqual(attrs["task.poll_count"], 5)
        self.assertEqual(attrs["task.final_status"], "succeed")
        self.assertEqual(attrs["upstream"], "web")
        self.assertNotIn("error", attrs)

    def test_metric_labels_use_the_normalized_vocabulary(self):
        """🔴 **指标标签只有一个词汇表**：盯梢记的那一条也必须写 `succeeded`。

        回归门禁（2026-09-17 CI 红的那条）：盯梢曾直接写站点原词 `succeed`，而轮询路径
        （`app.py`）写归一化词 `succeeded` ⇒ 同一个指标被劈成两条时间序列，
        按 `succeeded` 过滤的看板/告警会**漏掉盯梢那一半**。
        span 属性保留原词（逐任务证据，可用来与站点记录对账），**指标标签必须归一化**。
        """
        recorded = []
        with mock.patch(
            "ark_compat.web_queue.record_wait",
            side_effect=lambda **kw: recorded.append(kw),
        ):
            self.watch(["queueing", "processing", "succeed"])
        self.assertTrue(recorded, "盯梢到终态要记一次等待时长")
        self.assertEqual(
            recorded[-1]["final_status"], "succeeded",
            "盯梢写成了站点原词？指标会被劈成两套词汇（按归一化词过滤的看板会漏掉一半）",
        )
        self.assertEqual(recorded[-1]["source"], "watcher")

    def test_status_transitions_are_recorded_as_events(self):
        """跃迁用**事件**：3 次跃迁（含首见）⇒ 3 个 `status_change`，而不是 5 条 span。"""
        self.watch(["queueing", "queueing", "processing", "processing", "succeed"])
        events = [
            e for e in self.spans("ark.task.watch")[0].get("events") or []
            if e["name"] == "status_change"
        ]
        self.assertEqual(
            [e["attributes"]["to"] for e in events], ["queueing", "processing", "succeed"]
        )
        self.assertEqual(events[1]["attributes"]["from"], "queueing")

    def test_a_broken_client_still_releases_the_slot(self):
        """🔴 底线：盯梢失败也必须放槽位，否则闸门永久卡死（实测过的故障模式）。

        同时证明"埋点坏掉不赔上业务"：span 里记得下错误，槽位照样还回去。
        """
        q = self.watch([], boom=RuntimeError("upstream exploded"))
        self.assertEqual(q.stats()["running"], 0, "盯梢失败后 running 必须清空")
        self.assertTrue(q._sem.acquire(blocking=False), "槽位没被释放 ⇒ 闸门会永久卡死")
        q._sem.release()
        attrs = self.spans("ark.task.watch")[0]["attributes"]
        self.assertIn("upstream exploded", attrs["error"])

    def test_a_broken_on_poll_callback_does_not_break_the_watch(self):
        """`on_poll` 抛异常不许影响盯梢 —— 埋点失败绝不能拖垮业务。

        走**真** `WebClient.wait_for_task`（替身只覆盖盯梢那层），所以这条同时证明了
        真客户端里那道 try/except 真的在。
        """
        site = FakeSite()
        site.set("model.getModel", site_task())
        client = make_client(site)

        def boom(_status):
            raise RuntimeError("观测层炸了")

        res = client.wait_for_task("t1", timeout=5.0, interval=0.01, on_poll=boom)
        self.assertTrue(res["done"], "回调失败不该改变盯梢结论")
        self.assertEqual(res["status"], "succeed")

    def test_each_poll_get_is_wrapped_in_suppress_http(self):
        """**接线**：轮询 GET 真的包在 `suppress_http()` 里（"实现了" != "接线了"）。"""
        site = FakeSite()
        site.set("model.getModel", site_task())
        client = make_client(site)
        entered: list = []

        @contextmanager
        def spy():
            entered.append(1)
            yield

        with mock.patch.object(web_client, "suppress_http", spy):
            res = client.wait_for_task("t1", timeout=5.0, interval=0.01)

        self.assertTrue(res["done"])
        self.assertEqual(len(entered), 1, "每次轮询都要包一层（这次恰好轮询一次就进终态）")


# ======================================================================== 4 日志 ----


class TestAccessLogQuieting(unittest.TestCase):
    """成功的轮询不上报 Logfire，**失败照报** —— "少上报"不等于"把事故现场一起静音"。"""

    def setUp(self):
        self.site = FakeSite()
        self.site.set("model.getModel", site_task())
        self.site.set("ai.minimaxH3", "t1")
        app = create_app(web_settings(gate_key=GATE))
        client = make_client(self.site)
        app.state.upstreams = {
            "web": WebUpstream(client, WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01))
        }
        self.client = TestClient(app)

        # ⚠️ 观察 sink **必须建在 app 之后**：`create_app` 的装配会 `logger.remove()`
        #    把先建的 sink 一起清掉（实测：sink 静默消失 ⇒ 断言全变成空跑）。
        #
        # ⚠️ 第二个坑（本次踩到）：`logger.add()` 的**第一个参数是 sink**（收 `Message`），
        #    而"要不要上报"要在 **filter** 里判（收 record 字典）。把判据函数直接当 sink
        #    挂上去 ⇒ `Message["extra"]` 抛 TypeError ⇒ loguru 只在 stderr 打一句
        #    `--- Logging error ---`，测试侧看到的是"一条都没采到"。
        self.seen: list = []

        def spy(record):
            keep = O.keep_off_logfire(record)  # 复用生产判定，不另写一份
            self.seen.append((record["extra"].get("avm_probe"), keep, record["message"]))
            return keep

        def deliver(_message):
            pass  # 判决记在 filter 里，这里不必再存一遍

        self.sink = logger.add(deliver, level="INFO", filter=spy)

    def tearDown(self):
        try:
            logger.remove(self.sink)
        except ValueError:  # 已被别人的装配清掉
            pass

    def flags_for(self, needle: str) -> list:
        return [flag for flag, _keep, msg in self.seen if needle in msg]

    def test_successful_poll_is_quiet_and_a_failure_is_not(self):
        headers = {"Authorization": f"Bearer {GATE}"}
        r = self.client.post(TASKS_PATH, json=ark_body(), headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]
        self.client.get(f"{TASKS_PATH}/{tid}", headers=headers)  # 成功的轮询
        self.client.get(f"{TASKS_PATH}/cgt-nope", headers=headers)  # 404

        poll_line = f"GET {TASKS_PATH}/{tid} ->"
        self.assertTrue(self.flags_for(poll_line), f"没采到轮询那条日志：{self.seen}")
        self.assertEqual(
            self.flags_for(poll_line), [True], "成功的轮询必须被标成不上报（avm_probe=True）"
        )
        # 对照：**失败照报**。静音掉它就等于"调用方看到的 404 在 Logfire 里不存在"。
        self.assertEqual(
            self.flags_for("-> 404"), [False], "失败的轮询必须仍上报（avm_probe=False）"
        )
        # 对照：创建路径不是轮询路径，任何状态都照常上报。
        self.assertEqual(self.flags_for(f"POST {TASKS_PATH} ->"), [False])


class TestPollPathTable(unittest.TestCase):
    """路径表：默认表、显式关掉、以及"非法条目告警剔除"（与探活表同构）。"""

    def tearDown(self):
        O.set_poll_paths(None)

    def test_default_table_covers_both_task_query_paths(self):
        self.assertTrue(O.is_poll_path(f"{TASKS_PATH}/cgt-1"))
        self.assertTrue(O.is_poll_path("/v1/videos/vid_1"))

    def test_create_path_is_not_a_poll_path(self):
        self.assertFalse(O.is_poll_path(TASKS_PATH), "创建是写操作、不是轮询，不许被摘掉")

    def test_subtree_semantics_does_not_swallow_siblings(self):
        self.assertFalse(O.is_poll_path("/api/v3/contents/generations/models"))
        self.assertFalse(O.is_poll_path("/healthz"))

    def test_explicit_off_yields_no_exclusions(self):
        O.set_poll_paths(())
        self.assertFalse(O.is_poll_path(f"{TASKS_PATH}/cgt-1"))
        self.assertEqual(O.poll_excluded_urls(), "")

    def test_settings_parse_off_and_blank(self):
        self.assertIsNone(
            Settings.from_env({}).logfire_poll_paths, "留空 = 用内置默认表（不是'不排除'）"
        )
        self.assertEqual(Settings.from_env({"AVM_LOGFIRE_POLL_PATHS": "-"}).logfire_poll_paths, ())
        self.assertEqual(
            Settings.from_env({"AVM_LOGFIRE_POLL_PATHS": "/a/,/b/"}).logfire_poll_paths,
            ("/a/", "/b/"),
        )

    def test_rejected_entries_are_dropped_and_warned(self):
        """非法条目（写成正则）不许静默放行 —— 那正是"配了不生效"的形状。"""
        msgs: list = []
        sink = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
        try:
            effective = O._apply_poll_paths(Settings.from_env({"AVM_LOGFIRE_POLL_PATHS": "/ok/,^/bad"}))
        finally:
            logger.remove(sink)
        self.assertEqual(effective, ("/ok/",), "非法条目不得进生效表")
        self.assertIn("LOGFIRE_POLL_PATHS", "\n".join(msgs), "被拒的条目必须出现在告警里")


if __name__ == "__main__":
    unittest.main()
