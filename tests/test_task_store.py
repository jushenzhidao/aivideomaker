#!/usr/bin/env python3
"""任务持久化（`store`）的测试。

为什么值得单独测：这个模块存在的**唯一理由**就是"重启不丢任务"。
如果 sqlite 后端被换成内存实现，其它所有测试照样全绿 ——
只有这里的"跨实例可读"会红。所以持久化的证据必须是**新实例能读到旧实例写的数据**。

运行：python3 tests/test_task_store.py
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat.store import (  # noqa: E402
    MemoryTaskStore,
    SqliteTaskStore,
    build_task_store,
)

DAY_MS = 86_400_000


def entry(ark_id: str, created_ms: int | None = None, **kw) -> dict:
    e = {
        "id": ark_id,
        "taskId": f"up-{ark_id}",
        "upstream": "web",
        "model": "doubao-seedance-2-5-260628",
        "createdAtMs": created_ms if created_ms is not None else int(time.time() * 1000),
        "requested": {"model": "x", "content": [{"type": "text", "text": "hi"}]},
        "effective": {"billed": False},
        "warnings": ["w"],
        "unsupported": [],
    }
    e.update(kw)
    return e


class StoreContractMixin:
    """两种后端必须表现**一致** —— 同一套断言跑两遍。"""

    def _prepare(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._prepare()

    def test_roundtrip_keeps_nested_fields(self):
        e = entry("cgt-1")
        self.store.put(e)
        got = self.store.get("cgt-1")
        self.assertEqual(got["requested"], e["requested"])
        self.assertEqual(got["warnings"], ["w"])
        self.assertEqual(got["taskId"], "up-cgt-1")

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_delete_returns_the_entry_then_reports_missing(self):
        self.store.put(entry("cgt-2"))
        self.assertEqual(self.store.delete("cgt-2")["id"], "cgt-2")
        self.assertIsNone(self.store.delete("cgt-2"))
        self.assertIsNone(self.store.get("cgt-2"))

    def test_put_is_upsert(self):
        self.store.put(entry("cgt-3", effective={"billed": True}))
        self.store.put(entry("cgt-3", effective={"billed": False}))
        self.assertEqual(self.store.count(), 1)
        self.assertFalse(self.store.get("cgt-3")["effective"]["billed"])

    def test_list_is_newest_first_and_pages(self):
        base = int(time.time() * 1000)
        for i in range(5):
            self.store.put(entry(f"cgt-{i}", base + i * 1000))
        self.assertEqual(
            [e["id"] for e in self.store.list_recent(10)],
            ["cgt-4", "cgt-3", "cgt-2", "cgt-1", "cgt-0"],
        )
        self.assertEqual([e["id"] for e in self.store.list_recent(2, 2)], ["cgt-2", "cgt-1"])

    def test_count(self):
        self.assertEqual(self.store.count(), 0)
        self.store.put(entry("a"))
        self.store.put(entry("b"))
        self.assertEqual(self.store.count(), 2)

    def test_prune_drops_only_expired(self):
        now = int(time.time() * 1000)
        self.store.put(entry("fresh", now))
        self.store.put(entry("stale", now - 8 * DAY_MS))
        self.assertEqual(self.store.prune(7), 1)
        self.assertIsNone(self.store.get("stale"))
        self.assertIsNotNone(self.store.get("fresh"))

    def test_describe_shape(self):
        d = self.store.describe()
        self.assertIn(d["kind"], ("sqlite", "memory"))
        self.assertIn("durable", d)


class TestMemoryStore(StoreContractMixin, unittest.TestCase):
    def _prepare(self):
        return MemoryTaskStore()


class TestSqliteStore(StoreContractMixin, unittest.TestCase):
    def _prepare(self):
        self._tmp = tempfile.TemporaryDirectory()
        return SqliteTaskStore(str(Path(self._tmp.name) / "tasks.db"))

    def tearDown(self):
        self._tmp.cleanup()


class TestSqliteDurability(unittest.TestCase):
    """本模块存在的理由 —— 用**独立实例**模拟进程重启。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "tasks.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_new_instance_sees_what_the_previous_one_wrote(self):
        SqliteTaskStore(self.db).put(entry("cgt-persist"))
        fresh = SqliteTaskStore(self.db)  # 全新实例 = 模拟进程重启
        self.assertEqual(fresh.get("cgt-persist")["id"], "cgt-persist")
        self.assertEqual(fresh.count(), 1)

    def test_memory_store_loses_data_on_a_new_instance_by_design(self):
        """对照组：内存后端"会丢"是设计如此 —— 所以它只能是显式开关。"""
        MemoryTaskStore().put(entry("cgt-lost"))
        self.assertIsNone(MemoryTaskStore().get("cgt-lost"))

    def test_concurrent_writes_do_not_lose_records(self):
        store = SqliteTaskStore(self.db)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(10):
                    store.put(entry(f"cgt-{n}-{i}"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(store.count(), 60)
        self.assertEqual(len(store.list_recent(100)), 60)

    def test_database_file_lands_where_configured(self):
        SqliteTaskStore(self.db).put(entry("cgt-file"))
        self.assertTrue(Path(self.db).exists())


class TestStoreFactory(unittest.TestCase):
    def test_sqlite_is_the_default_and_is_durable(self):
        with tempfile.TemporaryDirectory() as d:
            s = build_task_store("sqlite", str(Path(d) / "t.db"))
            self.assertEqual(s.kind, "sqlite")
            self.assertTrue(s.describe()["durable"])

    def test_memory_is_explicit(self):
        self.assertEqual(build_task_store("memory", "").kind, "memory")
        self.assertFalse(build_task_store("memory", "").describe()["durable"])

    def test_unknown_backend_is_refused_not_silently_downgraded(self):
        """写错配置就报错 —— 静默退回内存会让持久化在没人注意时失效。"""
        with self.assertRaises(ValueError):
            build_task_store("redis-typo", "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
