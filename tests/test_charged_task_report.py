#!/usr/bin/env python3
"""任务**实际被扣了积分**必须上报 Logfire —— 两条线都报，但必须能分开读。

用户口径（2026-09-20）：
  · `/v1/videos` 上的扣费要报（该端点按分辨率把时长钉在免费档、对外承诺恒不花钱）；
  · **方舟线 `/tasks` 上的扣费也要报**（"方舟线 /tasks 的扣费也报"）。

两条线的"正常程度"不同，所以判定统一、读法分开：判据只有 `paid`（站点终态记录的
`usage.paid`），**入口只作为标签** `api`；再配一个 `expected_billed`（创建时
`effective.billed` 的预测）把"明示的选择"与"口径不符"分开：

| 情形 | `api` | `expected_billed` | 日志等级 |
|---|---|---|---|
| `/v1/videos` 的钉死档真扣费 | `openai` | `false` | **error**（对外承诺的破口）|
| 方舟线：预测免费、实际扣了 | `ark` | `false` | **error**（计费口径与预测不符）|
| 方舟线：`tier=base` / 超免费窗口 | `ark` | `true` | info（调用方自己点的档，记账）|

为什么单开一个门禁：这是**跨模块接线**（入口 → 任务记录 → 轮询观测 → 指标/日志/span），
任何一段断掉，外在表现都是"什么都没发生"—— 业务照跑、响应照对、既有测试全绿，只有钱在流走。
每条断言都瞄准"漏报 / 错报"这一侧的失效，并且都**能被变异证伪**：

  ① 去掉 `paid` 判据                    ⇒ `test_a_free_task_is_silent` 红
  ② 去掉终态判据                        ⇒ `test_a_running_task_is_not_reported` 红
  ③ 去掉"这次要留痕"判据（`decision`）  ⇒ `test_repeat_polling_reports_once` 红
  ④ 把 `entry.get(...)` 写成 `entry[...]` ⇒ `test_a_legacy_record_...` 红（老记录 500）
  ⑤ 标签里塞任务 id（键或值）           ⇒ `test_labels_stay_low_cardinality` 红
  ⑥ 日志降到 debug                      ⇒ 报错那两条用例红
  ⑦ `expected_billed` 恒判 true         ⇒ `test_ark_unpredicted_charge_...` 红（口径不符被吞）
  ⑧ `logger.info` 改成 `logger.error`   ⇒ `test_ark_expected_charge_...` 红（预期扣费被当故障）
  ⑨ `api` 标签写死 `openai`             ⇒ 方舟线三条用例红

纪律：零外发（内存 exporter / reader）、零额度消耗（`FakeSite` 替身，不创建任何真实任务）。

运行：python3 -m unittest tests.test_charged_task_report
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
METRIC = "avm.billing.charged_tasks"
NEEDLE = "任务实际扣了积分"

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


class ChargedTaskCase(unittest.TestCase):
    """真 app + 站点替身 + 内存 exporter / reader。

    ⚠️ 三个读取顺序陷阱（前两个照抄 `test_poll_metrics` 踩过的坑）：
      1. 指标**同一个测试里只能读一次** —— `InMemoryMetricReader.get_metrics_data()` 内部先
         `collect()` 再交数据，读完即空 ⇒ 本文件里 `points()` 只调用一次、且一律放在
         **任何 `force_flush()` 之前**（flush 会先 collect 一遍，把数据消费掉）。
      2. 观察 sink 必须建在 `create_app` **之后** —— 装配会 `logger.remove()` 清掉先建的 sink。
      3. sink 的 `level` 必须是 `INFO`：这条上报里**预期扣费走 info**，挂在 ERROR 上的 sink
         会把它们全漏掉，而那正是"只测一半"的经典假绿。
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
        self.sink = logger.add(lambda m: self.seen.append(m.record), level="INFO")
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

    def post_ark(self, **kw) -> str:
        r = self.client.post(
            TASKS_PATH, json=ark_body(**kw), headers={"Authorization": f"Bearer {GATE}"}
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

        ⚠️ 直方图的 data point 没有 `.value`（走 `sum` / `count`）—— 与 `test_poll_metrics`
        同一份处理，否则同一条断言会因为"顺带读了别的指标"而炸。
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

    def only_charge(self) -> tuple:
        """唯一那条指标 —— 顺带把"到底报了几条"钉住（漏报与重复报都要红）。"""
        rows = self.charged()
        self.assertEqual(len(rows), 1, f"应当恰好上报一条：{self.points()}")
        return rows[0]

    def spans(self, name: str) -> list:
        logfire.force_flush()
        return [s for s in self.exporter.exported_spans_as_dict() if s["name"] == name]

    def charge_logs(self) -> list:
        """`[(等级, 报文)]` —— 只挑这条上报的日志（别的 INFO 日志很多，不筛会误判）。"""
        return [
            (r["level"].name, r["message"])
            for r in self.seen
            if NEEDLE in str(r["message"])
        ]


class TestVideosLine(ChargedTaskCase):
    """OpenAI 兼容面：该端点钉死免费档 ⇒ 钉死档真扣费 = 对外承诺的破口（error 档）。"""

    def test_a_pinned_charge_is_reported_as_an_error(self):
        """★ 主断言：`/v1/videos` 的钉死档（480p）扣了积分 ⇒ 指标 + span + error 日志。"""
        tid = self.post_videos()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_videos(tid).status_code, 200)

        _name, value, attrs = self.only_charge()  # ⚠️ 先读指标
        self.assertEqual(value, 1)
        self.assertEqual(attrs["api"], "openai", "要能把这条线的钱单独拎出来看")
        self.assertIs(attrs["expected_billed"], False, "创建时预测免费 —— 这正是'口径不符'那一格")
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
        self.assertIs(span_attrs["billing_charged"], True, "trace 侧要能把扣了钱的任务筛出来")
        self.assertIs(span_attrs["billing_expected"], False)

        logs = self.charge_logs()
        self.assertEqual([lv for lv, _ in logs], ["ERROR"], f"钉死档扣费必须 error 档：{logs}")
        self.assertIn(tid, logs[0][1], "日志里要能拿到任务 id（它**不**进指标标签）")
        self.assertIn("t1", logs[0][1], "上游 taskId 同理")
        self.assertIn("480p", logs[0][1])

    def test_a_1080p_request_is_still_inside_the_free_tier(self):
        """对偶：`1080p` **也被收进免费档**（降级成 720p、钉到 8s）⇒ 它的扣费同样是 `error`。

        ⚠️ 旧版这条断言的是"1080p 原值透传 ⇒ 属于**预告过**的计费（info 档）"。2026-09-20
        本面改成"只跑免费线"之后，那个前提没了（降级 + 钉死 ⇒ 预测恒为免费），于是本用例
        换成钉住**新不变量**：本面上不存在"预告过的计费"，凡扣费必是口径不符。
        """
        tid = self.post_videos(model="minimaxH3_1080p", seconds=15)
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_videos(tid).status_code, 200)

        _name, _value, attrs = self.only_charge()
        self.assertEqual(attrs["api"], "openai")
        self.assertEqual(attrs["resolution"], "720p", "1080p 已被降级到免费档内的分辨率")
        self.assertEqual(int(attrs["duration"]), 8, "并按 720p 钉在免费区最长档")
        self.assertIs(attrs["expected_billed"], False, "本面所有请求预测都是免费 ⇒ 扣费即异常")

        logs = self.charge_logs()
        self.assertEqual([lv for lv, _ in logs], ["ERROR"], f"本面不存在'预告过的计费'：{logs}")


class TestArkLine(ChargedTaskCase):
    """方舟线：判定与上面**完全一致**，差别只在 `expected_billed` 决定日志等级。

    为什么必须分开：这条线上调用方可以自己点 `tier=base`、也可以传超出免费窗口的时长 ——
    那是它明示的选择（`billing_view` 当时就告警过）。把那些报成"异常"，真异常就再也看不见了。
    """

    def test_an_expected_charge_is_logged_as_info(self):
        """`duration=15`（超免费窗口）⇒ 创建时预测会计费 ⇒ info 档 + `expected_billed=true`。"""
        tid = self.post_ark(duration=15)
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_ark(tid).status_code, 200)

        _name, _value, attrs = self.only_charge()
        self.assertEqual(attrs["api"], "ark", "方舟线的钱必须与 OpenAI 面的分开")
        self.assertIs(attrs["expected_billed"], True)
        self.assertEqual(int(attrs["duration"]), 15)

        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertIs(span_attrs["billing_charged"], True)
        self.assertIs(span_attrs["billing_expected"], True)

        logs = self.charge_logs()
        self.assertEqual([lv for lv, _ in logs], ["INFO"], f"明示的选择不是故障：{logs}")
        self.assertIn("已预告会计费", logs[0][1])
        self.assertIn("dur=15s", logs[0][1])

    def test_an_unpredicted_charge_is_an_error(self):
        """★ 方舟线真正的异常：`duration=5`（创建时预测免费）却真扣了积分 ⇒ error 档。

        这一格是整条告警的价值所在 —— 站点收窄免费线、模型档位与假定不符，都只会在这里露头。
        """
        tid = self.post_ark()  # 默认 duration=5 ⇒ billed=false
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_ark(tid).status_code, 200)

        _name, _value, attrs = self.only_charge()
        self.assertEqual(attrs["api"], "ark")
        self.assertIs(attrs["expected_billed"], False, "预测免费却扣了 —— 这一格才是异常")

        logs = self.charge_logs()
        self.assertEqual([lv for lv, _ in logs], ["ERROR"], f"口径不符必须 error 档：{logs}")
        self.assertIn("创建时预测是免费的", logs[0][1])


class TestJudgement(ChargedTaskCase):
    """判据本身：不该报的一律不报，该报的只报一次。"""

    def test_a_free_task_is_silent(self):
        """对照组：没扣钱的什么都不报 —— 否则上面那些断言可能是空跑。"""
        tid = self.post_ark()
        self.assertEqual(self.poll_ark(tid).status_code, 200)  # 站点记录 paid=False

        self.assertEqual(self.charged(), [], f"免费任务不该上报：{self.points()}")
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertNotIn("billing_charged", span_attrs, "没扣费就不该有这个属性")
        self.assertEqual(self.charge_logs(), [])

    def test_a_running_task_is_not_reported(self):
        """非终态不报：`paid` 在终态才定格，而终态必然是**一次**跃迁 ⇒ 每条任务最多报一次。"""
        self.site.set("model.getModel", site_task(taskStatus="processing", paid=True))
        tid = self.post_ark()
        self.assertEqual(self.poll_ark(tid).status_code, 200)

        self.assertEqual(self.charged(), [], f"运行中不许报：{self.points()}")
        self.assertEqual(self.charge_logs(), [])
        # ★ 防**空跑**：这次必须真的观测到了"运行中 + 已扣费"这一格，断言才有意义
        span_attrs = self.spans("ark.task.fetch")[-1]["attributes"]
        self.assertEqual(span_attrs["status"], "running")
        self.assertTrue(span_attrs["paid"], "站点记录里已是 paid=true，只是还没进终态")

    def test_repeat_polling_reports_once(self):
        """同状态重复轮询**不重复上报** —— 判据是同一道产生层闸门（`PollReportGate`）。"""
        tid = self.post_ark()
        self.site.set("model.getModel", site_task(paid=True))
        for _ in range(3):
            self.assertEqual(self.poll_ark(tid).status_code, 200)

        self.assertEqual([p[1] for p in self.charged()], [1], "同状态重复轮询不许重复计数")
        self.assertEqual(len(self.charge_logs()), 1, "日志同理：一条任务一条")

    def test_a_legacy_record_is_reported_with_an_unknown_api(self):
        """老记录（入口标识落盘之前建的）没有 `response_shape` 键。

        判据里**不再**拿入口做门槛（两条线都报）⇒ 老记录照报，只是 `api` 标成 `unknown`；
        但取值必须用 `.get`：写成 `entry["response_shape"]` 会让老任务在第一次被观测时 500，
        而新任务全绿 —— 症状只在跑着老任务的实例上出现。
        """
        tid = self.post_ark()
        entry = self.app.state.tasks.get(tid)
        entry.pop("response_shape", None)
        self.app.state.tasks.put(entry)

        self.site.set("model.getModel", site_task(paid=True))
        r = self.poll_ark(tid)
        self.assertEqual(r.status_code, 200, f"老记录必须照常可查：{r.text}")

        _name, _value, attrs = self.only_charge()
        self.assertEqual(attrs["api"], "unknown", "缺这个键就说不出来源 —— 不许猜成某一条线")

    def test_labels_stay_low_cardinality(self):
        """🔴 指标标签**只放低基数枚举**：任务 id 一旦进标签，时间序列就废了。

        与 `test_poll_suppression` 里那条同款纪律 —— 一任务一条比 span 还贵，
        而且告警规则会变成"逐任务触发"。
        """
        tid = self.post_ark()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_ark(tid).status_code, 200)

        dumped = json.dumps(self.points(), ensure_ascii=False)
        self.assertIn(METRIC, dumped, "先确认这次真产生了指标（否则下面两条是空跑）")
        for forbidden in ("ark_id", "upstream_task_id", "task_id"):
            self.assertNotIn(forbidden, dumped, "指标标签不许带任务 id（高基数）")
        self.assertNotIn(tid, dumped, "任务 id 也不许作为标签值出现")

    def test_nothing_is_reported_when_logfire_is_down(self):
        """Logfire 没装配 ⇒ 零指标（no-op），但**日志照打**（本地 stderr 仍看得见）。"""
        O._LOGFIRE_READY = False
        tid = self.post_ark()
        self.site.set("model.getModel", site_task(paid=True))
        self.assertEqual(self.poll_ark(tid).status_code, 200)
        O._LOGFIRE_READY = True

        self.assertEqual(self.points(), [], "Logfire 没装配时不该产生任何指标")
        self.assertEqual(
            len(self.charge_logs()), 1,
            "上报通道断了，本地日志必须照旧把这条事实留下来",
        )


if __name__ == "__main__":
    unittest.main()
