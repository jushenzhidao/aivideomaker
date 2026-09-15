#!/usr/bin/env python3
"""探活与公开文档端点**不进 Logfire 上报** —— span 与日志两样都摘，且名单可配。

背景：容器 HEALTHCHECK 每 30s 打一次 `/healthz`，`/docs` 那一族不鉴权、会被扫描。
改动前每次请求在 Logfire 上留**一条 span + 一条日志**，一天上千条噪声，把真实请求淹掉，
还白跑出站流量。

**生效名单**（默认表 + 可配）：
  · 默认 = `observability.DEFAULT_PROBE_PATHS`（`/healthz`、`/` 加交互式文档三条）
  · 覆盖 = `AVM_LOGFIRE_EXCLUDED_PATHS`（逗号分隔的**路径**；留空 = 默认表；`-` = 不排除）
  · 语义 = `_path_matches()`：普通路径**精确**，以 `/` 结尾的按**子树**
  · 正则 = `_regex_for()` 同构生成 —— 使用方**永远不写正则**

本文件钉七层 —— 少任何一层都会出现"看着关掉了、其实没关"或"配了不生效"的形状：

1. **默认表**就是那五条，且边界（`/healthzz`、`/healthz/extra`、`/docsx`）不被误吃。
2. **正则语义**：上游拿 `excluded_urls` 做 `re.search`（**子串**匹配）。这里有一条
   **变异自证** —— 把错误写法（直接 join 路径、或不加尾部锚定）也真跑一遍，证明"锚定"
   决定的是"全站追踪开还是关"。
3. **两个派生口一致**：`is_probe_path()`（日志侧）与 `probe_excluded_urls()`（span 侧）
   对任何一张表都必须给同一答案，否则会出现"span 摘了、日志没摘"的半关状态。
   `/` 这条尤其关键：它必须只匹配根路径，**不能**按子树解释（那等于关掉全站）。
4. **配置面**（env → settings → 生效表）：留空/`-`/带空格/非法条目各自的行为，以及
   "非法条目**告警剔除**而不是静默忽略"。
5. **接线**：`instrument_fastapi` 真的把排除传下去，**且装好的中间件**里携带的就是我们的串。
6. **端到端 span**：真 app + 真 TestExporter。默认表下探活/文档端点**零 span**、边界路径
   **照常有 span**；换成自定义表后**真的换掉**（`/healthzz` 被摘、`/healthz` 反而有 span）。
7. **端到端日志**：`keep_off_logfire` 只是 logfire sink 上的 filter，`avm_probe` 得由请求
   中间件绑定 —— 光测 filter 函数测不到"中间件没绑那个键"。

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
from ark_compat.settings import Settings  # noqa: E402
from test_web_upstream import web_settings  # noqa: E402

# 与 asgi 中间件喂进 `url_disabled()` 的形态一致：`scheme://host + scope["path"]`
# （**不含 query**，见 `opentelemetry.instrumentation.asgi.get_host_port_url_tuple`）。
HOST = "http://host"

# 默认表的正例 / 反例（反例全是"差一点就被误吃"的边界）
DEFAULT_PROBES = ("/healthz", "/", "/docs", "/redoc", "/openapi.json")
DEFAULT_BOUNDARIES = (
    "/healthz/extra",  # 未锚定就会被连带吃掉
    "/healthzz",
    "/docsx",
    "/redocx",
    "/openapi.jsonx",
    "/api/v3/contents/generations/tasks",
    "//",
)


def excluded_for(paths=None):
    """用上游**同一个** `ExcludeList` 判定当前（或指定）表 —— 不自己重写匹配。"""
    O.set_probe_paths(paths)
    return parse_excluded_urls(O.probe_excluded_urls())


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


class TestDefaultTable(unittest.TestCase):
    """第 1 层：默认表就是那五条，边界不被误吃。"""

    def setUp(self):
        O.set_probe_paths(None)

    def test_default_table_is_exactly_the_documented_five(self):
        self.assertEqual(O.DEFAULT_PROBE_PATHS, DEFAULT_PROBES)
        self.assertEqual(O.probe_paths(), DEFAULT_PROBES)

    def test_boundary_paths_are_not_swallowed(self):
        excluded = excluded_for(None)
        for path in DEFAULT_BOUNDARIES:
            self.assertFalse(
                excluded.url_disabled(f"{HOST}{path}"),
                f"{path} 被排除了 —— 排除必须是精确/子树语义，不能退化成前缀或子串",
            )
            self.assertFalse(O.is_probe_path(path), f"{path} 不该被判为排除路径")


class TestPatternSemantics(unittest.TestCase):
    """第 2 层：正则语义（上游是 `re.search` 子串匹配，不是路径相等）。"""

    def test_probe_urls_are_disabled(self):
        excluded = excluded_for(None)
        for path in DEFAULT_PROBES:
            self.assertTrue(excluded.url_disabled(f"{HOST}{path}"), f"{path} 必须被排除")

    def test_a_bare_root_pattern_would_kill_all_tracing(self):
        """变异自证：手写 `"/"`（或直接 join 路径）会关掉**全站**追踪。

        把错误写法也真跑一遍，是为了证明"锚定"不是风格偏好 —— 它决定的是"全站追踪
        开还是关"。谁把 `_regex_for()` 退化成 `host + re.escape(p)`（去掉尾部锚定），
        下面 `test_boundary_paths_are_not_swallowed` 与本类都会立刻红。
        """
        naive = parse_excluded_urls(",".join(DEFAULT_PROBES))
        for path in DEFAULT_BOUNDARIES:
            self.assertTrue(
                naive.url_disabled(f"{HOST}{path}"),
                "朴素写法本该把业务路径也命中 —— 若这条不成立，说明上游匹配语义变了，"
                "本文件的锚定理由需要重新论证",
            )
        self.assertFalse(excluded_for(None).url_disabled(f"{HOST}/api/v3/x"))

    def test_query_string_is_not_part_of_the_matched_url(self):
        """探活常带 `?deep=1`：上游只拿 `scope["path"]` 拼 URL ⇒ 带不带 query 命中同一条。"""
        excluded = excluded_for(None)
        self.assertTrue(excluded.url_disabled(f"{HOST}/healthz"))
        self.assertFalse(
            self._excluded().url_disabled("http://host/healthz?deep=1"),
            "带 query 的串根本不会出现在判定入口；若它能命中，说明上游改成匹配完整 URL 了",
        )


class TestPathSemantics(unittest.TestCase):
    """第 3 层之一：`_path_matches` 的语义（精确 / 子树 / 根路径特例）。"""

    def tearDown(self):
        O.set_probe_paths(None)

    def test_exact_paths_match_only_themselves(self):
        O.set_probe_paths(("/internal",))
        self.assertTrue(O.is_probe_path("/internal"))
        self.assertFalse(O.is_probe_path("/internal/x"))
        self.assertFalse(O.is_probe_path("/internalx"))

    def test_trailing_slash_means_subtree(self):
        O.set_probe_paths(("/internal/",))
        self.assertTrue(O.is_probe_path("/internal/"))
        self.assertTrue(O.is_probe_path("/internal/x/y"))
        self.assertFalse(O.is_probe_path("/internal"))  # 子树不含"父路径本身"
        self.assertFalse(O.is_probe_path("/internalx"))

    def test_root_path_matches_only_the_root(self):
        """`/` 若按"子树"解释就等于**整个站** —— 这条钉住那个差别。"""
        O.set_probe_paths(("/",))
        self.assertTrue(O.is_probe_path("/"))
        for path in ("/healthz", "/api/v3/x", "/docs"):
            self.assertFalse(O.is_probe_path(path), f"{path} 不该因为表里有 `/` 就被吃掉")
        self.assertFalse(excluded_for(("/",)).url_disabled(f"{HOST}/healthz"))

    def test_empty_table_excludes_nothing(self):
        O.set_probe_paths(())
        self.assertEqual(O.probe_excluded_urls(), "")
        for path in DEFAULT_PROBES:
            self.assertFalse(O.is_probe_path(path))
        self.assertFalse(excluded_for(()).url_disabled(f"{HOST}/healthz"))


class TestDerivedPredicatesAgree(unittest.TestCase):
    """第 2 层：两个派生口（span 侧正则 / 日志侧谓词）必须给同一答案。"""

    def tearDown(self):
        O.set_probe_paths(None)

    def test_agree_on_every_table_and_path(self):
        tables = (
            None,  # 默认表
            (),
            ("/",),
            ("/healthz",),
            ("/internal/",),
            ("/healthz", "/", "/docs", "/internal/"),
        )
        paths = ("/", "//", "/healthz", "/healthz/extra", "/healthzz", "/docs", "/docsx",
                 "/redoc", "/openapi.json", "/internal", "/internal/", "/internal/x/y",
                 "/api/v3/contents/generations/tasks")
        for table in tables:
            excluded = excluded_for(table)
            for path in paths:
                self.assertEqual(
                    O.is_probe_path(path),
                    excluded.url_disabled(f"{HOST}{path}"),
                    f"表={table!r} 路径={path}：is_probe_path 与 excluded_urls 判定不一致 "
                    "⇒ 会出现「span 摘了、日志没摘」这类半关状态",
                )


class TestConfigSurface(unittest.TestCase):
    """第 4 层：env → settings → 生效表；非法条目**告警剔除**（不静默）。"""

    def tearDown(self):
        O.set_probe_paths(None)

    def test_unset_or_empty_means_default_table(self):
        for env in ({}, {"AVM_LOGFIRE_EXCLUDED_PATHS": ""}, {"AVM_LOGFIRE_EXCLUDED_PATHS": "   "}):
            s = Settings.from_env(env)
            self.assertIsNone(s.logfire_excluded_paths, f"{env} ⇒ 应表示'没设'")
            self.assertEqual(O._apply_excluded_paths(s), DEFAULT_PROBES)

    def test_dash_means_exclude_nothing(self):
        for raw in ("-", "none", "off", "OFF"):
            s = Settings.from_env({"AVM_LOGFIRE_EXCLUDED_PATHS": raw})
            self.assertEqual(s.logfire_excluded_paths, ())
            self.assertEqual(O._apply_excluded_paths(s), ())

    def test_comma_list_is_parsed_and_trimmed(self):
        s = Settings.from_env({"AVM_LOGFIRE_EXCLUDED_PATHS": " /healthz , /internal/ ,,"})
        self.assertEqual(s.logfire_excluded_paths, ("/healthz", "/internal/"))
        self.assertEqual(O._apply_excluded_paths(s), ("/healthz", "/internal/"))

    def test_the_users_own_value_keeps_the_site_traced(self):
        """★ 用户实际写过的那一行：`AVM_LOGFIRE_EXCLUDED_PATHS=/healthz,/`

        若这串被**当正则**喂给 otel，`/` 会命中每一个 URL（任何 URL 都含 `/`）⇒ 全站
        追踪静默关掉。按路径语义解释才得到他想要的结果：只排这两条。
        """
        s = Settings.from_env({"AVM_LOGFIRE_EXCLUDED_PATHS": "/healthz,/"})
        self.assertEqual(O._apply_excluded_paths(s), ("/healthz", "/"))
        excluded = parse_excluded_urls(O.probe_excluded_urls())
        self.assertTrue(excluded.url_disabled(f"{HOST}/healthz"))
        self.assertTrue(excluded.url_disabled(f"{HOST}/"))
        for path in ("/openapi.json", "/api/v3/contents/generations/tasks", "/docs"):
            self.assertFalse(
                excluded.url_disabled(f"{HOST}{path}"),
                f"{path} 被连带关掉了 —— 说明原样值进了正则（这正是要避免的形状）",
            )

    def test_invalid_entries_are_dropped_loudly(self):
        msgs = []
        sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
        try:
            effective = O._apply_excluded_paths(
                Settings(logfire_excluded_paths=("healthz", "http://x/y", "/a?b=1", "/ok"))
            )
        finally:
            logger.remove(sink)
        self.assertEqual(effective, ("/ok",), "非法条目不得进生效表")
        joined = "\n".join(msgs)
        self.assertIn("healthz", joined, "被拒的条目必须出现在告警里（静默忽略=配了不生效）")
        self.assertIn("/a?b=1", joined)

    def test_setup_observability_applies_the_configured_table(self):
        """接线：真 `setup_observability()` 会把它落到生效表上（不只是解析函数自己会）。"""
        saved = (O._LOGFIRE_READY, O._LOGFIRE_EXPORTING, O._MAX_ATTR_CHARS)
        O._LOGFIRE_READY = False
        try:
            with mock.patch.object(logfire, "configure", lambda **kw: None), mock.patch.object(
                logfire, "instrument_httpx", lambda **kw: None
            ):
                s = Settings.from_env({"AVM_LOGFIRE_EXCLUDED_PATHS": "/healthzz"})
                O.setup_observability(s)
        finally:
            O._LOGFIRE_READY, O._LOGFIRE_EXPORTING, O._MAX_ATTR_CHARS = saved
        self.assertEqual(O.probe_paths(), ("/healthzz",))


class TestWiring(unittest.TestCase):
    """第 5 层：调用点真的把排除传下去；装好的中间件里携带的就是它。"""

    def tearDown(self):
        O.set_probe_paths(None)

    def test_instrument_fastapi_passes_exclusions_and_keeps_headers_off(self):
        app = create_app(web_settings())
        seen: dict = {}
        with mock.patch.object(logfire, "instrument_fastapi", lambda a, **kw: seen.update(kw, app=a)):
            O.instrument_fastapi(app)
        # 排除表 = **探活表 + 轮询表**（闸门 1：产生层）；两张表各出一份、同源派生。
        # ⚠️ 入站 span 压不掉（ASGI 中间件在 handler 之外建 span，实测），所以
        #    `excluded_urls` 是"轮询不产生 span"在入站一侧的**唯一**杠杆。
        self.assertEqual(seen["excluded_urls"], O.fastapi_excluded_urls())
        for pattern in O.poll_paths():
            self.assertIn(
                pattern, seen["excluded_urls"],
                f"轮询路径 {pattern} 没进 excluded_urls ⇒ 轮询 GET 又会每次一条 span",
            )
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
        app = create_app(web_settings(logfire_excluded_paths=("/healthzz", "/internal/")))
        O.instrument_fastapi(app)
        stack = app.build_middleware_stack()
        mw = _find_otel_middleware(stack)
        self.assertIsNotNone(
            mw,
            "建好的中间件栈里没有带 `excluded_urls` 的中间件：上游换了挂载方式，本测试要跟着改",
        )
        self.assertTrue(mw.excluded_urls.url_disabled(f"{HOST}/healthzz"))
        self.assertTrue(mw.excluded_urls.url_disabled(f"{HOST}/internal/x"))
        self.assertFalse(
            mw.excluded_urls.url_disabled(f"{HOST}/healthz"),
            "配了自定义表就不该再按默认表排除 —— 配置没生效的典型形状",
        )


class TestRealAppSpans(unittest.TestCase):
    """第 6 层：真 app + 真 exporter。零 span / 对照组 / 自定义表真的换掉。"""

    def _client(self, settings) -> TestClient:
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

    def test_default_probe_and_doc_paths_produce_no_span(self):
        for path in DEFAULT_PROBES:
            self.exporter.clear()
            self.client.get(path)
            self.assertEqual(self.targets(), [], f"{path} 不该产生任何 span")

    def test_boundary_paths_still_produce_spans(self):
        """对照组：排除是**精确/子树**的，不是"把追踪整体关了"。"""
        for path in ("/healthz/extra", "/healthzz", "/docsx"):
            self.exporter.clear()
            r = self.client.get(path)
            self.assertIn(path, self.targets(), f"{path}（{r.status_code}）的 span 不该被顺手摘掉")

    def test_configured_table_replaces_the_default(self):
        """配置**真的**换掉了名单：/healthzz 被摘，同时 /healthz 反而重新有 span。"""
        self.client = self._client(web_settings(logfire_excluded_paths=("/healthzz",)))
        for path in ("/healthzz",):
            self.exporter.clear()
            self.client.get(path)
            self.assertEqual(self.targets(), [], f"{path} 应被自定义表排除")
        for path in ("/healthz", "/healthz/extra"):
            self.exporter.clear()
            self.client.get(path)
            self.assertIn(path, self.targets(), f"自定义表生效后 {path} 应重新被追踪")


class TestRealAppLogs(unittest.TestCase):
    """第 7 层：探活的日志**根本没到** logfire sink（本地 sink 不受影响）。

    做法：把 `logfire.loguru_handler()` 换成一个收集器，走**真** `setup_observability`
    —— 于是断言的是"生产那行 `logger.add(..., filter=keep_off_logfire)` 装上了没"。
    """

    def tearDown(self):
        O.set_probe_paths(None)

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
                for path in ("/healthz", "/docs", "/openapi.json"):
                    client.get(path)
                client.get("/healthzz")  # 对照：不在排除表里
        finally:
            O._LOGFIRE_READY, O._LOGFIRE_EXPORTING, O._MAX_ATTR_CHARS = saved

        joined = "\n".join(str(x) for x in delivered)
        # ⚠️ 标记要带 " ->"：`GET /healthzz` **包含** `GET /healthz` 这个子串，只按路径断言
        #    会被自己设的对照组骗过去（实测踩到）。
        self.assertIn("GET /healthzz ->", joined, "对照组：未排除的请求必须仍然上报（否则等于把出口整体关了）")
        for path in ("GET /healthz ->", "GET /docs ->", "GET /openapi.json ->"):
            self.assertNotIn(
                path,
                joined,
                f"{path} 的日志进了 logfire sink ⇒ 要么生产侧没挂 filter，要么中间件没绑 `avm_probe`",
            )

        probes = [row for row in seen if row[2].startswith("GET /healthz ->")]
        self.assertEqual(len(probes), 1, f"应当采样到恰好一条探活日志，实际 {seen}")
        flag, keep, _msg = probes[0]
        self.assertIs(flag, True, "中间件必须给探活请求绑 `avm_probe=True`（不绑就永远放行）")
        self.assertFalse(keep)


class TestLogFilter(unittest.TestCase):
    def test_filter_keeps_everything_without_the_flag(self):
        """请求之外的日志没有 `avm_probe` 键 —— 必须照常放行（别把全部日志静音）。"""
        self.assertTrue(O.keep_off_logfire({"extra": {}}))
        self.assertTrue(O.keep_off_logfire({"extra": {"avm_probe": False, "request_id": "x"}}))
        self.assertFalse(O.keep_off_logfire({"extra": {"avm_probe": True}}))


if __name__ == "__main__":
    unittest.main()
