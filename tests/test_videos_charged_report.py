#!/usr/bin/env python3
"""`/v1/videos` 线**真的被扣了积分**必须上报 Logfire（该端点对外承诺恒免费）。

为什么值得单开一个门禁：这条上报是**跨模块的接线**（入口 → 任务记录 → 轮询观测 →
指标 / span / 日志），任何一段断掉，外在表现都是"什么都没发生"—— 业务照跑、响应照对、
既有测试全绿，只有钱在流走。所以本文件每条断言都瞄准"漏报 / 错报"这一侧的失效，
并且都**能被变异证伪**：

  ① 去掉入口判据（`entry.response_shape`）  ⇒ `test_the_ark_line_is_not_reported` 红
  ② 去掉 `paid` 判据                        ⇒ `test_a_free_videos_task_is_silent` 红
  ③ 去掉终态判据                            ⇒ `test_a_running_task_is_not_reported` 红
  ④ 去掉"这次要留痕"判据（`decision`）      ⇒ `test_repeat_polling_reports_once` 红
  ⑤ 把 `entry.get(...)` 写成 `entry[...]`   ⇒ `test_a_legacy_record_does_not_blow_up` 红
  ⑥ 往标签里塞任务 id                       ⇒ `test_labels_stay_low_cardinality` 红
  ⑦ 只报指标、不写日志                      ⇒ `test_a_charged_task_is_reported_...` 红

纪律：零外发（内存 exporter / reader）、零额度消耗（`FakeSite` 替身，不创建任何真实任务）。

运行：python3 -m unittest tests.test_videos_charged_report
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

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
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.openai_videos import OPENAI_VIDEOS_PATH  # noqa: E402
from ark_compat.upstreams import WebUpstream  # noqa: E402
from ark_compat.web_queue import WebSubmitQueue  # noqa: E402

# 复用 web 上游那套**真站点替身**，不另造一个
from test_web_upstream import (  # noqa: E402
    FakeSite,
    ark_body,
    make_client,
    web_settings,
)

GATE = "gate-secret-123"
METRIC = "avm.videos.charged_tasks"
LOG_NEEDLE = "/v1/videos 任务实际扣了积分"

# 站点侧任务记录（与 `test_poll_suppression` 同形）。`url` 必须有：站点会把没有产出
# URL 的 `succeed` 判成失败（`translate.normalize_web_task` 里那条自相矛盾状态的修正）。
SITE_TASK = {
    "id": "t1",
    "taskStatus": "succeed",
    "aiModel": "minimax-h3",
    "url": "https://cdn.test/a.mp4",
    "kelingKeyId": "480",
    "credits": 0,
    "paid": False,
}


def site_task(**kw) -> dict:
    rec = dict(SITE_TASK)
    rec.update(kw)
    return rec


class VideosChargeCase(unittest.TestCase):
    """真 app + 站点替身 + 内存 exporter / reader。

    ⚠️ 两个读取顺序陷阱（照抄 `test_poll_metrics` 踩过的坑）：
      1. 指标**同一个测试里只能读一次** —— `InMemoryMetricReader.get_metrics_data()`
         内部先 `collect()` 再交数据，读完即空 ⇒ 本文件里 `points()` 只调用一次、
         且一律放在**任何 `force_flush()` 之前**（flush 会先 collect 一遍，把数据消费掉，
         之后 span 断言照常、指标断言静默变成空集合）。
      2. 观察 sink 必须建在 `create_app` **之后** —— 装配会 `logger.remove()` 清掉先建的 sink。
    """

    def setUp(self):
        O.reset_gauge_cache()  # 换了 reader 之后必须重建指标对象
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
        self.site.set("ai.minimaxH3", "t1")
        self.site.set("model.getModel", site_task())
        # TTL=0：每次查询都真的回上游读一次，好让"运行中"与"终态"都能被观测到
        app = create_app(web_settings(gate_key=GATE, task_cache_ttl=0.0))
        client = make_client(self.site)
        app.state.upstreams = {
            "web": WebUpstream(client, WebSubmitQueue(client, max_concurrent=2, poll_interval=0.01))
        }
        self.app = app
        self.client = TestClient(app)
        self.seen: list = []
        self.sink = logger.add(lambda m: self.seen.append(m.record), level="ERROR")
        O._LOGFIRE_READY = True
        self._points = None
        self.addCleanup(self._teardown)

    def _teardown(self):
        try:
            logger.remove(self.sink)
        except ValueError:  # 已被别人的装配清掉
            pass
        O._LOGFIRE_READY = False
        O.reset_gauge_cache()
        logfire.force_flush()

    # ---- helpers ----
    def post_videos(self, **kw) -> str:
        """在 OpenAI 兼容面建一条任务（默认 480p ⇒ 被钉到 10s 的免费档）。"""
        fields = {"model": "minimaxH3_480p", "prompt": "a cat", "size": "16:9"}
        fields.update(kw)
        r = self.client.post(
            OPENAI_VIDEOS_PATH, json=fields, headers={"Authorization": f"Bearer {GATE}"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def post_ark(self) -> str:
        r = self.client.post(
            TASKS_PATH, json=ark_body(), headers={"Authorization": f"Bearer {GATE}"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def poll_videos(self, tid: str):
        return self.client.get(
            f"{OPENAI_VIDEOS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"}
        )

    def poll_ark(self, tid: str):
        return self.client.get(f"{TASKS_PATH}/{tid}", headers={"Authorization": f"Bearer {GATE}"})

    def points(self) -> list:
        """`(指标名, 值, 标签)`。**整个测试只读一次**（见类 docstring 的顺序陷阱）。

        ⚠️ 直方图的 data point 没有 `.value`（走 `sum` / `count`）—— 与
        `test_poll_metrics` 同一份处理，否则同一条断言会因为"顺带读了别的指标"而炸。
        """
        if self._points is None:
            data = self.reader.get_metrics_data()
            out = []
            for rm in (data.resource_metrics if data is not None else []):
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        for dp in m.data.data_points:
                            value = getattr(dp, "value", None)
                            if value is None:  # HistogramDataPoint 一族
                                value = (getattr(dp, "sum", None), getattr(dp, "count", None))
                            out.append((m.name, value, dict(dp.attributes)))
            self._points = out
        return self._points

    def charged(self) -> list:
        return [p for p in self.points() if p[0] == METRIC]

    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def charge_logs(self) -> list[str]:
        return [r["message"] for r in self.seen if LOG_NEEDLE in r["message"]]


class TestVideosChargeReported(VideosChargeCase):
    def test_a_charged_task_is_reported_on_all_three_channels(self):
        """★ 主断言：`/v1/videos` 的终态任务扣了积分 ⇒ 指标 + span 属性 + error 日志。

        三条出口各自服务一种读法，缺一条都能被这里逮住：指标给时间序列（告警用）、
        span 属性给 trace 过滤（复盘用）、日志给逐任务明细（含**不进标签**的任务 id）。
        """
        tid = self.post_videos()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_videos(tid).status_code, 200)

        rows = self.charged()  # ⚠️ 先读指标（见类 docstring）
        self.assertEqual(len(rows), 1, f"扣了积分必须上报指标：{self.points()}")
        _name, value, attrs = rows[0]
        self.assertEqual(value, 1)
        self.assertEqual(attrs["upstream"], "web")
        self.assertEqual(attrs["status"], "succeeded", "标签用归一化词（与轮询指标同一套词表）")
        self.assertEqual(attrs["resolution"], "480p")
        self.assertEqual(
            int(attrs["duration"]), 10,
            "480p 被钉到 10 秒 —— 这正是'该端点落在免费档'的那条依据，上报里必须看得见",
        )
        self.assertEqual(attrs["model_slot"], "minimaxH3")

        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertTrue(span_attrs["paid"], "计费事实照旧挂在 span 上")
        self.assertIs(
            span_attrs["videos_line_charged"], True,
            "trace 侧要能按这个属性把'契约破了'的任务筛出来",
        )

        msgs = self.charge_logs()
        self.assertEqual(len(msgs), 1, "必须有一条 error 级日志（Logfire 上按等级挂告警）")
        self.assertIn(tid, msgs[0], "日志里要能拿到任务 id（它**不**进指标标签）")
        self.assertIn("t1", msgs[0], "上游 taskId 同理")
        self.assertIn("480p", msgs[0])

    def test_a_free_task_is_silent(self):
        """对照组：免费的那条什么都不报 —— 否则上面那条断言可能是空跑。"""
        tid = self.post_videos()
        self.assertEqual(self.poll_videos(tid).status_code, 200)  # 站点记录 paid=False

        self.assertEqual(self.charged(), [], f"免费任务不该上报：{self.points()}")
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertNotIn("videos_line_charged", span_attrs, "没扣费就不该有这个属性")
        self.assertEqual(self.charge_logs(), [])

    def test_the_ark_line_is_not_reported(self):
        """🔴 **范围**：这条上报是 `/v1/videos` 专属的。

        方舟线上调用方可以**自己点** `tier=base` 或传超出免费窗口的时长 —— 那是它明示的
        选择（`billing_view` 已经为它发过告警）。把那些也报成"端点契约破了"，
        只会让这条告警被读成噪声，真正该看的那条就再也看不见了。
        """
        tid = self.post_ark()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_ark(tid).status_code, 200)

        self.assertEqual(
            self.charged(), [],
            "方舟线的扣费不属于'该端点承诺恒免费'那条告警的射程",
        )
        self.assertEqual(self.charge_logs(), [])
        # ★ 防**空跑**：先证明"同样的条件这次真的成立了，只是入口不同"——否则上面两条
        #   断言在"轮询根本没走到终态"时也会绿（那样测的就不是范围，而是接线断了）。
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertTrue(span_attrs["paid"], "方舟线这条确实观测到了 paid=true")
        self.assertEqual(span_attrs["status"], "succeeded")
        self.assertNotIn("videos_line_charged", span_attrs)

    def test_a_running_task_is_not_reported(self):
        """非终态不报：`paid` 在终态才定格，而终态必然是**一次**跃迁 ⇒ 每条任务最多报一次。

        （运行中就报的话，同一任务会在进终态时再报一遍 —— 计数与告警都会翻倍。）
        """
        self.site.set("model.getModel", site_task(taskStatus="processing", paid=True))
        tid = self.post_videos()
        self.assertEqual(self.poll_videos(tid).status_code, 200)

        self.assertEqual(self.charged(), [], f"运行中不许报：{self.points()}")
        self.assertEqual(self.charge_logs(), [])
        # ★ 防**空跑**：这次必须真的观测到了"运行中 + 已扣费"这一格，断言才有意义
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertEqual(span_attrs["status"], "running")
        self.assertTrue(span_attrs["paid"], "站点记录里已是 paid=true，只是还没进终态")

    def test_repeat_polling_reports_once(self):
        """同状态重复轮询**不重复上报** —— 判据是同一道产生层闸门（`PollReportGate`）。"""
        tid = self.post_videos()
        self.site.set("model.getModel", site_task(paid=True))
        for _ in range(3):
            self.assertEqual(self.poll_videos(tid).status_code, 200)

        self.assertEqual([p[1] for p in self.charged()], [1], "同状态重复轮询不许重复计数")
        self.assertEqual(len(self.charge_logs()), 1, "日志同理：一条任务一条")

    def test_a_legacy_record_does_not_blow_up(self):
        """老记录（入口标识落盘之前建的）没有 `response_shape` 键。

        取值必须用 `.get`：写成 `entry["response_shape"]` 会让**老任务**在本进程第一次
        被观测到的那一刻 500 —— 而新任务全绿，症状只在跑着老任务的实例上出现。
        """
        tid = self.post_videos()
        entry = self.app.state.tasks.get(tid)
        entry.pop("response_shape", None)
        self.app.state.tasks.put(entry)

        self.site.set("model.getModel", site_task(paid=True))
        r = self.poll_videos(tid)
        self.assertEqual(r.status_code, 200, f"老记录必须照常可查：{r.text}")
        self.assertEqual(self.charged(), [], "缺这个键 ⇒ 按'非 OpenAI 线'处理（不猜）")
        # ★ 防**空跑**：确认这条老记录真的走到了"终态 + 已扣费"那一格
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertTrue(span_attrs["paid"])
        self.assertEqual(span_attrs["status"], "succeeded")

    def test_labels_stay_low_cardinality(self):
        """🔴 指标标签**只放低基数枚举**：任务 id 一旦进标签，时间序列就废了。

        与 `test_poll_suppression` 里那条同款纪律 —— 一任务一条比 span 还贵，
        而且告警规则会变成"逐任务触发"。
        """
        tid = self.post_videos()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_videos(tid).status_code, 200)

        dumped = json.dumps(self.points(), ensure_ascii=False)
        self.assertIn(METRIC, dumped, "先确认这次真产生了指标（否则下面两条是空跑）")
        for forbidden in ("ark_id", "upstream_task_id", "task_id"):
            self.assertNotIn(forbidden, dumped, "指标标签不许带任务 id（高基数）")
        self.assertNotIn(tid, dumped, "任务 id 也不许作为标签值出现")

    def test_nothing_is_reported_when_logfire_is_down(self):
        """Logfire 没装配 ⇒ 零指标（no-op），但**日志照打**（本地 stderr 仍看得见）。"""
        O._LOGFIRE_READY = False
        tid = self.post_videos()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_videos(tid).status_code, 200)
        O._LOGFIRE_READY = True

        self.assertEqual(self.points(), [], "Logfire 没装配时不该产生任何指标")
        self.assertEqual(
            len(self.charge_logs()), 1,
            "上报通道断了，本地日志必须照旧把这条事实留下来",
        )


if __name__ == "__main__":
    unittest.main()
