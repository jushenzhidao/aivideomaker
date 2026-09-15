#!/usr/bin/env python3
"""交互式接口文档（/docs、/redoc）必须保持可达。

为什么需要这个文件：`app.py` 里**曾经**是 `docs_url=None, redoc_url=None`（从首次提交
`c9777e2` 起就如此，且没留理由），`/docs` 直接 404。而"服务没坏、是文档被关掉"这件事
**从外部看不出来** —— 只能读代码，或注意到 `/openapi.json` 反而活着。本文件把"开着"
钉成契约：谁再把它关掉，这里会红，并指向 `app.py` 的 `FastAPI(...)`。

两条纪律与其余测试一致：**零额度消耗、零外发**（上游指向死端口、logfire 关）。

运行：python3 tests/test_api_docs_enabled.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import OPENAI_VIDEOS_PATH, TASKS_PATH, create_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402

DEAD_UPSTREAM = "http://127.0.0.1:9"  # discard 端口，保证不出网


def settings(**kw) -> Settings:
    base = dict(
        cookie="auth_session=deadbeef",
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,  # 关掉，避免测试互相污染全局 logfire
        trust_env=False,  # 绕开系统代理，保证"死端口"真的是死端口
        task_store="memory",
    )
    base.update(kw)
    return Settings(**base)


def probe(**kw) -> tuple:
    """返回 (app, client)。刻意**不**用 `with TestClient(...)` —— 那会触发 lifespan。"""
    app = create_app(settings(**kw))
    return app, TestClient(app)


DOC_PATHS = ("/docs", "/redoc")


class TestDocsReachable(unittest.TestCase):
    """文档端点得是 200，而且真的是 UI 页面（不是被别的路由蒙对的 200）。"""

    def test_docs_and_redoc_serve_their_ui(self):
        _, c = probe()
        # marker 取各自的**挂载点**而非品牌名：ReDoc 的页面里没有精确的 "Redoc" 字面量
        # （只有 "ReDoc" / "redoc.standalone"），拿品牌名当 marker 会误红。
        for path, marker in (("/docs", "SwaggerUIBundle"), ("/redoc", "<redoc spec-url=")):
            with self.subTest(path=path):
                r = c.get(path)
                self.assertEqual(
                    r.status_code,
                    200,
                    f"{path} 不是 200 —— docs_url/redoc_url 又被设成 None 了？",
                )
                self.assertIn("text/html", r.headers.get("content-type", ""))
                self.assertIn(marker, r.text, f"{path} 返回的 HTML 里没有 {marker}，不是 UI 页面")

    def test_both_uis_point_at_this_service_schema(self):
        """两份 UI 都必须指向本服务的 schema，而不是某个默认/外部地址。"""
        _, c = probe()
        self.assertIn("url: '/openapi.json'", c.get("/docs").text)  # Swagger UI 用单引号
        self.assertIn('spec-url="/openapi.json"', c.get("/redoc").text)

    def test_external_static_hosts_are_recorded(self):
        """把 UI 依赖的外部域名钉住：换 CDN 会改变"谁能打开这个页面"。

        ⚠️ 这两条断言**不是**在保证页面能显示 —— 恰恰是在记录一个风险：Swagger UI 与
        ReDoc 的 JS/CSS 分别从 `cdn.jsdelivr.net` 取（ReDoc 还额外拉
        `fonts.googleapis.com` 的字体）。浏览器到不了这些域名时，页面会**白屏或样式全丢**，
        而**服务端一切正常、日志里一个字都没有** —— 别把它误判成路由没生效。
        要消除这个外部依赖（自托管静态资源），请连带改这两条断言。
        """
        _, c = probe()
        self.assertIn("cdn.jsdelivr.net", c.get("/docs").text)
        self.assertIn("cdn.jsdelivr.net", c.get("/redoc").text)


class TestDocsArePublic(unittest.TestCase):
    """口径：文档与 schema **不带** `require_bearer` —— 闸门模式下也照样公开。

    这不是"忘了加鉴权"：鉴权是**逐路由** `Depends(require_bearer)`，而 /docs、/redoc、
    /openapi.json 是 FastAPI 自动注册的路由，天然没有那个依赖。若将来决定保护它们
    （加中间件或改注册方式），**请同步改这里** —— 别让口径默默翻转。
    """

    def test_public_without_credential_even_in_gate_mode(self):
        _, c = probe(gate_key="sk-gate")
        for path in (*DOC_PATHS, "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(c.get(path).status_code, 200)
                # 连垃圾凭据都放行 —— 进一步证明这四条路上没有鉴权依赖
                self.assertEqual(
                    c.get(path, headers={"Authorization": "Bearer garbage"}).status_code,
                    200,
                )

    def test_protected_routes_still_401_in_gate_mode(self):
        """★ 对照组。没有它，上面那条可能只是在说"本服务根本没有鉴权"。

        断言的是"闸门确实在拦业务路由"，从而"公开"是一个**被对比出来的**事实。
        """
        _, c = probe(gate_key="sk-gate")
        self.assertEqual(c.get(f"{TASKS_PATH}/cgt-does-not-matter").status_code, 401)
        self.assertEqual(c.post(TASKS_PATH, json={}).status_code, 401)


class TestSchemaCoversEveryRoute(unittest.TestCase):
    """schema 必须覆盖**每一个**对外路由：漏一条，文档就在撒谎。"""

    def test_every_declared_route_appears_in_schema(self):
        app, c = probe()
        spec = c.get("/openapi.json").json()
        documented = set(spec["paths"])
        declared = {
            r.path for r in app.routes if isinstance(r, APIRoute) and r.include_in_schema
        }
        self.assertEqual(
            declared - documented,
            set(),
            "有路由没进 openapi.json（有人设了 include_in_schema=False？）",
        )
        # 锚：协议主路径必须在，否则上面的集合比较可能因为两侧都空而假绿
        self.assertIn(TASKS_PATH, documented)
        self.assertIn(OPENAI_VIDEOS_PATH, documented)


if __name__ == "__main__":
    unittest.main(verbosity=2)
