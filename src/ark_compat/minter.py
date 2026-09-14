"""向常驻铸造服务要 Turnstile token（`AVM_MINTER_URL`）。

设计三原则：

1. **绝不抛异常**：铸造失败只是"闸门仍然拦着"的一种情形 —— 调用方据此退回
   "等闸门衰减"的慢路径（~190s/条）。把失败伪装成成功会让上游更糟。
2. **短超时 + 只走回环/内网**：这是能产出"绕过风控凭证"的服务，不对外暴露；
   `trust_env=False` 是必须的 —— 代理会把 `127.0.0.1` 也拐走（本站踩过）。
3. **只做一件事**：取回 token 字符串；不缓存（token 单次有效，缓存就是 bug 源）。
"""

from __future__ import annotations

import httpx
from loguru import logger

DEFAULT_TIMEOUT = 25.0


class TokenMinter:
    """铸造服务客户端。`url` 为空 ⇒ `configured=False`，语义上等价于"没接这个能力"。"""

    def __init__(self, url: str = "", key: str = "", timeout: float = DEFAULT_TIMEOUT):
        self.url = (url or "").rstrip("/")
        self.key = key or ""
        self.timeout = float(timeout)
        self._http: httpx.Client | None = None

    # ------------------------------------------------------------------ api --

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def mint(self) -> str | None:
        """取一个 token；任何失败都返回 `None`（调用方负责降级）。"""
        if not self.configured:
            return None
        try:
            if self._http is None:
                # trust_env=False：本机回环绝不能被 HTTP_PROXY 拐走
                self._http = httpx.Client(timeout=self.timeout, trust_env=False)
            headers = {"X-Minter-Key": self.key} if self.key else {}
            r = self._http.post(f"{self.url}/v1/turnstile/mint", headers=headers)
            if r.status_code != 200:
                logger.warning(f"铸造服务返回 {r.status_code}：{r.text[:160]}")
                return None
            token = str((r.json() or {}).get("token") or "")
            if not token:
                logger.warning("铸造服务返回空 token")
                return None
            return token
        except Exception as e:  # noqa: BLE001 网络/服务故障都算"取不到"
            logger.warning(f"铸造服务不可用（{type(e).__name__}: {e}）⇒ 本条退回慢路径")
            return None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
