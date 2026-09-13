"""FastAPI 应用：把 aivideomaker 暴露成火山方舟 Seedance 协议。

路由（与 Ark 原生一致）：

    POST   /api/v3/contents/generations/tasks          创建任务
    GET    /api/v3/contents/generations/tasks          列表（本进程内）
    GET    /api/v3/contents/generations/tasks/{id}     查询
    DELETE /api/v3/contents/generations/tasks/{id}     取消/删除
    GET    /healthz                                    存活 + 上游体检

把火山官方 SDK 的 base_url 指向本服务即可直接使用。

**两条上游并存**，不是二选一：

    默认线          AVM_UPSTREAM=official|web
    按请求覆盖      X-Avm-Upstream: web|official   （或 ?upstream=web）

两条线各有所长，所以都留着：web 线有免费窗口（turbo ≤8s），official 线可取消并全额退积分。
差异被 `upstreams.py` 吸收，对外协议完全一致。

四条安全约定：
  1. 官方线**提交即计费**，因此没有支出上限的提交一律 400 拒绝（见 translate.py）。
  2. 两条线的计费口径**不同**，由 `translate.billing_view` 按上游渲染 —— 把 web 线的
     "免费窗口"提示端给官方线的调用方，是本项目最贵的一类 bug。
  3. 上游 key / cookie / Bearer token **不进日志、不进 span 属性** —— 靠
     `capture_headers=False`（硬过滤）而不是靠脱敏；请求 / 响应**原文**进 trace，
     不脱敏（`observability.py` 顶部有完整取舍）。
  4. 所有请求都可 `X-Avm-Dry-Run: 1` 或 `extra_body.aivideomaker_dry_run=true`
     走零成本校验 —— 这正是"验证翻译层"与"真花钱"之间唯一的开关。

上游调用一律走 `asyncio.to_thread`：客户端是**同步 httpx**，直接在协程里调会阻塞
整个事件循环（web 线的并发闸门还可能阻塞数十秒）。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from loguru import logger

from . import __version__
from .errors import OfficialApiError, ParamError, WebApiError
from .observability import (
    clip,
    describe_error,
    instrument_fastapi,
    set_upstream_calls,
    setup_observability,
    span,
    upstream_exchanges,
)
from .settings import UPSTREAMS, Settings
from .store import build_task_store
from .translate import OFFICIAL_MODELS, billing_note, billing_view, translate_create
from .upstreams import build_official_for_key, build_upstreams

TASKS_PATH = "/api/v3/contents/generations/tasks"

# 透传模式下缓存"调用方 token -> 上游"。上限只是防止无界增长。
_PASSTHROUGH_CACHE_MAX = 64


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

_OFFICIAL_HTTP = (400, 401, 402, 409, 422, 429)
_WEB_HTTP = (400, 401, 403, 404, 429)


def _http_for(e: OfficialApiError) -> int:
    if e.code == "BUDGET_UNSET":
        return 400
    if e.http_status in _OFFICIAL_HTTP:
        return e.http_status
    return 502


def _code_for(e: OfficialApiError) -> str:
    return {
        "AUTH_FAILED": "AuthenticationError",
        "INSUFFICIENT_CREDITS": "InsufficientCredits",
        "BUDGET_EXCEEDED": "BudgetExceeded",
        "BUDGET_UNSET": "BudgetGuardRequired",
        "IDEMPOTENCY_CONFLICT": "IdempotencyConflict",
        "INVALID_PAYLOAD": "InvalidParameter",
        "INVALID_MODEL": "InvalidParameter",
        "RATE_LIMITED": "RateLimitExceeded",
    }.get(e.code, e.code or "InternalServiceError")


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
    """闸门鉴权。未设 AVM_GATE_KEY 时不校验（本地自用）。"""
    gate = request.app.state.settings.gate_key
    if not gate:
        return authorization or ""
    token = _bearer(request)
    # 常量时间比较，避免按字符泄露闸门密钥
    if not hmac.compare_digest(token, gate):
        raise ArkError(401, "AuthenticationError", "invalid or missing bearer token")
    return token


def _requested_upstream(request: Request) -> str:
    """默认上游 + 请求级覆盖（`X-Avm-Upstream` 头优先，其次 `?upstream=`）。"""
    settings = request.app.state.settings
    want = (
        request.headers.get("x-avm-upstream")
        or request.query_params.get("upstream")
        or settings.upstream
    )
    return str(want).strip().lower()


def _upstream_for(request: Request):
    """取本次请求要用的上游。"""
    settings = request.app.state.settings
    want = _requested_upstream(request)

    if want not in UPSTREAMS:
        raise ArkError(
            400, "InvalidParameter", f"unknown upstream {want!r} (expected one of {list(UPSTREAMS)})"
        )

    if want == "official" and settings.passthrough_key:
        # 透传模式：用调用方自带的 token 现建（并缓存）
        token = _bearer(request)
        if not token:
            raise ArkError(401, "AuthenticationError", "passthrough mode requires a bearer token")
        cache: dict = request.app.state.passthrough_upstreams
        if token not in cache and len(cache) >= _PASSTHROUGH_CACHE_MAX:
            cache.clear()  # 粗暴但安全的回收，避免无界增长
        if token not in cache:
            cache[token] = build_official_for_key(settings, token)
        return cache[token]

    pool: dict = request.app.state.upstreams
    if want not in pool:
        need = "AVM_KEY" if want == "official" else "AVM_COOKIE"
        raise ArkError(
            503,
            "UpstreamUnavailable",
            f"upstream {want!r} is not configured in this process (needs {need}); "
            f"available: {sorted(pool)}",
        )
    return pool[want]


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()

    logfire_ok = setup_observability(settings)

    app = FastAPI(
        title=settings.service_title,
        description="把 aivideomaker 包装成火山方舟 Seedance 协议形状（官方 API / 网页端两条上游并存）。",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.upstreams = build_upstreams(settings, log=lambda m: logger.warning(m))
    app.state.passthrough_upstreams = {}
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
        "上游就绪 default={} available={} task_store={}",
        settings.upstream,
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

    @app.exception_handler(OfficialApiError)
    async def _on_official_error(request: Request, exc: OfficialApiError):
        logger.warning("官方上游错误 code={} status={} — {}", exc.code, exc.http_status, exc)
        return _ark_envelope(_http_for(exc), _code_for(exc), str(exc))

    @app.exception_handler(WebApiError)
    async def _on_web_error(request: Request, exc: WebApiError):
        logger.warning("web 上游错误 code={} status={} — {}", exc.code, exc.http_status, exc)
        return _ark_envelope(_web_http_for(exc), _web_code_for(exc), str(exc))

    # ---------------------------------------------------------------- routes --

    @app.get("/healthz")
    async def healthz(request: Request, deep: int = 0):
        s = request.app.state.settings
        pool: dict = request.app.state.upstreams
        default_kind = s.upstream if s.upstream in pool else (sorted(pool)[0] if pool else None)

        info: dict = {
            "ok": True,
            "service": s.service_name,
            "upstream": default_kind,
            "available_upstreams": sorted(pool),
            "switch_via": "X-Avm-Upstream: web|official  (或 ?upstream=)",
            "billing_notes": {k: billing_note(k) for k in sorted(pool)},
            "supports_cancel": {k: bool(v.supports_cancel) for k, v in pool.items()},
            "base_url": s.base_url,
            "gate": "required" if s.gate_key else "open",
            "passthrough_key": s.passthrough_key,
            "default_model": s.default_model or None,
            "max_credits": s.max_credits,
            "tasks_tracked": request.app.state.tasks.count(),
            # 一眼看出任务表是否真的在持久化（kind=sqlite 才跨重启可读）
            "task_store": request.app.state.tasks.describe(),
            "logfire": logfire_ok,
            # 上报口径一眼可见：脱敏关着时请求/响应是原文，抓头开着则有凭证
            "logfire_scrubbing": bool(s.logfire_scrubbing),
            "logfire_capture_headers": bool(s.logfire_capture_headers),
        }
        if "official" in pool:
            info["supported_models"] = list(OFFICIAL_MODELS)
        default = pool.get(default_kind) if default_kind else None
        if default is not None and getattr(default, "queue", None) is not None:
            info["submit_queue"] = default.queue.stats()

        if deep:
            # 只有显式 deep=1 才打上游，避免存活探针把上游当依赖
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
        # 不该先拿到"缺支出上限"这种更像配置问题的错误。
        upstream = _upstream_for(request)
        settings_ = request.app.state.settings

        plan = translate_create(body, settings_.translate_env())
        # 计费口径按上游渲染：两条线的免费规则不同，不能混
        eff, warns = billing_view(plan, upstream.kind)
        plan = {**plan, "effective": eff, "warnings": warns}

        if _is_dry_run(request, body):
            with span(
                "ark.create.dry_run",
                upstream=upstream.kind,
                ark_model=plan["requested"]["model"],
                official_model=plan["official_model"],
                resolution=eff["resolution"],
                duration=eff["duration"],
                billed=eff["billed"],
                max_credits=plan["max_credits"],
                warning_count=len(warns),
                # 请求原文：dry-run 是零成本校验路径，出问题的多数是"我们理解错了参数"
                request=body,
            ):
                logger.info(
                    "dry-run upstream={} ark_model={} res={} dur={}s billed={} cap={}",
                    upstream.kind,
                    plan["requested"]["model"],
                    eff["resolution"],
                    eff["duration"],
                    eff["billed"],
                    plan["max_credits"],
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
            official_model=plan["official_model"],
            resolution=eff["resolution"],
            duration=eff["duration"],
            billed=eff["billed"],
            max_credits=plan["max_credits"],
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
            "model": plan["requested"]["model"],
            "requested": plan["requested"],
            "effective": eff,
            "warnings": warns,
            "unsupported": plan["unsupported"],
            "createdAtMs": int(time.time() * 1000),
        }
        request.app.state.tasks.put(entry)
        logger.info(
            "已提交 ark_id={} upstream={} upstream_task={} billed={} cap={}",
            entry["id"],
            upstream.kind,
            task_id,
            eff["billed"],
            plan["max_credits"],
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
        # 分页下推到存储层：sqlite 后端只取当前页，不必把整表读进内存
        window = store.list_recent(page_size, (page_num - 1) * page_size)
        items = [await _task_view(request, e) for e in window]
        # 说明：本服务只知道自己创建过的任务；上游的列表按 key/账号维度，未在此合并。
        return {
            "items": items,
            "total": store.count(),
            "page_num": page_num,
            "page_size": page_size,
        }

    @app.get(TASKS_PATH + "/{task_id}")
    async def get_task(task_id: str, request: Request, _: str = Depends(require_bearer)):
        entry = request.app.state.tasks.get(task_id)
        if not entry:
            raise ArkError(404, "TaskNotFound", f"task {task_id} not found")
        return await _task_view(request, entry)

    @app.delete(TASKS_PATH + "/{task_id}")
    async def delete_task(task_id: str, request: Request, _: str = Depends(require_bearer)):
        entry = request.app.state.tasks.delete(task_id)
        if not entry:
            raise ArkError(404, "TaskNotFound", f"task {task_id} not found")
        # 用创建时的上游 —— 任务在哪条线上，就该在哪条线上取消
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
            except (OfficialApiError, WebApiError) as e:
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
                    except (OfficialApiError, WebApiError) as e:
                        sp.set_attribute("error", describe_error(e))
                        set_upstream_calls(sp, calls)
                        raise
                    # 终态与**实际计费结果**必须进 trace —— 出片后对账就靠这两项
                    sp.set_attribute("status", (view or {}).get("status") or "")
                    sp.set_attribute("paid", bool(((view or {}).get("usage") or {}).get("paid")))
                    # 归一化后的任务对象（出片地址、用量、resolution 回填都在这里）
                    sp.set_attribute("upstream_response", view or {})
                    set_upstream_calls(sp, calls)
            except (OfficialApiError, WebApiError) as e:
                logger.warning("查任务失败 upstream_task={} — {}", entry["taskId"], e)
                view = {}
        view = dict(view or {})
        view["id"] = entry["id"]
        view["model"] = entry["model"]
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
