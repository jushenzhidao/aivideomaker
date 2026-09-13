"""web 线上游并发闸门。

上游 premium 套餐**同时只跑 2 个任务**：

    INTERNAL_SERVER_ERROR  The queue is full. The premium plan can only run 2 task at a time.

关键认知：**槽位从创建一直占到任务进入终态**，不是"创建请求返回就释放"。
所以直连转发在第 3 个并发请求就会开始报错。

本模块做三件事：

1. **闸门**：信号量限制同时在上游的任务数（默认 2）；
2. **延迟执行**：超出时**阻塞等待**空槽而不是直接失败 —— 调用方跑在
   `asyncio.to_thread` 里，所以阻塞的是工作线程，不是事件循环；
3. **占到底**：提交成功后起一个后台线程盯到终态，才释放槽位。

与 Node 版 `submit-queue.mjs` 的差别：那版还有"queue is full 时退避重试"，
这里没有 —— 因为闸门已经保证我们不会超过上游并发，真报 queue is full
多半是**浏览器里也在生成**，那种情况应当让调用方自己决定要不要重试。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import WebApiError


class WebSubmitQueue:
    def __init__(
        self,
        client,
        *,
        max_concurrent: int = 2,
        poll_interval: float = 10.0,
        watch_timeout: float = 3600.0,
        acquire_timeout: float = 600.0,
        log: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.max_concurrent = max(1, int(max_concurrent))
        self.poll_interval = poll_interval
        self.watch_timeout = watch_timeout
        self.acquire_timeout = acquire_timeout
        self._log = log or (lambda _m: None)

        self._sem = threading.Semaphore(self.max_concurrent)
        self._lock = threading.Lock()
        self._running: dict[str, float] = {}
        self.served = 0
        self.delayed = 0
        self.rejected = 0

    def stats(self) -> dict:
        with self._lock:
            running = list(self._running)
        return {
            "max_concurrent": self.max_concurrent,
            "running": len(running),
            "running_tasks": running,
            "served_total": self.served,
            "delayed_total": self.delayed,
            "rejected_total": self.rejected,
        }

    def submit(self, params: dict, token: str | None = None) -> str:
        """占一个槽位并提交。返回站点 taskId；槽位由后台线程在终态释放。"""
        t0 = time.time()
        if not self._sem.acquire(timeout=self.acquire_timeout):
            self.rejected += 1
            raise WebApiError(
                "queue",
                f"no upstream slot freed within {self.acquire_timeout:.0f}s "
                f"(max_concurrent={self.max_concurrent})",
                code="QUEUE_TIMEOUT",
                http_status=429,
            )

        waited = time.time() - t0
        if waited > 1.0:
            self.delayed += 1
            self._log(f"[queue] delayed {waited:.0f}s waiting for an upstream slot")

        try:
            task_id = self.client.create(params, token=token)
        except BaseException:
            # 提交没成功就不能占着槽位
            self._sem.release()
            self.rejected += 1
            raise

        with self._lock:
            self._running[str(task_id)] = time.time()
        self.served += 1
        self._log(
            f"[queue] submitted {task_id} — running={len(self._running)}/{self.max_concurrent}"
        )
        threading.Thread(target=self._watch, args=(str(task_id),), daemon=True).start()
        return str(task_id)

    def _watch(self, task_id: str) -> None:
        try:
            res = self.client.wait_for_task(
                task_id, timeout=self.watch_timeout, interval=self.poll_interval
            )
            self._log(f"[queue] slot released ({task_id} -> {res.get('status')})")
        except Exception as e:  # noqa: BLE001
            # 盯梢失败也必须放槽位，否则闸门会永久卡死
            self._log(f"[queue] watcher gave up on {task_id}: {e}")
        finally:
            with self._lock:
                self._running.pop(task_id, None)
            self._sem.release()
