"""异常类型。

两个边界各自的错误：
  - `ParamError`        调用方的请求本身不合法（→ Ark 的 400 InvalidParameter）
  - `OfficialApiError`  上游 aivideomaker 官方 API 报错（→ 按语义映射为 Ark 错误码）
"""

from __future__ import annotations


class ParamError(Exception):
    """请求不合法（对应 Ark 的 InvalidParameter）。"""

    def __init__(self, message: str, param: str = ""):
        super().__init__(message)
        self.param = param


class OfficialApiError(Exception):
    """上游官方 API 返回错误。"""

    def __init__(self, http_status: int, error_code: str | None, message: str, body=None):
        super().__init__(f"official api {http_status}{' ' + error_code if error_code else ''}: {message}")
        self.http_status = http_status
        self.code = error_code or f"HTTP_{http_status}"
        self.body = body


class BudgetUnsetError(OfficialApiError):
    """没有显式支出上限 —— 拒绝提交（官方线提交即计费）。"""

    def __init__(self, message: str):
        super().__init__(0, "BUDGET_UNSET", message)


class WebApiError(Exception):
    """web 逆向线（tRPC over /api）返回的错误。"""

    def __init__(self, procedure: str, message: str, code: str | None = None, http_status: int = 0):
        super().__init__(f"{procedure}: {message}")
        self.procedure = procedure
        self.code = code or "TRPC_ERROR"
        self.http_status = http_status


class CaptchaRequiredError(WebApiError):
    """账号当前的动态验证码闸门是开的（needsCaptcha=true）。

    这不是账号属性，是**按速率翻转的开关**：付费账号在一小时内连续生成约 7 条后
    翻转为 true，之后 `token: null` 与假 token 一样被静默拒绝（返回空串）。
    可用 BYO 真 token（`params.token`）穿过，或等它衰减，或改走官方 API。
    """

    def __init__(self, message: str):
        super().__init__("ai.minimaxH3", message, code="CAPTCHA_REQUIRED", http_status=429)
