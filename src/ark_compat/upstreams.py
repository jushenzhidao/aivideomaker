"""上游的统一接口（本项目只有一条：网页端内部接口）。

`app.py` 只认这一层，不关心背后是 tRPC 还是别的 —— 认证方式、参数形状、
能否取消、并发限制全部被 `WebUpstream` 吸收。

接口刻意很小：

    create(plan) -> taskId       提交（**唯一会产生费用的动作**）
    get_task(taskId) -> dict     读任务，**已归一化成 Ark 任务对象**
    balance() -> int | None      余额
    health() -> dict             /healthz 用

另有两个**受理分离专用**的记账方法（`begin_submit` / `end_submit`）与派生属性
`idle` —— 它们回答的是"这个上游此刻能不能被安全关掉"，见 `idle` 的说明。

（**没有** cancel/delete：站点没有取消端点，"删本地记录"只会制造任务已消失的
错觉 —— 对外接口面刻意只有创建 + 查询，记录随保留窗口自然淘汰。）

计费语义（见 `translate.billing_view`）：

==========  ==========================================================
`web`       `tier=base` 一律计费；`tier=turbo` 且 ≤10s **免费**；并发上限**按套餐**（premium 2 / pro 4）
==========  ==========================================================
"""

from __future__ import annotations

import base64
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, Callable

from loguru import logger

from .minter import TokenMinter
from .translate import normalize_web_task
from .web_client import MediaFetchBudget, WebClient
from .web_queue import WebSubmitQueue

_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)
_SITE_CDN_RE = re.compile(r"^https?://static\d*\.img2video\.ai/", re.I)

#: 转存的**默认并发上限**（2026-09-20 受理分离配套；2026-10-21 由写死的 4 改为可配）。
#: 一次创建最多 7 项互相独立（图 4 / 视频 1 / 音频 2），线程大部分时间在等网络 ⇒
#: 把并发提到**项数**（`min(len(jobs), workers)`）能把"分摊两轮"变成"一轮"，
#: 4 张图约省 1~3s。生产实测没有出现被站点上传限速打回的迹象（PUT 失败只有 4 条，
#: 且都是超时/断连）。
#: 预算语义不变（`MediaFetchBudget` 只读 deadline，线程安全）；失败语义也不变：
#: 任一项失败 ⇒ 整次创建失败。
_REHOST_MAX_WORKERS = 7


def _decode_data_uri(value: str) -> dict | None:
    """解出内联 base64 媒体，供站点上传接口使用。"""
    m = _DATA_URI_RE.match(value or "")
    if not m:
        return None
    try:
        return {"mime": m.group(1), "buf": base64.b64decode(m.group(2))}
    except Exception:  # noqa: BLE001
        return None


class WebUpstream:
    """网页端内部接口（会话 cookie）。有免费窗口；没有取消端点，也不提供删除。"""

    kind = "web"

    def __init__(self, client: WebClient, queue: WebSubmitQueue, *, media_rehost_budget: float = 90.0,
                 rehost_workers: int = _REHOST_MAX_WORKERS):
        self.client = client
        self.queue = queue
        self.rehost_workers = max(1, int(rehost_workers))
        # 整个转存阶段的总预算（秒）。见 `MediaFetchBudget`：没有它，最多 7 个媒体项
        # 串行转存的最坏耗时会把调用方的超时甩在后面，而我们还接着把任务提交出去。
        self.media_rehost_budget = max(1.0, float(media_rehost_budget))
        # 「已受理、提交还没写完记录」的条数（受理分离配套）。**必须**与本类的
        # `idle` 判据同锁读写，否则淘汰侧会读到撕裂的中间态。
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def create(self, plan: dict) -> str:
        budget = MediaFetchBudget(self.media_rehost_budget)
        token = plan.get("captcha_token")
        # ★ 2026-10-21：**转存期间并行预热闸门探测**。
        #   `needs_captcha()` 是提交关键路径上的一次上游往返（实测 0.1~1s），而转存
        #   通常要 1~3s（下载 + 预签名 + PUT）—— 把它塞进这段时间等于白赚。
        #   探测是只读、不占上游槽位、不产生计费，失败也无所谓（缓存没热就照旧再探一次）。
        #   · 有 BYO token 时不必探（`_create_once` 里只在 `not token` 时才问闸门）；
        #   · 缓存 TTL=0（关掉）时也不探，否则等于凭空多一次请求。
        # `getattr` 兜底：测试替身与"旧客户端"没有这个属性时不预热（安全降级）。
        # 真实的 `WebClient` 永远有它，所以生产行为不受影响。
        if not token and getattr(self.client, "captcha_probe_ttl", 0.0) > 0:
            try:
                ctx = copy_context()
                threading.Thread(
                    target=ctx.run, args=(self.client.needs_captcha,),
                    name="avm-gate-warm", daemon=True,
                ).start()
            except Exception as e:  # noqa: BLE001 预热失败绝不影响业务
                logger.debug("闸门预热线程起不来（不影响提交）：{}", e)
        params = self._rehost(dict(plan["web_params"]), budget)
        return self.queue.submit(params, token=token)

    # ---- 媒体转存 ----------------------------------------------------------

    def _rehost(self, params: dict, budget) -> dict:
        """把外链 / 内联 data URI 媒体转存到**站点自己的 CDN**。

        站点只收自己 CDN 的地址：外链会按 Content-Type 白名单被拒
        （实测过一个 `.jpg` 外链返回非标准 MIME `image/jpg`，字节其实是 PNG）。
        已经是 `static*.img2video.ai` 的地址原样放过，不重复上传。

        ⚠️ 2026-09-20 起**并行**执行（`_REHOST_MAX_WORKERS`，受理分离的配套 ——
        这段耗时现在发生在后台线程，但压它仍直接影响"多久能真正提交出去"）：
        最多 7 项互相独立，串行时总耗时 = 各项之和（3 张图各 3s ⇒ 9s），并行后 ≈ 最慢一项。
        预算语义不变（任一项发现超预算就抛 504，**点名**是哪一步哪一项）；
        失败语义不变：任一项失败 ⇒ 整次创建失败（结果按提交顺序回收，第一个异常照原样上抛）。
        """
        jobs: list[tuple[str, Any]] = []
        for key in ("imageUrl", "lastFrameUrl", "referenceVideoUrl"):
            # 只给**真有值**的项起任务：空值也会走到 `_upload_one` 里空转一趟，
            # 白白占掉并发名额（4 张图的请求会因此从 4 个 worker 变成 7 个里 3 个空转）。
            if params.get(key):
                jobs.append((key, params.get(key)))
        for key in ("referenceImageUrls", "referenceAudioUrls"):
            vals = params.get(key)
            if isinstance(vals, list) and vals:
                for i, v in enumerate(vals):
                    jobs.append((f"{key}[{i}]", v))
        if not jobs:
            return params  # 纯文生视频：没有要转存的东西，连线程池都不必起

        with ThreadPoolExecutor(max_workers=min(len(jobs), self.rehost_workers)) as pool:
            # 🔴 必须**带着上下文**进工作线程：上传链路的证据采集（`note_upstream`）靠
            #    contextvar 找采集箱，而 ThreadPoolExecutor 的线程**不继承**提交者的上下文
            #    ⇒ 不 copy 的话，上传发生在"没有采集箱"的线程里，`ark.create.submit` 的
            #    `upstream_calls` 会**静默变空**（2026-09-20 实测踩中：并行化之后
            #    download/presign/PUT 的留痕全部消失，靠失败路径的门禁抓回来的）。
            #    ⚠️ **每项各自 copy 一份**：`Context.run` 不可重入（并行两个线程进同一个
            #    Context 会抛 "already entered"），但每份拷贝里装的**引用**是同一个采集箱，
            #    各线程照样写进同一个 box（list.append 原子）。
            futures = [
                pool.submit(copy_context().run, self._upload_one, value, budget)
                for _, value in jobs
            ]
            # 按提交顺序回收 ⇒ 第一个异常就是"第一项"的异常，与串行时代一致
            uploaded = [f.result() for f in futures]
        for (key, _old), new in zip(jobs, uploaded):
            if key.endswith("]"):
                base, idx = key[:-1].split("[")
                params[base][int(idx)] = new
            else:
                params[key] = new
        return params

    def _upload_one(self, value, budget=None):
        if not isinstance(value, str) or not value:
            return value
        if _SITE_CDN_RE.match(value):
            return value  # 已经在站点 CDN 上
        data = _decode_data_uri(value)
        if data:
            return self.client.upload_file(data["buf"], name="inline", budget=budget)["publicUrl"]
        if value.startswith("data:"):
            return value  # 解不开的 data URI 原样透传，让上游给出更明确的报错
        if re.match(r"^https?://", value, re.I):
            return self.client.upload_file(value, budget=budget)["publicUrl"]
        return value

    # ---- 在飞提交记账（受理分离配套）--------------------------------------

    def begin_submit(self) -> None:
        """受理侧登记：这条任务**已经拿到 id**、提交压后做 —— 从此刻起本上游不算空闲。

        必须**先于**任务落库调用（见 `app._submit_ark`）：落库与调度之间即使只有毫秒级
        窗口，淘汰侧也可能恰好在那时扫到"running 为空"。
        """
        with self._in_flight_lock:
            self._in_flight += 1

    def end_submit(self) -> None:
        """推进侧注销：无论提交成功、失败、还是线程被掐断，都必须调用一次。

        用 `max(0, …)` 兜底而不是直接自减：多减一次会让计数**变负**并永久骗过 `idle`
        （那个上游从此再也不会被淘汰，只会占着内存与 socket）。
        """
        with self._in_flight_lock:
            self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    @property
    def idle(self) -> bool:
        """这个上游此刻**能不能被安全关掉**（透传缓存的淘汰判据，唯一定义处）。

        两个条件同时成立才算空闲：

          ① 闸门里没有正在跑的任务（`queue.stats()["running"]`）—— 关掉正在被轮询的
             client 会让那次轮询失败（现象是"任务查不到"，见 `app._sweep`）；
          ② **没有在飞提交**（`in_flight`）—— 这是受理分离带来的**新窗口**：任务已受理
             （上行请求早已返回 id），但提交还压在后台线程里（转存 / 抢槽位 / create）。
             那个线程**正握着这个 client**，此时 `client.close()` 会让提交直接炸
             （httpx 对已关闭客户端的请求即抛错）。

        ② 是 2026-10-12 补的：此前只判 ①，于是在**多账号透传**下，A 账号的提交正在后台跑、
        缓存又恰好满、B 账号来建新客户端 ⇒ A 被误判空闲并关掉 ⇒ 那种"偶发提交失败"
        查起来毫无头绪（记录里只有一句 client closed，指向不了淘汰逻辑）。
        """
        with self._in_flight_lock:
            if self._in_flight:
                return False
        return not self.queue.stats()["running"]

    def get_task(self, task_id: str) -> dict:
        return normalize_web_task(self.client.get_task(task_id))

    def balance(self) -> int | None:
        return self.client.get_credits()

    def health(self) -> dict:
        info: dict[str, Any] = {
            "upstream": self.kind,
            "base_url": self.client.base_url,
            "submit_queue": self.queue.stats(),
            # 受理分离：已受理但提交尚未写完记录的条数。它与 submit_queue.running
            # **相加**才是"这个上游此刻真正占用的任务数"（淘汰判据 `idle` 认的也是这两者）。
            "pending_submits": self.in_flight,
        }
        try:
            info["balance"] = self.balance()
        except Exception as e:  # noqa: BLE001
            info["upstream_error"] = str(e)
        return info


def _fallback_slots(settings) -> int:
    """探测不可用时的槽位数（已按 worker 数分摊）。"""
    divisor = max(1, int(getattr(settings, "concurrency_divisor", 1) or 1))
    return max(1, settings.max_concurrent_fallback // divisor)


def resolve_max_concurrent(settings, client, *, probe: bool = True) -> int:
    """定提交闸门槽位。

    `settings.max_concurrent > 0` ⇒ 用它（显式覆盖优先）。
    `settings.max_concurrent == 0` ⇒ **按账号额度自动**：读
    `ai.queryUserPermission.maxQueueLength`（premium 2 / pro 4）。
    读不到（网络抖动 / 站点改字段）⇒ 回落到 `max_concurrent_fallback`，并留一条日志 ——
    这里**不能**猜成"没限制"，那会把上游配额直接撞满。

    ★ `probe=False`（2026-10-21，**透传建实例用**）：**绝不在受理路径上等这次往返**。
      透传模式下每个新账号的**首条** `POST /tasks` 都要现建上游，而这个探测是跨境的
      一次完整往返（新 HTTP 客户端还要先做 TLS 握手，实测把"受理即返"从毫秒拖到 3.1s）。
      改成：先用 `max_concurrent_fallback` 把闸门建起来立即服务，额度交给
      `_probe_capacity_async` 后台补许可（`WebSubmitQueue.raise_capacity`，只放宽）。
    """
    if settings.max_concurrent > 0:
        return settings.max_concurrent
    if not probe:
        return _fallback_slots(settings)
    divisor = max(1, int(getattr(settings, "concurrency_divisor", 1) or 1))
    try:
        perm = client.get_permission() or {}
        n = int(perm.get("maxQueueLength") or 0)
        if n > 0:
            slots = max(1, n // divisor)   # 多 worker：把账号额度向下分摊到每个 worker
            if divisor > 1:
                logger.info(
                    "提交闸门槽位按账号额度自动定为 {}（额度 {} ÷ {} worker，planName={}）",
                    slots, n, divisor, perm.get("planName") or "?",
                )
            else:
                logger.info(
                    "提交闸门槽位按账号额度自动定为 {}（planName={}）", n, perm.get("planName") or "?"
                )
            return slots
        fallback = max(1, settings.max_concurrent_fallback // divisor)
        logger.warning("账号额度读不到 maxQueueLength ⇒ 回落 {} 槽（已按 {} worker 分摊）",
                       fallback, divisor)
    except Exception as e:  # noqa: BLE001 探测失败不该让建上游失败
        logger.warning("账号额度探测失败（{}: {}）⇒ 回落 {} 槽（已按 {} worker 分摊）",
                       type(e).__name__, e,
                       max(1, settings.max_concurrent_fallback // divisor), divisor)
    return max(1, settings.max_concurrent_fallback // divisor)


def _probe_capacity_async(upstream: "WebUpstream", settings) -> None:
    """后台探账号额度，探到就**补许可**（只放宽）。

    为什么要 `begin_submit`/`end_submit` 包着：探测线程握着这个 client，而
    `WebUpstream.idle` 的判据是"闸门没在跑 **且** 没有在飞提交" —— 不登记的话，
    多账号透传缓存恰好满时，这个刚建好还在探测的 client 可能被判空闲并 close，
    探测就会莫名其妙地失败（现象是"偶发没有额度日志"）。
    """
    def run() -> None:
        upstream.begin_submit()
        try:
            perm = upstream.client.get_permission() or {}
            n = int(perm.get("maxQueueLength") or 0)
            plan = perm.get("planName") or "?"
            if n <= 0:
                logger.warning(
                    "后台额度探测：读不到 maxQueueLength（planName={}）⇒ 保持 {} 槽",
                    plan, upstream.queue.max_concurrent,
                )
                return
            divisor = max(1, int(getattr(settings, "concurrency_divisor", 1) or 1))
            slots = max(1, n // divisor)
            final = upstream.queue.raise_capacity(slots)
            logger.info(
                "后台额度探测完成：额度 {} ÷ {} worker ⇒ {} 槽（planName={}），闸门现为 {} 槽",
                n, divisor, slots, plan, final,
            )
        except Exception as e:  # noqa: BLE001 探测失败不能影响服务
            logger.warning(
                "后台额度探测失败（{}: {}）⇒ 保持 {} 槽",
                type(e).__name__, e, upstream.queue.max_concurrent,
            )
        finally:
            upstream.end_submit()

    threading.Thread(target=run, name="avm-perm-probe", daemon=True).start()


def _build_web(
    settings,
    log: Callable[[str], None] | None = None,
    *,
    cookie: str | None = None,
    probe_concurrency: bool = True,
) -> WebUpstream:
    """建一个 web 上游。`cookie=None` 用本进程自己那份（AVM_COOKIE）。

    `user_id` 只在"用本进程 cookie"时继承配置；透传时必须是 `""`（明确不指定），
    否则会把**默认账号**的 userId 配到调用方的会话上 —— 列表类接口按错误的
    userId 查，现象是"任务明明存在却查不到"。
    """
    passthrough = cookie is not None
    client = WebClient(
        settings.cookie if cookie is None else cookie,
        base_url=settings.base_url,
        # user_id 是**账号身份**，透传时绝不能继承（见 docstring）；
        # visitor_id 只是"访客"标记、服务端不校验（web_client.py 的注释），
        # 部署级共用取值即可。
        user_id="" if passthrough else settings.user_id,
        visitor_id=settings.visitor_id,
        trust_env=settings.trust_env,
        # 主调用超时：此前吃 WebClient 的硬编码默认 30 ⇒ 上游慢于 30s 时没有出口可调
        timeout=settings.upstream_timeout,
        probe_timeout=settings.probe_timeout,
        # 取"参考文件链接"的单项预算：调大了就是把调用方的超时甩在后面（见 _fetch_media）
        media_fetch_timeout=settings.media_fetch_timeout,
        # 闸门开着时会自动去铸造服务取 token（未配置则为 None ⇒ 行为不变）
        minter=TokenMinter(
            settings.minter_url,
            settings.minter_key,
            settings.minter_timeout,
            # 铸造的重试次数要一起传下去，否则 `AVM_MINTER_RETRIES` 配了也不生效
            retries=getattr(settings, "minter_retries", 1),
            # 提前量（提前过 CF）：池子空时声明"接下来还要 N 个"，见 TokenMinter 的说明
            reserve=getattr(settings, "minter_reserve", 8),
        ),
        # 提交重试（只对"上游确定没接单"的失败生效，见 web_client.retryable_submit_error）
        submit_retries=settings.submit_retries,
        submit_retry_backoff=settings.submit_retry_backoff,
        # 转存（下载外链 / PUT 到站点 CDN）的重试：幂等操作
        media_retries=settings.media_retries,
        # 闸门探测的短期缓存（省掉每条提交的一次上游往返）
        captcha_probe_ttl=settings.captcha_probe_ttl,
    )
    queue = WebSubmitQueue(
        client,
        max_concurrent=resolve_max_concurrent(settings, client, probe=probe_concurrency),
        poll_interval=settings.poll_interval,
        # 闸门盯梢（轮询）的总时限，此前只能吃构造默认值 3600
        watch_timeout=settings.watch_timeout,
        log=log,
    )
    return WebUpstream(
        client, queue,
        media_rehost_budget=settings.media_rehost_budget,
        rehost_workers=settings.rehost_workers,
    )


def build_upstreams(settings, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """构造**本进程持有凭据**的 web 上游。

    ⚠️ 只建**本进程持有凭据**的那份：透传线的客户端必须等看到调用方凭据才能建
    （见 `build_web_for_cookie`）。所以 `AVM_AUTH=passthrough` 且没配
    `AVM_COOKIE` 时，这里返回**空字典** —— 但服务依然可用。
    """
    if not settings.cookie:
        return {}
    return {"web": _build_web(settings, log)}


def build_web_for_cookie(settings, cookie: str, log: Callable[[str], None] | None = None) -> WebUpstream:
    """透传模式：用调用方自带的**网页会话 cookie** 现建一个 web 上游。

    这正是"走逆向线"的落地形态 —— 本进程不需要 AVM_COOKIE，每个调用方用自己的
    会话，各自的免费窗口与上游并发额度都归各自的账号。

    并发闸门是**每个上游一把**（`WebSubmitQueue._sem`），这是对的：上游那条限制
    是按账号算的（"The premium plan can only run 2 task at a time"），不同 cookie
    就是不同账号。已知限制仍是「同一 cookie 的 2 个槽位是进程内状态」⇒
    多 worker 下同一账号可能同时跑 2×worker 个（见 gunicorn_conf 的说明）。
    """
    upstream = _build_web(settings, log, cookie=cookie, probe_concurrency=False)
    # 只有"要自动探测额度"时才需要补：显式配了 `AVM_MAX_CONCURRENT>0` 的部署额度是已知的
    if settings.max_concurrent == 0:
        _probe_capacity_async(upstream, settings)
    return upstream
