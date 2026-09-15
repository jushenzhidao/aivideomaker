"""上游的统一接口（本项目只有一条：网页端内部接口）。

`app.py` 只认这一层，不关心背后是 tRPC 还是别的 —— 认证方式、参数形状、
能否取消、并发限制全部被 `WebUpstream` 吸收。

接口刻意很小：

    create(plan) -> taskId       提交（**唯一会产生费用的动作**）
    get_task(taskId) -> dict     读任务，**已归一化成 Ark 任务对象**
    balance() -> int | None      余额
    health() -> dict             /healthz 用

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
from typing import Any, Callable

from loguru import logger

from .minter import TokenMinter
from .translate import normalize_web_task
from .web_client import WebClient
from .web_queue import WebSubmitQueue

_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)
_SITE_CDN_RE = re.compile(r"^https?://static\d*\.img2video\.ai/", re.I)


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

    def __init__(self, client: WebClient, queue: WebSubmitQueue):
        self.client = client
        self.queue = queue

    def create(self, plan: dict) -> str:
        params = self._rehost(dict(plan["web_params"]))
        return self.queue.submit(params, token=plan.get("captcha_token"))

    # ---- 媒体转存 ----------------------------------------------------------

    def _rehost(self, params: dict) -> dict:
        """把外链 / 内联 data URI 媒体转存到**站点自己的 CDN**。

        站点只收自己 CDN 的地址：外链会按 Content-Type 白名单被拒
        （实测过一个 `.jpg` 外链返回非标准 MIME `image/jpg`，字节其实是 PNG）。
        已经是 `static*.img2video.ai` 的地址原样放过，不重复上传。
        """
        for key in ("imageUrl", "lastFrameUrl", "referenceVideoUrl"):
            params[key] = self._upload_one(params.get(key))
        for key in ("referenceImageUrls", "referenceAudioUrls"):
            vals = params.get(key)
            if isinstance(vals, list) and vals:
                params[key] = [self._upload_one(v) for v in vals]
        return params

    def _upload_one(self, value):
        if not isinstance(value, str) or not value:
            return value
        if _SITE_CDN_RE.match(value):
            return value  # 已经在站点 CDN 上
        data = _decode_data_uri(value)
        if data:
            return self.client.upload_file(data["buf"], name="inline")["publicUrl"]
        if value.startswith("data:"):
            return value  # 解不开的 data URI 原样透传，让上游给出更明确的报错
        if re.match(r"^https?://", value, re.I):
            return self.client.upload_file(value)["publicUrl"]
        return value

    def get_task(self, task_id: str) -> dict:
        return normalize_web_task(self.client.get_task(task_id))

    def balance(self) -> int | None:
        return self.client.get_credits()

    def health(self) -> dict:
        info: dict[str, Any] = {
            "upstream": self.kind,
            "base_url": self.client.base_url,
            "submit_queue": self.queue.stats(),
        }
        try:
            info["balance"] = self.balance()
        except Exception as e:  # noqa: BLE001
            info["upstream_error"] = str(e)
        return info


def resolve_max_concurrent(settings, client) -> int:
    """定提交闸门槽位。

    `settings.max_concurrent > 0` ⇒ 用它（显式覆盖优先）。
    `settings.max_concurrent == 0` ⇒ **按账号额度自动**：读
    `ai.queryUserPermission.maxQueueLength`（premium 2 / pro 4）。
    读不到（网络抖动 / 站点改字段）⇒ 回落到 `max_concurrent_fallback`，并留一条日志 ——
    这里**不能**猜成"没限制"，那会把上游配额直接撞满。
    """
    if settings.max_concurrent > 0:
        return settings.max_concurrent
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


def _build_web(
    settings,
    log: Callable[[str], None] | None = None,
    *,
    cookie: str | None = None,
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
        probe_timeout=settings.probe_timeout,
        # 闸门开着时会自动去铸造服务取 token（未配置则为 None ⇒ 行为不变）
        minter=TokenMinter(settings.minter_url, settings.minter_key, settings.minter_timeout),
    )
    queue = WebSubmitQueue(
        client,
        max_concurrent=resolve_max_concurrent(settings, client),
        poll_interval=settings.poll_interval,
        log=log,
    )
    return WebUpstream(client, queue)


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
    return _build_web(settings, log, cookie=cookie)
