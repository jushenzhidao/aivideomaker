"""异常类型。

两个边界各自的错误：
  - `ParamError`            调用方的请求本身不合法（→ Ark 的 400 InvalidParameter）
  - `WebApiError`           上游（网页端内部接口）报错（→ 按语义映射为 Ark 错误码）
  - `CaptchaRequiredError`  动态验证码闸门开启时的特化（→ 429 RateLimitExceeded）
"""

from __future__ import annotations


class ParamError(Exception):
    """请求不合法（对应 Ark 的 InvalidParameter）。"""

    def __init__(self, message: str, param: str = ""):
        super().__init__(message)
        self.param = param


class WebApiError(Exception):
    """web 逆向线（tRPC over /api）返回的错误。

    `transport`：**传输层**异常类名（`ReadTimeout` / `ConnectError` / …），只在"根本没拿到
    响应"时填。它的用途是让**对外脱敏**后的报文仍能区分"超时（可重试）"与"连不上" ——
    这类事实调用方需要，而且不含任何内部标识（见 `app._upstream_client_message`）。
    """

    def __init__(
        self,
        procedure: str,
        message: str,
        code: str | None = None,
        http_status: int = 0,
        *,
        transport: str = "",
    ):
        super().__init__(f"{procedure}: {message}")
        self.procedure = procedure
        self.code = code or "TRPC_ERROR"
        self.http_status = http_status
        self.transport = transport


class CaptchaRequiredError(WebApiError):
    """账号当前的动态验证码闸门是开的（needsCaptcha=true）。

    这不是账号属性，是**按速率翻转的开关**：付费账号在一小时内连续生成约 7 条后
    翻转为 true，之后 `token: null` 与假 token 一样被静默拒绝（返回空串）。
    可用 BYO 真 token（`params.token`）穿过，或等它衰减。

    `minter_last_error`：接了铸造服务且**取 token 失败**时的归因原文
    （`TokenMinter.last_error`，如 `unreachable: …`）。没接铸造/没试过 = None。
    app 层会把它提升为 `ark.create.submit` 的**结构化 span 属性**
    （`minter_unreachable` / `minter_last_error`，E2E-AVM-008）——
    只躺在报文散文里的话，Logfire 里没法按属性过滤，"3 条 429 全是防火墙"
    这种聚合结论就出不来。
    """

    def __init__(self, message: str, minter_last_error: str | None = None):
        super().__init__("ai.minimaxH3", message, code="CAPTCHA_REQUIRED", http_status=429)
        self.minter_last_error = minter_last_error
