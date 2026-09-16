"""FastAPI 应用：把 aivideomaker 暴露成火山方舟 Seedance 协议。

路由（与 Ark 原生一致，**只保留创建 + 查询**）：

    POST   /api/v3/contents/generations/tasks          创建任务（火山方舟形状）
    GET    /api/v3/contents/generations/tasks          列表（本进程内）
    GET    /api/v3/contents/generations/tasks/{id}     查询
    POST   /v1/videos                                  创建任务（OpenAI v1/videos 形状）
    GET    /v1/videos/{id}                             查询（OpenAI v1/videos 形状）
    GET    /healthz                                    存活 + 上游体检

两条对外形状（方舟 / OpenAI）共用同一条提交与查询管线 —— 翻译层只换"外壳"，
计费口径、dry-run、凭据绑定、租户隔离在两条线上行为完全一致。

把火山官方 SDK 的 base_url 指向本服务即可直接使用。

上游只有一条：**网页端内部接口**（tRPC over `/api`，会话 cookie）。
差异被 `upstreams.py` 吸收。

**鉴权是一个变量（`AVM_AUTH`）三选一**（详解见 `settings.py` 的模块头）：

    AVM_AUTH=              不校验（本机自用）；上游凭据取 `AVM_COOKIE`
    AVM_AUTH=passthrough   调用方的 `Authorization: Bearer` 就是**上游凭据本身**
                           （`auth_session=…` 裸 token 或完整 Cookie 串，见
                           `docs/web-reverse/`）；本进程不需要 `AVM_COOKIE`
    AVM_AUTH=key:<密钥>    闸门：Bearer 必须等于 `<密钥>`；上游凭据仍取 `AVM_COOKIE`

闸门与透传**互斥**（同一个 Bearer 不可能既当闸门密钥又当上游凭据）：以前用两个变量
表达、靠 `Settings.validate()` 拒绝那个必然 401 的组合；现在用一个变量让那种组合
**根本无法表达**（`settings.parse_auth`；旧变量若还有行为会直接拒绝启动）。
透传即多租户：任务表按**凭据指纹**隔离，见 `store.py` 的 `owner`。

**任务查询的凭据绑定（E2E-AVM-011）**：透传的鉴权前置在 newapi —— 调用方
（newapi 的任务轮询）**不再要求重带凭据**。创建任务时把当时的凭据绑定到这条
任务上（task_id ↔ api-key，sqlite 的 `credential` 列），`GET /tasks/{id}`
没带凭据就按绑定解析上游凭据；带了凭据则必须与任务归属一致（否则 404）。
列表 `GET /tasks` 是**跨任务**的租户视图，仍然必须带凭据（否则 401）——
不然没法界定"这是谁的列表"。凭据原文只落 sqlite（文件 0600），绝不进
日志 / span / 响应体。

四条安全约定：
  1. 站点**没有取消端点**，本服务也**不提供删除/取消** —— 对外接口面只有创建 +
     查询；任务记录随保留窗口（7 天）自然淘汰。跑着的任务照跑照扣，这是上游事实，
     不提供"已删除"的错觉入口。
  2. 计费有两个陷阱：`tier=base` 一律计费；`turbo` 只在 `duration ≤ 10s` 时免费
     （`translate.FREE_MAX_DURATION`；口径载体清单见 `tests/test_docs_billing_sync.py`）。
     判据是任务记录里的 `paid`（`credits` 与它反相，别用它判断）。
  3. 会话 cookie / Bearer token **不进日志、不进 span 属性** —— 靠
     `capture_headers=False`（硬过滤）而不是靠脱敏；请求 / 响应**原文**进 trace，
     不脱敏（`observability.py` 顶部有完整取舍）。
  4. 所有请求都可 `X-Avm-Dry-Run: 1` 或 `extra_body.aivideomaker_dry_run=true`
     走零成本校验 —— 这正是"验证翻译层"与"真花钱"之间唯一的开关。

上游调用一律走 `asyncio.to_thread`：客户端是**同步 httpx**，直接在协程里调会阻塞
整个事件循环（并发闸门还可能阻塞数十秒）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
import warnings
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from . import __version__
from .cookie import AUTH_COOKIE_NAME, normalize_cookie_header
from .errors import ParamError, WebApiError
from .observability import (
    clip,
    count_poll,
    describe_error,
    instrument_fastapi,
    is_poll_path,
    is_probe_path,
    logfire_exporting,
    logfire_ready,
    record_account,
    record_wait,
    set_upstream_calls,
    setup_observability,
    span,
    upstream_exchanges,
)
from .media_proxy import MediaProxy, MediaSourceError
from .channel_options import CHANNEL_OPTIONS_HEADER, parse_channel_options
from .settings import Settings
from .openai_videos import OPENAI_VIDEOS_PATH, ark_body_from_openai, openai_task_view
from .sniff import sniff_file
from .store import build_task_store
from .translate import ark_task_view, billing_note, billing_view, translate_create
from .upstreams import build_upstreams, build_web_for_cookie

TASKS_PATH = "/api/v3/contents/generations/tasks"

# ---- `/v1/videos` 表单上传的两道体积闸 --------------------------------------
#
# 🔴 `.form()` 会把 >1MB 的部件**落盘**，`read()` 再**整个**读进内存，然后 base64（+33%）。
# 没有任何闸时，一个 1GB 的文件部件 = "先写满磁盘 → 再吃掉几倍内存 → 最后才被站点的
# `maxBytes` 拒掉"。下线上传都撞上过 maxBytes，说明这条路上确实有大件在跑。
#
# 单件上限与 `WebClient.MEDIA_HARD_MAX_BYTES` **同值**（站点分类型上限里的最大者，
# 见 `docs/web-reverse/captured/js` 的 `{image:10,audio:15,video:50}`）。两处同值由门禁
# 钉住（`tests/test_media_fetch_budget.py`）—— 改一处必须改另一处，否则这里会悄悄放行更大的件。
_FORM_PART_MAX_BYTES = 50 * 1024 * 1024
# 请求体上限 = 站点**合法的最坏组合**（图 4×10MB + 视频 50MB + 音频 2×15MB = 120MB）+ 余量。
# 合法请求永远超不过它；而它能在 `.form()` **落盘之前**挡住一个巨大的件（真正的第一道闸）。
_FORMS_MAX_BODY_BYTES = 128 * 1024 * 1024


async def _read_upload(part, *, limit: int | None = None) -> bytes:
    """把表单里的文件部件**有界**地读进内存。

    刻意**不**用 `part.read()`（一次读空）：没有 `Content-Length` 的 chunked 上传只能靠
    "边读边判"兜底，否则一个超大部件会把进程内存吃干。超限时给 **400** 并说清上限与
    已读字节 —— 调用方能据此直接定位到"哪个素材太大了"。
    """
    cap = _FORM_PART_MAX_BYTES if limit is None else int(limit)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await part.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ParamError(
                f"上传的参考素材过大：已读 {total} 字节，超过单件上限 {cap} 字节"
                f"（{cap // 1048576}MB）—— 请压小后再传，或改用链接（URL 由适配层按预算流式取回）。",
                "input_reference",
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ---- 调用方带的「渠道/上游选择」头：无法满足时必须**留痕** ----------------------
#
# 🔴 这类头表达的是"请走 XX 线"，而本服务**只有一条** web 逆向线。静默按 web 线服务等于
# **悄悄换掉上游** —— 计费口径（免费窗口 vs 按量）、排队、产出质量都不同，调用方却以为
# 自己要到了那条线。留痕走 warnings（与"未知字段被忽略"同一条通道）：OpenAI 面的响应体
# 只有四个字段，所以它只在 trace（`ark.create.submit` 的 `warnings`）与日志里可见 ——
# 这是刻意的，不为了一句提示去破坏四字段契约。
_CHANNEL_HEADERS = ("x-base-url",)


def _channel_header_notes(request) -> list[str]:
    """调用方声明了、而本服务无法满足的上游选择头 → 说明（进 warnings）。"""
    notes = []
    for name in _CHANNEL_HEADERS:
        value = (request.headers.get(name) or "").strip()
        if value:
            notes.append(
                f'header "{name}: {value}" was ignored — this service only serves the "web" '
                f"upstream and does not route by base_url"
            )
    return notes


# 透传模式下缓存"调用方凭据 -> 上游"。上限只是防止无界增长。
_PASSTHROUGH_CACHE_MAX = 64
# 每个凭据要养一个轮询线程，不能无限开
_PASSTHROUGH_WEB_CACHE_MAX = 64


class ArkError(Exception):
    """要按 Ark 错误信封返回的业务错误。"""

    def __init__(self, status: int, code: str, message: str, param: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.param = param


def _with_request_id(message: str, request_id: str) -> str:
    """让 message 以 `Request ID: {id}` 结尾 —— **全服务唯一负责这件事的地方**。

    官方「公共错误码」表里每条 message 都以它结尾。两个作用：① 与官方形态一致；
    ② **脱敏之后调用方仍有唯一抓手** —— 报障时报这个 ID，运维能在 trace 里捞到全量上游原文
    （见 `_upstream_client_message`）。幂等：已经有就不再拼（防两处各拼一遍）。
    """
    if not request_id or "Request ID:" in message:
        return message
    return f"{message} Request ID: {request_id}"


def _ark_envelope(
    status: int, code: str, message: str, param: str = "", *, request_id: str = ""
) -> JSONResponse:
    """对外错误信封（官方形状：`code` / `message` / `param` / `type`）。"""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": _with_request_id(message, request_id),
                "param": param,
                "type": "InvalidRequest",
            }
        },
    )


def _rid_of(request) -> str:
    """本次请求的 id（中间件在 `request.state` 上绑的，与响应头 `x-request-id` 同源）。"""
    return str(getattr(request.state, "request_id", "") or "")


# ------------------------------------------------------------ 错误映射 ----

# 上游能直接对应的状态码白名单。**504 是我们自己产生的**（取"参考文件链接"或整个转存
# 阶段超过预算，见 `web_client._fetch_media` / `MediaFetchBudget`）—— 不放进来的话会被
# 压成 502，调用方就分不清"上游坏了"与"你给的链接太慢/太大"。
_WEB_HTTP = (400, 401, 403, 404, 429, 504)


def _web_http_for(e: WebApiError) -> int:
    if e.code in ("CAPTCHA_REQUIRED", "QUEUE_TIMEOUT"):
        return 429
    if e.http_status in _WEB_HTTP:
        return e.http_status
    return 502


def _web_code_for(e: WebApiError) -> str:
    """内部错误码 → **对外**错误码：**白名单**，认不出的一律 `UpstreamError`。

    🔴 不许把 `e.code` 原样透传：`trpc` 会把**站点自己的**错误码（上游信封里的
    `data.code`）带进来，那是上游的内部词表 —— 出现在我们的 `code` 字段里就是泄漏
    （也是把"实现细节"写进了对外契约）。只有 `NOT_FOUND` 这类**我们已确认语义**的才映射。
    """
    return {
        "CAPTCHA_REQUIRED": "RateLimitExceeded",
        "QUEUE_TIMEOUT": "TaskQueueFull",
        "NOT_FOUND": "TaskNotFound",
    }.get(e.code, "UpstreamError")


# ---- 上游错误的**对外脱敏**（`WebApiError` → 客户端只看到类别级事实）------------
#
# 🔴 为什么必须做：上游错误原文里塞满了**内部/实现标识**，而它会原样进客户端报文：
#   · 站点内部 procedure 名 `ai.minimaxH3` / `model.needsCaptcha` / `uploads.PUT`
#   · 铸造服务主机与端口 `host.docker.internal:8899`
#   · **运维口令**（`ufw allow proto tcp from 172.16.0.0/12 to any port 8899`）
#   · 内部 runbook 编号（`E2E-AVM-008`）与工具路径（`tools/compose_wiring_check.py`）
#   · 上游返回的原始报文（`{status}: {text[:300]}`）
# 这些是白送的攻击面与实现细节，调用方拿到也办不了事。
#
# 官方方舟的错误报文正是范例（见官方「公共错误码」表）：**通用描述句 + `Request ID: {id}`**，
# 正文里没有任何上游/实现标识。本服务照这个形态出对外报文。
#
# 🔴 两个通道（与既有纪律一致：**证据换通道，不是消失**）：
#   · 对外：类别级事实 + Request ID
#   · 对内：**全量**原文（`ark.*` span 的 `error` / `upstream_calls` 属性 + 本地日志）——
#     用户口径「logfire 侧全部上报上游」就是在说这件事：脱敏只作用于出口。
# 「失败在**哪一阶段**」对调用方是**可行动的**（"你给的链接慢" vs "上游自己慢"），
# 而 procedure 名是内部标识 ⇒ 翻成阶段说法，原文一律不出现。
# ⚠️ 用户口径（2026-09-16）：超时**多发生在传图片/传 URL 这条路上** —— 那时快慢取决于
# 调用方自己的素材，用一句笼统的"上游没响应"会把责任指错方向。
_PHASE_BY_PROCEDURE = {
    "download": "fetching a reference file you supplied",
    "uploads.getPresignedUrl": "preparing the reference upload",
    "uploads.PUT": "uploading a reference file to the upstream",
}


def _upstream_client_message(e: WebApiError) -> str:
    """把上游错误翻成**对外可用**的一句话：不出现任何内部标识。

    ⚠️ 这里**不拼 Request ID** —— 那件事只由 `_with_request_id` 负责（信封层统一加），
    两处各拼一遍的话，其中一处会变成死代码，而门禁也就证伪不了它（变异自证实测踩到）。
    """
    timed_out = (
        e.http_status == 504
        or e.transport.endswith("Timeout")
        or e.transport == "TimeoutException"
    )
    phase = _PHASE_BY_PROCEDURE.get(e.procedure)
    if phase:
        # 参考素材那三步：**调用方能自己修**（换更快/更小的直链），所以要说清是哪一步
        why = (
            f"{phase} did not finish within the configured budget — a faster or smaller file "
            f"may help"
            if timed_out
            else f"{phase} failed"
        )
    elif e.code == "CAPTCHA_REQUIRED":
        why = (
            "the upstream currently requires a fresh captcha token for this account (a dynamic, "
            "velocity-based gate, not an account property); supply one via "
            "extra_body.aivideomaker_captcha_token, wait for the gate to decay, or spread your "
            "submissions out"
        )
    elif e.code == "QUEUE_TIMEOUT":
        why = (
            "the upstream concurrency limit is saturated and no slot was freed within the wait "
            "budget; retry later or lower your concurrency"
        )
    elif e.code == "NOT_FOUND":
        why = "the upstream has no record of this task"
    elif timed_out:
        why = "the upstream did not respond in time (a retry may succeed)"
    elif e.http_status == 429:
        why = "the upstream rate-limited this request"
    elif e.transport:
        why = "the upstream is unreachable"
    elif 400 <= e.http_status < 500:
        why = "the upstream rejected this request"
    else:
        why = "the upstream call failed"
    tail = f" (HTTP {e.http_status})" if e.http_status else ""
    return f"The request failed because {why}{tail}."


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def require_bearer(request: Request, authorization: str | None = Header(default=None)) -> str:
    """闸门鉴权。`AVM_AUTH` 不是 `key:<密钥>` 时不校验（本机自用 / 透传）。

    `AVM_AUTH=passthrough` 时**不该**再是闸门模式：此时 Bearer 要拿去当上游凭据，
    而闸门会先把它挡掉 —— 那种组合现在**无法用环境变量表达**（一个变量三选一，
    见 `settings.parse_auth`）；`Settings.validate()` 只兜住代码内直接构造 Settings
    的场景，不让它变成"每个请求都 401"的线上迷局。
    """
    gate = request.app.state.settings.gate_key
    if not gate:
        return authorization or ""
    token = _bearer(request)
    # 常量时间比较，避免按字符泄露闸门密钥
    if not hmac.compare_digest(token, gate):
        raise ArkError(401, "AuthenticationError", "invalid or missing bearer token")
    return token


def _owner_of(request: Request) -> str:
    """任务归属 = **凭据指纹**（sha256 前 16 位）。绝不落凭据原文。

    非透传模式返回空串 —— 那时进程内只有一份凭据，所有任务本来就属于同一账号，
    查询侧也就不做归属过滤。
    """
    if not request.app.state.settings.passthrough_cookie:
        return ""
    token = _bearer(request)
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""


def _visible(request: Request, entry: dict | None) -> bool:
    """这条任务对本次请求可见吗？归属不符一律当"不存在"，不透露存在性。"""
    if not entry:
        return False
    owner = _owner_of(request)
    return not owner or (entry.get("owner") or "") == owner


def _close_upstream(up: object) -> None:
    """尽力关掉不再复用的上游客户端 —— 别把 socket 的回收押在 GC 上。"""
    try:
        up.client.close()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001  关闭失败不该影响请求
        logger.warning(f"关闭透传上游失败：{e}")


def _sweep(cache: dict, limit: int) -> None:
    """缓存到顶时淘汰**空闲**项，直到 `len(cache) < limit`（全忙则不淘汰）。

    web 上游的槽位被**后台轮询线程**占到任务终态，关掉正在被轮询的客户端会让那次
    轮询直接失败（现象是"任务查不到"）⇒ 只淘汰空闲的。
    """
    while len(cache) >= limit:
        key = next((k for k, v in cache.items() if not v.queue.stats()["running"]), None)
        if key is None:
            logger.warning(
                "透传上游缓存已满（{} 条）且没有空闲项可淘汰 —— 暂不淘汰，等任务结束", len(cache)
            )
            return
        _close_upstream(cache.pop(key))


# Ark 归一化后的终态集合（translate._WEB_STATUS_TO_ARK 的值域子集）。
# 终态视图**不可变**：站点的任务记录定格，视频地址与用量不会再变 —— 缓存到记录删除为止。
_TERMINAL_ARK_STATUS = frozenset({"succeeded", "failed", "cancelled"})


class TaskViewCache:
    """`GET /tasks/{id}` 的上游视图缓存 —— **轮询节流，避免把上游打到 429**。

    任务从提交到出片要 ~60s，而调用方（newapi 的任务轮询）可能每秒来一发：
    每次都实时回上游取，查询请求就成了上游的主要负载来源（429 的常见成因）。
    两条规则：

      - **非终态**视图：TTL 秒内直接复用（任务要跑 ~60s，15s 的粒度不丢信息）；
      - **终态**视图（succeeded/failed/cancelled）：**永久**复用 —— 记录已定格，
        多查一次只是白挨限流的风险。

    `ttl <= 0` = 显式关闭（每次都实时回上游，行为与旧版完全一致）。
    只有**成功取到**的视图才进缓存（上游失败时的空壳不该被记住）。
    """

    def __init__(self, ttl: float, max_entries: int = 1024):
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        # ark_id -> (view, expires_ms)；expires_ms=None 表示终态（不过期）
        self._views: dict[str, tuple[dict, float | None]] = {}

    def get(self, ark_id: str) -> dict | None:
        if self.ttl <= 0:
            return None
        with self._lock:
            hit = self._views.get(ark_id)
            if hit is None:
                return None
            view, expires_ms = hit
            if expires_ms is not None and time.time() * 1000 >= expires_ms:
                del self._views[ark_id]
                return None
            return dict(view)

    def put(self, ark_id: str, view: dict) -> None:
        if self.ttl <= 0 or not view:
            return
        terminal = str((view or {}).get("status") or "") in _TERMINAL_ARK_STATUS
        expires_ms = None if terminal else time.time() * 1000 + self.ttl * 1000
        with self._lock:
            if len(self._views) >= self.max_entries and ark_id not in self._views:
                # 超限先扔非终态（随时可重取），还不够就扔最早插入的一条。
                # 任务量是每小时几条，1024 的上限只是防无界增长的保险丝。
                for k in [k for k, (_, exp) in self._views.items() if exp is not None]:
                    del self._views[k]
                if len(self._views) >= self.max_entries:
                    del self._views[next(iter(self._views))]
            self._views[ark_id] = (dict(view), expires_ms)


class PollReportGate:
    """轮询上报闸门（**闸门 1：产生层**）：同一任务只在**状态跃迁**时才留 span。

    为什么需要它：调用方（newapi 的任务轮询）按秒级打 `GET /tasks/{id}`，一个 60~300s
    的任务就是几十上百次查询，而查到的**绝大多数状态与上一条完全相同** ——
    逐条存 span 不是可观测性，是重复计价（Logfire 官方文档就把 "High-frequency polling"
    列为应当排除埋点的用例）。

    所以：**没见过的状态才留痕**，其余只累加指标（`avm.task.poll_requests`）。

    契约不出现空洞（这是与"干脆整条端点不埋点"的关键区别）：
      · 终态一定是一次跃迁（或首次观测）⇒ 每个任务仍有一条带**终态 + `paid`** 的
        `ark.task.fetch`，出片后对账口径不变；
      · **失败一律留痕**（`reason="error"`）—— 失败时上游状态未知，不能据此认定"没变化"。

    🔴 进程内状态（与 `TaskViewCache` 同口径）：多 worker 下每个 worker 各有一份
    "已上报状态"，同一任务**最多** N(worker) 条"首次观测"span。这是刻意接受的代价
    （相对每任务上百条），换的是：不必为纯观测用途引入一个跨进程账本。
    """

    def __init__(self, max_entries: int = 1024):
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        # ark_id -> {"status": 已上报的状态, "polls": 本进程观测到的查询次数}
        self._seen: dict[str, dict] = {}

    def observe(self, ark_id: str, status: str, *, error: str | None = None) -> dict | None:
        """这次查询要不要留痕。返回 `None` = 不留（只计数）；否则给出理由与跃迁信息。

        返回的字典直接变成 span 属性：`reason`（first / transition / error）、
        `from`（上一次已上报的状态）、`to`（这次看到的）、`polls`（本进程累计查询次数）。
        """
        key = str(ark_id)
        status = str(status or "")
        with self._lock:
            prev = self._seen.get(key)
            polls = (int(prev["polls"]) + 1) if prev else 1
            previous = prev["status"] if prev else ""
            if error:
                reason = "error"
            elif prev is None:
                reason = "first"
            elif previous != status:
                reason = "transition"
            else:
                prev["polls"] = polls          # 还在轮询，但状态没变 ⇒ 只计数
                return None
            # 🔴 失败时状态未知 ⇒ **不许**用空串覆盖已知状态，否则下一次成功查询会被读成
            #    "跃迁"（凭空多一条 span，还给对账一个假的跃迁方向）。
            self._remember(key, {"status": previous if error else status, "polls": polls})
            return {"reason": reason, "from": previous, "to": status, "polls": polls}

    def _remember(self, key: str, rec: dict) -> None:
        if len(self._seen) >= self.max_entries and key not in self._seen:
            # 超限扔最早插入的一条。任务量是每小时几条，这个上限只是防无界增长的保险丝。
            del self._seen[next(iter(self._seen))]
        self._seen[key] = rec


# 成片出口（`/v/{ark_id}.mp4`）回源失败时对外的措辞。刻意**不含**任何上游信息
# （域名 / procedure 名 / 上游原文一律不出口，与 `_upstream_client_message` 同一条纪律）。
MEDIA_SOURCE_ERROR = "the video source is temporarily unavailable from this service"


def _apply_media_gate(proxy, entry: dict, view: dict) -> dict:
    """成片出口的**出口闸门**：上游 URL 永远不许出现在对外响应体里。

    把 `content.video_url` 换成本服务自己的下载地址（`{public_base}/v/{ark_id}.mp4`）。
    这是**恒等**替换 —— 与"有没有搬过"无关，所以任务状态照实上报 `succeeded`，
    不必引入"成功了但拿不到地址"那种自相矛盾的中间态。

    ⚠️ 返回**新 dict**：`view` 是查询缓存的浅拷贝，它的 `content` 仍是缓存里那一个
    对象 —— 原地改会污染缓存，让后续请求拿到被改写过的视图，而且没有任何提示。
    """
    content = view.get("content")
    source = content.get("video_url") if isinstance(content, dict) else None
    if not source or str(view.get("status") or "") != "succeeded":
        return view
    out = dict(view)
    # ⚠️ 只给 video_url。尾帧（`last_frame_url`）同样是上游 CDN 直链、且**不走**这条
    #    出口 —— 宁可少一个可选字段，也不让一条未脱敏的上游链接从另一个键漏出去。
    #    （上游目前根本不产出它，见 `translate.normalize_web_task`。）
    out["content"] = {"video_url": proxy.download_url(entry["id"], source)}
    return out


def _remember_source_url(request: Request, entry: dict, view: dict) -> None:
    """把成片的上游地址记进任务记录，供 `/v/{ark_id}.mp4` 回源时读。

    两点刻意的取舍：

    · **只在缺失时写一次**。这是一次 sqlite 写，而查询路径会被高频轮询（`task_cache`
      TTL 15s）；每轮都写就变成"读缓存省下的开销又写回去"。
    · **失败不冒泡**。这只是省一次回源查询的优化，写不进去时下载端点会退回
      "当场查一次上游"，查询本身没有任何理由为此失败。

    记的是**内部字段**（`entry["source_url"]`），不进响应体 —— 响应体字段由
    `ark_task_view` 的白名单收窄，多出来的键只是在 sqlite 里躺着。
    """
    if entry.get("source_url"):
        return
    content = view.get("content")
    url = content.get("video_url") if isinstance(content, dict) else None
    if not url or str(view.get("status") or "") != "succeeded":
        return
    try:
        request.app.state.tasks.patch(entry["id"], source_url=url)
    except Exception as e:  # noqa: BLE001
        logger.warning("记下成片源地址失败 ark_id={} — {}", entry.get("id"), describe_error(e))
        return
    entry["source_url"] = url


def _media_ext(name: str) -> str:
    """`cgt-…-a1b2c3d4.mp4` → `.mp4`（取不到就 `.mp4`）。"""
    _base, dot, ext = str(name or "").rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isascii() and ext.isalnum():
        return f".{ext.lower()}"
    return ".mp4"


def _strip_media_ext(name: str) -> str:
    """`cgt-…-a1b2c3d4.mp4` → `cgt-…-a1b2c3d4`，用于按 id 查任务记录。

    ark_id 形态固定（`cgt-<ts>-<hex8>`，不含点号），所以剥掉最后一段带点的后缀是
    安全的；剥不动就原样返回 —— 查不到就是 404，不会误命中别的记录。
    """
    base, dot, ext = str(name or "").rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isascii() and ext.isalnum():
        return base
    return str(name or "")


def _billing_check(app: FastAPI) -> dict:
    """计费自查的**权威口径**（`/healthz` 的 `billing_check` 字段）。

    存在的理由很具体（livetest 报告 E2E-AVM-015 的 config_warning）：0.0.27 起对外任务
    视图按官方 schema **收窄掉了 `usage`**，于是「看任务记录里的 `paid=False`」这条用了
    很久的判据**静默失效** —— 而它失效的样子不是报错，是"响应里没有这个字段"，
    极易被读成"本次没计费"（结论正好相反）。

    这里只回答一个问题：**想确认某条任务花没花钱，该看哪里**。数值本身要靠 `?deep=1`
    那一步（它会打上游取余额；浅探活刻意不打，避免存活探针把上游当依赖）。
    """
    return {
        # 恒 False。写成**字段**而不是文档里的一句话：调用方与巡检可以断言它，
        # 而不是靠人去读注释 —— 哪天 usage 回来了，这里也会跟着变。
        "usage_in_task_response": False,
        "how": (
            "GET /healthz?deep=1 取 balance：提交前记一次、出片后再记一次，差值即该条任务的"
            "花费（免费组合差 0）；或读 Logfire 的 ark.task.fetch span 属性 paid"
        ),
        # 仅 `?deep=1` 时填（要打上游）。浅探活恒为 None。
        "balance": None,
    }


def _media_summary(app: FastAPI) -> dict:
    """`/healthz` 里的成片出口视图。**只报形态（开没开、基址），不含凭据**。"""
    proxy = getattr(app.state, "media_proxy", None)
    return proxy.describe() if proxy is not None else {"enabled": False}


def _passthrough_web_upstream(request: Request, cookie: str):
    """按调用方凭据取（或建）一个 web 上游：同一凭据复用同一客户端与并发闸门。"""
    cache: dict = request.app.state.passthrough_web
    key = hashlib.sha256(cookie.encode()).hexdigest()[:16]
    hit = cache.get(key)
    if hit is not None:
        return hit
    _sweep(cache, _PASSTHROUGH_WEB_CACHE_MAX)
    upstream = build_web_for_cookie(request.app.state.settings, cookie)
    cache[key] = upstream
    logger.info("透传：为新凭据建立 web 上游（缓存 {} 条）", len(cache))
    return upstream


def _passthrough_cookie_of(request: Request) -> str:
    """透传模式：从 Bearer 解出**规范化后**的会话 cookie。不合格式直接 401。"""
    raw = _bearer(request)
    if not raw:
        raise ArkError(
            401,
            "AuthenticationError",
            "passthrough mode requires a bearer token (your aivideomaker session cookie)",
        )
    # 裸 token 在透传里是**推荐形态**，所以这里不弹"你可能贴错了"的告警：
    # 那条告警的读者是配 AVM_COOKIE 的人（见 ark_compat/cookie.py 的取舍）。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        cookie = normalize_cookie_header(raw)
    if f"{AUTH_COOKIE_NAME}=" not in cookie:
        raise ArkError(
            401,
            "AuthenticationError",
            f"凭据里没有 {AUTH_COOKIE_NAME} —— 透传请把网页会话 cookie 放在 "
            f"Authorization: Bearer 里（裸 token 或完整 Cookie 串都行）",
        )
    return cookie


def _upstream_and_credential_for(request: Request):
    """取本次请求要用的 web 上游，以及**本次请求的凭据原文**。

    - **透传模式**（`AVM_AUTH=passthrough`）：调用方的 Bearer 就是网页会话 cookie，
      按凭据指纹建/复用客户端 —— 每个调用方用自己的账号与免费窗口。
      返回 `(upstream, cookie)`；cookie 只用于创建时的凭据绑定（见 create_task）。
    - 否则用本进程持有的那份（`AVM_COOKIE`），返回 `(upstream, "")` ——
      凭据在 env 里，不需要（也不应该）再往任务表里写一份。
    """
    settings = request.app.state.settings

    if settings.passthrough_cookie:
        cookie = _passthrough_cookie_of(request)
        return _passthrough_web_upstream(request, cookie), cookie

    pool: dict = request.app.state.upstreams
    if "web" not in pool:
        raise ArkError(
            503,
            "UpstreamUnavailable",
            "web upstream is not configured in this process (needs AVM_COOKIE); "
            f"available: {sorted(pool)}",
        )
    return pool["web"], ""


def _upstream_for(request: Request):
    """取本次请求要用的 web 上游（不需要凭据原文的调用方用这个薄壳）。"""
    return _upstream_and_credential_for(request)[0]


def _upstream_for_task(request: Request, entry: dict):
    """按**这条任务**取上游 —— 任务查询的凭据绑定（E2E-AVM-011）。

    透传模式下轮询 `GET /tasks/{id}` 的调用方（newapi 的任务轮询）**不再要求
    重带凭据**：用创建任务时绑定到这条任务上的那份（sqlite `credential` 列）。
    调用方带了凭据则按它解析 —— 归属一致性已由 `_visible` 挡在前面（404）。
    非透传模式无所谓绑定：进程内只有一份凭据。
    """
    settings = request.app.state.settings
    if not settings.passthrough_cookie:
        return _upstream_for(request)

    if _bearer(request):
        return _passthrough_web_upstream(request, _passthrough_cookie_of(request))

    credential = request.app.state.tasks.credential_for(entry["id"])
    if not credential:
        # 两种可能：任务创建于绑定机制上线之前；或非透传时期留下的记录。
        # 如实说清，别让它看起来像"任务不存在"。
        raise ArkError(
            401,
            "AuthenticationError",
            "该任务没有绑定凭据（旧版本创建），且本次请求未带凭据 —— "
            "请带上创建任务时的 Authorization: Bearer 重试",
        )
    return _passthrough_web_upstream(request, credential)


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        raise ParamError("body is required")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParamError(f"invalid JSON body: {e}") from None
    if not isinstance(body, dict):
        raise ParamError("body must be a JSON object")
    return body


def _is_dry_run(request: Request, body: dict) -> bool:
    if request.headers.get("x-avm-dry-run") == "1":
        return True
    extra = body.get("extra_body")
    return isinstance(extra, dict) and extra.get("aivideomaker_dry_run") is True


# ------------------------------------------------------------- 号池采样 ----
#
# 为什么要它：透传之后"这个进程手里有哪些账号、各自还剩多少积分、闸门开没开"
# 只能靠翻日志猜。余额是**号池的耗尽信号**，闸门是**免费档的可用性信号**，
# 两者都需要时间序列，所以做成指标（而不是挂在某个 span 上）。
#
# 三条硬约束：
#   1. **只读**：只用 credits.getCredits / GET /api/v1/account / model.needsCaptcha——
#      都不创建任务、不计费。
#   2. **凭据原文绝不外发**：标签用 sha256 前 16 位指纹（`_fingerprint`）。
#   3. **采样失败不能影响服务**：任何异常都转成 `reachable=0`，并把异常挡在
#      上报循环内部（一轮失败不该让上报永久停摆）。


def _fingerprint(secret: str) -> str:
    """凭据指纹（sha256 前 16 位）。**绝不外发凭据原文。**"""
    if not secret:
        return ""
    return hashlib.sha256(str(secret).encode()).hexdigest()[:16]


def _pool_accounts(app) -> list[tuple[str, str, str, object]]:
    """号池视图：``(upstream, 凭据指纹, 来源, 上游对象)``。

    来源两种：`process` = 本进程持有凭据（`AVM_COOKIE`）；
    `passthrough` = 调用方自带凭据（每个凭据一个上游对象）。
    """
    s = app.state.settings
    out: list[tuple[str, str, str, object]] = []
    for kind, up in sorted(app.state.upstreams.items()):
        out.append((kind, _fingerprint(s.cookie or kind), "process", up))
    for key, up in list(app.state.passthrough_web.items()):
        # web 透传的缓存键**本身**就是 cookie 的 sha256[:16]，直接复用
        out.append(("web", key, "passthrough", up))
    return out


# 套餐 → 并发上限。**来源是站点自己的 Stripe 商品描述，不是猜的**：
#   prod_SymV739ojwmGEN  name="premium"  "… and 2 concurrent jobs so you can create faster."
#   prod_SwAdxHcUSHOJIK  name="pro"      "… the ability to run 4 concurrent jobs—…"
# 站点 FAQ 同口径：「高级套餐可拥有两个并发任务，专业套餐可拥有四个并发任务。」
# 上游错误原文也只说 "The premium plan can only run 2 task at a time."。
# 未知 planId ⇒ 报 `None` 并只告警一次（**不猜**，别把"没映射"读成"没限制"）。
_PLAN_ID_CONCURRENCY = {
    "prod_SymV739ojwmGEN": 2,
    "prod_SwAdxHcUsHOJIK": 4,
}
_unknown_plans_warned: set = set()


def _plan_concurrency(plan_id) -> int | None:
    pid = str(plan_id or "").strip()
    if not pid:
        return None
    if pid in _PLAN_ID_CONCURRENCY:
        return _PLAN_ID_CONCURRENCY[pid]
    if pid not in _unknown_plans_warned:
        _unknown_plans_warned.add(pid)
        logger.warning("号池：未知套餐 {} ⇒ 并发上限按未知处理（补 _PLAN_ID_CONCURRENCY）", pid)
    return None


def _sample_account(kind: str, up, settings) -> dict:
    """只读采样一个账号（余额 / 闸门 / 订阅）。**不抛异常**（抛了会让整轮上报断掉）。"""
    sample = {
        "reachable": False, "credits": None, "captcha_required": None,
        "plan_id": None, "plan_price_cents": None, "sub_status": None,
        "concurrency_limit": None, "days_to_renewal": None, "identity": None,
    }
    try:
        sample["credits"] = up.balance()
        sample["reachable"] = True
    except Exception as e:  # noqa: BLE001 会话过期 / 上游抖动都算"不可达"
        logger.warning(f"号池采样失败 upstream={kind}：{type(e).__name__}: {e}")
        return sample
    if kind != "web":
        return sample

    # 闸门是**动态**的（按速率翻转），每轮现问，不能缓存 —— 顺带把
    # "开了多久才衰减"变成可回看的时间序列。
    try:
        sample["captcha_required"] = bool(up.client.needs_captcha())
    except Exception as e:  # noqa: BLE001 问不到闸门不影响余额这一项
        logger.warning(f"号池闸门探测失败：{type(e).__name__}: {e}")

    # 订阅详情：套餐 / 价格 / 状态 / 下次扣费 —— 对号池这是**可排期**信息
    # （哪天扣费、还剩几天、这个账号到底几并发）。
    try:
        sub = up.client.get_subscription() or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"号池订阅探测失败：{type(e).__name__}: {e}")
        sub = {}
    if sub:
        pid = str(sub.get("planId") or "").strip()
        sample["plan_id"] = pid or None
        sample["concurrency_limit"] = _plan_concurrency(pid)
        price = sub.get("price")
        sample["plan_price_cents"] = int(price) if isinstance(price, (int, float)) else None
        sample["sub_status"] = str(sub.get("status") or "").strip() or None
        nxt = str(sub.get("nextPaymentDate") or "").strip()
        if nxt:
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(nxt.replace("Z", "+00:00"))
                sample["days_to_renewal"] = round(
                    (dt - datetime.now(timezone.utc)).total_seconds() / 86400, 2
                )
            except ValueError:
                pass

    if getattr(settings, "account_report_identity", False):
        # PII 默认不开：只有显式 AVM_ACCOUNT_REPORT_IDENTITY=1 才把邮箱带上
        try:
            sample["identity"] = str((up.client.get_user() or {}).get("email") or "") or None
        except Exception:  # noqa: BLE001
            pass
    return sample


async def _report_pool_once(app) -> list[tuple[str, str, str, dict]]:
    """采一轮并上报，返回本轮明细。**测试直接调它**，不必等定时器。"""
    out: list[tuple[str, str, str, dict]] = []
    for kind, account, source, up in _pool_accounts(app):
        sample = await asyncio.to_thread(_sample_account, kind, up, app.state.settings)
        record_account(upstream=kind, account=account, source=source, **sample)
        out.append((kind, account, source, sample))
    return out


async def _account_reporter(app, seconds: int) -> None:
    """周期性上报号池状态。自身永不抛出 —— 观测功能不该拖倒服务。"""
    while True:
        try:
            rows = await _report_pool_once(app)
            if rows:
                logger.info(
                    "号池上报 n={} 明细={}",
                    len(rows),
                    " ".join(
                        f"{kind}:{source}:{account[:6]}="
                        f"{sample['credits'] if sample['reachable'] else 'unreachable'}"
                        f"{'/captcha' if sample.get('captcha_required') else ''}"
                        f"{('/plan=' + sample['plan_id']) if sample.get('plan_id') else ''}"
                        f"{('/conc=%d' % sample['concurrency_limit']) if sample.get('concurrency_limit') else ''}"
                        for kind, account, source, sample in rows
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 一轮失败不该让上报停摆
            logger.warning(f"号池上报这一轮失败：{type(e).__name__}: {e}")
        await asyncio.sleep(seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()

    logfire_ok = setup_observability(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 号池上报：**只读**探测 + Logfire 指标。
        # ⚠️ 刻意**不**因"Logfire 未装配/出口不通"就停止采样 —— 采集与出口是两件事。
        # 本次实测踩到：服务进程内经沙箱代理导出 logfire 会撞 10s 读超时而超时。
        # 若那时连采样也停掉，就正好在"出口坏了"这个最需要数据的时刻彻底瞎掉；
        # 现在出口不通时至少还有本地日志里的号池视图。
        reporter = None
        if settings.account_report_seconds > 0:
            reporter = asyncio.create_task(
                _account_reporter(app, settings.account_report_seconds)
            )
            # 挂到 state 上：运维与测试都要能一眼看出"上报到底起没起、指标通不通"
            app.state.account_reporter = reporter
            app.state.account_metrics_enabled = logfire_ready()
            logger.info(
                "号池上报已启用：每 {}s 采样一次；指标{}",
                settings.account_report_seconds,
                "已接入 Logfire" if logfire_ready() else "不可用（Logfire 未装配）⇒ 仅本地日志",
            )
        yield
        if reporter is not None:
            reporter.cancel()
            with suppress(asyncio.CancelledError):
                await reporter
        # 停机收尾：把上游客户端关掉。轮询线程是 daemon，会随之结束；这里只做
        # "尽力而为"的连接回收，失败不影响退出码。
        closed = 0
        for name in ("upstreams", "passthrough_web"):
            for up in list(getattr(app.state, name, {}).values()):
                _close_upstream(up)
                closed += 1
        if closed:
            logger.info("停机：已关闭 {} 个上游客户端", closed)

    app = FastAPI(
        lifespan=_lifespan,
        title=settings.service_title,
        description="把 aivideomaker 包装成火山方舟 Seedance 协议形状（上游：网页端内部接口）。",
        version=__version__,
        # 交互式文档**保持开启**（此前的 `docs_url=None, redoc_url=None` 已移除）：
        # 本兼容层字段映射细节多（content 元素、ratio / resolution 取值域、`@图像n`
        # 引用规则），无 UI 时只能靠读 README。回归断言见
        # `tests/test_api_docs_enabled.py`。
        # 🔴 两者走 FastAPI 默认 ⇒ **不带** `require_bearer`（鉴权是逐路由 Depends，
        #    不是全局中间件）⇒ 与 `/openapi.json` 同级的**公开**端点，别当受保护资源。
        # 🔴 Swagger UI 的 JS/CSS 由浏览器从 CDN（cdn.jsdelivr.net）取：服务端一切正常时
        #    页面仍可能白屏——服务端日志**看不出**这个问题，别误判成路由没生效。
    )
    app.state.settings = settings

    app.state.upstreams = build_upstreams(settings, log=lambda m: logger.warning(m))
    # 透传的 web 上游（凭据指纹 -> upstream）。淘汰只挑空闲项（见 _sweep）。
    app.state.passthrough_web = {}
    # 任务表**必须持久化**：契约要求 `GET /tasks/{id}` 在保留窗口内始终可查，
    # 而进程内 dict 一重启就让调用方手里正在轮询的 `cgt-*` 凭空 404（实测踩到过）。
    app.state.tasks = build_task_store(
        settings.task_store, settings.task_db, settings.task_retention_days
    )
    try:
        purged = app.state.tasks.prune(settings.task_retention_days)
        if purged:
            logger.info("任务表已清理过期记录 {} 条", purged)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理过期任务失败（不影响启动）：{e}")
    # 查询节流缓存：挡调用方的高频轮询，别把上游打到 429（ttl<=0 = 关闭）
    app.state.task_cache = TaskViewCache(settings.task_cache_ttl)
    # 轮询上报闸门：同一任务**只有状态跃迁（含首次观测、失败）才留 span**
    app.state.poll_gate = PollReportGate()
    # ---- 成片对外出口（见 media_proxy）----
    # 上游直链里带着上游域名与**上游实际执行的模型名**，响应头 `content-disposition`
    # 里还重复一份模型名。配上 `AVM_PUBLIC_BASE` 后对外只给 `{base}/v/{ark_id}.mp4`，
    # 取用时流式回源并**重写响应头** —— 三条泄露面一次关掉。
    # 未配置 ⇒ `enabled` 为 False，行为与从前逐字节一致。
    app.state.media_proxy = MediaProxy(
        public_base=settings.public_base,
        path=settings.media_path,
        trust_env=settings.trust_env,
        read_timeout=settings.media_read_timeout,
    )
    logger.info(
        "上游就绪 available={} task_store={} task_cache_ttl={}s media_proxy={}",
        sorted(app.state.upstreams),
        app.state.tasks.describe(),
        settings.task_cache_ttl,
        app.state.media_proxy.describe() if app.state.media_proxy.enabled else "off",
    )

    if logfire_ok:
        try:
            # 每个请求自动一条 span。抓头由 observability 统一关掉（凭证头不入 trace）
            instrument_fastapi(app)
        except Exception as e:  # 不因为追踪装不上就起不来
            logger.warning(f"instrument_fastapi 不可用：{e}")

    # ------------------------------------------------------------ middleware --

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        # 绑到 `request.state`：异常处理器要用它给对外报文补 `Request ID: …`（脱敏之后，
        # 这个 ID 是调用方**唯一**的排障抓手 —— 见 `_upstream_client_message`）。
        request.state.request_id = rid
        # 探活请求：本地照打日志，但**不上报 Logfire**（`observability.keep_off_logfire`
        # 按这个 extra 键判定）。与 `instrument_fastapi` 的 span 排除**同源** ——
        # 两边都从 `is_probe_path()` 派生，改一处不会漏另一处。
        probe = is_probe_path(request.url.path)
        with logger.contextualize(request_id=rid, avm_probe=probe):
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("未处理异常 {} {}", request.method, request.url.path)
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["x-request-id"] = rid
            # 轮询路径（任务查询）里**成功的那些**同样不入库：一个任务几十上百行
            # `GET …/tasks/{id} -> 200`，与它的 span 是同一笔重复计价（闸门 1：产生层）。
            # 🔴 但**失败必须留痕** —— 4xx/5xx 是"调用方到底看到了什么"的唯一记录，
            #    静音掉等于把事故现场一起删掉。span 侧的排除粒度是 URL、做不到这个区分，
            #    所以在这里补：失败照报、成功静音。
            quiet = probe or (is_poll_path(request.url.path) and response.status_code < 400)
            with logger.contextualize(avm_probe=quiet):
                logger.info(
                    "{} {} -> {} ({:.0f}ms)",
                    request.method,
                    request.url.path,
                    response.status_code,
                    elapsed_ms,
                )
            return response

    # -------------------------------------------------------- error handlers --

    @app.exception_handler(ArkError)
    async def _on_ark_error(request: Request, exc: ArkError):
        # 这是我们**自己**的报文（参数校验 / 能力不支持 / 任务不存在…）：它不含上游实现细节，
        # 对调用方是有用的，直接给；只补 Request ID。
        return _ark_envelope(exc.status, exc.code, str(exc), exc.param, request_id=_rid_of(request))

    @app.exception_handler(ParamError)
    async def _on_param_error(request: Request, exc: ParamError):
        logger.info("参数不合法：{}", exc)
        return _ark_envelope(
            400, "InvalidParameter", str(exc), exc.param, request_id=_rid_of(request)
        )

    @app.exception_handler(WebApiError)
    async def _on_web_error(request: Request, exc: WebApiError):
        # 🔴 这里**必须**脱敏：`str(exc)` 里带站点 procedure 名、minter 主机、运维口令、
        #    runbook 编号（见 `_upstream_client_message` 的说明）。全量原文留在**日志与
        #    trace** 里（用户口径：logfire 侧全部上报上游）。
        logger.warning("web 上游错误 code={} status={} — {}", exc.code, exc.http_status, exc)
        rid = _rid_of(request)
        return _ark_envelope(
            _web_http_for(exc),
            _web_code_for(exc),
            _upstream_client_message(exc),
            request_id=rid,
        )

    # ---------------------------------------------------------------- routes --

    @app.get("/healthz")
    async def healthz(request: Request, deep: int = 0):
        s = request.app.state.settings
        pool: dict = request.app.state.upstreams
        # 透传线的客户端要等看到凭据才存在，所以"能力"要并上 settings 的声明 ——
        # 否则 `AVM_AUTH=passthrough` 的健康检查会显示"没有可用上游"，
        # 而服务明明是好的（这会把人引向错误的方向）。
        kinds = sorted(set(pool) | (set(s.available_upstreams) if s.passthrough_cookie else set()))

        info: dict = {
            "ok": True,
            "service": s.service_name,
            "upstream": "web",
            "available_upstreams": kinds,
            "billing_notes": {k: billing_note() for k in kinds},
            "base_url": s.base_url,
            # 鉴权模式**如实报出**（一个变量三选一）：排障时不必再猜"到底是不是闸门模式"。
            # `gate` / `passthrough_cookie` / `credentials_from_caller` 一并保留 ——
            # 它们由 `auth` 派生，已有调用方与巡检在按这几个键取值。
            "auth": s.auth,
            "gate": "required" if s.gate_key else "open",
            "passthrough_cookie": s.passthrough_cookie,
            "credentials_from_caller": s.passthrough_cookie,
            # 号池规模：**只给数量，不列明细** —— /healthz 不鉴权，透传模式下明细
            # 就是别人的账号（余额与指纹）。明细走 Logfire 指标。
            "accounts_tracked": len(_pool_accounts(request.app)),
            "tasks_tracked": request.app.state.tasks.count(),
            # 一眼看出任务表是否真的在持久化（kind=sqlite 才跨重启可读）
            "task_store": request.app.state.tasks.describe(),
            # 查询节流：0 = 关闭（每次实时回上游）
            "task_cache_ttl": s.task_cache_ttl,
            # ⚠️ 两个信号必须**分开**报（2026-09-14 报告 AVM12-OPEN-LOGFIRE 实测发现）：
            #   `logfire`          = SDK 装配成功（span 会生成，但可能一条都不外发）
            #   `logfire_exporting`= 数据**真的**在往外发（需要 token，或显式强制发送）
            # 只报前者时，`send_to_logfire=if-token-present` + 没有 LOGFIRE_TOKEN 会给出
            # `logfire: true` —— 运维据此以为 trace 在云端，实际只有本地收集。
            "logfire": logfire_ok,
            "logfire_exporting": logfire_exporting(),
            "logfire_send": s.logfire_send,
            # 上报口径一眼可见：脱敏关着时请求/响应是原文，抓头开着则有凭证
            "logfire_scrubbing": bool(s.logfire_scrubbing),
            "logfire_capture_headers": bool(s.logfire_capture_headers),
            # 成片出口：对外给的是不是本服务自己的地址（脱敏到底生没生效）。
            # 只报形态，**不含任何凭据**；未配置时 `enabled=False`（= 原样透传上游链接）。
            "media_proxy": _media_summary(request.app),
            # ★ 计费自查口径（2026-09-16 依 livetest 报告 E2E-AVM-015 的告警加）：
            #   0.0.27 起对外任务视图**不含 `usage`**（官方 schema 白名单，
            #   见 `translate.ARK_TASK_FIELDS`）⇒「这条任务花没花钱」**不能**从
            #   `GET /tasks/{id}` 读。把口径直接挂在运维端点上，省得每个人去翻文档、
            #   或更糟 —— 误以为"没给 usage 就是没计费"。
            "billing_check": _billing_check(request.app),
        }
        default = pool.get("web")
        if default is not None and getattr(default, "queue", None) is not None:
            info["submit_queue"] = default.queue.stats()

        if deep:
            # 只有显式 deep=1 才打上游，避免存活探针把上游当依赖
            if s.passthrough_cookie and not _bearer(request):
                # 透传模式下没凭据就**没法**探上游。这里必须把原因说清楚，而不是让
                # /healthz?deep=1 抛 401 —— 那看起来像"服务坏了或鉴权错"。
                info["upstream_probe"] = (
                    "skipped: 透传模式需要调用方自带凭据（Authorization: Bearer <凭据>）"
                )
            else:
                upstream = _upstream_for(request)
                try:
                    with span("ark.healthz.upstream", upstream=upstream.kind):
                        info.update(await asyncio.to_thread(upstream.health))
                    # 把刚取到的余额也挂进自查块：一个字段读完，不必在响应里找两处。
                    # 放在 update 之后、try 之内 —— 探上游失败时它保持 None（如实"没取到"，
                    # 不编一个 0：0 会被读成"余额为零"这种完全相反的事实）。
                    info["billing_check"]["balance"] = info.get("balance")
                except Exception as e:  # noqa: BLE001
                    info["upstream_error"] = str(e)
        return info

    async def _submit_ark(
        request: Request,
        body: dict,
        *,
        extra_warnings: list[str] | None = None,
        response_shape: str = "ark",
    ) -> dict:
        """创建任务的**共享管线** —— 方舟与 OpenAI 两条入口只在外壳上不同，
        计费口径、dry-run、前置拒绝、凭据绑定、任务落库全在这里走一遍。

        `extra_warnings`：入口层自己的"映射说明"（如 keep_ratio→adaptive），
        必须并进 warnings —— 映射不静默。
        `response_shape`：ark → `{"id"}`（旧契约，测试锁死）；openai → Chatfire
        契约四字段 `{id, object, status, created_at}`，一个不多一个不少。
        """
        # 先解析上游：透传模式下这一步同时完成鉴权 —— 未授权的请求
        # 不该先拿到"参数不合法"这种更像配置问题的错误。
        upstream, credential = _upstream_and_credential_for(request)

        # 渠道级选项头：`model_map` 映射（精确键 + 至多一条 `*` 兜底），**默认上游 model
        # 透传**；`model`（钉住）**已撤除**，遗留即拒绝（见 channel_options）。
        # 读头只在这一层（translate 保持纯函数）；坏掉的头**拒绝**而不是当成没配 ——
        # 当成没配会静默按透传跑掉，而运维以为自己的映射生效了。
        channel_options = parse_channel_options(request.headers.get(CHANNEL_OPTIONS_HEADER))
        plan = translate_create(body, channel_options=channel_options)
        eff, warns = billing_view(plan)
        if extra_warnings:
            warns = [*warns, *extra_warnings]
        # 「渠道选择头」两条入口都要留痕（它是请求级事实，不是 OpenAI 面的特性）
        warns = [*warns, *_channel_header_notes(request)]
        plan = {**plan, "effective": eff, "warnings": warns}

        if _is_dry_run(request, body):
            with span(
                "ark.create.dry_run",
                upstream=upstream.kind,
                ark_model=plan["requested"]["model"],
                # 模型解析证据：请求名 → 上游槽位（判据是槽位，不是请求名）
                upstream_slot=eff["model"],
                model_source=eff["model_source"],
                model_verified=eff["model_verified"],
                resolution=eff["resolution"],
                duration=eff["duration"],
                billed=eff["billed"],
                warning_count=len(warns),
                # 请求原文：dry-run 是零成本校验路径，出问题的多数是"我们理解错了参数"
                request=body,
            ):
                logger.info(
                    "dry-run upstream={} ark_model={} slot={}({}) res={} dur={}s billed={}",
                    upstream.kind,
                    plan["requested"]["model"],
                    eff["model"],
                    eff["model_source"],
                    eff["resolution"],
                    eff["duration"],
                    eff["billed"],
                )
                return {"dry_run": True, "ok": True, "upstream": upstream.kind, **plan}

        # 上游明确不具备的能力（例如 Seedance 2.5 的视频编辑/延长）在**发出上游请求之前**
        # 拒绝。dry-run 已在上面放行，所以调用方能零成本看到同一份校验结论。
        if plan.get("incompatible"):
            raise ArkError(
                400,
                "InvalidParameter",
                "; ".join(plan["incompatible"]),
                param="omni_reference_task_type",
            )

        ark_id = _new_ark_id()
        with upstream_exchanges() as calls, span(
            "ark.create.submit",
            upstream=upstream.kind,
            # ark_id 先于提交生成并一起上报：出问题时能拿它去 Logfire 反查整条链路
            ark_id=ark_id,
            ark_model=plan["requested"]["model"],
            # 模型解析证据（请求名 → 上游槽位）：换模型 = 换计费档位，这条链必须能事后复盘
            upstream_slot=eff["model"],
            model_source=eff["model_source"],
            model_verified=eff["model_verified"],
            resolution=eff["resolution"],
            duration=eff["duration"],
            billed=eff["billed"],
            warning_count=len(warns),
            warnings=warns,
            # 调用方发来的 Ark 请求原文（不脱敏；data URI 只留摘要，见 observability.clip）
            request=body,
        ) as sp:
            # 同步 httpx + 可能阻塞的并发闸门 → 必须丢到线程里
            try:
                task_id = await asyncio.to_thread(upstream.create, plan)
            except BaseException as e:
                # 失败路径最需要证据：上游到底回了什么，只有这次采集里有
                sp.set_attribute("error", describe_error(e))
                # ★ 对外脱敏之后，"**调用方实际收到了什么**"必须自己也留一份 —— 否则事后
                #   无法核对"有没有把内部标识漏出出口"（出口回归只能靠这条属性复盘）。
                if isinstance(e, WebApiError):
                    sp.set_attribute(
                        "client_message",
                        # 存**调用方实际收到的那一整句**（含 Request ID）—— 出口回归全靠它
                        _with_request_id(_upstream_client_message(e), _rid_of(request)),
                    )
                # ★ 429 归因必须是**结构化属性**（E2E-AVM-008）：node064 那 3 条 429 的
                #   根因（宿主防火墙丢包）当初只能靠去 minter 上手查 served 才排除——
                #   归因文本躺在 error 散文里，Logfire 里没法过滤聚合。
                #   "captcha_gate" 标记闸门路径；minter 取 token 失败时把归因带上，
                #   `minter_unreachable=True` = 网络层不通（查防火墙路径），False/缺 = 铸造失败。
                if getattr(e, "code", None) == "CAPTCHA_REQUIRED":
                    sp.set_attribute("captcha_gate", True)
                    mle = getattr(e, "minter_last_error", None)
                    if mle:
                        sp.set_attribute("minter_last_error", str(mle)[:200])
                        sp.set_attribute("minter_unreachable", "unreachable" in str(mle))
                set_upstream_calls(sp, calls)
                raise
            sp.set_attribute("upstream_task_id", task_id or "")
            set_upstream_calls(sp, calls)

        if not task_id:
            raise ArkError(400, "InvalidParameter", "upstream accepted nothing (no taskId returned)")

        entry = {
            "id": ark_id,
            "taskId": task_id,
            "upstream": upstream.kind,
            # 归属（凭据指纹）：透传模式下 GET /tasks 要靠它做租户隔离
            "owner": _owner_of(request),
            "model": plan["requested"]["model"],
            "requested": plan["requested"],
            "effective": eff,
            "warnings": warns,
            "unsupported": plan["unsupported"],
            "createdAtMs": int(time.time() * 1000),
        }
        # 凭据绑定（E2E-AVM-011）：task_id ↔ api-key（透传下即 newapi 传来的会话凭据）。
        # 之后 GET/DELETE /tasks/{id} 不带凭据也按这条绑定解析上游凭据 —— 轮询方
        # 不必再持 cookie。原文只进 sqlite（0600），绝不进日志 / span / 响应体。
        request.app.state.tasks.put(entry, credential=credential)
        logger.info(
            "已提交 ark_id={} upstream={} upstream_task={} billed={}",
            entry["id"],
            upstream.kind,
            task_id,
            eff["billed"],
        )
        if response_shape == "openai":
            # Chatfire 创建响应契约：恰好四个字段。id 形态本就是 `cgt-*`，与示例一致。
            return {
                "id": entry["id"],
                "object": "video",
                "status": "queued",
                "created_at": int(entry["createdAtMs"]) // 1000,
            }
        return {"id": entry["id"]}

    @app.post(TASKS_PATH)
    async def create_task(request: Request, _: str = Depends(require_bearer)):
        body = await _json_body(request)
        return await _submit_ark(request, body)

    @app.get(TASKS_PATH)
    async def list_tasks(
        request: Request,
        page_num: int = 1,
        page_size: int = 20,
        _: str = Depends(require_bearer),
    ):
        # 列表是**跨任务**的租户视图：透传下没凭据就没法界定"这是谁的列表"，
        # 也不能退化成"返回所有人的任务"（会把 B 的 prompt 泄给 A）—— 必须 401。
        # 单条查询不同：它有凭据绑定兜底，见 _upstream_for_task。
        if request.app.state.settings.passthrough_cookie and not _bearer(request):
            raise ArkError(
                401,
                "AuthenticationError",
                "passthrough mode requires a bearer token to scope the task list "
                "(tasks are isolated per credential)",
            )
        page_size = max(1, min(100, page_size))
        page_num = max(1, page_num)
        store = request.app.state.tasks
        # 归属过滤必须下推到存储层：先取一页再筛会让页码与 total 双双错位
        owner = _owner_of(request) or None
        # 分页下推到存储层：sqlite 后端只取当前页，不必把整表读进内存
        window = store.list_recent(page_size, (page_num - 1) * page_size, owner)
        upstream = _upstream_for(request)
        items = [await _task_view(request, e, upstream) for e in window]
        # 说明：本服务只知道自己创建过的任务；上游的列表按会话维度，未在此合并。
        return {
            "items": items,
            "total": store.count(owner),
            "page_num": page_num,
            "page_size": page_size,
        }

    @app.get(TASKS_PATH + "/{task_id}")
    async def get_task(task_id: str, request: Request, _: str = Depends(require_bearer)):
        entry = request.app.state.tasks.get(task_id)
        if not _visible(request, entry):
            raise ArkError(404, "TaskNotFound", f"task {task_id} not found")
        # 透传下调用方可以不带凭据 —— 按创建时绑定的凭据回上游取（E2E-AVM-011）
        return await _task_view(request, entry, _upstream_for_task(request, entry))

    # ---------------------------------------------- OpenAI /v1/videos 兼容面 ----
    #
    # 契约：Chatfire「OpenaiVideos格式 / Seedance」两份 OpenAPI（369966278 创建 /
    # 369966279 查询）—— "务必一样"：响应只含契约声明的字段。
    # 表单与 JSON 都收：Chatfire 的 curl 用 multipart 表单，OpenAI 官方 SDK 用
    # JSON；两种形态等价，进同一条 _submit_ark 管线。

    async def _openai_fields(request: Request) -> dict:
        """收 OpenAI 创建请求的字段：JSON body 或 multipart/urlencoded 表单。

        表单里的文件部件（UploadFile）读成字节、按 **magic bytes** 定 MIME 包成
        data URI —— 这是进程内转换、零网络；真实提交时由既有转存管线上传到
        站点 CDN，dry-run 依旧零副作用。

        🔴 **两道体积闸**（2026-09-15 补）：`.form()` 会把 >1MB 的部件落盘、`read()` 再整个
        读进内存、然后 base64（+33%）⇒ 没有闸时一个超大件是"先写满磁盘 → 吃掉几倍内存 →
        最后才被站点 `maxBytes` 拒掉"。顺序是：
          ① **解析之前**看请求声明的 `Content-Length`（这才是真正防落盘的那道）；
          ② 读部件时**边读边判**（chunked 上传没有 Content-Length，只能靠它兜底）。
        """
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            return await _json_body(request)
        declared = int(request.headers.get("content-length") or 0)
        if declared > _FORMS_MAX_BODY_BYTES:
            raise ParamError(
                f"上传的请求体过大：声明 {declared} 字节，超过上限 {_FORMS_MAX_BODY_BYTES} 字节"
                f"（{_FORMS_MAX_BODY_BYTES // 1048576}MB）—— 参考素材请压小后再传，"
                f"或改用链接（URL 由适配层按预算流式取回）。",
                "input_reference",
            )
        form = await request.form()
        fields: dict = {}
        for key in form.keys():
            converted: list = []
            for v in form.getlist(key):
                if str(v) == "":
                    continue  # curl 里的 --form 'input_reference=""' = 未提供
                if hasattr(v, "read"):  # UploadFile：文件部件 → 有界读 → data URI
                    buf = await _read_upload(v)
                    with suppress(Exception):  # 及时关闭，别留 SpooledTemporaryFile 告警
                        await v.close()
                    sniffed = sniff_file(buf)
                    mime = (
                        sniffed["content_type"]
                        if sniffed["kind"] != "unknown"
                        else (v.content_type or "image/png")
                    )
                    converted.append(f"data:{mime};base64," + base64.b64encode(buf).decode())
                else:
                    converted.append(str(v))
            if not converted:
                continue
            # input_reference 是数组字段：重复出现即为多张参考图
            fields[key] = converted if key == "input_reference" else converted[0]
        return fields

    @app.post(OPENAI_VIDEOS_PATH)
    async def create_video(request: Request, _: str = Depends(require_bearer)):
        fields = await _openai_fields(request)
        body, notes = ark_body_from_openai(
            fields, reference_format=request.headers.get("input-reference-format") or ""
        )
        return await _submit_ark(request, body, extra_warnings=notes, response_shape="openai")

    @app.get(OPENAI_VIDEOS_PATH + "/{video_id}")
    async def get_video(video_id: str, request: Request, _: str = Depends(require_bearer)):
        entry = request.app.state.tasks.get(video_id)
        if not _visible(request, entry):
            raise ArkError(404, "TaskNotFound", f"task {video_id} not found")
        # 凭据绑定与方舟线完全一致：透传下可不带凭据，按创建时绑定的那份取
        view = await _task_view(request, entry, _upstream_for_task(request, entry))
        return openai_task_view(view, created_at_fallback=int(entry.get("createdAtMs") or 0) // 1000 or None)

    # ---- 成片下载出口（见 media_proxy）------------------------------------
    # 对外给出的成片地址形如 `{AVM_PUBLIC_BASE}/v/{ark_id}.mp4`：路径里只有本服务
    # 的任务 id，读不出上游域名与模型名。取用时**鉴权 + 归属校验**，再流式回源，并把
    # 响应头**白名单化**（上游的 `content-disposition` 里带着模型名，绝不透传）。
    @app.get(settings.media_path + "/{name}")
    async def download_media(name: str, request: Request, _: str = Depends(require_bearer)):
        proxy = getattr(request.app.state, "media_proxy", None)
        if proxy is None or not proxy.enabled:
            raise ArkError(404, "TaskNotFound", f"media {name} not found")
        entry = request.app.state.tasks.get(_strip_media_ext(name))
        # ⚠️ 归属校验与任务查询**同一套**（`_visible`）：透传模式下 A 拿不到 B 的成片。
        #    不通过时回 404 而不是 403 —— 与任务查询保持一致，"存在但无权限"本身
        #    就是一条不该给出的信息。
        if not _visible(request, entry):
            raise ArkError(404, "TaskNotFound", f"media {name} not found")

        source = await _source_url_for(request, entry)
        if not source:
            # 任务在、但还没有成片（或者说上游那条记录还没给出地址）。用 409 让调用方
            # 明白"不是没有，是还没好"，可以重试 —— 与 404（不存在）刻意分开。
            raise ArkError(
                409,
                "TaskNotReady",
                f"task {entry['id']} has no finished video yet",
            )

        try:
            stream = await asyncio.to_thread(
                proxy.open, source, range_header=request.headers.get("range") or ""
            )
        except MediaSourceError as e:
            logger.warning(
                "成片回源失败 ark_id={} transport={}", entry["id"], e.transport or "-"
            )
            raise ArkError(502, "UpstreamError", MEDIA_SOURCE_ERROR) from None

        if stream.status_code >= 400:
            logger.warning("成片回源 HTTP {} ark_id={}", stream.status_code, entry["id"])
            status = 404 if stream.status_code == 404 else 502
            stream.close()
            raise ArkError(status, "UpstreamError", MEDIA_SOURCE_ERROR)

        return StreamingResponse(
            stream,
            status_code=stream.status_code,
            headers=proxy.out_headers(
                stream, ark_id=entry["id"], ext=_media_ext(name)
            ),
        )

    async def _source_url_for(request: Request, entry: dict) -> str:
        """成片的**上游**地址：优先用记录里存的那份，没有再当场查一次上游。

        为什么要有第一条路径：查询路径已在高频轮询，顺手记下的 `source_url` 让下载
        不必再打一次上游（E2E-AVM-011 之后轮询本来就该尽量少打上游）。
        注意这里走的是 `_internal_view`（**未脱敏**的内部视图）—— 出口层会把地址
        换成我们自己的，不能拿它去回源（会自己指回自己）。
        """
        cached = entry.get("source_url")
        if cached:
            return str(cached)
        view = await _internal_view(request, entry, _upstream_for_task(request, entry))
        content = view.get("content")
        url = content.get("video_url") if isinstance(content, dict) else None
        if url and str(view.get("status") or "") == "succeeded":
            _remember_source_url(request, entry, view)
            return str(url)
        return ""

    # ⚠️ 刻意**没有** DELETE /tasks/{id}（2026-09-15 定）：上游没有取消端点，
    # "删本地记录"只会制造"任务没了"的错觉（跑着的照跑照扣）。对外接口面
    # 只有创建 + 查询；记录随保留窗口自然淘汰。

    async def _internal_view(request: Request, entry: dict, upstream) -> dict:
        """内部任务视图：取数（含节流缓存）+ 证据（span / 轮询闸门 / 内部字段）。

        ⚠️ **不含出口脱敏** —— 这里的 `content.video_url` 仍是**上游直链**。
        对外的那个地址由 `_task_view` 换；`/v/{ark_id}.mp4` 下载端点则直接用它回源。
        """
        # 先看节流缓存：非终态 TTL 内复用、终态永久复用 —— 轮询的请求量不能
        # 原样打到上游（429 的主要来源，任务出片要 ~60s）。
        ark_id = entry["id"]
        calls: list = []
        error: str | None = None
        view = request.app.state.task_cache.get(ark_id)
        cached = view is not None
        if not cached:
            # 🔴 **闸门 1（产生层）**：先取数据、**后决定要不要留痕**。
            #    OTel 没有"丢弃已开的 span"，所以不能"先开 span 再看结论 ⇒ 不该记就扔" ——
            #    只能把 span 的创建推迟到结论出来之后。代价是这条 span 不再包住上游调用，
            #    它的耗时改看 `upstream_calls[].duration_ms`（另有 `upstream_ms` 汇总）。
            #    换来的：轮询（同状态重复查询）**一条 span 都不产生**。
            with upstream_exchanges() as box:
                try:
                    view = await asyncio.to_thread(upstream.get_task, entry["taskId"])
                except WebApiError as e:
                    error = describe_error(e)
                    logger.warning("查任务失败 upstream_task={} — {}", entry["taskId"], e)
                    view = {}
                else:
                    # 只有**成功取到**的视图才进缓存（上游失败的空壳不该被记住）
                    request.app.state.task_cache.put(ark_id, view or {})
            calls = list(box)

        view = dict(view or {})
        status = str(view.get("status") or "")
        # 这次查询要不要留痕：首次观测 / 状态跃迁 / 失败 ⇒ 留；同状态重复轮询 ⇒ 只计数。
        # 这就是那道闸门 —— 判据只有"状态变没变"，与轮询有多密无关。
        decision = request.app.state.poll_gate.observe(ark_id, status, error=error)
        count_poll(
            upstream=upstream.kind,
            status=status,
            reported=decision is not None,
            cached=cached,
        )
        if decision is not None:
            with span(
                "ark.task.fetch",
                upstream=upstream.kind,
                ark_id=ark_id,
                upstream_task_id=entry["taskId"],
                cached=cached,
                # ★ 跃迁信息：读 trace 的人不必再靠"跟上一行对比"来推断状态变没变。
                #   `reason` = first（首次观测）/ transition（跃迁）/ error（失败）。
                transition_reason=decision["reason"],
                transition_from=decision["from"],
                transition_to=decision["to"],
                poll_count=decision["polls"],
                # 终态与**实际计费结果**必须进 trace —— 出片后对账就靠这两项
                status=status,
                paid=bool((view.get("usage") or {}).get("paid")),
                # 归一化后的任务对象（出片地址、用量、resolution 回填都在这里）
                upstream_response=view,
                # 只有这两项必须单独挂：它们来自**本地任务记录**（`entry`），不在
                # `upstream_response` 里 —— 不挂就真的丢了。
                # 其余证据（上游实际模型 / 站点原始记录 `upstream_record` / 实际产出档位
                # `resolution`）**早在 `upstream_response` 里**（归一化后的完整视图），
                # 同一份数据挂两遍只会让 trace 变大、口径还容易漂。
                warnings=entry.get("warnings") or [],
                unsupported=entry.get("unsupported") or [],
            ) as sp:
                if error:
                    sp.set_attribute("error", error)
                total_ms = sum(float(c.get("duration_ms") or 0) for c in calls)
                if total_ms:
                    sp.set_attribute("upstream_ms", round(total_ms, 1))
                set_upstream_calls(sp, calls)
                if decision["reason"] == "transition":
                    # 「状态跃迁用事件而不是 span」的落点：跃迁本身记成一个**事件**。
                    # ⚠️ 事件必须挂在一条 span 上，而"跨请求的全程唯一 span"在本服务
                    #    落不了地（span 不能跨 HTTP 请求；多 worker 下同一条轮询会落到
                    #    不同进程）⇒ 这里退一步：事件挂在**跃迁那一条** span 上，同时把
                    #    跃迁方向做成属性（`transition_from` / `transition_to`）便于过滤。
                    sp.add_event(
                        "status_change",
                        {"from": decision["from"], "to": decision["to"]},
                    )
        # 等待时长只在**真的看到跃迁进终态**时记一次（同状态轮询记一遍 = 重复计数）
        if (
            decision is not None
            and decision["reason"] == "transition"
            and status in _TERMINAL_ARK_STATUS
        ):
            # 老记录（`createdAtMs` 落库之前建的）没有创建时刻 ⇒ **不记**：与其记一个 0 秒，
            # 不如不给 —— 与响应体收窄同一条纪律（值不确定的一律不给）。
            created_ms = int(entry.get("createdAtMs") or 0)
            if created_ms:
                record_wait(
                    upstream=upstream.kind,
                    final_status=status,
                    seconds=max(0.0, time.time() - created_ms / 1000),
                    source="poll",
                )
        view["id"] = entry["id"]
        # `model` = **调用方请求的**模型（覆盖掉上游记录里的同名值）
        view["model"] = entry["model"]
        # ★ `upstream_model` = **上游实际执行**的模型（站点任务记录里的 `aiModel`）。
        #   2026-09-14 报告 AVM12-OPEN-UPSTREAM：只有 `model` 时，调用方无从知道上游换了
        #   模型（请求 `doubao-seedance-2-5-260628`，成片的却是 `minimax_h3`）。
        #   查不到（上游查询失败、或记录里没有该字段）时**如实给 None**，
        #   绝不用请求值顶上 —— 那等于把"看不到"变成"看到一个假的"。
        view["upstream_model"] = view.get("upstream_model")
        view["upstream"] = entry["upstream"]
        view["requested"] = entry["requested"]
        view["effective"] = entry["effective"]
        view["warnings"] = entry["warnings"]
        view["unsupported"] = entry["unsupported"]
        # 注：出口脱敏（上游 URL → 本服务地址）**不在这一层**。本函数只负责取数与
        #     证据（节流缓存 / span / 内部字段），出口在 `_task_view` 那一层。
        #     拆开是为了让 `/v/{ark_id}.mp4` 的下载端点复用同一份取数逻辑 ——
        #     它要的正是**上游**地址，走出口层反而会被脱敏改写掉。
        # 说明：`generate_audio` 此前在这里"回显请求值"，现已删除 —— 站点根本不承接它，
        # 回显等于替上游承诺一件它没答应的事（2026-09-15 口径：值不确定的字段一律不给）。
        # 🔴 最后一跳收窄（`ark_task_view`）：只留官方 schema 里、且**我们真知道值**的字段
        #    —— `id` / `status` / `error` / `content.video_url` / `duration` / `ratio` /
        #    `created_at` / `updated_at`。上面挂的内部证据（`model` / `upstream_model` /
        #    `upstream_record` / `resolution` / `requested` / `effective` / `warnings` /
        #    `unsupported` / `usage.credits` / `usage.paid`）**一律不进响应体**。
        #    理由：官方 SDK（Java/Go）碰到 unknown field 是**报错**而非忽略，多一个键就是把
        #    一个能跑的客户端变成报错的客户端；而给不准的值等于往契约里塞假话。
        #    证据不丢：`ark.task.fetch` span 上带着**裁剪前**的完整内部视图
        #    （`upstream_response`）、站点原始记录（`upstream_record`）以及
        #    `upstream_model` / `warnings` / `unsupported`，logfire 侧照常可查。
        return view

    async def _task_view(request: Request, entry: dict, upstream) -> dict:
        """内部任务视图 → **对外响应体**：先脱敏换址，再走白名单收窄。

        分两步是刻意的：`ark_task_view` 只认白名单，**过了它就没有再改写的机会** ——
        所以出口脱敏（上游 URL → 本服务成片地址）必须发生在它之前，否则上游直链
        会原样进响应体。未配置 `AVM_PUBLIC_BASE` 时这里是恒等变换。
        """
        view = await _internal_view(request, entry, upstream)
        proxy = getattr(request.app.state, "media_proxy", None)
        if proxy is not None and proxy.enabled:
            # 顺手把上游地址记回任务记录，供 `/v/{ark_id}.mp4` 回源时读（只在缺失时写）
            _remember_source_url(request, entry, view)
            view = _apply_media_gate(proxy, entry, view)
        return ark_task_view(view)

    return app


def _new_ark_id() -> str:
    """Ark 任务 id 形态：cgt-YYYYMMDDHHMMSS-xxxxxxxx。"""
    return "cgt-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
