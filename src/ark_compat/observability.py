"""可观测性装配：loguru（日志）+ logfire（追踪）。

设计原则：
  1. **默认不发数据。** `send_to_logfire="if-token-present"` —— 没配 `LOGFIRE_TOKEN`
     就只在本地收集，不会因为跑一个测试就往云端发 trace。
  2. **loguru 是唯一日志出口**，并通过 `logfire.loguru_handler()` 桥接进 Logfire，
     这样一条 `logger.info(...)` 同时出现在终端和 trace 时间线上。
  3. **装配失败不致命**：logfire 装不上就退回纯日志，服务照常提供。
  4. **请求 / 响应原文进 trace，凭证不进。** 上游的请求与响应**不做脱敏**
     （`scrubbing=False` 是默认；`AVM_LOGFIRE_SCRUBBING=1` 可重新打开）。
     脱敏一关，"凭证不入 trace"就只剩**一条**防线，而且必须是硬的：
     `capture_headers=False` 让 `key` / `cookie` / `Authorization` 根本不进 span。
     这两件事必须**成对出现**，所以有测试钉住「脱敏关着，也搜不到 key/cookie」。
     另有 `AVM_LOGFIRE_CAPTURE_HEADERS=1` 可显式打开抓头 —— 那时凭证会明文进
     trace，只在完全由自己掌控的 Logfire 项目里这么干。
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger

_LOGFIRE_READY = False
_CAPTURE_HEADERS = False
# 「SDK 装配成功」与「数据真的在往外发」是两个不同的信号，必须分开报（见 logfire_exporting）。
# 默认 False：没配 token / 关了 logfire 时，它就是 False —— 不猜"大概会发吧"。
_LOGFIRE_EXPORTING = False

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[request_id]}</cyan> | "
    "<level>{message}</level>"
)

# ---- 属性体积控制 ----------------------------------------------------------
#
# 不做脱敏，但**要做体积控制**：参考图可以是几 MB 的 data URI，原样挂上去会把
# trace 撑爆（而且真正有用的信息——"这是一张 png，1.8MB"——一句话就说清了）。
# 这不是脱敏，是照抄日志轮转的思路：留下判断所需的最小充分信息。
DEFAULT_MAX_ATTR_CHARS = 20_000
_MAX_ATTR_CHARS = DEFAULT_MAX_ATTR_CHARS
MAX_LIST_ITEMS = 50

_DATA_URI_RE = re.compile(r"^data:([^;,]{0,80})", re.I)

# 明文凭证只可能从这些请求头进来。**硬过滤，与脱敏开关无关。**
SECRET_HEADERS = frozenset(
    {
        "key",
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-adapter-key",
    }
)


def setup_observability(settings) -> bool:
    """装配日志与追踪。幂等：重复调用只重装 loguru sink。返回 logfire 是否可用。"""
    global _LOGFIRE_READY, _MAX_ATTR_CHARS, _CAPTURE_HEADERS

    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(sys.stderr, level=settings.log_level, format=LOG_FORMAT, colorize=True)

    _MAX_ATTR_CHARS = int(getattr(settings, "logfire_max_chars", 0) or DEFAULT_MAX_ATTR_CHARS)

    if not settings.enable_logfire:
        logger.info("logfire 已关闭（AVM_DISABLE_LOGFIRE=1）；仅本地日志")
        return False
    if _LOGFIRE_READY:
        return True

    try:
        import logfire

        logfire.configure(
            service_name=settings.service_name,
            environment=settings.environment or None,
            send_to_logfire=settings.logfire_send,
            console=settings.logfire_console,
            # 不把函数参数自动塞进 span —— 参数里可能有 key/token
            inspect_arguments=False,
            # 默认关：请求/响应要能直接读（打开后含 session/cookie 字样的值会被整条替换）
            scrubbing=bool(getattr(settings, "logfire_scrubbing", False)),
        )

        try:
            logger.add(logfire.loguru_handler(), level=settings.log_level)
        except Exception as e:  # 不同 logfire 版本的桥接 API 略有差异
            logger.debug(f"loguru→logfire 桥接不可用：{e}")

        capture_headers = bool(getattr(settings, "logfire_capture_headers", False))
        _CAPTURE_HEADERS = capture_headers
        if capture_headers:
            logger.warning(
                "AVM_LOGFIRE_CAPTURE_HEADERS=1：请求头（含 key / cookie）会明文进 trace"
            )
        try:
            # 上游调用自动成为子 span：排障时"网关慢还是上游慢"一眼可辨
            logfire.instrument_httpx(capture_headers=capture_headers)
        except Exception as e:
            logger.debug(f"instrument_httpx 不可用：{e}")

        global _LOGFIRE_EXPORTING
        _LOGFIRE_EXPORTING = will_export(settings)
        _LOGFIRE_READY = True
        logger.info(
            "logfire 已装配 | service={} send_to_logfire={} token={} 实际外发={} scrubbing={} capture_headers={}",
            settings.service_name,
            settings.logfire_send,
            "present" if _has_token() else "absent",
            "是" if _LOGFIRE_EXPORTING else "否（仅本地收集）",
            bool(getattr(settings, "logfire_scrubbing", False)),
            capture_headers,
        )
        return True
    except Exception as e:
        logger.warning(f"logfire 装配失败，降级为纯日志：{e}")
        return False


def _has_token() -> bool:
    import os

    return bool(os.environ.get("LOGFIRE_TOKEN", "").strip())


def instrument_fastapi(app) -> None:
    """给 FastAPI 挂自动 span。抓头显式关掉（默认也是关，这里把意图写死）。"""
    import logfire

    logfire.instrument_fastapi(app, capture_headers=_CAPTURE_HEADERS)


# ---- 值裁剪 ----------------------------------------------------------------


def clip(value: Any, *, limit: int | None = None) -> Any:
    """把要上报的值裁到"可读且不失控"：data URI 只留摘要，超长串截断，长列表裁短。

    容器原样返回（让 logfire 自己序列化成 JSON + `logfire.json_schema`，
    UI 里可展开），只把**内部的**超长字符串换掉 —— 这样结构还在，体积可控。
    """
    lim = _MAX_ATTR_CHARS if limit is None else limit
    if isinstance(value, str):
        m = _DATA_URI_RE.match(value)
        if m:
            return f"<data-uri {m.group(1)} · {len(value)} chars>"
        if len(value) > lim:
            return value[:lim] + f"…<truncated: kept {lim} of {len(value)} chars>"
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {str(k): clip(v, limit=lim) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out = [clip(v, limit=lim) for v in list(value)[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            out.append(f"…<{len(value) - MAX_LIST_ITEMS} more items>")
        return out
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clip(repr(value), limit=lim)


def safe_headers(headers) -> dict:
    """只保留非凭证请求头。**硬过滤，不看脱敏开关** —— 关了脱敏就靠它兜底。"""
    return {
        str(k): str(v)
        for k, v in dict(headers or {}).items()
        if str(k).strip().lower() not in SECRET_HEADERS
    }


def describe_error(exc: BaseException) -> str:
    """统一的错误摘要，**必须带错误码**。

    异常类型把码放在属性上（`WebApiError.code`），而 `str(exc)` 只有散文 ——
    只看散文的话，trace 里没法按码过滤，也分不清"上游明确拒绝"和"我们自己拦下来的"。
    """
    code = getattr(exc, "code", None)
    status = getattr(exc, "http_status", None)
    head = type(exc).__name__
    if code:
        head += f"[{code}]"
    if status:
        head += f" http={status}"
    return f"{head}: {exc}"


# ---- 上游请求 / 响应采集 ---------------------------------------------------
#
# 采集点在客户端内部（`WebClient.trpc`），span 在 app 层。
# 用 contextvar 把两者接起来，客户端因此**不需要**多一个 `sink` 参数：
# `asyncio.to_thread` 会复制上下文，所以"app 里开、线程里的客户端写"是通的。
_EXCHANGES: ContextVar[list | None] = ContextVar("avm_upstream_exchanges", default=None)


@contextmanager
def upstream_exchanges():
    """收集本次调用期间上游的请求 / 响应原文；退出时清掉，绝不留到下一个请求。"""
    box: list = []
    token = _EXCHANGES.set(box)
    try:
        yield box
    finally:
        _EXCHANGES.reset(token)


def note_upstream(
    call: str,
    *,
    upstream: str = "",
    request: Any = None,
    response: Any = None,
    task_id: str | None = None,
    status: str = "ok",
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """记录一次上游调用。没有活跃采集箱时**立即返回**（零开销，也不序列化）。"""
    box = _EXCHANGES.get()
    if box is None:
        return
    rec: dict[str, Any] = {"call": call, "status": status, "request": clip(request)}
    if upstream:
        rec["upstream"] = upstream
    if response is not None:
        rec["response"] = clip(response)
    if task_id:
        rec["task_id"] = task_id
    if error:
        rec["error"] = clip(error)
    if duration_ms is not None:
        rec["duration_ms"] = round(float(duration_ms), 1)
    box.append(rec)


def set_upstream_calls(sp, box) -> None:
    """把采集到的上游调用挂到 span 上。空箱不写属性 —— 别让每条 trace 拖一个空数组。"""
    if not box:
        return
    sp.set_attribute("upstream_call_count", len(box))
    sp.set_attribute("upstream_calls", clip(box))


# ---- span ------------------------------------------------------------------


class _NullSpan:
    """logfire 不可用时的占位 span，保证业务代码无需分支。"""

    def set_attribute(self, *args, **kwargs) -> None:
        return None

    def record_exception(self, *args, **kwargs) -> None:
        return None

    def add_event(self, *args, **kwargs) -> None:
        return None

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@contextmanager
def span(name: str, **attrs: Any):
    """统一的 span 入口：logfire 可用就走 logfire，否则 no-op。

    属性值一律过 `clip`：调用方不必自己记得裁 data URI。
    """
    if _LOGFIRE_READY:
        import logfire

        with logfire.span(name, **{k: clip(v) for k, v in attrs.items()}) as s:
            yield s
    else:
        yield _NullSpan()


def logfire_ready() -> bool:
    return _LOGFIRE_READY


def will_export(settings) -> bool:
    """按**配置**判断数据是否真的会外发 —— 与"SDK 装上了"是两件事。

    `send_to_logfire="if-token-present"`（默认）在没有 `LOGFIRE_TOKEN` 时**不会**外发，
    但 `logfire.configure()` 照样成功、span 照样生成。只报"装配成功"会让
    `/healthz` 给出 `logfire: true` 而实际上一条 trace 都没出去（2026-09-14 实测踩到）。
    """
    if not getattr(settings, "enable_logfire", True):
        return False
    send = getattr(settings, "logfire_send", "if-token-present")
    if send is True:
        return True
    if send is False:
        return False
    return _has_token()      # "if-token-present"：没有 token 就只在本地收集


def logfire_exporting() -> bool:
    """数据**真的**在往外发吗？（`logfire_ready()` 只说明 SDK 装配成功）"""
    return _LOGFIRE_EXPORTING


# ------------------------------------------------------- 号池指标（余额 / 闸门）----
#
# 为什么是**指标**而不是 span 属性：余额与闸门状态要看的是**时间序列**（"什么时候
# 掉到 0"、"闸门开了多久才衰减"），而 span 属性只能在 trace 里逐条翻。
#
# 为什么走独立采样循环而不是挂在请求路径上：给每个请求加一次上游只读调用，
# 会把请求延迟和"我们向上游发请求的频次"一起抬上去 —— 而频次恰恰是站点风控
# （Turnstile 闸门）的触发维度之一。宁可每 N 分钟统一采一次。

_GAUGES: dict = {}


def _gauge(name: str, *, unit: str, description: str):
    """懒建并缓存指标对象。

    每次 `logfire.metric_gauge()` 都新建会让 SDK 侧重复注册同名仪表；而 logfire
    没装配时提前建也不合适（对象会绑在当时的全局 provider 上）。
    """
    g = _GAUGES.get(name)
    if g is None:
        import logfire

        g = logfire.metric_gauge(name, unit=unit, description=description)
        _GAUGES[name] = g
    return g


def record_account(
    *,
    upstream: str,
    account: str,
    credits: int | None = None,
    captcha_required: bool | None = None,
    reachable: bool | None = None,
    source: str = "",
    plan_id: str | None = None,
    plan_price_cents: int | None = None,
    sub_status: str | None = None,
    concurrency_limit: int | None = None,
    days_to_renewal: float | None = None,
    identity: str | None = None,
) -> None:
    """把一个账号的状态上报为 Logfire 指标（号池监控）。

    🔴 `account` **必须是凭据指纹**（`sha256(凭据)[:16]`），**绝不能是凭据原文** ——
    trace / 指标同样是外发数据，把 cookie 或 API Key 写进去等于把凭据交出去。
    这条由 `tests/test_pool_metrics.py` 专门把关（含变异测试）。

    三项互相独立，缺哪项就不报哪项：
      - `credits`          剩余积分（站点两线的积分是**同一池**，所以两线数值一致）
      - `captcha_required` Turnstile 动态闸门是否开启（web 线才有；只读探测）
      - `reachable`        凭据本身是否可用（会话/Key 有效）
    """
    if not _LOGFIRE_READY:
        return
    # `pid` 用来分辨是哪个进程报的：gunicorn 多 worker 时每个 worker 都会报一份
    # （采样器是进程内的），看板按 pid 过滤即可，不必因此关掉上报。
    attrs = {"upstream": upstream, "account": account, "source": source, "pid": os.getpid()}
    # 订阅类字段走**标签**（低基数、便于按套餐分组），只有"还剩几天扣费"做成时间序列 ——
    # 它对号池是可排期事件（到期/停订阅会让整条线不可用）。
    # ⚠️ `identity`（邮箱）默认**不上报**（PII）：需要时由
    # `AVM_ACCOUNT_REPORT_IDENTITY=1` 显式打开。
    for key, val in (
        ("plan_id", plan_id),
        ("plan_price_cents", plan_price_cents),
        ("sub_status", sub_status),
        ("concurrency_limit", concurrency_limit),
        ("identity", identity),
    ):
        if val is not None and val != "":
            attrs[key] = val
    if days_to_renewal is not None:
        _gauge(
            "avm.account.days_to_renewal",
            unit="days",
            description="距下次扣费还有几天（号池可排期事件）",
        ).set(days_to_renewal, attributes=attrs)
    if credits is not None:
        _gauge(
            "avm.account.credits", unit="credits", description="账号剩余积分（号池监控）"
        ).set(credits, attributes=attrs)
    if captcha_required is not None:
        _gauge(
            "avm.account.captcha_required",
            unit="1",
            description="该账号当前是否需要 Turnstile 验证码（1=需要）",
        ).set(int(bool(captcha_required)), attributes=attrs)
    if reachable is not None:
        _gauge(
            "avm.account.reachable",
            unit="1",
            description="凭据是否可用（1=可用；0=会话过期/Key 失效）",
        ).set(int(bool(reachable)), attributes=attrs)


def reset_gauge_cache() -> None:
    """测试用：丢掉已缓存的指标对象（换了 provider / reader 之后必须重建）。"""
    _GAUGES.clear()
