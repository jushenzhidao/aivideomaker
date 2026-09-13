"""运行配置（全部来自环境变量，无配置文件）。

服务名（`service_name` / `service_title`）的**唯一来源**是 `ark_compat/__init__.py`
里的常量 —— 确定名字后只改那里一处即可，其余全部引用它。

上游二选一（`AVM_UPSTREAM`）：

    official  官方 API（`key` 头）—— 提交即计费，必须有支出上限
    web       网页端内部接口（会话 cookie）—— 有免费窗口，但没有取消端点

**凭据的两种来源**（每条线各有一个透传开关）：

===========================================  ==========================================
本进程持有一份凭据（默认）                     调用方按请求自带凭据（透传）
===========================================  ==========================================
official：`AVM_KEY`                          official：`AVM_PASSTHROUGH_KEY=1`
web：`AVM_COOKIE`                            web：`AVM_PASSTHROUGH_COOKIE=1`
===========================================  ==========================================

透传模式下调用方的 `Authorization: Bearer` 就是**上游凭据本身** —— 官方线放 API Key，
web 线放网页会话 cookie。因此它与闸门（`AVM_GATE_KEY`）**互斥**：单一 Bearer 不可能
既当闸门密钥又当上游凭据，同设的后果是每个请求都 401（见 `validate()`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from . import SERVICE_NAME, SERVICE_TITLE
from .client import DEFAULT_BASE_URL
from .cookie import normalize_cookie_header
from .store import DEFAULT_DB_PATH, DEFAULT_RETENTION_DAYS

UPSTREAMS = ("official", "web")
TASK_STORES = ("sqlite", "memory")


def _env_int(env: Mapping[str, str], key: str) -> int | None:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer (got {raw!r})") from None


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


@dataclass(frozen=True)
class Settings:
    """服务配置。测试可直接构造，不必碰环境变量。"""

    # ---- 上游选择 ----
    upstream: str = "official"

    # ---- official 线 ----
    upstream_key: str = ""
    passthrough_key: bool = False
    max_credits: int | None = None
    default_model: str = ""

    # ---- web 线 ----
    cookie: str = ""
    # 调用方的 Bearer 里放网页会话 cookie（裸 token 或完整 Cookie 串），
    # 本进程不再需要 AVM_COOKIE。这就是"走逆向线"的对外形态。
    passthrough_cookie: bool = False
    user_id: str = ""
    visitor_id: str = ""
    max_concurrent: int = 2
    poll_interval: float = 10.0

    # ---- 通用 ----
    base_url: str = DEFAULT_BASE_URL
    gate_key: str = ""
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
    # 抓请求头（含 key / cookie）要显式打开 —— 打开即明文进 trace。
    logfire_capture_headers: bool = False
    # 单个属性值里字符串的截断上限：防 data URI 把 trace 撑爆，不是脱敏。
    logfire_max_chars: int = 20000
    # False 时忽略 HTTP_PROXY / macOS scutil 代理。本地或对回环上游自测时设
    # AVM_NO_TRUST_ENV=1，否则请求会被代理走并拿到网关的 502。
    trust_env: bool = True
    # 号池上报周期（秒）：每 N 秒把**每个凭据**的余额 / 闸门状态作为 Logfire 指标
    # 上报（只读探测）。0 = 关闭；Logfire 未装配时**不采样**（避免白打上游）。
    account_report_seconds: int = 300

    # ---- 任务持久化 ----
    # sqlite 是默认后端（跨重启可读）；memory 是**显式**的开发/测试开关 —— 重启即丢，
    # 绝不是 sqlite 失败后的隐式兜底（那会让持久化在没人注意时悄悄失效）。
    task_store: str = "sqlite"
    task_db: str = DEFAULT_DB_PATH
    task_retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            upstream=str(env.get("AVM_UPSTREAM", "official")).strip().lower() or "official",
            task_store=str(env.get("AVM_TASK_STORE", "sqlite")).strip().lower() or "sqlite",
            task_db=str(env.get("AVM_TASK_DB", DEFAULT_DB_PATH)).strip() or DEFAULT_DB_PATH,
            task_retention_days=int(env.get("AVM_TASK_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS),
            upstream_key=str(env.get("AVM_KEY", "")).strip(),
            passthrough_key=_env_flag(env, "AVM_PASSTHROUGH_KEY"),
            passthrough_cookie=_env_flag(env, "AVM_PASSTHROUGH_COOKIE"),
            max_credits=_env_int(env, "AVM_OFFICIAL_MAX_CREDITS"),
            default_model=str(env.get("AVM_OFFICIAL_MODEL", "")).strip(),
            cookie=normalize_cookie_header(env.get("AVM_COOKIE", "")),
            user_id=str(env.get("AVM_USER_ID", "")).strip(),
            visitor_id=str(env.get("AVM_VISITOR_ID", "")).strip(),
            max_concurrent=int(env.get("AVM_MAX_CONCURRENT") or 2),
            poll_interval=float(env.get("AVM_POLL_SECONDS") or 10),
            base_url=str(env.get("AVM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
            gate_key=str(env.get("AVM_GATE_KEY", "")).strip(),
            # 服务名可用 AVM_SERVICE_NAME 临时覆盖，但默认值只有一个来源
            service_name=str(env.get("AVM_SERVICE_NAME", SERVICE_NAME)).strip(),
            environment=str(env.get("AVM_ENVIRONMENT", "")).strip(),
            log_level=str(env.get("AVM_LOG_LEVEL", "INFO")).strip().upper(),
            enable_logfire=not _env_flag(env, "AVM_DISABLE_LOGFIRE"),
            logfire_send=_parse_send(env.get("AVM_LOGFIRE_SEND", "if-token-present")),
            logfire_console=_env_flag(env, "AVM_LOGFIRE_CONSOLE"),
            logfire_scrubbing=_env_flag(env, "AVM_LOGFIRE_SCRUBBING"),
            logfire_capture_headers=_env_flag(env, "AVM_LOGFIRE_CAPTURE_HEADERS"),
            logfire_max_chars=int(env.get("AVM_LOGFIRE_MAX_CHARS") or 20000),
            trust_env=not _env_flag(env, "AVM_NO_TRUST_ENV"),
            account_report_seconds=int(env.get("AVM_ACCOUNT_REPORT_SECONDS") or 300),
        )

    @property
    def official_ready(self) -> bool:
        """官方线可用？（有 key，或开了透传让调用方自带 key）"""
        return bool(self.upstream_key or self.passthrough_key)

    @property
    def web_ready(self) -> bool:
        """web 线可用？（有会话 cookie，或开了透传让调用方自带 cookie）"""
        return bool(self.cookie or self.passthrough_cookie)

    @property
    def passthrough(self) -> bool:
        """是否有任一条线在"调用方自带凭据"形态下运行。"""
        return bool(self.passthrough_key or self.passthrough_cookie)

    @property
    def available_upstreams(self) -> list[str]:
        """本进程实际能用的上游 —— 凭据齐全的那些。两条都配齐就两条都能用。

        注意：这里说的是**能力**，不是"进程内已建好客户端"。透传线的客户端要等到
        看到调用方凭据才能建（见 `upstreams.build_web_for_cookie`），所以它出现在这里
        但不会出现在 `app.state.upstreams` 里。
        """
        out: list[str] = []
        if self.official_ready:
            out.append("official")
        if self.web_ready:
            out.append("web")
        return out

    def validate(self) -> None:
        """配置错误在起服务之前就暴露，而不是等第一个请求打进来。"""
        if self.upstream not in UPSTREAMS:
            raise ValueError(f"AVM_UPSTREAM 必须是 {UPSTREAMS} 之一（收到 {self.upstream!r}）")
        if self.task_store not in TASK_STORES:
            raise ValueError(
                f"AVM_TASK_STORE 必须是 {TASK_STORES} 之一（收到 {self.task_store!r}）"
            )

        if self.passthrough and self.gate_key:
            # 这不是"运行时才发现的偶发问题"，而是**必然失败**的组合：闸门先校验
            # `Authorization: Bearer`，而透传要求同一个 Bearer 就是上游凭据。
            # 两者同设时每个请求都会在闸门处 401，透传永远不生效 —— 且现象
            # （401 AuthenticationError）看起来像是"调用方凭据错了"，极易误诊。
            raise ValueError(
                "AVM_GATE_KEY 与透传互斥（AVM_PASSTHROUGH_KEY / AVM_PASSTHROUGH_COOKIE）："
                "同一个 Authorization: Bearer 不可能既是闸门密钥又是上游凭据。"
                "要透传就清空 AVM_GATE_KEY；要闸门就关掉透传。"
            )

        available = self.available_upstreams
        if not available:
            raise ValueError(
                "至少要配一条上游：官方线用 AVM_KEY（或 AVM_PASSTHROUGH_KEY=1 透传调用方 API Key），"
                "web 线用 AVM_COOKIE（至少含 auth_session=...）"
                "（或 AVM_PASSTHROUGH_COOKIE=1 透传调用方会话 cookie）"
            )
        if self.upstream not in available:
            # 不隐式回退 —— 那会悄悄改变计费语义（免费窗口 vs 一律计费）
            raise ValueError(
                f"AVM_UPSTREAM={self.upstream} 缺少对应凭据；当前可用的是 {available}。"
                f"官方线需要 AVM_KEY，web 线需要 AVM_COOKIE（或对应的透传开关）。"
                f"只开透传时请显式写 AVM_UPSTREAM={available[0]}。"
            )

    @property
    def upstream_key_for_client(self) -> str:
        """OfficialClient 需要一个非空 key 才能构造（透传模式下先用占位）。"""
        return self.upstream_key or "passthrough"

    def translate_env(self) -> dict[str, str]:
        """注入翻译层的环境视图 —— 让纯函数不必读 os.environ。"""
        env: dict[str, str] = {}
        if self.default_model:
            env["AVM_OFFICIAL_MODEL"] = self.default_model
        if self.max_credits is not None:
            env["AVM_OFFICIAL_MAX_CREDITS"] = str(self.max_credits)
        return env
