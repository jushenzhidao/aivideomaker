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

import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger

_LOGFIRE_READY = False
_CAPTURE_HEADERS = False

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

        _LOGFIRE_READY = True
        logger.info(
            "logfire 已装配 | service={} send_to_logfire={} token={} scrubbing={} capture_headers={}",
            settings.service_name,
            settings.logfire_send,
            "present" if _has_token() else "absent",
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

    两个异常类型都把码放在属性上（`WebApiError.code` / `OfficialApiError.code`），
    而 `str(exc)` 只有散文 —— 只看散文的话，trace 里没法按码过滤，也分不清
    "上游明确拒绝"和"我们自己拦下来的"。
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
# 采集点在客户端内部（`OfficialClient._req` / `WebClient.trpc`），span 在 app 层。
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
