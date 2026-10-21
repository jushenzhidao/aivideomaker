"""向常驻铸造服务要 Turnstile token（`AVM_MINTER_URL`）。

设计三原则：

1. **绝不抛异常**：铸造失败只是"闸门仍然拦着"的一种情形 —— 调用方据此退回
   "等闸门衰减"的慢路径（~190s/条）。把失败伪装成成功会让上游更糟。
2. **短超时 + 只走回环/内网**：这是能产出"绕过风控凭证"的服务，不对外暴露；
   `trust_env=False` 是必须的 —— 代理会把 `127.0.0.1` 也拐走（本站踩过）。
3. **只做一件事**：取回 token 字符串；不缓存（token 单次有效，缓存就是 bug 源）。

★ 失败归因（E2E-AVM-008）：node064 上 3 条 429 的根因是"配置全对、但宿主防火墙把
  桥接容器 → 宿主端口的包丢了"——minter 侧 `served` 纹丝不动，适配层却只有一句笼统的
  "minter configured but unavailable"。所以这里把**最近一次失败的归因**留在
  `last_error` 上（`unreachable: …` = 网络层到不了；`HTTP xxx` / `empty token` =
  到得了但铸造失败），调用方据此在 429 报文里直接给出修法，别让人猜。
"""

from __future__ import annotations

import time

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

    `last_error`：最近一次 `mint()` 失败的**归因**（成功后清空为 None）——
    `unreachable: <url> (<异常类>)` = 网络层不可达（E2E-AVM-008：多为宿主防火墙
    丢包）；`<url>: HTTP xxx` / `<url>: empty token` = 可达但铸造失败。
    仅供调用方拼 429 报文用，不参与任何控制流。
    """

    def __init__(
        self,
        url: str = "",
        key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 1,
        reserve: int = 8,
    ):
        # 逗号分隔多地址；逐个清理空白与尾斜杠
        self.urls = [u.strip().rstrip("/") for u in (url or "").split(",") if u.strip()]
        self.key = key or ""
        self.timeout = float(timeout)
        # 单个地址的**短退避重试**（`AVM_MINTER_RETRIES`，2026-10-21）。
        # 生产实测：16 条 `CAPTCHA_REQUIRED` 的 `last_error` **全是 `HTTP 503`** ——
        # 铸造服务瞬时过载/池子空，隔半秒再要一次就有。铸造是幂等的（每次铸一个新
        # token，多铸一个只浪费一次配额），**不会多建任务、不会多扣费**。
        self.retries = max(0, int(retries))
        # ---- 提前过 CF（`AVM_MINTER_RESERVE`，2026-10-21）----
        # 注意：**适配层不再自己建 token 池** —— 铸造服务已经有池（`?reserve=N` 会把
        # 目标池深临时抬到 N，即使 `prewarm: false` 也生效，见 minter `TargetState.want`）。
        # 本地缓存 token 是错的：Turnstile token **单次有效**，缓存等于缓存废票。
        #
        # 自适应（关键，别改成"每条都传 N"）：
        #   · 上一次是 `live`（池子空了）⇒ 传 `reserve=N`，让服务端在我们提交当前这条的
        #     同时就把接下来 N 个铸好 ⇒ 后续 N 次直接命中池子（~0 延迟）；
        #   · 上一次是 `pool`（还有存货）⇒ 传 `reserve=0`（显式关掉追加）。
        #     ⚠️ 显式 reserve 在服务端是**累加**的：每条都传 N 会把池深顶到 RESERVE_MAX
        #     ⇒ 等于"只要有流量就永久满池铸造"，主动抬高站点侧风控分。
        #     自适应下铸造**总次数**与按需现铸相同（一条一次），只是把"每次要 1 个"
        #     摊成"每次批量要 N 个"。
        self.reserve = max(0, int(reserve))
        #: 最近一次拿到的 token 来源（`pool` / `live` / None=没取过）。只用于自适应预约
        #: 与观测 —— 不参与任何正确性判断。
        self.last_source: str | None = None
        self._http: httpx.Client | None = None
        self._next = 0          # 轮询指针：从上次成功的下一个开始
        self.last_error: str | None = None

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
        self.last_error = None      # 本次尝试的归因：成功清空、失败覆盖
        if not self.configured:
            return None
        try:
            if self._http is None:
                # trust_env=False：本机回环绝不能被 HTTP_PROXY 拐走
                self._http = httpx.Client(timeout=self.timeout, trust_env=False)
            headers = {"X-Minter-Key": self.key} if self.key else {}
            n = len(self.urls)
            # 重试**只在单地址部署时**开（生产就是这一形态）。多地址部署的初衷是
            # "一个坏了立刻换下一个"；在同址重试只会把故障转移拖慢 0.5s×N。
            retries = self.retries if n == 1 else 0
            last_err = None
            for k in range(n):
                i = (self._next + k) % n
                u = self.urls[i]
                # 单地址：最多 1 + retries 次，**失败先在同一地址短退避重试**
                #（0.5s / 1s），次数用完才换下一个地址。
                # 为什么把重试放在地址内层：生产那 16 条 `HTTP 503` 是**瞬时过载/池子空**，
                # 同一地址隔半秒再要就有；先绕一圈其他地址在单地址部署时纯属白等。
                for attempt in range(1 + retries):
                    more = attempt + 1 < 1 + retries
                    try:
                        # 自适应预约：live ⇒ 声明 N 个提前量；pool ⇒ 不再追加（见 __init__）
                        want_reserve = self.reserve if self.last_source != "pool" else 0
                        r = self._http.post(
                            f"{u}/v1/turnstile/mint",
                            params={"reserve": want_reserve},
                            headers=headers,
                        )
                        if r.status_code != 200:
                            self.last_error = f"{u}: HTTP {r.status_code}"
                            logger.warning(
                                f"铸造服务 {u} 返回 {r.status_code}（第 {attempt + 1}/"
                                f"{1 + retries} 次）：{r.text[:160]}"
                            )
                            last_err = f"{u}: HTTP {r.status_code}"
                            if more:
                                time.sleep(0.5 * (2 ** attempt))
                                continue
                            break  # 次数用完 ⇒ 换下一个地址
                        payload = r.json() or {}
                        token = str(payload.get("token") or "")
                        if token:
                            # `source=pool` 意味着**没等铸造**（这是"提前过 CF"生效的
                            # 唯一证据）；变化时打一条，便于确认预约真的在起作用。
                            src = str(payload.get("source") or "") or None
                            if src != self.last_source:
                                logger.info(
                                    "铸造来源切换：{} → {}（age_ms={} reserve={}）",
                                    self.last_source, src,
                                    payload.get("age_ms"), want_reserve,
                                )
                            self.last_source = src
                        if not token:
                            self.last_error = f"{u}: empty token"
                            logger.warning(f"铸造服务 {u} 返回空 token")
                            last_err = f"{u}: empty token"
                            if more:
                                time.sleep(0.5 * (2 ** attempt))
                                continue
                            break
                        self._next = (i + 1) % n     # 成功 ⇒ 下次从下一个开始（均匀分摊）
                        # 🔴 重试成功也要把**本次**的失败归因清掉：否则下一次 429 的报文
                        #    会带上一次重试留下的 `HTTP 503`，指向错误的根因。
                        self.last_error = None
                        return token
                    except Exception as e:  # noqa: BLE001 单实例故障 ⇒ 先重试，再转移
                        self.last_error = f"unreachable: {u} ({type(e).__name__})"
                        logger.warning(
                            f"铸造服务 {u} 不可用（第 {attempt + 1}/{1 + retries} 次）："
                            f"{type(e).__name__}: {e}"
                        )
                        last_err = f"{u}: {type(e).__name__}"
                        if more:
                            time.sleep(0.5 * (2 ** attempt))
                            continue
                        break
            if last_err:
                logger.warning(f"所有铸造服务都失败（{last_err}）⇒ 本条退回慢路径")
            return None
        except Exception as e:  # noqa: BLE001 网络/客户端故障都算"取不到"
            self.last_error = f"unreachable: minter client ({type(e).__name__})"
            logger.warning(f"铸造客户端异常（{type(e).__name__}: {e}）")
            return None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
