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
    """铸造服务客户端。`url` 支持**逗号分隔多个地址**（多实例轮询 + 故障转移）。

    - 空 ⇒ `configured=False`，语义上等价于"没接这个能力"；
    - 单地址：行为与历史版本完全一致；
    - 多地址：**从上次成功的下一个开始轮询**（长期均匀分摊负载，20 账号场景
      单实例产能 ~0.35 token/s 不够 10 个满负荷 pro ⇒ 双实例分摊），某实例失败
      立即转移到下一个，**全失败才返回 None**（调用方据此退回慢路径）。
    """

    def __init__(self, url: str = "", key: str = "", timeout: float = DEFAULT_TIMEOUT):
        # 逗号分隔多地址；逐个清理空白与尾斜杠
        self.urls = [u.strip().rstrip("/") for u in (url or "").split(",") if u.strip()]
        self.key = key or ""
        self.timeout = float(timeout)
        self._http: httpx.Client | None = None
        self._next = 0          # 轮询指针：从上次成功的下一个开始

    # ------------------------------------------------------------------ api --

    @property
    def url(self) -> str:
        """兼容旧引用：返回第一个地址（日志/监控用）。"""
        return self.urls[0] if self.urls else ""

    @property
    def configured(self) -> bool:
        return bool(self.urls)

    def mint(self) -> str | None:
        """取一个 token；任何失败都返回 `None`（调用方负责降级）。"""
        if not self.configured:
            return None
        try:
            if self._http is None:
                # trust_env=False：本机回环绝不能被 HTTP_PROXY 拐走
                self._http = httpx.Client(timeout=self.timeout, trust_env=False)
            headers = {"X-Minter-Key": self.key} if self.key else {}
            n = len(self.urls)
            last_err = None
            for k in range(n):
                i = (self._next + k) % n
                u = self.urls[i]
                try:
                    r = self._http.post(f"{u}/v1/turnstile/mint", headers=headers)
                    if r.status_code != 200:
                        logger.warning(f"铸造服务 {u} 返回 {r.status_code}：{r.text[:160]}")
                        last_err = f"{u}: HTTP {r.status_code}"
                        continue
                    token = str((r.json() or {}).get("token") or "")
                    if not token:
                        logger.warning(f"铸造服务 {u} 返回空 token")
                        last_err = f"{u}: empty token"
                        continue
                    self._next = (i + 1) % n     # 成功 ⇒ 下次从下一个开始（均匀分摊）
                    return token
                except Exception as e:  # noqa: BLE001 单实例故障 ⇒ 转移下一个
                    logger.warning(f"铸造服务 {u} 不可用（{type(e).__name__}: {e}）⇒ 转移下一个")
                    last_err = f"{u}: {type(e).__name__}"
            if last_err:
                logger.warning(f"所有铸造服务都失败（{last_err}）⇒ 本条退回慢路径")
            return None
        except Exception as e:  # noqa: BLE001 网络/客户端故障都算"取不到"
            logger.warning(f"铸造客户端异常（{type(e).__name__}: {e}）")
            return None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
