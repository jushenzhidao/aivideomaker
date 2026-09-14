"""运行配置（全部来自环境变量，无配置文件）。

服务名（`service_name` / `service_title`）的**唯一来源**是 `ark_compat/__init__.py`
里的常量 —— 确定名字后只改那里一处即可，其余全部引用它。

本项目只对接**一条上游**：网页端内部接口（tRPC over `/api`，会话 cookie）。
它没有取消端点，但有一个免费窗口 —— `tier=turbo` 且 `duration ≤ 10s` 不计费。

**凭据的两种来源**：

===========================================  ==========================================
本进程持有一份凭据（默认）                     调用方按请求自带凭据（透传）
===========================================  ==========================================
web：`AVM_COOKIE`                            web：`AVM_PASSTHROUGH_COOKIE=1`
===========================================  ==========================================

透传模式下调用方的 `Authorization: Bearer` 就是**上游凭据本身**（网页会话 cookie）。
因此它与闸门（`AVM_GATE_KEY`）**互斥**：单一 Bearer 不可能既当闸门密钥又当上游凭据，
同设的后果是每个请求都 401（见 `validate()`）。
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
    poll_interval: float = 10.0
    # 探测类**只读**请求的超时（秒）：出口代理的 TLS 握手实测抖到 10s+，
    # 一发卡住的探测会占满重试循环（默认 30s），所以探测单独用短超时。
    probe_timeout: float = 8.0
    # 号池上报是否带**账号身份（邮箱）**。默认关：遥测里放 PII 要显式点头。
    account_report_identity: bool = False
    # ---- Turnstile token 铸造服务（见 tools/turnstile_service.py）----
    # 闸门开着且调用方没带 token 时，自动去这里取一个；取不到则如实失败（退回慢路径）。
    # 留空 = 不启用（行为与以前完全一致）。
    minter_url: str = ""
    minter_key: str = ""
    minter_timeout: float = 25.0

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
    # 抓请求头（含 cookie）要显式打开 —— 打开即明文进 trace。
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
            cookie=normalize_cookie_header(env.get("AVM_COOKIE", "")),
            passthrough_cookie=_env_flag(env, "AVM_PASSTHROUGH_COOKIE"),
            user_id=str(env.get("AVM_USER_ID", "")).strip(),
            visitor_id=str(env.get("AVM_VISITOR_ID", "")).strip(),
            max_concurrent=int(env.get("AVM_MAX_CONCURRENT") or 2),
            poll_interval=float(env.get("AVM_POLL_SECONDS") or 10),
            probe_timeout=float(env.get("AVM_PROBE_TIMEOUT") or 8),
            minter_url=(env.get("AVM_MINTER_URL") or "").strip(),
            minter_key=(env.get("AVM_MINTER_KEY") or "").strip(),
            minter_timeout=float(env.get("AVM_MINTER_TIMEOUT") or 25),
            account_report_identity=_env_flag(env, "AVM_ACCOUNT_REPORT_IDENTITY"),
            base_url=str(env.get("AVM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
            gate_key=str(env.get("AVM_GATE_KEY", "")).strip(),
            # 服务名可用 AVM_SERVICE_NAME 临时覆盖，但默认值只有一个来源
            service_name=str(env.get("AVM_SERVICE_NAME", SERVICE_NAME)).strip(),
            environment=str(env.get("AVM_ENVIRONMENT", "")).strip(),
            log_level=str(env.get("AVM_LOG_LEVEL", "INFO")).strip().upper(),
            # 任务持久化
            task_store=str(env.get("AVM_TASK_STORE", "sqlite")).strip().lower() or "sqlite",
            task_db=str(env.get("AVM_TASK_DB", DEFAULT_DB_PATH)).strip() or DEFAULT_DB_PATH,
            task_retention_days=int(env.get("AVM_TASK_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS),
            # 可观测性
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
            # 这不是"运行时才发现的偶发问题"，而是**必然失败**的组合：闸门先校验
            # `Authorization: Bearer`，而透传要求同一个 Bearer 就是上游凭据。
            # 两者同设时每个请求都会在闸门处 401，透传永远不生效 —— 且现象
            # （401 AuthenticationError）看起来像是"调用方凭据错了"，极易误诊。
            raise ValueError(
                "AVM_GATE_KEY 与透传互斥（AVM_PASSTHROUGH_COOKIE）："
                "同一个 Authorization: Bearer 不可能既是闸门密钥又是上游凭据。"
                "要透传就清空 AVM_GATE_KEY；要闸门就关掉透传。"
            )

        if not self.web_ready:
            raise ValueError(
                "至少要配一份凭据：AVM_COOKIE（至少含 auth_session=...），"
                "或 AVM_PASSTHROUGH_COOKIE=1 透传调用方的会话 cookie。"
            )
