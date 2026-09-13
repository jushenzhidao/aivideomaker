"""两条上游的统一接口。

`app.py` 只认这一层，不关心背后是**官方 API** 还是**网页端内部接口**——
差异（认证方式、参数形状、计费口径、能否取消、并发限制）全部被各自的实现吸收。

接口刻意很小：

    create(plan) -> taskId       提交（**唯一会产生费用的动作**）
    get_task(taskId) -> dict     读任务，**已归一化成 Ark 任务对象**
    cancel_task(taskId) -> dict  取消 / 删除
    balance() -> int | None      余额
    health() -> dict             /healthz 用

两条线的计费语义不同（见 `translate.billing_view`），**不能互相套用**：

==========  ==========================================================
`web`       `tier=base` 一律计费；`tier=turbo` 且 ≤8s **免费**；无取消端点；并发上限 2
`official`  **一律计费**，无免费窗口；可取消并全额退积分；`X-Max-Credits` 兜底
==========  ==========================================================
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable

from .client import OfficialClient
from .errors import BudgetUnsetError
from .translate import BUDGET_REQUIRED_MESSAGE, normalize_task, normalize_web_task
from .web_client import WebClient
from .web_queue import WebSubmitQueue


class OfficialUpstream:
    """官方 API（`key` 头）。提交即计费，所以必须有支出上限。"""

    kind = "official"
    supports_cancel = True

    def __init__(self, client: OfficialClient, default_max_credits: int | None = None):
        self.client = client
        self.default_max_credits = default_max_credits

    def create(self, plan: dict) -> str | None:
        max_credits = plan.get("max_credits")
        if max_credits is None:
            max_credits = self.default_max_credits
        if max_credits is None:
            # 官方线提交即计费 —— 拿不到上限就拒绝，且**在发出请求之前**
            raise BudgetUnsetError(BUDGET_REQUIRED_MESSAGE)
        return self.client.create(
            plan["official_model"],
            plan["official_payload"],
            max_credits,
            plan.get("idempotency_key"),
        )

    def get_task(self, task_id: str) -> dict:
        return normalize_task(self.client.get_task(task_id))

    def cancel_task(self, task_id: str) -> dict:
        self.client.cancel_task(task_id)
        return {"cancelled": True, "task_id": task_id, "refund": "full"}

    def balance(self) -> int | None:
        return self.client.balance()

    def health(self) -> dict:
        info: dict[str, Any] = {"upstream": self.kind, "base_url": self.client.base_url}
        try:
            info["balance"] = self.balance()
        except Exception as e:  # noqa: BLE001
            info["upstream_error"] = str(e)
        return info


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
    """网页端内部接口（会话 cookie）。有免费窗口，但没有取消端点。"""

    kind = "web"
    supports_cancel = False

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

    def cancel_task(self, task_id: str) -> dict:
        # 站点**没有**取消端点。删记录 ≠ 取消生成：跑着的任务照跑照扣。
        # 所以这里如实返回 cancelled=false，绝不谎报。
        self.client.delete_tasks([task_id])
        return {
            "cancelled": False,
            "task_id": task_id,
            "reason": (
                "aivideomaker exposes no cancel endpoint for this task type; "
                "only the local record was deleted — a running task keeps running and keeps costing"
            ),
        }

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


def _build_official(settings, *, key: str | None = None) -> OfficialUpstream:
    return OfficialUpstream(
        OfficialClient(
            key or settings.upstream_key_for_client,
            base_url=settings.base_url,
            trust_env=settings.trust_env,
        ),
        default_max_credits=settings.max_credits,
    )


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
        # 两条线共用部署级取值即可。
        user_id="" if passthrough else settings.user_id,
        visitor_id=settings.visitor_id,
        trust_env=settings.trust_env,
    )
    queue = WebSubmitQueue(
        client,
        max_concurrent=settings.max_concurrent,
        poll_interval=settings.poll_interval,
        log=log,
    )
    return WebUpstream(client, queue)


def build_upstreams(settings, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """构造**所有凭据齐全**的上游 —— 两条线可以并存，不是二选一。

    `settings.upstream` 只决定**默认**用哪条；调用方还能按请求覆盖
    （`X-Avm-Upstream: web|official` 头，或 `?upstream=` 查询参数）。
    两条线各有各的长处：web 有免费窗口，official 可取消并全额退积分。

    ⚠️ 只建**本进程持有凭据**的那些：透传线的客户端必须等看到调用方凭据才能建
    （见 `build_web_for_cookie`）。所以 `AVM_PASSTHROUGH_COOKIE=1` 且没配
    `AVM_COOKIE` 时，这里返回的字典里**没有** web —— 但那条线依然可用。
    """
    out: dict[str, Any] = {}
    if settings.official_ready:
        out["official"] = _build_official(settings)
    if settings.cookie:
        out["web"] = _build_web(settings, log)
    return out


def build_official_for_key(settings, key: str) -> OfficialUpstream:
    """透传模式：用调用方自带的 token 现建一个官方线上游。"""
    return _build_official(settings, key=key)


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
