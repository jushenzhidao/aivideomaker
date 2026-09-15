#!/usr/bin/env python3
"""探活路径（`/healthz`、`/`）**不进 Logfire 上报** —— span 与日志两样都要摘。

背景：探活是**被反复轮询**的（compose 的 HEALTHCHECK 每 30s 打一次 `/healthz`）。
改动前每次请求在 Logfire 上留**一条 span + 一条日志**，一天上千条噪声，把真实请求淹掉，
还白跑出站流量。

本文件钉五层 —— 少任何一层都会出现"看着关掉了、其实没关"的形状：

1. **正则语义**（`probe_excluded_urls`）。上游拿这串做 `re.search`（**子串**匹配），
   不是路径相等 ⇒ 手写 `"/"` 会命中**每一个** URL（任何 URL 都含 `/`），等于把全站追踪
   静默关掉。这里有一条**变异自证**：把错误写法也真的跑一遍，证明"锚定"不是风格问题。
2. **两个派生口一致**：`is_probe_path()`（日志侧）与 `probe_excluded_urls()`（span 侧）
   对同一张表必须给同一答案，否则会出现"span 摘了、日志没摘"的半关状态。
3. **接线**：`instrument_fastapi` 真的把 `excluded_urls` 传下去，**且装好的中间件**里
   携带的就是我们的串（改参数名 / 漏传都会红）—— 只断言"我传了个参数"不够。
4. **端到端 span**：真 app + 真 TestExporter。探活**零 span**，而 `/healthz/extra`、
   `/openapi.json` **照常有 span**（对照组排掉"整体压根没装上"这种假绿）。
5. **端到端日志**：`keep_off_logfire` 只是 logfire sink 上的 filter，`avm_probe` 得由
   请求中间件绑定 —— 光测 filter 函数是测不到"中间件没绑那个键"的。所以第 5 组用真 app
   打两个请求，断言探活那条**根本没到** logfire sink，而普通请求到了。

跑法：python3 -m unittest tests.test_logfire_probe_exclusion
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

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
from opentelemetry.util.http import parse_excluded_urls  # noqa: E402

from ark_compat import observability as O  # noqa: E402
from ark_compat.app import create_app  # noqa: E402
from test_web_upstream import web_settings  # noqa: E402

# 与 asgi 中间件喂进 `url_disabled()` 的形态一致：`scheme://host + scope["path"]`
# （**不含 query**，见 `opentelemetry.instrumentation.asgi.get_host_port_url_tuple`）。
HOST = "http://host"
PROBE_URLS = ("http://host/healthz", "http://host/", "http://127.0.0.1:8899/")
NON_PROBE_URLS = (
    "http://host/healthz/extra",  # 未锚定就会连带吃掉它
    "http://host/healthzz",
    "http://host/openapi.json",
    "http://host/docs",
    "http://host/api/v3/contents/generations/tasks",
    "http://host//",
)


def _find_otel_middleware(stack):
    """顺着 `.app` 链找装好的 OpenTelemetryMiddleware（它会带 `excluded_urls`）。

    上游是在 `build_middleware_stack` 里注进去的（不是 `add_middleware`），所以
    `app.user_middleware` 里**看不到**它 —— 只能从建好的栈上走。
    """
    node, hops = stack, 16
    while node is not None and hops:
        if hasattr(node, "excluded_urls"):
            return node
        node = getattr(node, "app", None)
        hops -= 1
    return None


class TestPatternSemantics(unittest.TestCase):
    """第 1 层：正则语义。用上游**同一个** `ExcludeList` 实现判定，不自己重写匹配。"""

    def _excluded(self):
        return parse_excluded_urls(O.probe_excluded_urls())

    def test_probe_urls_are_disabled(self):
        for url in PROBE_URLS:
            self.assertTrue(self._excluded().url_disabled(url), f"{url} 必须被排除")

    def test_non_probe_urls_are_untouched(self):
        for url in NON_PROBE_URLS:
            self.assertFalse(self._excluded().url_disabled(url), f"{url} 不该被排除")

    def test_a_bare_root_pattern_would_kill_all_tracing(self):
        """变异自证：手写 `"/"`（或直接 join `PROBE_PATHS`）会关掉**全站**追踪。

        把错误写法也真跑一遍，是为了证明"锚定"不是风格偏好 —— 它决定的是
        "全站追踪开还是关"。谁把 `probe_excluded_urls` 退化成 `",".join(PROBE_PATHS)`，
        下面 `test_non_probe_urls_are_untouched` 会立刻红。
        """
        naive = parse_excluded_urls(",".join(O.PROBE_PATHS))
        for url in NON_PROBE_URLS:
            self.assertTrue(
                naive.url_disabled(url),
                "朴素写法本该把业务路径也命中 —— 若这条不成立，说明上游匹配语义变了，"
                "本文件的锚定理由需要重新论证",
            )
        self.assertFalse(self._excluded().url_disabled("http://host/openapi.json"))

    def test_query_string_is_not_part_of_the_matched_url(self):
        """探活常带 `?deep=1`：otel 只拿 `scope["path"]` 拼 URL ⇒ 带不带 query 命中同一条。

        所以这里用**不含 query** 的形态断言，和 `is_probe_path(request.url.path)` 同源。
        """
        self.assertTrue(self._excluded().url_disabled("http://host/healthz"))
        self.assertFalse(
            self._excluded().url_disabled("http://host/healthz?deep=1"),
            "带 query 的串根本不会出现在判定入口；若它能命中，说明上游改成匹配完整 URL 了",
        )


class TestDerivedPredicatesAgree(unittest.TestCase):
    """第 2 层：两个派生口（span 侧正则 / 日志侧谓词）必须给同一答案。"""

    def test_predicate_matches_regex_for_the_whole_table(self):
        excluded = parse_excluded_urls(O.probe_excluded_urls())
        paths = ("/healthz", "/", "/healthz/extra", "/healthzz", "/openapi.json", "/docs", "/redoc", "//")
        for path in paths:
            self.assertEqual(
                O.is_probe_path(path),
                excluded.url_disabled(f"http://host{path}"),
                f"{path}：is_probe_path 与 excluded_urls 判定不一致 ⇒ 会出现半关状态",
            )

    def test_probe_paths_are_exactly_the_documented_two(self):
        self.assertEqual(O.PROBE_PATHS, ("/healthz", "/"))


class TestWiring(unittest.TestCase):
    """第 3 层之一：调用点真的把排除传下去了（改名 / 漏传在这里红）。"""

    def test_instrument_fastapi_passes_exclusions_and_keeps_headers_off(self):
        app = create_app(web_settings())
        seen: dict = {}
        with mock.patch.object(logfire, "instrument_fastapi", lambda a, **kw: seen.update(kw, app=a)):
            O.instrument_fastapi(app)
        self.assertEqual(seen["excluded_urls"], O.probe_excluded_urls())
        self.assertIs(seen["capture_headers"], False, "抓头必须仍然关着（凭证头不入 trace）")
        self.assertIs(seen["app"], app)

    def test_installed_middleware_carries_our_exclusions(self):
        """第 3 层之二：**装好的** ASGI 中间件里的排除串就是我们的。

        只验"我们传了个 kwarg"不够 —— 中间那一跳（logfire → otel instrumentor → 中间件）
        任何一处把参数丢掉，请求就照旧成 span。
        """
        logfire.configure(
            send_to_logfire=False,
            console=False,
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(),
                ns_timestamp_generator=TimeGenerator(),
            ),
        )
        app = create_app(web_settings())
        O.instrument_fastapi(app)
        stack = app.build_middleware_stack()
        mw = _find_otel_middleware(stack)
        self.assertIsNotNone(
            mw,
            "建好的中间件栈里没有带 `excluded_urls` 的中间件：上游换了挂载方式，本测试要跟着改",
        )
        for url in PROBE_URLS:
            self.assertTrue(mw.excluded_urls.url_disabled(url), f"{url} 应在装好的中间件里被排除")
        for url in NON_PROBE_URLS:
            self.assertFalse(mw.excluded_urls.url_disabled(url), f"{url} 不该被排除")


class TestRealAppSpans(unittest.TestCase):
    """第 4 层：真 app + 真 exporter。探活零 span，对照组照常有 span。"""

    def setUp(self):
        self.exporter = TestExporter()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            advanced=logfire.AdvancedOptions(
                id_generator=IncrementalIdGenerator(),
                ns_timestamp_generator=TimeGenerator(),
            ),
            additional_span_processors=[SimpleSpanProcessor(self.exporter)],
        )
        app = create_app(web_settings())
        # 生产路径就是这一句（`create_app` 里 logfire_ok 时调的同一个函数）
        O.instrument_fastapi(app)
        self.client = TestClient(app)

    def tearDown(self):
        logfire.force_flush()

    def topics(self) -> list:
        """本轮请求产生的 span 的 `http.target`（ASGI span 带这个属性）。"""
        logfire.force_flush()
        return [s["attributes"].get("http.target") for s in self.exporter.exported_spans_as_dict()]

    def test_probe_requests_produce_no_span(self):
        self.client.get("/healthz")
        self.client.get("/")
        self.assertEqual(self.topics(), [], "探活请求不该产生任何 span")

    def test_non_probe_requests_still_produce_spans(self):
        """对照组：排除是**精确**的，不是"把追踪整体关了"。"""
        for path in ("/healthz/extra", "/openapi.json"):
            self.exporter.clear()
            self.client.get(path)
            self.assertIn(path, self.topics(), f"{path} 的 span 不该被顺手摘掉")


class TestRealAppLogs(unittest.TestCase):
    """第 5 层：探活的日志**根本没到** logfire sink（本地 sink 不受影响）。

    做法：把 `logfire.loguru_handler()` 换成一个收集器，走**真** `setup_observability`
    —— 于是断言的是"生产那行 `logger.add(..., filter=keep_off_logfire)` 装上了没"，而不是
    "filter 函数自己长什么样"。
    """

    def test_probe_log_line_never_reaches_the_logfire_sink(self):
        # ⚠️ `log_level` 必须是 INFO：`web_settings()` 默认 WARNING，请求日志是 INFO，
        #    那样生产那条 sink 一条都收不到 —— 对照组会变成"我自己的观察 sink"（假绿）。
        # ⚠️ `create_app` 这一步会 logger.remove()：sink 必须在它之后装。
        app = create_app(web_settings(log_level="INFO"))
        # 🔴 两个收集器**必须分开**：曾经让同一个兼任"logfire sink"与"观察 sink"，
        #    结果生产侧 filter 被删掉也照样绿 —— 自己的 filter 把那条又滤了一遍（假绿）。
        delivered: list = []  # 生产那条 logfire sink 的去处
        observed: list = []  # 我自己的观察 sink（只为了看判决，不参与断言）
        seen: list = []

        def spy(record):
            keep = O.keep_off_logfire(record)  # 复用生产判定，不另写一份
            seen.append((record["extra"].get("avm_probe"), keep, record["message"]))
            return keep

        saved = (O._LOGFIRE_READY, O._LOGFIRE_EXPORTING, O._MAX_ATTR_CHARS)
        O._LOGFIRE_READY = False  # 绕开"重复装配直接 return True"的短路
        try:
            with mock.patch.object(logfire, "loguru_handler", lambda: delivered.append), mock.patch.object(
                logfire, "configure", lambda **kw: None
            ), mock.patch.object(logfire, "instrument_httpx", lambda **kw: None):
                O.setup_observability(web_settings(enable_logfire=True, log_level="INFO"))
                logger.add(observed.append, level="INFO", filter=spy)
                client = TestClient(app)
                client.get("/healthz")
                client.get("/openapi.json")
        finally:
            O._LOGFIRE_READY, O._LOGFIRE_EXPORTING, O._MAX_ATTR_CHARS = saved

        joined = "\n".join(str(x) for x in delivered)
        self.assertIn("GET /openapi.json", joined, "对照组：普通请求必须仍然上报（否则等于把出口整体关了）")
        self.assertNotIn(
            "GET /healthz",
            joined,
            "探活请求的日志进了 logfire sink ⇒ 要么生产侧没挂 filter，要么中间件没绑 `avm_probe`",
        )

        probe = [row for row in seen if row[2].startswith("GET /healthz")]
        self.assertEqual(len(probe), 1, f"应当采样到恰好一条探活日志，实际 {seen}")
        flag, keep, _msg = probe[0]
        self.assertIs(flag, True, "中间件必须给探活请求绑 `avm_probe=True`（不绑就永远放行）")
        self.assertFalse(keep)

    def test_filter_keeps_everything_without_the_flag(self):
        """请求之外的日志没有 `avm_probe` 键 —— 必须照常放行（别把全部日志静音）。"""
        self.assertTrue(O.keep_off_logfire({"extra": {}}))
        self.assertTrue(O.keep_off_logfire({"extra": {"avm_probe": False, "request_id": "x"}}))
        self.assertFalse(O.keep_off_logfire({"extra": {"avm_probe": True}}))


if __name__ == "__main__":
    unittest.main()
