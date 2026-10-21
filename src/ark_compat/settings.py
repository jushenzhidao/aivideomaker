"""运行配置（全部来自环境变量，无配置文件）。

服务名（`service_name` / `service_title`）的**唯一来源**是 `ark_compat/__init__.py`
里的常量 —— 确定名字后只改那里一处即可，其余全部引用它。

本项目只对接**一条上游**：网页端内部接口（tRPC over `/api`，会话 cookie）。
它没有取消端点，但有一个免费窗口 —— `tier=turbo` 且 `duration ≤ 10s` 不计费。

**凭据与鉴权：一个变量（`AVM_AUTH`）三选一**

=================  =========================================================
留空 / `open`      不校验（本机自用）；上游凭据取本进程的 `AVM_COOKIE`
`passthrough`      调用方按请求自带凭据：`Authorization: Bearer` **就是上游凭据**
                   （网页会话 cookie，裸 token 或完整 Cookie 串），本进程不需要 `AVM_COOKIE`
`key:<密钥>`       闸门：`Authorization: Bearer` 必须等于 `<密钥>`；上游凭据仍取 `AVM_COOKIE`
=================  =========================================================

为什么收成一个变量：闸门与透传**互斥** —— 单一 Bearer 不可能既当闸门密钥又当上游凭据，
同设的后果是每个请求都 401。那是个**必然失败**的组合，以前只能靠 `validate()` 拦；
现在这种组合**根本无法表达**（见 `parse_auth`）。

旧变量 `AVM_GATE_KEY` / `AVM_PASSTHROUGH_COOKIE` **已废弃**：只要它们还"有行为"
（闸门非空 / 透传为真），`from_env()` 就拒绝并打印迁移映射 —— **不静默放过**：
忽略一个有行为的旧变量，会让闸门无声变开放、或透传无声失效，两者都是
本项目最忌讳的「配了不生效」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from . import SERVICE_NAME, SERVICE_TITLE
from .cookie import normalize_cookie_header
from .store import DEFAULT_DB_PATH, DEFAULT_RETENTION_DAYS

# 站点根地址（`AVM_BASE_URL` 可覆盖）。网页端接口都在这个域下。
DEFAULT_BASE_URL = "https://aivideomaker.ai"

TASK_STORES = ("sqlite", "memory")


def _env_flag(env: Mapping[str, str], key: str) -> bool:
    return str(env.get(key, "")).strip() in ("1", "true", "True", "yes")


def _parse_send(raw) -> bool | str:
    """AVM_LOGFIRE_SEND：false/0 关闭，true/1 强制发送，其余走 if-token-present。"""
    v = str(raw).strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return "if-token-present"


def _parse_excluded_paths(raw):
    """AVM_LOGFIRE_EXCLUDED_PATHS：逗号分隔的**路径**表（**不是正则**，见 observability）。

    · 留空/未设 ⇒ `None`（= 用内置默认表）——与模板里 `VAR=` 等于"没设"的全局口径一致
    · `-` / `none` / `off` ⇒ `()`（**显式**不排除任何路径）
    · 其余 ⇒ 按逗号切开、去空白后的元组；**合法性不在这里判**（`observability.reject_reason`
      负责，且会把不合规的条目**告警**剔除，不静默放过）
    """
    v = str(raw or "").strip()
    if not v:
        return None
    if v.lower() in ("-", "none", "off"):
        return ()
    return tuple(p.strip() for p in v.split(",") if p.strip())


def _normalize_media_path(raw) -> str:
    """`AVM_PUBLIC_PATH` → `/xxx`。留空/只写斜杠 ⇒ 默认 `/v`（**不是** `/`）。

    ⚠️ 写成 `/` 会让下载路由变成 `/{name}` —— 一条**通配**路径，把每一条未知路径都
    吃掉，既遮蔽了正常的 404 语义，也等于给调用方多开一个入口。所以这里刻意不
    允许退化成根。
    """
    value = str(raw or "").strip().strip("/")
    return f"/{value}" if value else "/v"


# ---------------------------------------------------------------- 鉴权（一个变量）--

AUTH_OPEN = "open"                  # 不校验（本机自用）
AUTH_PASSTHROUGH = "passthrough"    # 调用方的 Bearer 就是上游凭据
AUTH_GATE = "gate"                  # 调用方的 Bearer 必须等于闸门密钥
AUTH_FORMS = "留空（或 open）/ passthrough / key:<密钥>"

# 已废弃的旧变量：**只在启动期用来拒绝**，不再参与任何行为判定。
# 刻意不登记进 .env.example —— 登记会让人以为还能用（见 test_env_template 的 NOT_IN_TEMPLATE）。
LEGACY_AUTH_VARS = ("AVM_GATE_KEY", "AVM_PASSTHROUGH_COOKIE")
# 显式的"关"值：只有这些（与空串）算"没有行为"。其余任何非空值都按"意图开启"处理 ——
# 旧代码的 flag 白名单只认 1/true/yes，写 `on` 的人以为自己开了透传而实际是关的，
# 迁移期不能沿用那个口径（见 legacy_auth_usage 的说明）。
_LEGACY_OFF_VALUES = frozenset({"0", "false", "no", "off"})
_LEGACY_MIGRATION = {
    "AVM_GATE_KEY": "AVM_AUTH=key:<原 AVM_GATE_KEY 的值>",
    "AVM_PASSTHROUGH_COOKIE": "AVM_AUTH=passthrough",
}


def parse_auth(raw) -> tuple:
    """`AVM_AUTH` → `(模式, 闸门密钥, 问题)`；问题非空 ⇒ 取值不合法。

    刻意**不猜意图**：裸密钥（`AVM_AUTH=sk-xxx`）不当作闸门密钥。猜错的代价不对称 ——
    把"我想开闸门"猜成别的模式，结果就是闸门静默消失。`key:` 前缀因此是必需的，
    顺带让密钥本身叫 `passthrough` 也不产生歧义。
    """
    value = str(raw or "").strip()
    low = value.lower()
    if low in ("", AUTH_OPEN, "none"):
        return AUTH_OPEN, "", ""
    if low == AUTH_PASSTHROUGH:
        return AUTH_PASSTHROUGH, "", ""
    if low.startswith("key:"):
        secret = value.split(":", 1)[1].strip()
        if secret:
            return AUTH_GATE, secret, ""
        return AUTH_OPEN, "", (
            "AVM_AUTH=key: 的密钥是空的 ⇒ 等于没有闸门。拒绝启动（不猜：空密钥一律不算闸门）。"
            f"可选写法：{AUTH_FORMS}。"
        )
    return AUTH_OPEN, "", (
        f"AVM_AUTH={value!r} 不是合法取值。可选写法：{AUTH_FORMS}。"
        "裸密钥不算 —— 闸门必须显式写成 `key:<密钥>`，否则无法与模式名区分。"
    )


def legacy_auth_usage(env: Mapping[str, str]) -> tuple:
    """`(还有行为的旧变量, 仅显式写了"关"值的旧变量)` —— 迁移期用。

    "有行为" = 闸门**非空** / 透传**不是显式的关值**。

    为什么透传侧按"非关即开"判定、而不是按旧代码那套 flag 白名单：旧代码的白名单只认
    `1/true/yes`，写 `AVM_PASSTHROUGH_COOKIE=on` 的人**以为自己开了透传，实际是关的**
    （这次审计顺带发现的旧口径不一致）。迁移期不能再沿用那个口径 —— 那种取值的**意图**
    明确是"开"，静默按关处理正是本项目最忌讳的「配了不生效」⇒ 一律拒绝启动，
    让它按迁移映射改成 `AVM_AUTH=passthrough`。

    只写了显式关值（`0/false/no/off`）或空串的旧部署语义与 `AVM_AUTH` 留空完全一致
    ⇒ 不拦，只在启动日志里提示删除。⚠️ 空串按本项目既有口径 = **没设**
    （模板里 `VAR=` 就是"没设"），故 `AVM_GATE_KEY=` 不做任何提示。
    """
    effective, deprecated = [], []
    for name in LEGACY_AUTH_VARS:
        raw = str(env.get(name, "") or "")
        if not raw.strip():
            continue
        has_effect = bool(raw.strip()) if name == "AVM_GATE_KEY" else (
            raw.strip().lower() not in _LEGACY_OFF_VALUES
        )
        (effective if has_effect else deprecated).append(name)
    return tuple(effective), tuple(deprecated)


def legacy_auth_error(names: tuple) -> str:
    """旧变量迁移指引（拒绝启动时打印）。**必须给出精确映射**，否则运维只能猜。"""
    lines = [f"{'、'.join(names)} 已废弃 —— 鉴权现在是**一个变量** `AVM_AUTH`，"
             f"取值只能是：{AUTH_FORMS}。"]
    lines += [f"  你设的 {n} ⇒ 改成 {_LEGACY_MIGRATION[n]}" for n in names]
    lines += [
        "  改完请把旧变量从 .env / compose override 里删掉（否则仍会拒绝启动）。",
        "  为什么不做兼容：忽略一个有行为的旧变量，会让闸门无声变开放、或透传无声失效 ——"
        "代价不对称，宁可起不来。",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class Settings:
    """服务配置。测试可直接构造，不必碰环境变量。"""

    # ---- 上游：只剩 web 一条 ----
    cookie: str = ""
    # 调用方的 Bearer 里放网页会话 cookie（裸 token 或完整 Cookie 串），
    # 本进程不再需要 AVM_COOKIE。这就是"走逆向线"的对外形态。
    passthrough_cookie: bool = False
    user_id: str = ""
    visitor_id: str = ""
    # 提交闸门槽位。**0 = 按账号额度自动**（每次建上游时读
    # `ai.queryUserPermission.maxQueueLength`：premium 2 / pro 4；读不到回落到 2）。
    # 写死一个数会让「pro 账号被卡在 premium 的额度上」——实测踩过。
    max_concurrent: int = 2
    # 自动探测失败时的回落值
    max_concurrent_fallback: int = 2
    # 多 worker 分摊除数（gunicorn master 在 auto=0 模式下经 AVM_CONCURRENCY_DIVISOR
    # 下发）：worker 内探测到的账号额度会 // divisor，使「每账号全局并发」仍恰好
    # 等于该账号的 maxQueueLength —— 不放大（撞上游）也不缩水（额度浪费）。
    concurrency_divisor: int = 1
    poll_interval: float = 10.0
    # tRPC **主调用**（提交 `ai.minimaxH3` / 任务查询 / `uploads.getPresignedUrl`）的
    # 单次请求超时（秒，`AVM_UPSTREAM_TIMEOUT`）。
    # 2026-09-18 之前它是 `WebClient` 里写死的 **30.0**（构造参数默认值，没有任何变量
    # 能改）⇒ 上游偶发慢于 30s 时（实测 `ReadTimeout: The read operation timed out`），
    # 运维**只能改代码**，没法按环境调。现在暴露成变量，**默认值仍是 30 ⇒ 行为不变**。
    # 🔴 与**调用方超时**直接竞争：调大 ⇒ 我们还在等，调用方可能已经放弃；而 POST
    #   提交**不自动重试**（没有幂等键，重试＝重复建任务），读超时之后上游**可能已经
    #   把任务建好了**（那笔会计费，但我们拿不到 task id）。只在"上游确实偶发慢于
    #   30s"时调，并确保调用方超时留足余量（闸门等空槽另占 600s，见 gunicorn 超时）。
    upstream_timeout: float = 120.0
    # 2026-10-21 由 60 调到 120。依据（生产 9 天日志 + 容器实测）：
    #   · `ReadTimeout: The read operation timed out` 共 54 条，是**可重试失败里最大的一类**；
    #   · 而 ai-prod 的容器**从 10-12 开始就是 AVM_UPSTREAM_TIMEOUT=60**
    #     （`docker inspect` 实测，不是 30）⇒ 那 54 条是**在 60s 上限下**超时的。
    #   ⇒ 60 秒都不够，说明上游 `create` 有时排队到一分钟以上（站点并发按账号算，我们这边
    #     只是把请求递上去，真正排队在它那边）⇒ 只能继续加超时。
    # 🔴 与"调用方超时"的竞争**已经不存在**：受理/推进分离（v0.0.39）之后 POST 毫秒级返回，
    #    create 跑在 daemon 后台线程里 ⇒ 这里的 120s 只影响"多久能确定提交成败"，不拖调用方。
    #    代价要认：失败可见时间也一起变长（调用方在这段时间看到的是 queued，不是 failed）。
    #    ⚠️ 但**仍不重试读超时**（见 `web_client.retryable_submit_error`）：120s 都没回，
    #    上游很可能已经建好任务，重发就是重复扣费。减少这类失败只能靠加超时，不能靠重试。
    # 探测类**只读**请求的超时（秒）：出口代理的 TLS 握手实测抖到 10s+，
    # 一发卡住的探测会占满重试循环（默认 30s），所以探测单独用短超时。
    probe_timeout: float = 8.0
    # 「参考文件链接」的**单项**网络预算（秒，`AVM_MEDIA_FETCH_TIMEOUT`）。
    # 🔴 它与**调用方的超时**直接竞争：调大 ⇒ 调用方先超时，而我们还在跑并接着把任务
    # 提交出去（那一步计费）。所以宁可快失败、给出点名步骤的错误。
    # 2026-09-16 由 20 调到 30（= WebClient 的默认请求超时）：给慢链路留出与其它上游调用
    # 同等的余量。**前提是调用方超时不少于 180 秒**（30 + 提交/排队）。
    # ⚠️ 此处刻意不写「≥ 数字s」那种形式：它会撞上计费文案门禁当"免费窗口上界"的锚点
    #    （`tests/test_docs_billing_sync.py` 的锚点要求 `≥` 后面紧跟数字与 `s`）而报红。
    #    措辞别扭是有原因的，别改回去 —— 连解释里也不能出现那个形式（本次已踩）。
    media_fetch_timeout: float = 30.0
    # **整个转存阶段**的总预算（秒，`AVM_MEDIA_REHOST_BUDGET`）：一次创建最多 7 个媒体项
    # （图 4 / 视频 1 / 音频 2）且串行转存，没有总闸就是"单项超时 × 项数"。
    # 2026-09-16 由 90 调到 120 = **图 4 × 单项 30s**（最常见的"多图带链接"档位）。
    media_rehost_budget: float = 120.0
    # 转存阶段**单项**的重试次数（`AVM_MEDIA_RETRIES`）：下载用户外链、PUT 到站点 CDN
    # 都是幂等的（同一个预签名 URL / 同一份字节），失败重发不会多传一份或重复计费。
    # 生产实测（2026-10-21）：`uploads.PUT WriteTimeout` / `ConnectError` /
    # `RemoteProtocolError` 共 4 条，都是"重发一次就好"的抖动。
    # ⚠️ 只对**传输层**错误重试；HTTP 4xx（含预签名 403 过期）走"重签一次"的独立路径，
    #    内容类错误（超限 / 格式不支持）一次都不重试。
    media_retries: int = 2
    # 转存阶段的**并发上限**（`AVM_REHOST_WORKERS`，默认 7 = 一次创建的最大项数）。
    # 2026-10-21 由代码里写死的 4 提上来：4 张图原来要分两轮（≈ 各项之和），
    # 现在一轮跑完（≈ 最慢一项），省 1~3s。真正的限流在站点的上传限速上，
    # 7 个并发还没见被限过（生产 PUT 失败 4 条全是超时/断连，不是 429/403）。
    rehost_workers: int = 7
    # 闸门探测（`model.needsCaptcha`）的缓存 TTL（秒，`AVM_CAPTCHA_PROBE_TTL`，默认 5）。
    # 这条探测串在**每次提交**的关键路径上（一次上游往返 0.1~1s）；同一账号连续提交时
    # 5 秒内的答案几乎不变。0 = 关闭缓存（退回"每次真探"）。
    # ⚠️ 缓存"关"的风险窗口 = TTL，兜底见 `WebClient._create_once`（空 taskId ⇒ 强制真探 + 换 token）。
    captcha_probe_ttl: float = 5.0
    # 提交**推进名额**（`AVM_SUBMIT_WORKERS`）。受理/推进分离之后，真正的提交（素材转存 →
    # 抢上游槽位 → `ai.minimaxH3` create）跑在 daemon 后台线程里，这个数就是"同时在推进的
    # 提交"上限；超出名额的提交在信号量上排队 —— **对调用方无感**（id 早已返回，状态仍是
    # queued）。两条线（方舟 `/tasks` 与 OpenAI `/v1/videos`）**共用同一个池子**。
    #
    # 为什么是 32（此前是 app.py 里写死的 8）：
    #   · 推进线程绝大部分时间阻塞在**上游闸门**上（每账号一把，premium 2 / pro 4），
    #     并不产生额外的上游流量 ⇒ 名额偏小不会"少打上游"，只会让**上游槽位空转**；
    #   · 透传线每份凭据 = 一个账号 = 一把闸门 ⇒ 号池有多少账号，就有多少份并行槽位。
    #     2026-10-12 的生产号池实测 12 个账号 × 2 槽 = 24 ⇒ 8 会让近 2/3 的槽位闲着；
    #   · 方舟线并入同一个池子后，两条线的推进需求叠加，8 更不够。
    # 调到 1 可复现"串行提交"（排查时序用）；调大**没有副作用**（真正的限流在上游闸门，
    # 这里多出来的线程只是更早地去排队）。
    submit_workers: int = 32
    # ---- 提交重试（`AVM_SUBMIT_RETRIES` / `AVM_SUBMIT_RETRY_BACKOFF`）----
    # 🔴 这条线**没有幂等键** ⇒ 重试的**唯一**许可条件是"上游确定没建任务"：
    #
    #   ✅ 可重试：连接类传输错误（`ConnectError` / `ConnectTimeout` / `PoolTimeout` /
    #      `ProxyError` —— 请求根本没发出去）；上游**明确拒绝**（`queue is full` / 429
    #      闸门 / 空 taskId —— 上游自己说没接）。
    #   ⛔ 绝不重试：`ReadTimeout` / `WriteTimeout` / `RemoteProtocolError`（请求可能已被
    #      接收，重发＝重复建任务＝重复扣费）；内容审核（两种文案，重发还是拒）；
    #      参数类 4xx。
    #
    # 生产依据（2026-10-12 → 10-21，1052 条受理 / 214 条后台失败）：
    #   · `queue is full` 19 条 —— 上游并发满，**隔几秒再发就进得去**；
    #   · Turnstile/mint 失败 16 条（minter 503）—— 换 token 重发；
    #   · 读超时 54 条 —— 靠上面的 60s 超时减少发生，**不靠重试**（见上）。
    submit_retries: int = 2
    # 退避基数（秒）：第 n 次重试等 `backoff * 2**(n-1)`。`queue is full` 要的是**等**，
    # 不是立刻撞；但也不能等到上游槽位窗口作废 ⇒ 默认 3s ⇒ 3s / 6s。
    submit_retry_backoff: float = 3.0
    # **只给测试与本地调试**：受理分离的"推进侧"改为**同步执行** —— POST 要等提交完成才返回，
    # 与 2026-09 之前的行为同形。
    # 存在的理由只有一个：让「参数映射 / 响应契约」这类功能门禁保持**确定性**（它们考的是
    # 映射与证据，不是受理时序）。真实时序由 `tests/test_videos_async_accept.py` 在
    # **异步模式**下逐条钉住。两条线的写回语义完全一致（成功补 taskId / 失败落 submit_error），
    # 所以两条路被门禁覆盖的程度是一样的。
    # 🔴 刻意**不**从环境变量读、也不登记 `.env.example`：这是个能改变**对外时序契约**的开关，
    #    生产误开等于把"提交立即返回 id"这个特性静默关掉。要复现同步行为请走测试基座
    #    （`tests/test_web_upstream.py` 的 `web_settings`）。
    submit_inline: bool = False
    # 号池上报是否带**账号身份（邮箱）**。默认关：遥测里放 PII 要显式点头。
    account_report_identity: bool = False
    # ---- Turnstile token 铸造服务（见 tools/turnstile_service.py）----
    # 闸门开着且调用方没带 token 时，自动去这里取一个；取不到则如实失败（退回慢路径）。
    # 留空 = 不启用（行为与以前完全一致）。
    minter_url: str = ""
    minter_key: str = ""
    minter_timeout: float = 25.0
    # 铸造服务的**单地址重试次数**（`AVM_MINTER_RETRIES`）。生产实测：minter 偶发 503
    # （16 条 CAPTCHA_REQUIRED 的 last_error 全是 `HTTP 503`），而铸造本身是幂等的
    # （每次铸一个新 token，多铸一个只是浪费一次配额，不会多建任务）。
    minter_retries: int = 1
    # 铸造服务的**提前量**（`AVM_MINTER_RESERVE`，默认 8）—— 这是"提前过 CF"的开关。
    # 语义（见 minter `?reserve=N`）：池子空（上次拿到 `source=live`）时声明"接下来还要
    # N 个"，服务端在铸当前这个的同时把后续 N 个铸好 ⇒ 后续 N 次直接命中池子（~0 延迟）。
    # ⚠️ 服务端的显式 reserve 是**累加**的 ⇒ 适配层做**自适应**：上次 `source=pool`
    #    时传 0 不再追加（否则池深会被顶到 RESERVE_MAX = 只要有流量就永久满池铸造）。
    # 0 = 不声明（服务端仍会用自己的缺省 2，吸收式、不累加）。
    minter_reserve: int = 8

    # ---- 通用 ----
    base_url: str = DEFAULT_BASE_URL
    # ---- 鉴权：**一个变量**（见模块头）----
    # `gate_key` / `passthrough_cookie` 是**生效字段**（下游 `require_bearer` / `upstreams` /
    # `/healthz` 只认它们）；模式 `auth` 由它们**派生**（见下面的 property）。
    # 刻意不做成字段：字段就可能与真实行为不一致（`Settings(passthrough_cookie=True)`
    # 这种直接构造在测试夹具里很常见），而报出去的模式必须是行为的忠实描述。
    gate_key: str = ""
    # 旧变量残留（只显式写了"关"值的那几个）：仅用于启动期提示，不参与任何行为。
    auth_deprecated: tuple = ()
    service_name: str = SERVICE_NAME
    service_title: str = SERVICE_TITLE
    environment: str = ""
    log_level: str = "INFO"
    enable_logfire: bool = True
    logfire_send: bool | str = "if-token-present"
    logfire_console: bool = False
    # 脱敏默认**关**：上报的请求/响应要能直接读（打开后，含 session/cookie/api_key
    # 字样的属性值会被 logfire 整条替换成 [Scrubbed …]）。凭证的防线改为
    # 「请求头不进 span」（capture_headers=False），见 observability.py。
    logfire_scrubbing: bool = False
    # 抓请求头（含 cookie）要显式打开 —— 打开即明文进 trace。
    logfire_capture_headers: bool = False
    # 单个属性值里字符串的截断上限：防 data URI 把 trace 撑爆，不是脱敏。
    logfire_max_chars: int = 20000
    # 不进 Logfire 上报的路径（`AVM_LOGFIRE_EXCLUDED_PATHS`，逗号分隔）。
    # `None` = **未设/留空 ⇒ 用内置默认表**（探活 + 公开文档端点，见 observability）；
    # `()` = 显式不排除任何路径（写成 `-`）——"留空"与"显式关掉"必须能分开表达。
    # ⚠️ 语义是**路径**，不是正则：原样值永不进正则（否则写 `/` 会关掉全站追踪）。
    logfire_excluded_paths: tuple | None = None
    # 被**轮询**的业务路径（`AVM_LOGFIRE_POLL_PATHS`，逗号分隔，与上表同构、同解析器）。
    # 这些路径不算"探活"，但同样被反复请求 ⇒ 不进 Logfire 上报（span + 成功响应的
    # access log）。默认表 = 任务查询路径（方舟线 + OpenAI 兼容线），见 observability。
    # `None` = 未设/留空 ⇒ 内置默认表；`()` = 显式不排除（写成 `-`）。
    logfire_poll_paths: tuple | None = None
    # False 时忽略 HTTP_PROXY / macOS scutil 代理。本地或对回环上游自测时设
    # AVM_NO_TRUST_ENV=1，否则请求会被代理走并拿到网关的 502。
    trust_env: bool = True
    # 号池上报周期（秒）：每 N 秒把**每个凭据**的余额 / 闸门状态作为 Logfire 指标
    # 上报（只读探测）。0 = 关闭。
    # ⚠️ 采集与出口**解耦**：Logfire 未装配/出口不通时**照样采样**（否则出口坏掉的
    #    那一刻反而彻底瞎掉），只是指标不可用。见 app.py 的 _account_reporter。
    account_report_seconds: int = 300

    # ---- 任务持久化 ----
    # sqlite 是默认后端（跨重启可读）；memory 是**显式**的开发/测试开关 —— 重启即丢，
    # 绝不是 sqlite 失败后的隐式兜底（那会让持久化在没人注意时悄悄失效）。
    task_store: str = "sqlite"
    task_db: str = DEFAULT_DB_PATH
    task_retention_days: int = DEFAULT_RETENTION_DAYS
    # GET /tasks/{id} 的上游视图缓存 TTL（秒）：非终态 TTL 内复用、终态永久复用。
    # 用途 = 轮询节流（E2E-AVM-011 配套）：任务出片要 ~60s，调用方高频轮询时在
    # TTL 内直接回缓存，不打上游 —— 否则查询请求会变成 429 的主要来源。
    # 0 = 显式关闭（每次实时回上游，行为与旧版一致）。
    task_cache_ttl: float = 15.0
    # 闸门"盯到终态"的总时限（秒，`AVM_WATCH_TIMEOUT`，默认 3600）。
    # 它就是轮询的**总超时**：超时后 `_watch` 放掉槽位、任务记录留在非终态由调用方继续查
    # （轮询路径另有 `task_cache_ttl` 缓存与只读重试，不依赖闸门这一路）。
    # 此前写死在 `WebSubmitQueue` 的构造默认值里（3600），运维改不了。
    watch_timeout: float = 3600.0

    # ---- 成片对外出口（见 media_proxy）----
    # 上游成片是一个**公开直链**，里面同时带着上游域名与上游**实际执行**的模型名
    # （形如 `static2.img2video.ai/…_0_minimax_h3_….mp4`），响应头 `content-disposition`
    # 里还重复一份模型名。配上本服务的对外基址后，`GET /tasks/{id}` 给出的
    # `content.video_url` 会变成 `{public_base}/v/{ark_id}.mp4` —— 读不出任何上游信息；
    # 取用时由本服务流式回源并**重写响应头**，三条泄露面一次关掉。
    #
    # 留空 = 功能关闭，行为与从前**逐字节一致**（原样透传上游链接）。
    public_base: str = ""
    # 成片下载端点的路径前缀（`AVM_PUBLIC_PATH`），默认 `/v`。
    media_path: str = "/v"
    # 回源的**空闲**超时（秒，`AVM_MEDIA_READ_TIMEOUT`）：两次读到字节之间超过它才算卡死。
    # ⚠️ 刻意不用"总超时"：成片可以几百 MB，任何固定总时长都会变成"文件越大越容易失败"。
    media_read_timeout: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        # 鉴权：一个变量定模式。非法取值、以及"旧变量还有行为"都在这里拒绝
        # —— 刻意放在 from_env 而不是 validate()：环境变量的**原文**只在这里读得到，
        # 而迁移判定依赖原文（`AVM_GATE_KEY=` 空串 = 没设 vs `AVM_PASSTHROUGH_COOKIE=0` = 显式关）。
        auth_mode, gate_key, auth_problem = parse_auth(env.get("AVM_AUTH", ""))
        legacy_effective, legacy_deprecated = legacy_auth_usage(env)
        if auth_problem:
            raise ValueError(auth_problem)
        if legacy_effective:
            raise ValueError(legacy_auth_error(legacy_effective))
        return cls(
            cookie=normalize_cookie_header(env.get("AVM_COOKIE", "")),
            # 生效字段由模式派生（`auth` 自己是 property，见类里）：
            # `AVM_AUTH=passthrough` ⇒ 透传；`key:<密钥>` ⇒ 闸门密钥；留空 ⇒ 都不设。
            passthrough_cookie=(auth_mode == AUTH_PASSTHROUGH),
            auth_deprecated=legacy_deprecated,
            user_id=str(env.get("AVM_USER_ID", "")).strip(),
            visitor_id=str(env.get("AVM_VISITOR_ID", "")).strip(),
            max_concurrent=int(env.get("AVM_MAX_CONCURRENT") or 2),
            concurrency_divisor=max(1, int(env.get("AVM_CONCURRENCY_DIVISOR") or 1)),
            poll_interval=float(env.get("AVM_POLL_SECONDS") or 10),
            upstream_timeout=float(env.get("AVM_UPSTREAM_TIMEOUT") or 120),
            probe_timeout=float(env.get("AVM_PROBE_TIMEOUT") or 8),
            media_fetch_timeout=float(env.get("AVM_MEDIA_FETCH_TIMEOUT") or 30),
            media_rehost_budget=float(env.get("AVM_MEDIA_REHOST_BUDGET") or 120),
            media_retries=max(0, int(env.get("AVM_MEDIA_RETRIES") or 2)),
            rehost_workers=max(1, int(env.get("AVM_REHOST_WORKERS") or 7)),
            captcha_probe_ttl=max(0.0, float(env.get("AVM_CAPTCHA_PROBE_TTL") or 5)),
            submit_workers=max(1, int(env.get("AVM_SUBMIT_WORKERS") or 32)),
            submit_retries=max(0, int(env.get("AVM_SUBMIT_RETRIES") or 2)),
            submit_retry_backoff=max(0.0, float(env.get("AVM_SUBMIT_RETRY_BACKOFF") or 3.0)),
            minter_url=(env.get("AVM_MINTER_URL") or "").strip(),
            minter_key=(env.get("AVM_MINTER_KEY") or "").strip(),
            minter_timeout=float(env.get("AVM_MINTER_TIMEOUT") or 25),
            minter_retries=max(0, int(env.get("AVM_MINTER_RETRIES") or 1)),
            minter_reserve=max(0, int(env.get("AVM_MINTER_RESERVE") or 8)),
            account_report_identity=_env_flag(env, "AVM_ACCOUNT_REPORT_IDENTITY"),
            base_url=str(env.get("AVM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            # 闸门密钥（来自 `AVM_AUTH=key:<密钥>`；裸密钥不被接受 —— 见 parse_auth）
            gate_key=gate_key,
            # 服务名可用 AVM_SERVICE_NAME 临时覆盖，但默认值只有一个来源
            service_name=str(env.get("AVM_SERVICE_NAME") or SERVICE_NAME).strip(),
            environment=str(env.get("AVM_ENVIRONMENT", "")).strip(),
            log_level=str(env.get("AVM_LOG_LEVEL", "INFO")).strip().upper(),
            # 任务持久化
            task_store=str(env.get("AVM_TASK_STORE", "sqlite")).strip().lower() or "sqlite",
            task_db=str(env.get("AVM_TASK_DB", DEFAULT_DB_PATH)).strip() or DEFAULT_DB_PATH,
            task_retention_days=int(env.get("AVM_TASK_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS),
            task_cache_ttl=float(env.get("AVM_TASK_CACHE_TTL") or 15),
            watch_timeout=float(env.get("AVM_WATCH_TIMEOUT") or 3600),
            # 成片对外出口：留空 = 关闭（原样透传上游链接，行为与从前一致）
            public_base=(env.get("AVM_PUBLIC_BASE") or "").strip().rstrip("/"),
            media_path=_normalize_media_path(env.get("AVM_PUBLIC_PATH")),
            media_read_timeout=float(env.get("AVM_MEDIA_READ_TIMEOUT") or 60),
            # 可观测性
            enable_logfire=not _env_flag(env, "AVM_DISABLE_LOGFIRE"),
            logfire_send=_parse_send(env.get("AVM_LOGFIRE_SEND", "if-token-present")),
            logfire_console=_env_flag(env, "AVM_LOGFIRE_CONSOLE"),
            logfire_scrubbing=_env_flag(env, "AVM_LOGFIRE_SCRUBBING"),
            logfire_capture_headers=_env_flag(env, "AVM_LOGFIRE_CAPTURE_HEADERS"),
            logfire_max_chars=int(env.get("AVM_LOGFIRE_MAX_CHARS") or 20000),
            logfire_excluded_paths=_parse_excluded_paths(env.get("AVM_LOGFIRE_EXCLUDED_PATHS")),
            # 同一套解析器、同一套语义（路径表，`-` = 显式不排除）
            logfire_poll_paths=_parse_excluded_paths(env.get("AVM_LOGFIRE_POLL_PATHS")),
            trust_env=not _env_flag(env, "AVM_NO_TRUST_ENV"),
            account_report_seconds=int(env.get("AVM_ACCOUNT_REPORT_SECONDS") or 300),
        )

    @property
    def auth(self) -> str:
        """归一化的鉴权模式（`open` / `passthrough` / `gate`）—— 由**生效字段**派生。

        为什么是 property 而不是字段：字段就可能与真实行为不一致（直接构造 `Settings` 的
        调用方——测试夹具、嵌入用法——不会自动同步那个副本），而 `/healthz` 与启动日志报出的
        模式必须是**行为**的忠实描述。派生之后二者永远一致，不可能漂移。
        """
        if self.gate_key:
            return AUTH_GATE
        return AUTH_PASSTHROUGH if self.passthrough_cookie else AUTH_OPEN

    @property
    def web_ready(self) -> bool:
        """凭据是否齐备？（本进程持有会话 cookie，或开了透传让调用方自带）"""
        return bool(self.cookie or self.passthrough_cookie)

    @property
    def available_upstreams(self) -> list[str]:
        """对外能力声明 —— 本进程实际能用的上游。

        只剩 web 一条，但这里仍返回**列表**而不是布尔：`/healthz` 已经在用这个
        字段，且"能力"与"进程内已建好客户端"是两回事 —— 透传线的客户端要等到
        看到调用方凭据才能建（见 `upstreams.build_web_for_cookie`）。
        """
        return ["web"] if self.web_ready else []

    def validate(self) -> None:
        """配置错误在起服务之前就暴露，而不是等第一个请求打进来。"""
        if self.task_store not in TASK_STORES:
            raise ValueError(
                f"AVM_TASK_STORE 必须是 {TASK_STORES} 之一（收到 {self.task_store!r}）"
            )

        if self.passthrough_cookie and self.gate_key:
            # 环境变量层**已不可能**表达这个组合（`AVM_AUTH` 三态天然互斥，见 parse_auth）；
            # 这里守的是代码内直接构造 Settings 的场景（测试夹具、嵌进别的进程）。
            raise ValueError(
                "闸门（gate_key）与透传（passthrough_cookie）互斥：同一个 Authorization: "
                "Bearer 不可能既是闸门密钥又是上游凭据。用 **一个变量** 表达："
                f"AVM_AUTH={AUTH_FORMS}（二选一，不设 = 不校验）。"
            )

        if not self.web_ready:
            raise ValueError(
                "至少要配一份凭据：AVM_COOKIE（至少含 auth_session=...），"
                f"或 AVM_AUTH={AUTH_PASSTHROUGH} 透传调用方的会话 cookie。"
            )
