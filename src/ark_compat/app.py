"""FastAPI 应用：把 aivideomaker 暴露成火山方舟 Seedance 协议。

路由（与 Ark 原生一致）：

    POST   /api/v3/contents/generations/tasks          创建任务
    GET    /api/v3/contents/generations/tasks          列表（本进程内）
    GET    /api/v3/contents/generations/tasks/{id}     查询
    DELETE /api/v3/contents/generations/tasks/{id}     取消/删除
    GET    /healthz                                    存活 + 上游体检

把火山官方 SDK 的 base_url 指向本服务即可直接使用。

上游只有一条：**网页端内部接口**（tRPC over `/api`，会话 cookie）。
差异被 `upstreams.py` 吸收。

**鉴权有两种形态**：

    进程持有凭据      `AVM_COOKIE`
    调用方自带凭据    `AVM_PASSTHROUGH_COOKIE=1`（Bearer 里放网页会话 cookie）

透传时调用方的 `Authorization: Bearer` 就是**上游凭据本身**（`auth_session=…`
裸 token 或完整 Cookie 串，见 `docs/web-reverse/`）。它与闸门 `AVM_GATE_KEY`
**互斥** —— 同一个 Bearer 不可能既是闸门密钥又是上游凭据（`Settings.validate()`
直接拒绝启动）。透传即多租户：任务表按**凭据指纹**隔离，见 `store.py` 的 `owner`。

四条安全约定：
  1. 站点**没有取消端点** —— 删除只删本地记录，跑着的任务照跑照扣，绝不谎报"已取消"。
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
import hashlib
import hmac
import json
import time
import uuid
import warnings
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from loguru import logger

from . import __version__
from .cookie import AUTH_COOKIE_NAME, normalize_cookie_header
from .errors import ParamError, WebApiError
from .observability import (
    clip,
    describe_error,
    instrument_fastapi,
    logfire_exporting,
    logfire_ready,
    record_account,
    set_upstream_calls,
    setup_observability,
    span,
    upstream_exchanges,
)
from .settings import Settings
from .store import build_task_store
from .translate import billing_note, billing_view, translate_create
from .upstreams import build_upstreams, build_web_for_cookie

TASKS_PATH = "/api/v3/contents/generations/tasks"

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


def _ark_envelope(status: int, code: str, message: str, param: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "param": param, "type": "InvalidRequest"}},
    )


# ------------------------------------------------------------ 错误映射 ----

_WEB_HTTP = (400, 401, 403, 404, 429)


def _web_http_for(e: WebApiError) -> int:
    if e.code in ("CAPTCHA_REQUIRED", "QUEUE_TIMEOUT"):
        return 429
    if e.http_status in _WEB_HTTP:
        return e.http_status
    return 502


def _web_code_for(e: WebApiError) -> str:
    return {
        "CAPTCHA_REQUIRED": "RateLimitExceeded",
        "QUEUE_TIMEOUT": "TaskQueueFull",
        "NOT_FOUND": "TaskNotFound",
    }.get(e.code, "UpstreamError" if e.code in (None, "TRPC_ERROR") else e.code)


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def require_bearer(request: Request, authorization: str | None = Header(default=None)) -> str:
    """闸门鉴权。未设 AVM_GATE_KEY 时不校验（本地自用）。

    开了透传（`AVM_PASSTHROUGH_COOKIE`）就**不该**再设闸门：此时 Bearer 要拿去当
    上游凭据，而闸门会先把它挡掉 —— 那种组合在 `Settings.validate()` 里直接拒绝启动，
    不让它变成"每个请求都 401"的线上迷局。
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


def _upstream_for(request: Request):
    """取本次请求要用的 web 上游。

    - **透传模式**（`AVM_PASSTHROUGH_COOKIE=1`）：调用方的 Bearer 就是网页会话 cookie，
      按凭据指纹建/复用客户端 —— 每个调用方用自己的账号与免费窗口。
    - 否则用本进程持有的那份（`AVM_COOKIE`）。
    """
    settings = request.app.state.settings

    if settings.passthrough_cookie:
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
        return _passthrough_web_upstream(request, cookie)

    pool: dict = request.app.state.upstreams
    if "web" not in pool:
        raise ArkError(
            503,
            "UpstreamUnavailable",
            "web upstream is not configured in this process (needs AVM_COOKIE); "
            f"available: {sorted(pool)}",
        )
    return pool["web"]


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
        docs_url=None,
        redoc_url=None,
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
    logger.info(
        "上游就绪 available={} task_store={}",
        sorted(app.state.upstreams),
        app.state.tasks.describe(),
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
        with logger.contextualize(request_id=rid):
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("未处理异常 {} {}", request.method, request.url.path)
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["x-request-id"] = rid
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
        return _ark_envelope(exc.status, exc.code, str(exc), exc.param)

    @app.exception_handler(ParamError)
    async def _on_param_error(request: Request, exc: ParamError):
        logger.info("参数不合法：{}", exc)
        return _ark_envelope(400, "InvalidParameter", str(exc), exc.param)

    @app.exception_handler(WebApiError)
    async def _on_web_error(request: Request, exc: WebApiError):
        logger.warning("web 上游错误 code={} status={} — {}", exc.code, exc.http_status, exc)
        return _ark_envelope(_web_http_for(exc), _web_code_for(exc), str(exc))

    # ---------------------------------------------------------------- routes --

    @app.get("/healthz")
    async def healthz(request: Request, deep: int = 0):
        s = request.app.state.settings
        pool: dict = request.app.state.upstreams
        # 透传线的客户端要等看到凭据才存在，所以"能力"要并上 settings 的声明 ——
        # 否则 `AVM_PASSTHROUGH_COOKIE=1` 的健康检查会显示"没有可用上游"，
        # 而服务明明是好的（这会把人引向错误的方向）。
        kinds = sorted(set(pool) | (set(s.available_upstreams) if s.passthrough_cookie else set()))

        info: dict = {
            "ok": True,
            "service": s.service_name,
            "upstream": "web",
            "available_upstreams": kinds,
            "billing_notes": {k: billing_note() for k in kinds},
            "supports_cancel": {
                k: bool(getattr(pool.get(k), "supports_cancel", False)) for k in kinds
            },
            "base_url": s.base_url,
            "gate": "required" if s.gate_key else "open",
            "passthrough_cookie": s.passthrough_cookie,
            "credentials_from_caller": s.passthrough_cookie,
            # 号池规模：**只给数量，不列明细** —— /healthz 不鉴权，透传模式下明细
            # 就是别人的账号（余额与指纹）。明细走 Logfire 指标。
            "accounts_tracked": len(_pool_accounts(request.app)),
            "tasks_tracked": request.app.state.tasks.count(),
            # 一眼看出任务表是否真的在持久化（kind=sqlite 才跨重启可读）
            "task_store": request.app.state.tasks.describe(),
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
                except Exception as e:  # noqa: BLE001
                    info["upstream_error"] = str(e)
        return info

    @app.post(TASKS_PATH)
    async def create_task(request: Request, _: str = Depends(require_bearer)):
        body = await _json_body(request)
        # 先解析上游：透传模式下这一步同时完成鉴权 —— 未授权的请求
        # 不该先拿到"参数不合法"这种更像配置问题的错误。
        upstream = _upstream_for(request)

        plan = translate_create(body)
        eff, warns = billing_view(plan)
        plan = {**plan, "effective": eff, "warnings": warns}

        if _is_dry_run(request, body):
            with span(
                "ark.create.dry_run",
                upstream=upstream.kind,
                ark_model=plan["requested"]["model"],
                resolution=eff["resolution"],
                duration=eff["duration"],
                billed=eff["billed"],
                warning_count=len(warns),
                # 请求原文：dry-run 是零成本校验路径，出问题的多数是"我们理解错了参数"
                request=body,
            ):
                logger.info(
                    "dry-run upstream={} ark_model={} res={} dur={}s billed={}",
                    upstream.kind,
                    plan["requested"]["model"],
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
        request.app.state.tasks.put(entry)
        logger.info(
            "已提交 ark_id={} upstream={} upstream_task={} billed={}",
            entry["id"],
            upstream.kind,
            task_id,
            eff["billed"],
        )
        return {"id": entry["id"]}

    @app.get(TASKS_PATH)
    async def list_tasks(
        request: Request,
        page_num: int = 1,
        page_size: int = 20,
        _: str = Depends(require_bearer),
    ):
        page_size = max(1, min(100, page_size))
        page_num = max(1, page_num)
        store = request.app.state.tasks
        # 归属过滤必须下推到存储层：先取一页再筛会让页码与 total 双双错位
        owner = _owner_of(request) or None
        # 分页下推到存储层：sqlite 后端只取当前页，不必把整表读进内存
        window = store.list_recent(page_size, (page_num - 1) * page_size, owner)
        items = [await _task_view(request, e) for e in window]
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
        return await _task_view(request, entry)

    @app.delete(TASKS_PATH + "/{task_id}")
    async def delete_task(task_id: str, request: Request, _: str = Depends(require_bearer)):
        entry = request.app.state.tasks.get(task_id)
        if not _visible(request, entry):
            raise ArkError(404, "TaskNotFound", f"task {task_id} not found")
        request.app.state.tasks.delete(task_id)
        upstream = _upstream_for(request)
        # 取消失败（web 线根本没有取消端点）时，上游原文是唯一的证据 —— 所以错误
        # 分支留在 span 内部处理，而不是把异常抛出去让 span 只剩一个空壳。
        with upstream_exchanges() as calls, span(
            "ark.task.cancel",
            upstream=upstream.kind,
            ark_id=entry["id"],
            upstream_task_id=entry["taskId"],
        ) as sp:
            try:
                result = await asyncio.to_thread(upstream.cancel_task, entry["taskId"])
            except WebApiError as e:
                # 记录已删，但不能谎报"已取消"
                sp.set_attribute("cancelled", False)
                sp.set_attribute("error", describe_error(e))
                set_upstream_calls(sp, calls)
                logger.warning("取消失败 upstream_task={} — {}", entry["taskId"], e)
                return {"cancelled": False, "task_id": entry["taskId"], "reason": str(e)}
            sp.set_attribute("cancelled", bool(result.get("cancelled")))
            sp.set_attribute("upstream_response", result)
            set_upstream_calls(sp, calls)
        if result.get("cancelled"):
            logger.info("已取消并全额退积分 upstream_task={}", entry["taskId"])
        else:
            logger.warning(
                "仅删除记录，未真正取消 upstream_task={} — {}", entry["taskId"], result.get("reason")
            )
        return result

    async def _task_view(request: Request, entry: dict) -> dict:
        upstream = _upstream_for(request)
        with upstream_exchanges() as calls:
            try:
                with span(
                    "ark.task.fetch",
                    upstream=upstream.kind,
                    ark_id=entry["id"],
                    upstream_task_id=entry["taskId"],
                ) as sp:
                    # 上游层返回的已经是归一化好的 Ark 任务对象
                    try:
                        view = await asyncio.to_thread(upstream.get_task, entry["taskId"])
                    except WebApiError as e:
                        sp.set_attribute("error", describe_error(e))
                        set_upstream_calls(sp, calls)
                        raise
                    # 终态与**实际计费结果**必须进 trace —— 出片后对账就靠这两项
                    sp.set_attribute("status", (view or {}).get("status") or "")
                    sp.set_attribute("paid", bool(((view or {}).get("usage") or {}).get("paid")))
                    # 归一化后的任务对象（出片地址、用量、resolution 回填都在这里）
                    sp.set_attribute("upstream_response", view or {})
                    set_upstream_calls(sp, calls)
            except WebApiError as e:
                logger.warning("查任务失败 upstream_task={} — {}", entry["taskId"], e)
                view = {}
        view = dict(view or {})
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
        # 上游不承接 generate_audio，回显调用方的请求值，而不是伪造一个默认值
        view["generate_audio"] = bool(entry["requested"].get("generate_audio"))
        return view

    return app


def _new_ark_id() -> str:
    """Ark 任务 id 形态：cgt-YYYYMMDDHHMMSS-xxxxxxxx。"""
    return "cgt-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
