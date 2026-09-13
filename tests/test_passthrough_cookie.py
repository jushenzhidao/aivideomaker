#!/usr/bin/env python3
"""透传鉴权：调用方的 `Authorization: Bearer` 就是**上游凭据本身**。

本文件测 web 线的透传开关（`AVM_PASSTHROUGH_COOKIE=1`）：调用方带自己的网页
会话 cookie，本进程不再需要 `AVM_COOKIE`。

纪律（与 test_ark_compat.py 一致）：
  1. **零额度消耗** —— 提交一律走 dry-run，或让假上游直接返回一个 taskId；
     本文件**不会**产生任何真实生成请求。
  2. **零外发** —— 上游 base_url 指向死端口；上游对象整个被替身替换；
     Logfire 关掉。

覆盖的重点不是"happy path 能跑通"，而是三个**容易静默出错**的地方：
  - 凭据没到上游（cookie 没规范成 `auth_session=…`）
  - 多租户串味（A 看得到 B 的任务）
  - 缓存淘汰把正在轮询的客户端关掉（现象是"任务查不到"）

运行：python3 tests/test_passthrough_cookie.py
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat import app as app_mod  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.upstreams import _build_web, build_web_for_cookie  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

ARK_MODEL = "doubao-seedance-2-5-260628"
DEAD_UPSTREAM = "http://127.0.0.1:9"
TOKEN_A = "a" * 40
TOKEN_B = "b" * 40
COOKIE_A = f"auth_session={TOKEN_A}"


def settings(**kw) -> Settings:
    base = dict(
        passthrough_cookie=True,
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,
        trust_env=False,
        task_store="memory",
    )
    base.update(kw)
    return Settings(**base)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def body(**kw) -> dict:
    b = {
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "a red balloon"}],
        "ratio": "16:9",
        "resolution": "480p",
        "duration": 5,
    }
    b.update(kw)
    return b


class FakeQueue:
    def __init__(self, running: bool = False):
        self.running = running

    def stats(self) -> dict:
        return {"max_concurrent": 2, "running": 1 if self.running else 0, "running_tasks": []}


class FakeClient:
    def __init__(self):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeWebUpstream:
    """只实现 app.py 真正碰到的那几个成员。"""

    kind = "web"
    supports_cancel = False

    def __init__(self, cookie: str, *, running: bool = False):
        self.cookie = cookie
        self.client = FakeClient()
        self.queue = FakeQueue(running=running)
        self.created = []

    def create(self, plan) -> str:
        self.created.append(plan)
        return "site-task-1"

    def get_task(self, task_id: str) -> dict:
        return {"status": "Success", "usage": {"paid": False}, "upstream_task_id": task_id}

    def cancel_task(self, task_id: str) -> dict:
        return {"cancelled": False, "task_id": task_id, "reason": "web 线没有取消端点"}

    def health(self) -> dict:
        return {"upstream": "web", "ok": True}


class TrackingBuilder:
    """记录"哪个凭据被拿去建了上游"——凭据有没有正确规范化，看这里。"""

    def __init__(self, running: bool = False):
        self.calls: list[str] = []
        self.upstreams: list[FakeWebUpstream] = []
        self._running = running

    def __call__(self, settings_, cookie, log=None):
        self.calls.append(cookie)
        up = FakeWebUpstream(cookie, running=self._running)
        self.upstreams.append(up)
        return up


# --------------------------------------------------------------- 配置 ----


class TestPassthroughCookieConfig(unittest.TestCase):
    def test_only_passthrough_is_enough(self):
        s = settings()
        s.validate()  # 不抛
        self.assertTrue(s.web_ready)
        self.assertEqual(s.available_upstreams, ["web"])
        self.assertTrue(s.passthrough_cookie)

    def test_from_env_reads_the_switch(self):
        s = Settings.from_env({"AVM_PASSTHROUGH_COOKIE": "1"})
        self.assertTrue(s.passthrough_cookie)

    def test_gate_and_passthrough_cannot_coexist(self):
        with self.assertRaises(ValueError) as ctx:
            settings(gate_key="sk-gate").validate()
        self.assertIn("AVM_GATE_KEY", str(ctx.exception))

    def test_gate_alone_is_still_fine(self):
        settings(passthrough_cookie=False, gate_key="sk-gate", cookie=COOKIE_A).validate()


# ------------------------------------------------------------ HTTP 路径 ----


class PassthroughHttpCase(unittest.TestCase):
    builder: TrackingBuilder

    @classmethod
    def setUpClass(cls):
        cls.builder = TrackingBuilder()
        cls._patch = mock.patch.object(app_mod, "build_web_for_cookie", cls.builder)
        cls._patch.start()
        cls.app = create_app(settings())
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()


class TestBearerIsTheCookie(PassthroughHttpCase):
    def test_missing_bearer_is_401(self):
        r = self.client.post(TASKS_PATH, json=body())
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AuthenticationError")

    def test_credential_without_auth_session_is_401(self):
        r = self.client.post(TASKS_PATH, json=body(), headers=auth("nonsense=1; other=2"))
        self.assertEqual(r.status_code, 401)
        self.assertIn("auth_session", r.json()["error"]["message"])

    def test_bare_token_is_normalized_into_a_cookie_header(self):
        # 裸 token 是透传的**推荐形态**：必须被规范成 auth_session=<token>，
        # 否则上游只当你没登录 —— 而现象与"会话过期"完全一致（最难查的一类）。
        self.client.post(
            TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth(TOKEN_A)
        )
        self.assertIn(COOKIE_A, self.builder.calls)

    def test_full_cookie_string_is_passed_through(self):
        raw = f"Cookie: auth_session={TOKEN_B}; NEXT_LOCALE=zh"
        self.client.post(
            TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth(raw)
        )
        self.assertIn(f"auth_session={TOKEN_B}; NEXT_LOCALE=zh", self.builder.calls)

    def test_dry_run_needs_no_upstream_credentials(self):
        r = self.client.post(
            TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth(TOKEN_A)
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])
        self.assertEqual(r.json()["upstream"], "web")

    def test_upstream_selector_headers_are_ignored(self):
        """选线已彻底移除：旧的 `X-Avm-Upstream` / `?upstream=` 不再被解析，也不报错 ——
        请求一律走 web 线。"""
        for value in ("official", "nope"):
            r = self.client.post(
                TASKS_PATH,
                json=body(extra_body={"aivideomaker_dry_run": True}),
                headers={**auth(TOKEN_A), "X-Avm-Upstream": value},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["upstream"], "web")

    def test_same_credential_reuses_one_upstream(self):
        before = len(self.builder.calls)
        for _ in range(3):
            self.client.post(
                TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth(TOKEN_A)
            )
        self.assertEqual(len(self.builder.calls), before, "同一凭据不该反复建上游")

    def test_different_credentials_get_different_upstreams(self):
        n = len(self.builder.upstreams)
        self.client.post(
            TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth("c" * 40)
        )
        self.assertEqual(len(self.builder.upstreams), n + 1)


class TestHealthzHidesNothing(PassthroughHttpCase):
    def test_capabilities_are_declared_even_without_a_client_yet(self):
        j = self.client.get("/healthz").json()
        self.assertEqual(j["available_upstreams"], ["web"])
        self.assertEqual(j["upstream"], "web")
        self.assertTrue(j["passthrough_cookie"])
        self.assertTrue(j["credentials_from_caller"])
        self.assertIn("billing_notes", j)
        self.assertIn("web", j["supports_cancel"])
        # 已移除的官方线不该在健康检查里留下任何字段
        for gone in ("switch_via", "max_credits", "default_model", "passthrough_key"):
            self.assertNotIn(gone, j, f"{gone} 属于已移除的 official 线，不该再出现")

    def test_deep_probe_without_credentials_explains_itself(self):
        # 这里绝不能 401：存活探针拿到 401 会被读成"服务坏了 / 鉴权失败"
        r = self.client.get("/healthz?deep=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("透传", r.json()["upstream_probe"])


class TestTenantIsolation(PassthroughHttpCase):
    """透传即多租户：任务表按凭据指纹隔离。"""

    def _create(self, token: str) -> str:
        r = self.client.post(TASKS_PATH, json=body(), headers=auth(token))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_owner_is_stored_as_a_fingerprint_not_the_credential(self):
        ark_id = self._create(TOKEN_A)
        entry = self.app.state.tasks.get(ark_id)
        self.assertTrue(entry["owner"])
        self.assertNotIn(TOKEN_A, json.dumps(entry), "凭据原文绝不能落进任务表")

    def test_other_credential_cannot_see_the_task(self):
        ark_id = self._create(TOKEN_A)
        r = self.client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_B))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TaskNotFound")

    def test_other_credential_cannot_delete_it_and_it_stays(self):
        ark_id = self._create(TOKEN_A)
        r = self.client.delete(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_B))
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(self.app.state.tasks.get(ark_id), "别人的删除不该删掉任务")

    def test_list_only_shows_my_own_tasks(self):
        mine = self._create(TOKEN_A)
        theirs = self._create(TOKEN_B)

        a = self.client.get(TASKS_PATH, headers=auth(TOKEN_A)).json()
        b = self.client.get(TASKS_PATH, headers=auth(TOKEN_B)).json()

        self.assertIn(mine, [i["id"] for i in a["items"]])
        self.assertNotIn(theirs, [i["id"] for i in a["items"]])
        self.assertIn(theirs, [i["id"] for i in b["items"]])
        self.assertNotIn(mine, [i["id"] for i in b["items"]])
        self.assertEqual(a["total"], 1)
        self.assertEqual(b["total"], 1)


class TestEviction(unittest.TestCase):
    """缓存到顶时只淘汰**空闲**上游 —— 正在被轮询的那个关不得。"""

    def _app_with_limit(self, limit: int, running: bool):
        builder = TrackingBuilder(running=running)
        patch = mock.patch.object(app_mod, "build_web_for_cookie", builder)
        patch.start()
        self.addCleanup(patch.stop)
        limit_patch = mock.patch.object(app_mod, "_PASSTHROUGH_WEB_CACHE_MAX", limit)
        limit_patch.start()
        self.addCleanup(limit_patch.stop)
        return create_app(settings()), builder

    def _hit(self, client, token):
        client.post(
            TASKS_PATH, json=body(extra_body={"aivideomaker_dry_run": True}), headers=auth(token)
        )

    def test_idle_upstream_is_closed_when_cache_is_full(self):
        app, builder = self._app_with_limit(1, running=False)
        client = TestClient(app)
        self._hit(client, TOKEN_A)
        self._hit(client, TOKEN_B)
        self.assertEqual(len(app.state.passthrough_web), 1)
        self.assertEqual(builder.upstreams[0].client.closed, 1, "被淘汰的客户端必须关闭")

    def test_busy_upstream_is_kept(self):
        app, builder = self._app_with_limit(1, running=True)
        client = TestClient(app)
        self._hit(client, TOKEN_A)
        self._hit(client, TOKEN_B)
        # 全忙 ⇒ 宁可暂时超出上限，也不能关掉正在轮询的客户端
        self.assertEqual(len(app.state.passthrough_web), 2)
        self.assertEqual(builder.upstreams[0].client.closed, 0)

    def test_busy_upstream_is_swept_once_it_finishes(self):
        app, builder = self._app_with_limit(1, running=True)
        client = TestClient(app)
        self._hit(client, TOKEN_A)
        builder.upstreams[0].queue.running = False  # 任务进终态，槽位释放
        self._hit(client, TOKEN_B)
        self.assertEqual(len(app.state.passthrough_web), 1)
        self.assertEqual(builder.upstreams[0].client.closed, 1)


class TestCredentialPlumbing(unittest.TestCase):
    """凭据是怎么接到客户端上的 —— 这层错了不会报错，只会"查不到任务"。"""

    def test_passthrough_does_not_inherit_the_default_accounts_user_id(self):
        # 默认账号的 userId 配到别人的会话上，列表类接口会按错误的 userId 查，
        # 现象是"任务明明存在却查不到"——所以透传必须显式 user_id=""。
        s = settings(user_id="u-of-default-account", cookie=COOKIE_A, visitor_id="v-1")
        passed = build_web_for_cookie(s, COOKIE_A)
        fixed = _build_web(s)
        self.addCleanup(passed.client.close)
        self.addCleanup(fixed.client.close)
        self.assertEqual(passed.client.user_id, "")
        self.assertEqual(fixed.client.user_id, "u-of-default-account")
        # visitorId 不是账号身份（服务端不校验），共用部署级取值是刻意的
        self.assertEqual(passed.client.visitor_id, fixed.client.visitor_id)
        self.assertEqual(passed.client.cookie, COOKIE_A)

    def test_user_id_none_still_reads_the_env(self):
        # 历史行为不能变：None（默认）= 读 AVM_USER_ID
        with mock.patch.dict("os.environ", {"AVM_USER_ID": "u-from-env"}):
            client = WebClient(cookie=COOKIE_A, base_url=DEAD_UPSTREAM, trust_env=False)
        self.addCleanup(client.close)
        self.assertEqual(client.user_id, "u-from-env")


class TestOwnerColumnUpgrade(unittest.TestCase):
    """老库（无 owner 列）必须能**就地升级**：不能让调用方删库重来。

    为什么单测这条：迁移只在"已有库"上才走到，全新库走的是 SCHEMA —— 而真实部署
    升级时恰恰是最不该出问题、也最容易出问题（读不出数据）的一刻。
    """

    OLD_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id         TEXT PRIMARY KEY,
        task_id    TEXT NOT NULL,
        upstream   TEXT NOT NULL,
        model      TEXT NOT NULL DEFAULT '',
        created_ms INTEGER NOT NULL,
        entry      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_ms DESC);
    """

    def test_old_db_gains_the_column_and_keeps_its_rows(self):
        import sqlite3
        import tempfile

        from ark_compat.store import SqliteTaskStore

        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/old.db"
            conn = sqlite3.connect(path)
            conn.executescript(self.OLD_SCHEMA)
            conn.execute(
                "INSERT INTO tasks (id, task_id, upstream, model, created_ms, entry)"
                " VALUES (?,?,?,?,?,?)",
                ("cgt-old", "t-old", "web", ARK_MODEL, 1, json.dumps({"id": "cgt-old", "createdAtMs": 1})),
            )
            conn.commit()
            conn.close()

            store = SqliteTaskStore(path)
            cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(tasks)")]
            self.assertIn("owner", cols, "老库必须补上 owner 列")
            self.assertEqual(store.count(), 1, "旧记录不能被升级动作弄丢")
            # 旧记录没有归属 ⇒ 透传模式下查不到（比"替它猜一个归属"安全）
            self.assertEqual(store.count(owner="deadbeef"), 0)
            self.assertEqual(store.count(owner=""), 1)

            store.put(
                {
                    "id": "cgt-new",
                    "taskId": "t-new",
                    "upstream": "web",
                    "model": ARK_MODEL,
                    "createdAtMs": 2,
                    "owner": "deadbeef",
                }
            )
            self.assertEqual(store.count(owner="deadbeef"), 1)
            self.assertEqual(
                [e["id"] for e in store.list_recent(10, 0, owner="deadbeef")], ["cgt-new"]
            )

    def test_owner_filter_is_off_by_default(self):
        # owner=None 表示"不过滤"（非透传模式），不是"匹配空串"
        from ark_compat.store import MemoryTaskStore

        store = MemoryTaskStore()
        store.put({"id": "a", "createdAtMs": 1, "owner": "x"})
        store.put({"id": "b", "createdAtMs": 2})
        self.assertEqual(store.count(), 2)
        self.assertEqual(store.count(owner="x"), 1)
        self.assertEqual([e["id"] for e in store.list_recent(10, 0)], ["b", "a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
