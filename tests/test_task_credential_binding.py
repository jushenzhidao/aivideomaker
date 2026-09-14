#!/usr/bin/env python3
"""任务查询的凭据绑定（task_id ↔ api-key）与轮询节流缓存（E2E-AVM-011 配套）。

两件事：

1. **凭据绑定**：透传创建任务时把当时的凭据绑到这条任务上（sqlite `credential`
   列）⇒ `GET /tasks/{id}` 不带凭据也能查 —— 轮询方（newapi）不必再持 cookie。
   列表 `GET /tasks` 是跨任务租户视图，**必须**带凭据（否则 401）。
2. **查询节流**：`GET /tasks/{id}` 的上游视图缓存 —— 非终态 TTL 内复用、终态
   永久复用，把调用方的高频轮询挡在缓存里，避免把上游打到 429。

同时钉住 2026-09-15 的**接口面收窄**：对外只有创建 + 查询，DELETE/取消已整体移除。

纪律（与 test_passthrough_cookie.py 一致）：
  - **零额度消耗** —— 提交要么 dry-run、要么打在假上游上，本文件不会产生任何
    真实生成请求；
  - **零外发** —— 上游 base_url 指向死端口；上游对象整个被替身替换；Logfire 关掉。

运行：python3 tests/test_task_credential_binding.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat import app as app_mod  # noqa: E402
from ark_compat.app import TASKS_PATH, create_app  # noqa: E402
from ark_compat.errors import WebApiError  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.store import SqliteTaskStore  # noqa: E402

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
    def stats(self) -> dict:
        return {"max_concurrent": 2, "running": 0, "running_tasks": []}


class FakeClient:
    def __init__(self):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class CountingUpstream:
    """替身上游：记录 create/get_task 调用次数，get_task 的视图可随时改写。"""

    kind = "web"

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.client = FakeClient()
        self.queue = FakeQueue()
        self.created: list[dict] = []
        self.fetches = 0
        # 初始视图：非终态（running）。测试里按需改写 status。
        self.view = {"status": "running", "usage": {"paid": False}}

    def create(self, plan) -> str:
        self.created.append(plan)
        return "site-task-1"

    def get_task(self, task_id: str) -> dict:
        self.fetches += 1
        return dict(self.view)

    def health(self) -> dict:
        return {"upstream": "web", "ok": True}


class TrackingBuilder:
    def __init__(self):
        self.calls: list[str] = []
        self.upstreams: list[CountingUpstream] = []

    def __call__(self, settings_, cookie, log=None):
        self.calls.append(cookie)
        up = CountingUpstream(cookie)
        self.upstreams.append(up)
        return up


class BindingCase(unittest.TestCase):
    """每个测试一份**全新** app —— 类级共享 app 会被字母序在前的测试污染计数。"""

    def setUp(self):
        self.builder = TrackingBuilder()
        patch = mock.patch.object(app_mod, "build_web_for_cookie", self.builder)
        patch.start()
        self.addCleanup(patch.stop)
        self.app = create_app(settings())
        self.client = TestClient(self.app)

    def _create(self, token: str = TOKEN_A) -> str:
        r = self.client.post(TASKS_PATH, json=body(), headers=auth(token))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]


class TestCredentialBinding(BindingCase):
    """E2E-AVM-011 的核心修复：轮询 `GET /tasks/{id}` 不必再带凭据。"""

    def test_get_without_bearer_uses_the_bound_credential(self):
        ark_id = self._create(TOKEN_A)
        upstream = self.builder.upstreams[0]
        r = self.client.get(f"{TASKS_PATH}/{ark_id}")  # 故意不带 Bearer
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], ark_id)
        # 上游拿到的凭据 = 创建时绑定的那份（规范化成 auth_session=…）
        self.assertIn(COOKIE_A, self.builder.calls)
        # 复用创建时的那个上游实例，而不是新建一个
        self.assertEqual(upstream.fetches, 1)

    def test_get_with_the_same_bearer_still_works(self):
        ark_id = self._create(TOKEN_A)
        r = self.client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(r.status_code, 200, r.text)

    def test_get_with_other_credential_is_still_404(self):
        ark_id = self._create(TOKEN_A)
        r = self.client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_B))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TaskNotFound")

    def test_get_without_bearer_on_unbound_task_is_401_with_hint(self):
        # 机制上线前创建的记录没有绑定：明确 401 说清原因，不能被读成"任务不存在"
        entry = {
            "id": "cgt-legacy-0001",
            "taskId": "site-legacy",
            "upstream": "web",
            "owner": "0123456789abcdef",
            "model": ARK_MODEL,
            "requested": {},
            "effective": {},
            "warnings": [],
            "unsupported": [],
            "createdAtMs": 1,
        }
        self.app.state.tasks.put(entry, credential="")
        r = self.client.get(f"{TASKS_PATH}/cgt-legacy-0001")
        self.assertEqual(r.status_code, 401)
        self.assertIn("绑定", r.json()["error"]["message"])

    def test_list_without_bearer_is_401_even_with_tasks_present(self):
        self._create(TOKEN_A)
        r = self.client.get(TASKS_PATH)
        self.assertEqual(r.status_code, 401, "列表是跨任务租户视图，没凭据必须拒绝")
        # ⚠️ 401 必须来自**列表自己的显式门**，而不是下游 _upstream_for 的兜底 ——
        # 两者报文不同；不钉报文的话，删掉显式门后测试照样绿（变异自证踩过）。
        self.assertIn("scope the task list", r.json()["error"]["message"])

    def test_list_without_bearer_on_empty_store_is_also_401(self):
        # 显式门的真实差异在**空表**：没有它，空列表会 200 返回 —— 泄漏"服务里没有任务"
        r = self.client.get(TASKS_PATH)
        self.assertEqual(r.status_code, 401)
        self.assertIn("scope the task list", r.json()["error"]["message"])

    def test_list_with_bearer_keeps_tenant_isolation(self):
        mine = self._create(TOKEN_A)
        theirs = self._create(TOKEN_B)
        a = self.client.get(TASKS_PATH, headers=auth(TOKEN_A)).json()
        b = self.client.get(TASKS_PATH, headers=auth(TOKEN_B)).json()
        self.assertIn(mine, [i["id"] for i in a["items"]])
        self.assertNotIn(theirs, [i["id"] for i in a["items"]])
        self.assertIn(theirs, [i["id"] for i in b["items"]])

    def test_delete_endpoint_is_gone(self):
        """2026-09-15 接口面收窄：连归属人自己也没有删除入口（405）。"""
        ark_id = self._create(TOKEN_A)
        r = self.client.delete(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(r.status_code, 405)
        self.assertIsNotNone(self.app.state.tasks.get(ark_id))

    def test_credential_never_leaks_into_responses_or_the_entry(self):
        ark_id = self._create(TOKEN_A)
        view = self.client.get(f"{TASKS_PATH}/{ark_id}").json()
        self.assertNotIn(TOKEN_A, json.dumps(view), "凭据原文绝不能出现在响应里")
        entry = self.app.state.tasks.get(ark_id)
        self.assertNotIn(TOKEN_A, json.dumps(entry), "凭据原文绝不能写进 entry JSON")
        # 但绑定确实在（独立于 entry 存储）
        self.assertEqual(self.app.state.tasks.credential_for(ark_id), COOKIE_A)


class TestViewCacheThrottling(unittest.TestCase):
    """轮询节流：非终态 TTL 内复用、终态永久复用、ttl=0 显式关闭。"""

    def _make(self, ttl: float):
        builder = TrackingBuilder()
        patch = mock.patch.object(app_mod, "build_web_for_cookie", builder)
        patch.start()
        self.addCleanup(patch.stop)
        app = create_app(settings(task_cache_ttl=ttl))
        return app, TestClient(app), builder

    def _create(self, client) -> str:
        r = client.post(TASKS_PATH, json=body(), headers=auth(TOKEN_A))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_nonterminal_within_ttl_is_served_from_cache(self):
        app, client, builder = self._make(ttl=15)
        ark_id = self._create(client)
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(builder.upstreams[0].fetches, 1, "TTL 内的第二次轮询不该打上游")

    def test_nonterminal_after_ttl_refetches(self):
        app, client, builder = self._make(ttl=0.05)
        ark_id = self._create(client)
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        time.sleep(0.12)
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(builder.upstreams[0].fetches, 2, "TTL 过后必须重新取")

    def test_terminal_view_is_cached_forever(self):
        app, client, builder = self._make(ttl=15)
        ark_id = self._create(client)
        builder.upstreams[0].view = {"status": "succeeded", "usage": {"paid": False}}
        r1 = client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A)).json()
        builder.upstreams[0].view = {"status": "failed", "usage": {"paid": True}}
        r2 = client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A)).json()
        self.assertEqual(r1["status"], "succeeded")
        self.assertEqual(r2["status"], "succeeded", "终态视图不可变，必须一直回缓存")
        self.assertEqual(builder.upstreams[0].fetches, 1)

    def test_ttl_zero_disables_the_cache(self):
        app, client, builder = self._make(ttl=0)
        ark_id = self._create(client)
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(builder.upstreams[0].fetches, 2, "ttl=0 = 每次实时回上游")

    def test_failed_fetch_is_not_cached(self):
        app, client, builder = self._make(ttl=15)
        ark_id = self._create(client)
        # 上游第一次失败（app 只捕 WebApiError ⇒ 用它模拟真实失败路径），第二次成功
        # —— 失败的空壳绝不能被缓存记住，否则成功永远进不来。
        with mock.patch.object(builder.upstreams[0], "get_task", side_effect=[
            WebApiError("model.getModel", "task not found", code="NOT_FOUND", http_status=404),
            {"status": "succeeded", "usage": {"paid": False}},
        ]):
            r1 = client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
            r2 = client.get(f"{TASKS_PATH}/{ark_id}", headers=auth(TOKEN_A))
        self.assertEqual(r1.status_code, 200)
        self.assertNotIn("status", r1.json(), "失败时只回本地记录，没有上游状态")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "succeeded", "失败的空壳不该占据缓存位")


class TestTaskViewCacheUnit(unittest.TestCase):
    """直接钉 TaskViewCache 的过期语义 —— HTTP 层的 15s TTL 探不到毫秒级差异。"""

    def test_terminal_entry_outlives_ttl(self):
        c = app_mod.TaskViewCache(ttl=0.05)
        c.put("k", {"status": "succeeded", "usage": {"paid": False}})
        self.assertIsNotNone(c.get("k"))
        time.sleep(0.12)
        self.assertIsNotNone(c.get("k"), "终态视图必须活过 TTL（记录定格，重取毫无意义）")

    def test_nonterminal_entry_expires_after_ttl(self):
        c = app_mod.TaskViewCache(ttl=0.05)
        c.put("k", {"status": "running", "usage": {"paid": False}})
        self.assertIsNotNone(c.get("k"))
        time.sleep(0.12)
        self.assertIsNone(c.get("k"), "非终态视图过 TTL 必须重新取")

    def test_ttl_zero_disables_even_terminal_caching(self):
        c = app_mod.TaskViewCache(ttl=0)
        c.put("k", {"status": "succeeded"})
        self.assertIsNone(c.get("k"), "ttl=0 = 显式全关")

    def test_put_ignores_empty_views(self):
        c = app_mod.TaskViewCache(ttl=15)
        c.put("k", {})
        self.assertIsNone(c.get("k"), "上游失败的空壳不该进缓存")


class TestNonPassthroughRegression(unittest.TestCase):
    """非透传模式不受影响：GET 不带 Bearer 照常工作（进程自带凭据）。"""

    def test_get_without_bearer_still_works_in_process_mode(self):
        s = Settings(
            passthrough_cookie=False,
            cookie=COOKIE_A,
            base_url=DEAD_UPSTREAM,
            log_level="WARNING",
            enable_logfire=False,
            trust_env=False,
            task_store="memory",
        )
        builder = TrackingBuilder()
        with mock.patch.object(app_mod, "build_web_for_cookie", builder):
            app = create_app(s)
            # 塞一条任务（进程模式下 owner/credential 均为空串）
            app.state.tasks.put({
                "id": "cgt-proc-0001", "taskId": "site-1", "upstream": "web",
                "owner": "", "model": ARK_MODEL, "requested": {}, "effective": {},
                "warnings": [], "unsupported": [], "createdAtMs": 1,
            })
            client = TestClient(app)
            r = client.get(f"{TASKS_PATH}/cgt-proc-0001")
            self.assertEqual(r.status_code, 200, r.text)


class TestSqliteCredentialStore(unittest.TestCase):
    """sqlite 后端：绑定列、老库迁移、文件权限。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "tasks.db")

    def test_credential_round_trip_and_independent_of_entry(self):
        store = SqliteTaskStore(self.db)
        entry = {"id": "cgt-1", "taskId": "t1", "createdAtMs": 1, "owner": "x"}
        store.put(entry, credential=COOKIE_A)
        self.assertEqual(store.credential_for("cgt-1"), COOKIE_A)
        self.assertEqual(store.credential_for("missing"), "")
        # entry 读出来不含凭据（列独立于 JSON blob）
        self.assertNotIn(COOKIE_A, json.dumps(store.get("cgt-1")))
        # 覆盖写入会更新绑定
        store.put(entry, credential="")
        self.assertEqual(store.credential_for("cgt-1"), "")

    def test_old_schema_db_is_migrated_in_place(self):
        # 用**旧** schema（没有 credential 列）建库，模拟机制上线前的老库
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, upstream TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
                created_ms INTEGER NOT NULL, entry TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO tasks (id, task_id, upstream, created_ms, entry)"
            " VALUES ('cgt-old', 't0', 'web', 1, '{}')"
        )
        conn.commit()
        conn.close()
        store = SqliteTaskStore(self.db)
        self.assertEqual(store.credential_for("cgt-old"), "", "老行没有绑定，如实返回空串")
        entry = {"id": "cgt-new", "taskId": "t1", "createdAtMs": 2}
        store.put(entry, credential=COOKIE_A)
        self.assertEqual(store.credential_for("cgt-new"), COOKIE_A)

    def test_owner_only_db_is_migrated_too(self):
        # 只补过 owner 的中间代库（有 owner、没 credential）也必须能就地升级
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, upstream TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
                credential TEXT NOT NULL DEFAULT '', created_ms INTEGER NOT NULL,
                entry TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()
        store = SqliteTaskStore(self.db)
        store.put({"id": "cgt-2", "taskId": "t2", "createdAtMs": 3}, credential=COOKIE_A)
        self.assertEqual(store.credential_for("cgt-2"), COOKIE_A)

    def test_db_file_permissions_are_0600(self):
        if os.name != "posix":
            self.skipTest("仅 POSIX 有 0600 语义")
        SqliteTaskStore(self.db)
        mode = os.stat(self.db).st_mode & 0o777
        self.assertEqual(mode, 0o600, "库里存了会话凭据，权限必须是 0600")

    def test_delete_and_prune_drop_the_credential(self):
        store = SqliteTaskStore(self.db)
        store.put({"id": "cgt-3", "taskId": "t3", "createdAtMs": int(time.time() * 1000)},
                  credential=COOKIE_A)
        store.delete("cgt-3")
        self.assertEqual(store.credential_for("cgt-3"), "")
        store.put({"id": "cgt-4", "taskId": "t4", "createdAtMs": 1}, credential=COOKIE_A)
        pruned = store.prune(retention_days=7)
        self.assertEqual(pruned, 1)
        self.assertEqual(store.credential_for("cgt-4"), "")


class TestMemoryStoreParity(unittest.TestCase):
    """memory 后端与 sqlite 行为一致（测试靠它跑，不能半吊子）。"""

    def test_round_trip_and_delete(self):
        from ark_compat.store import MemoryTaskStore

        store = MemoryTaskStore()
        store.put({"id": "cgt-1", "taskId": "t1", "createdAtMs": 1}, credential=COOKIE_A)
        self.assertEqual(store.credential_for("cgt-1"), COOKIE_A)
        self.assertEqual(store.credential_for("nope"), "")
        store.delete("cgt-1")
        self.assertEqual(store.credential_for("cgt-1"), "")


class TestSettingsParsing(unittest.TestCase):
    def test_default_ttl_is_15(self):
        self.assertEqual(Settings.from_env({}).task_cache_ttl, 15)

    def test_ttl_is_parsed_from_env(self):
        self.assertEqual(Settings.from_env({"AVM_TASK_CACHE_TTL": "30"}).task_cache_ttl, 30)
        self.assertEqual(Settings.from_env({"AVM_TASK_CACHE_TTL": "0"}).task_cache_ttl, 0)

    def test_blank_ttl_falls_back_to_default(self):
        self.assertEqual(Settings.from_env({"AVM_TASK_CACHE_TTL": ""}).task_cache_ttl, 15)


if __name__ == "__main__":
    unittest.main()
