"""gunicorn 配置 —— ark 兼容服务的生产/高可用形态。

    PYTHONPATH=src gunicorn -c src/gunicorn_conf.py asgi_app:app

═══════════════════════════════════════════════════════════════════════════════
先读这两条硬约束，它们决定了本文件为什么这样配（不是照抄网上模板）
═══════════════════════════════════════════════════════════════════════════════

① **上游并发闸门是「进程内」的 ⇒ worker 数不能随手调大。**

   `src/ark_compat/web_queue.py` 的 `WebSubmitQueue` 用 `threading.Semaphore(max_concurrent)`
   限流，且槽位**从任务创建一直占到达终态**（不是提交返回就释放）。上游 premium 套餐
   同时只跑 2 个任务，所以「全局只跑 2 个」这个保证**依赖单进程**。

   开 N 个 worker ⇒ 实际全局并发变成 N×2 ⇒ 第 3 个起上游直接报
   `The queue is full. The premium plan can only run 2 task at a time.`
   并且更容易触发 Turnstile 闸门。

   本文件的做法是**显式换算**，而不是默默放开或默默乘以 N：
     全局额度 ÷ worker 数，向下取整、每 worker 至少 1，并把实际全局值打印出来。
   于是存在一个恰好安全的高可用组合：

       AVM_MAX_CONCURRENT=2  +  AVM_WORKERS=2   →  每 worker 1，实际全局 **仍是 2** ✅

   既拿到了 worker 级故障隔离（挂一个不影响服务），又没有超上游限制。
   若换算后实际全局 > 配置额度（例如 workers=3、全局=2 ⇒ 每 worker 1 ⇒ 实际 3），
   会明确告警 —— 不假装还是 2 个。

   代价（必须知道）：`/healthz` 的 `submit_queue` 统计是**单 worker 视图**，不是全局；
   任务表虽落在 SQLite（多进程可读），但闸门状态不共享。

② **`timeout` 必须显著大于闸门等待上限，否则等槽的 worker 会被 gunicorn 杀掉。**

   `WebSubmitQueue.acquire_timeout` 默认 **600s**，且**没有暴露成环境变量**
   （构造点见 `src/ark_compat/upstreams.py`，未传该参数）。也就是说一个提交请求
   合法地可以阻塞 10 分钟等空槽。gunicorn 默认 `timeout=30` 会在 30 秒时把 worker
   判定为卡死并杀掉 —— 表现为随机的 502/连接重置，且很难归因。

   所以 `AVM_GUNICORN_TIMEOUT` 默认取 **660s**（600 等待 + 60 余量）。

═══════════════════════════════════════════════════════════════════════════════
本文件覆盖的高可用措施 / 与「没覆盖」的边界
═══════════════════════════════════════════════════════════════════════════════

覆盖：
  · worker 崩溃由 master 自动重建（gunicorn 内建）+ `worker_exit` 钩子留痕；
  · 多 worker 故障隔离（见约束①的安全组合）；
  · 优雅退出：`graceful_timeout` 给在途请求收尾时间，配合容器 stop_grace_period；
  · fd 保护：`worker_connections` / `backlog` 显式设限，避免慢连接打爆句柄；
  · 心跳文件避开磁盘 IO（`worker_tmp_dir`，仅当 /dev/shm 可用）；
  · 可选**存活看门狗**（AVM_GUNICORN_WATCHDOG=1）：连续 N 次本地健康检查失败就
    让 master 退出，交给容器 restart policy 拉起 —— 这是唯一能覆盖「master 活着
    但 worker 全部僵死」的手段。

未覆盖（诚实清单，别指望本文件解决）：
  · **多副本 / 多容器横向扩展**：闸门是进程内的，副本数会同样放大上游并发。
    真要横向扩展必须先把闸门换成**共享**实现（Redis / DB 租约），这是代码改动，
    而项目当初明确选择了不上 Redis。所以当前**不支持**多副本。
  · **健康检查触发的自动重启**：Docker 的 `HEALTHCHECK` 只标记 unhealthy，
    默认**不会**重启容器（Swarm/K8s 才会）。要么开本文件的可选看门狗，
    要么另配 autoheal 类代理。
  · 零停机发布：单容器只能靠 `docker compose up -d --build` 后的短暂中断。
"""

from __future__ import annotations

import os
import signal
import threading
import time
import urllib.request

# --------------------------------------------------------------------------- #
# 工具                                                                          #
# --------------------------------------------------------------------------- #


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[gunicorn] {name} 必须是整数（收到 {raw!r}）") from None


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _log(msg: str) -> None:
    # 配置在 master 里求值，此时 gunicorn 的 logger 尚未就绪 ⇒ 直接写 stderr
    print(f"[gunicorn] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 监听                                                                          #
# --------------------------------------------------------------------------- #

# 默认与项目一致（127.0.0.1）。容器里由 Dockerfile 的 ENV 置为 0.0.0.0，
# 所以镜像开箱即可被端口映射访问，而本机直接跑时不会意外对外暴露。
bind = f"{os.environ.get('ARK_HOST', '127.0.0.1')}:{_int('ARK_PORT', 8808)}"

# --------------------------------------------------------------------------- #
# worker 数与上游闸门额度（见文档①）                                             #
# --------------------------------------------------------------------------- #

workers = max(1, _int("AVM_WORKERS", 1))

_GLOBAL_CAP = max(1, _int("AVM_MAX_CONCURRENT", 2))
if workers > 1:
    per_worker = max(1, _GLOBAL_CAP // workers)
    effective = per_worker * workers
    # 用 env 下发给子进程：worker fork 自 master，会继承到设置。
    os.environ["AVM_MAX_CONCURRENT"] = str(per_worker)
    _log(
        f"多 worker 换算：全局 AVM_MAX_CONCURRENT={_GLOBAL_CAP} ÷ {workers} "
        f"=> 每 worker {per_worker}，实际全局 {effective}"
    )
    if effective > _GLOBAL_CAP:
        _log(
            f"⚠️  实际全局提交并发 {effective} > 配置额度 {_GLOBAL_CAP}；"
            "上游 premium 同时只跑 2 个任务，超出会报 'The queue is full'。"
            "如非必要请设 AVM_WORKERS=1，或把 worker 数降到 ≤ 额度。"
        )

# PR 建议：uvicorn 自带的 uvicorn.workers 已废弃（导入即告警），用维护中的替代包。
worker_class = "uvicorn_worker.UvicornWorker"

# 不 preload：preload 会在 master 里建 app 再 fork，导致 SQLite 连接与 logfire SDK
# 状态被多个子进程共享 —— 这两样都不该跨进程继承。
preload_app = False

# --------------------------------------------------------------------------- #
# 超时（见文档②）                                                                #
# --------------------------------------------------------------------------- #

# 闸门等待上限 600s（不可配）+ 余量。绝不能沿用 gunicorn 默认的 30s。
timeout = _int("AVM_GUNICORN_TIMEOUT", 660)

# 收到 SIGTERM 后在途请求的收尾窗口。要 ≤ 容器的 stop_grace_period，
# 否则容器先 SIGKILL，gunicorn 来不及优雅退出。
graceful_timeout = _int("AVM_GUNICORN_GRACEFUL_TIMEOUT", 30)

# 长连接复用（前面有反向代理时，建议略大于代理的 idle timeout）
keepalive = _int("AVM_GUNICORN_KEEPALIVE", 5)

# --------------------------------------------------------------------------- #
# worker 回收                                                                   #
# --------------------------------------------------------------------------- #

# ⚠️ 默认关闭（0）。本服务的请求可能合法地持续数分钟（等上游空槽最长 600s，
#    出片 2–9 分钟）。max_requests 触发的回收虽然走 SIGTERM，
#    但 graceful_timeout(30s) 远小于这种在途时长 ⇒ 会被 SIGKILL、请求直接丢失。
#    内存卫生因此交给容器层的重建，而不是这里。
max_requests = _int("AVM_GUNICORN_MAX_REQUESTS", 0)
max_requests_jitter = _int("AVM_GUNICORN_MAX_REQUESTS_JITTER", 0) if max_requests else 0

# 注：**不要**在这里写 `worker_abort = None`。gunicorn 对该项有 validate_callable
# 校验，传 None 会在启动时直接抛 `TypeError: Value is not callable: None`，
# 结果是服务根本起不来（实测确认）。长时间无响应的 worker 由上面的 timeout 杀掉即可。

# --------------------------------------------------------------------------- #
# 连接与 fd 保护                                                                #
# --------------------------------------------------------------------------- #

# UvicornWorker 把它映射为 uvicorn 的 limit_concurrency。显式设限，
# 避免慢连接堆积把 fd / 内存打爆（默认无上限）。
worker_connections = _int("AVM_GUNICORN_WORKER_CONNECTIONS", 1000)

backlog = _int("AVM_GUNICORN_BACKLOG", 2048)

# 心跳临时文件走 tmpfs：避开磁盘 IO 抖动导致的「Worker failed to boot」误判。
# ⚠️ macOS 没有 /dev/shm，必须探测后再用，否则本机直接跑会起不来。
if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
    worker_tmp_dir = "/dev/shm"
else:
    _log("未启用 worker_tmp_dir=/dev/shm（当前平台不可用），使用系统默认临时目录")

# 反向代理期望：只信任本机（gunicorn 默认）。容器里前面挂了同网络的反代时，
# 该反代来源 IP 不是 127.0.0.1，需要显式放宽 —— 用 AVM_GUNICORN_FORWARDED_IPS 覆盖。
forwarded_allow_ips = os.environ.get("AVM_GUNICORN_FORWARDED_IPS") or "127.0.0.1"

# --------------------------------------------------------------------------- #
# 日志                                                                          #
# --------------------------------------------------------------------------- #

# 访问日志已由 app 的 request_context 中间件统一输出（对应 uvicorn 侧 access_log=False），
# 这里关掉 gunicorn 自身的 access log，避免同一请求打两遍。
accesslog = None
errorlog = "-"
loglevel = (os.environ.get("AVM_GUNICORN_LOGLEVEL") or "info").strip().lower()
capture_output = True      # 把 app 的 stdout/stderr 收进 gunicorn 错误日志

proc_name = os.environ.get("AVM_SERVICE_NAME") or "ark-compat"

# --------------------------------------------------------------------------- #
# 高可用钩子                                                                    #
# --------------------------------------------------------------------------- #


def on_starting(server):  # noqa: ARG001
    _log(f"启动：bind={bind} workers={workers} worker_class={worker_class} timeout={timeout}s")


def when_ready(server):
    _log(f"就绪：{bind}（workers={workers}）")
    if _flag("AVM_GUNICORN_WATCHDOG"):
        _start_watchdog(server)


def worker_exit(server, worker):  # noqa: ARG001
    """worker 死亡留痕 —— 没有这行，worker 被 OOM/timeout 杀掉是静默的。

    这里只记录，不尝试干预：重建由 master 负责。
    """
    _log(f"worker 退出：pid={worker.pid} age={getattr(worker, 'age', '?')}s")


def child_exit(server, worker):  # noqa: ARG001
    _log(f"worker 回收完成：pid={worker.pid}")


# --------------------------------------------------------------------------- #
# 可选存活看门狗                                                                #
# --------------------------------------------------------------------------- #


def _start_watchdog(server) -> None:
    """连续 N 次本地 /healthz 失败 ⇒ 让 master 退出，由容器 restart policy 拉起。

    为什么要它：Docker 的 HEALTHCHECK **不会**重启容器，只会把状态标成 unhealthy。
    若 master 还活着但 worker 全部僵死，容器会永远停在 unhealthy。
    这是本文件里唯一能覆盖该场景的手段。

    ⚠️ 风险与护栏：判定错误会造成重启循环，所以
      · 默认**关闭**（AVM_GUNICORN_WATCHDOG=1 才启用）；
      · 必须**连续** N 次失败才动作（默认 5 次 × 30s ≈ 150s 窗口）；
      · 动作是给自己发 SIGTERM（走优雅退出），不是 _exit —— 让 gunicorn 自己收尾。
    """
    interval = max(5, _int("AVM_GUNICORN_WATCHDOG_INTERVAL", 30))
    failures_needed = max(2, _int("AVM_GUNICORN_WATCHDOG_FAILURES", 5))
    url = (os.environ.get("AVM_GUNICORN_WATCHDOG_URL") or "").strip() or (
        f"http://127.0.0.1:{_int('ARK_PORT', 8808)}/healthz"
    )

    def _loop() -> None:
        fails = 0
        while True:
            time.sleep(interval)
            try:
                # 绕开代理：看门狗打的是回环，不该被 HTTP_PROXY 接管
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=5) as resp:
                    ok = resp.status == 200
            except Exception as e:  # noqa: BLE001
                ok = False
                err = f"{type(e).__name__}: {e}"
            else:
                err = ""

            if ok:
                if fails:
                    _log(f"看门狗：健康检查恢复（此前连续 {fails} 次失败）")
                fails = 0
                continue

            fails += 1
            _log(f"看门狗：{url} 健康检查失败 {fails}/{failures_needed}（{err}）")
            if fails >= failures_needed:
                _log(
                    f"看门狗：连续 {fails} 次失败，向 master 发 SIGTERM 触发容器重建。"
                    "若这是误判，请调大 AVM_GUNICORN_WATCHDOG_FAILURES 或关掉看门狗。"
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=_loop, name="ark-liveness-watchdog", daemon=True).start()
    _log(f"看门狗已启用：{url}，间隔 {interval}s，连续 {failures_needed} 次失败即重启")
