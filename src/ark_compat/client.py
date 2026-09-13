"""aivideomaker 官方 API 客户端（httpx）。

用 httpx 而不是 urllib，是为了让 `logfire.instrument_httpx()` 能自动把每一次
上游调用变成一条子 span —— 排障时"网关慢还是上游慢"一眼可辨，无需手工埋点。

安全约定：`key` 只进请求头，**绝不写进任何 span 属性或日志**。
请求/响应**原文**（含 body）会经 `note_upstream` 进 trace，但请求头走
`observability.safe_headers()` 过滤 —— 凭证头永远不进。
"""

from __future__ import annotations

import time

import httpx

from .errors import BudgetUnsetError, OfficialApiError
from .observability import note_upstream, safe_headers
from .translate import BUDGET_REQUIRED_MESSAGE

DEFAULT_BASE_URL = "https://aivideomaker.ai"


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


class OfficialClient:
    """官方 API 客户端。提交**会计费**，因此 create() 没有支出上限就拒绝。"""

    def __init__(
        self,
        key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        trust_env: bool = True,
    ):
        if not key:
            raise ValueError("official upstream requires an API key — set AVM_KEY")
        self.key = key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # trust_env=False 时忽略 HTTP_PROXY 等环境代理。注意 macOS 上 `scutil --proxy`
        # 设的代理 httpx 也会读到，本地自测务必关掉，否则 127.0.0.1 会被代理走，
        # 拿到的是网关的 502 而不是 connection refused。
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"key": self.key, "accept": "application/json"},
            trust_env=trust_env,
        )

    # ---- lifecycle ----

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OfficialClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- plumbing ----

    def _req(self, method: str, path: str, *, json=None, headers: dict | None = None, timeout: float | None = None):
        call = f"{method} {path}"
        # 请求原文进 trace；凭证头（key）在这里就被过滤掉，与脱敏开关无关
        req_view = {"json": json, "headers": safe_headers(headers)}
        started = time.perf_counter()
        try:
            r = self._http.request(method, path, json=json, headers=headers, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            note_upstream(
                call,
                upstream="official",
                request=req_view,
                status="error",
                error=f"{type(e).__name__}: {e}",
                duration_ms=_ms(started),
            )
            raise OfficialApiError(0, "NETWORK_ERROR", f"{type(e).__name__}: {e}") from None

        elapsed = _ms(started)
        if r.status_code >= 400:
            try:
                parsed = r.json()
            except Exception:
                parsed = {"raw": r.text[:300]}
            code = parsed.get("errorCode") or (parsed.get("error") or {}).get("code")
            if not code and r.status_code == 429:
                code = "RATE_LIMITED"
            msg = (
                parsed.get("message")
                or (parsed.get("error") or {}).get("message")
                or parsed.get("raw")
                or r.text[:300]
                or f"HTTP {r.status_code}"
            )
            # 失败路径也要留痕：上游的拒绝理由（body）往往只在这里出现一次
            note_upstream(
                call,
                upstream="official",
                request=req_view,
                response={"http_status": r.status_code, "body": parsed},
                status="error",
                error=f"HTTP {r.status_code} {code or ''} {msg}".strip(),
                duration_ms=elapsed,
            )
            raise OfficialApiError(r.status_code, code, str(msg), parsed)

        if not r.content:
            res = None
        else:
            try:
                res = r.json()
            except Exception:
                res = {"raw": r.text[:1000]}
        note_upstream(
            call,
            upstream="official",
            request=req_view,
            response={"http_status": r.status_code, "body": res},
            task_id=str((res or {}).get("taskId") or "") if isinstance(res, dict) else None,
            duration_ms=elapsed,
        )
        return res

    # ---- api ----

    def account(self) -> dict:
        """免计费的体检端点：余额、密钥限额、supportedModels。"""
        return self._req("GET", "/api/v1/account") or {}

    def balance(self):
        return self.account().get("currentBalance")

    def quote(self, model: str, payload: dict) -> dict:
        """免计费的权威报价（同时校验参数格式）。"""
        return self._req("POST", f"/api/v1/quote/{model}", json=payload) or {}

    def create(
        self,
        model: str,
        payload: dict,
        max_credits: int | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        """提交生成。**会计费** —— 没有 max_credits 时直接拒绝，不发请求。"""
        if max_credits is None:
            raise BudgetUnsetError(BUDGET_REQUIRED_MESSAGE)

        headers = {"X-Max-Credits": str(int(max_credits))}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)

        res = self._req("POST", f"/api/v1/generate/{model}", json=payload, headers=headers)
        return (res or {}).get("taskId")

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/api/v1/tasks/{task_id}", timeout=30.0) or {}

    def cancel_task(self, task_id: str) -> dict:
        """真取消，积分全额退回（web 线没有这个能力）。"""
        return self._req("PUT", f"/api/v1/tasks/{task_id}/cancel") or {}

    def list_tasks(self) -> list:
        return (self._req("GET", "/api/v1/tasks", timeout=30.0) or {}).get("tasks") or []
