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

# ---- 不进上报的路径：探活 + 公开文档端点 ------------------------------------
#
# 这一串路径的共同点是**被人反复轮询或扫描**：容器 HEALTHCHECK 每 30s 打一次
# `/healthz`；交互式文档（`/docs` `/redoc` `/openapi.json`）不鉴权、是扫描器的常规
# 目标。一次请求在 Logfire 上就是一条 span **加**一条日志 —— 一天上千条，真实请求被
# 淹掉，还白跑出站流量。所以两样都摘：**span** 在 `instrument_fastapi(excluded_urls=…)`
# 里摘，**日志**在 logfire 那个 sink 的 filter 里摘。
# 摘掉的只是"上报"：本地 stderr 照打 —— 探活真坏掉时，本机仍然看得见。
#
# 施工面：`AVM_LOGFIRE_EXCLUDED_PATHS`（路径表，逗号分隔；留空 = 用下表；`-` = 不排除
# 任何路径）。**使用方只写路径，永远不写正则** —— 正则由 `_regex_for()` 机械生成。
DEFAULT_PROBE_PATHS = ("/healthz", "/", "/docs", "/redoc", "/openapi.json")

# 生效表：由 `setup_observability()` 按 settings 解析后写入。做成模块级而不是逐请求
# 读 settings，是为了**保证 span 排除与日志过滤同源**（两者都只读这一个变量）。
_PROBE_PATHS: tuple = DEFAULT_PROBE_PATHS


def probe_paths() -> tuple:
    """当前生效的排除表（排障用；刻意不进 `/healthz` 响应 —— 那是个不鉴权端点）。"""
    return _PROBE_PATHS


def set_probe_paths(paths=None) -> tuple:
    """写入生效表（去重、保序）。`None` = 回落内置默认表。返回生效表。"""
    global _PROBE_PATHS
    _PROBE_PATHS = tuple(dict.fromkeys(DEFAULT_PROBE_PATHS if paths is None else paths))
    return _PROBE_PATHS


def _path_matches(path: str, pattern: str) -> bool:
    """一条路径的匹配规则 —— **语义的唯一定义处**（谓词与正则都从这里来）。

    · 普通路径（`/healthz`）：**精确**相等
    · 以 `/` 结尾（`/internal/`）：**子树**（前缀）
    · `/` 本身：只匹配根路径 —— 若按"子树"解释它就等于**整个站**，那是另一件事
      （要全局关掉上报请用 `AVM_DISABLE_LOGFIRE`，而不是在这里写 `/`）
    """
    if pattern.endswith("/") and len(pattern) > 1:
        return path.startswith(pattern)
    return path == pattern


def is_probe_path(path: str) -> bool:
    """这个请求路径要不要**排除在上报之外**。span 侧与日志侧都只调它。"""
    return any(_path_matches(path, p) for p in _PROBE_PATHS)


def _regex_for(pattern: str) -> str:
    """把一条路径翻成 otel `excluded_urls` 用的正则（与 `_path_matches` 同构）。

    🔴 坑：上游拿 `excluded_urls` 做 `re.search`（**子串**匹配），不是路径相等 ——
    裸写 `"/"` 会命中**每一个** URL（任何 URL 都含 `/`）；未锚定的 `"/healthz"` 会
    连带吃掉 `/healthz/extra`。所以这里**必须**全锚定。实测：朴素写法 `/healthz,/`
    会把 `/api/v3/...` 与 `/openapi.json` 一并判为排除（全站追踪静默关掉）。

    ⚠️ 被匹配的串是 `scheme://host + scope["path"]`（**不含 query**，见
    `opentelemetry.instrumentation.asgi.get_host_port_url_tuple`）⇒ 探活常带的
    `/healthz?deep=1` 与 `/healthz` 命中同一条。
    """
    host = r"^https?://[^/]+"
    if pattern.endswith("/") and len(pattern) > 1:
        return host + re.escape(pattern)              # 子树
    return host + re.escape(pattern) + "$"            # 精确（含根路径）


def probe_excluded_urls() -> str:
    """生效表 → otel 的 `excluded_urls`（逗号分隔的正则串）。"""
    return ",".join(_regex_for(p) for p in _PROBE_PATHS)


def reject_reason(pattern: str) -> str | None:
    """`AVM_LOGFIRE_EXCLUDED_PATHS` 条目不合规时给出理由（None = 合规）。

    不合规**不静默**：调用方（`_apply_excluded_paths`）会把它剔出生效表并告警 ——
    "配了不生效还不说话"正是本项目最忌讳的形状。
    """
    if not pattern.startswith("/"):
        return "必须以 / 开头（这里写路径，不是正则、也不是域名）"
    if "?" in pattern or "#" in pattern:
        return "不要带 query/fragment（判定用的是路径，不含 query）"
    return None


def keep_off_logfire(record) -> bool:
    """logfire 那个 sink 的 filter：探活请求的日志不上报（本地 sink 不受影响）。

    `avm_probe` 由 app 的请求中间件按 `is_probe_path()` 绑定；请求之外的日志没有
    这个键，照常上报。
    """
    return not record["extra"].get("avm_probe")


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


def _apply_excluded_paths(settings) -> tuple:
    """把 `settings.logfire_excluded_paths` 变成生效表，并**如实报告**被拒的条目。

    被拒的条目**不进生效表**且**告警** —— 静默忽略等于"配了不生效"，那正是要消灭的
    形状。`None`（未设/留空）⇒ 内置默认表；`()`（写了 `-`）⇒ 一条都不排除。
    """
    configured = getattr(settings, "logfire_excluded_paths", None)
    if configured is None:
        effective = set_probe_paths(None)
    else:
        kept, rejected = [], []
        for raw in configured:
            why = reject_reason(raw)
            if why:
                rejected.append(f"{raw}（{why}）")
            else:
                kept.append(raw)
        effective = set_probe_paths(kept)
        if rejected:
            logger.warning(
                "AVM_LOGFIRE_EXCLUDED_PATHS 里这些条目被忽略（只写路径，别写正则）：{}",
                "；".join(rejected),
            )
    logger.info("Logfire 上报排除路径：{}", "、".join(effective) or "（无）")
    return effective


def setup_observability(settings) -> bool:
    """装配日志与追踪。幂等：重复调用只重装 loguru sink。返回 logfire 是否可用。"""
    global _LOGFIRE_READY, _MAX_ATTR_CHARS, _CAPTURE_HEADERS

    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(sys.stderr, level=settings.log_level, format=LOG_FORMAT, colorize=True)

    _MAX_ATTR_CHARS = int(getattr(settings, "logfire_max_chars", 0) or DEFAULT_MAX_ATTR_CHARS)
    # 排除表与"logfire 装不装"无关（日志侧也要用它），所以放在下面那条早退之前
    _apply_excluded_paths(settings)
    # 轮询表同理：**两张表在装配期各解析一次**，span 侧与日志侧都只读生效表
    _apply_poll_paths(settings)

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
            # 探活请求的日志不上报（`keep_off_logfire` 按中间件绑的 `avm_probe` 判）
            logger.add(
                logfire.loguru_handler(),
                level=settings.log_level,
                filter=keep_off_logfire,
            )
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


def fastapi_excluded_urls() -> str:
    """`instrument_fastapi` 用的排除正则：**探活表 + 轮询表**，两张表各出一份。

    ⚠️ 这张表是**入站 span 唯一的杠杆**：实测（2026-09-15）`suppress_instrumentation()`
    在 ASGI 中间件上无效 —— 中间件在 handler 之外就把 span 建好了，handler 里再包一层
    也追不回。所以"轮询不产生 span"只能靠这里。

    留空（用户把两张表都写成 `-`）时返回空串 —— 那正是"不排除任何路径"。
    """
    return ",".join(x for x in (probe_excluded_urls(), poll_excluded_urls()) if x)


def instrument_fastapi(app) -> None:
    """给 FastAPI 挂自动 span。抓头显式关掉（默认也是关，这里把意图写死）。

    探活路径（`/healthz` 等）与**轮询路径**（任务查询）排除在外：两者都被反复请求，
    每次成一条 span 就是纯噪声。两件事分别由 `probe_excluded_urls()` /
    `poll_excluded_urls()` 出，**同源**于 `is_probe_path` / `is_poll_path`。
    """
    import logfire

    logfire.instrument_fastapi(
        app,
        capture_headers=_CAPTURE_HEADERS,
        excluded_urls=fastapi_excluded_urls(),
    )


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


def count_poll(*, upstream: str, status: str, reported: bool, cached: bool = False,
               source: str = "poll") -> None:
    """记一次任务查询。**不产生 span** —— 这就是闸门 1（产生层）的落点。

    轮询本身是有价值的信息（"调用方多密"、"各状态各占多少"），只是**不该一条一条存
    span**：同一个任务几十上百条几乎逐字段相同的 span，读的时候要靠肉眼去重、存的时候
    按条计价。做成计数器即"量在、明细不要"。

    🔴 标签**只能放低基数字段**：`upstream` / `status` / `reported` / `cached` / `source`
    都是有限的枚举；**绝不放 `ark_id` / `task_id`** —— 那会把时间序列打成一任务一条，
    比 span 还贵。要按任务追明细，去 trace 里查（跃迁那几条一定在）。
    """
    if not _LOGFIRE_READY:
        return
    _metric(
        "counter",
        "avm.task.poll_requests",
        unit="1",
        description="任务查询请求数（含不产生 span 的重复轮询；reported=true 才是留了 span 的那些）",
    ).add(
        1,
        attributes={
            "upstream": upstream,
            "status": status or "unknown",
            "reported": bool(reported),
            "cached": bool(cached),
            "source": source,
        },
    )


def record_wait(*, upstream: str, final_status: str, seconds: float, source: str = "poll") -> None:
    """记一个任务「从提交到终态」的等待时长（只在确实拿到终态时记）。

    `source` 区分口径：`watcher` = 后台盯梢线程测到的（含排队等待），
    `poll` = 调用方轮询时观测到的。两者是同一段时间的两次独立测量，混起来会互相干扰。
    """
    if not _LOGFIRE_READY:
        return
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    if value < 0:
        return
    _metric(
        "histogram",
        "avm.task.wait_seconds",
        unit="s",
        description="任务从提交到进入终态的等待时长",
    ).record(value, attributes={"upstream": upstream, "final_status": final_status, "source": source})


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
    """测试用：丢掉**全部**已缓存的指标对象（换了 provider / reader 之后必须重建）。

    名字沿用历史（它起初只管 gauge）；现在 gauge / counter / histogram 共用一份缓存。
    """
    _METRICS.clear()


# ---- 轮询路径：不算进 trace 的「高频无害流量」 -------------------------------
#
# 🔴 **闸门 1（产生层）**：本轮改动的唯一定性依据。
#
# 为什么需要第二张表，而不是往探活表里塞一行：**语义不同，判定也不同**。
#   · 探活路径（`/healthz` 等）：**根本不是业务**，任何状态都不该上报；
#   · 轮询路径（任务查询）：**是业务，但被重复调用**。同一个任务被调用方按秒级轮询
#     到终态，几百次请求里绝大多数 span 与上一条**逐字段相同**（同 task_id、同 status）
#     —— 那不是可观测性，是重复计价。Logfire 官方文档明确把 "High-frequency polling"
#     列为 `suppress_instrumentation()` 的用例。
#
# 排除的粒度**只能是 URL** —— 这是实测定下来的，不是偏好：
#   `suppress_instrumentation()` 的压制作用在 **span 处理器层**
#   （`logfire._internal.exporters.processor_wrapper.SuppressInstrumentationProcessorWrapper`），
#   而入站 span 由 ASGI 中间件在 **handler 之外**创建 ⇒ 在 handler 里包 suppress 追不回来。
#   所以排的是整条任务查询路径。代价是「首次查询」的 HTTP 层 span 也没了，但那次由我们
#   自己的 `ark.task.fetch` span 覆盖（含失败路径）——证据不丢，只是换了通道。
#
# 🔴 **轮询的失败必须留痕**（span 侧按 URL 排不出去，粒度做不到，所以分两处补）：
#   · span：失败路径照发 `ark.task.fetch`（见 `app._task_view`）；
#   · 日志：请求中间件对轮询路径**只在 4xx/5xx 时**才发那条 access log。
DEFAULT_POLL_PATHS = (
    "/api/v3/contents/generations/tasks/",   # 方舟线：查询任务
    "/v1/videos/",                          # OpenAI 兼容线：查询任务
)

_POLL_PATHS: tuple = DEFAULT_POLL_PATHS


def poll_paths() -> tuple:
    """当前生效的轮询排除表（排障用）。"""
    return _POLL_PATHS


def set_poll_paths(paths=None) -> tuple:
    """写入生效表（去重、保序）。`None` = 回落内置默认表。返回生效表。"""
    global _POLL_PATHS
    _POLL_PATHS = tuple(dict.fromkeys(DEFAULT_POLL_PATHS if paths is None else paths))
    return _POLL_PATHS


def is_poll_path(path: str) -> bool:
    """这条路径是不是「被轮询的业务端点」。**span 侧与日志侧都只调它**。

    与 `is_probe_path` 共用 `_path_matches` —— 匹配语义只有一处定义，两张表不会漂。
    """
    return any(_path_matches(path, p) for p in _POLL_PATHS)


def poll_excluded_urls() -> str:
    """生效表 → otel `excluded_urls`（与探活表**同一套正则生成器**）。"""
    return ",".join(_regex_for(p) for p in _POLL_PATHS)


def _apply_poll_paths(settings) -> tuple:
    """与 `_apply_excluded_paths` 同构：不合规条目**不进生效表**且**告警**。"""
    configured = getattr(settings, "logfire_poll_paths", None)
    if configured is None:
        effective = set_poll_paths(None)
    else:
        kept, rejected = [], []
        for raw in configured:
            why = reject_reason(raw)
            if why:
                rejected.append(f"{raw}（{why}）")
            else:
                kept.append(raw)
        effective = set_poll_paths(kept)
        if rejected:
            logger.warning(
                "AVM_LOGFIRE_POLL_PATHS 里这些条目被忽略（只写路径，别写正则）：{}",
                "；".join(rejected),
            )
    logger.info("Logfire 轮询排除路径：{}", "、".join(effective) or "（无）")
    return effective


# ---- 压制出站调用（片段里的 `with suppress_instrumentation():` 落点） ---------


@contextmanager
def suppress_http():
    """包住一次**轮询用的出站调用**：它产生的 span 不进 trace（含 httpx 自动 span）。

    🔴 实测语义（2026-09-15；logfire 5.0.0 + httpx 0.27.2），四条都是跑出来的：
      1. 压制判定在 **span 处理器层**、按 contextvar 生效 ⇒ 上下文里的**一切** span 都被丢，
         **包括我们自己显式开的 `logfire.span()`**。所以绝不能把要留痕的 span 包在里面
         —— 正确顺序是「先开 span，再用它只包 GET」（与片段一致）。
      2. 出站 httpx 埋点打在 **`HTTPTransport.handle_request`** 上 ⇒ 用 `MockTransport`
         的测试**看不到** httpx span。这解释了为什么「httpx 自动子 span」从来没被测试
         钉住过（测试全绿 ≠ 它真的在）。
      3. 入站 ASGI span **压不掉**（中间件在 handler 之外建 span）⇒ 入站一侧只能靠
         `excluded_urls`（见 `poll_excluded_urls`）。
      4. contextvar 是**按上下文**的 ⇒ 后台线程里的压制不影响同进程的在飞请求，反之亦然。
    """
    if not _LOGFIRE_READY:
        yield
        return
    import logfire

    with logfire.suppress_instrumentation():
        yield
