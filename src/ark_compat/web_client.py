"""web 逆向线客户端：tRPC over `/api/*` + 预签名上传 + SSE 状态流。

与 `client.py`（官方 API）并列的第二条上游。**纯 HTTP：不需要浏览器、不需要过验证码**
（验证码是动态闸门，见 `errors.CaptchaRequiredError`）。

站点不是 REST，而是 **tRPC over `/api`**：

    GET  /api/{procedure}?batch=1&input={"0":{"json":<input>}}
    POST /api/{procedure}?batch=1&input=...   body: {"0":{"json":<input>}}

注意路径**没有 /trpc 前缀**，procedure 直接拼在 `/api` 之后（如 `/api/auth.user`）。

几个实测结论都写在对应方法上：读取任务的三种姿势、SSE 会挂死、预签名 60 秒有效期、
上传上限按类型分档。测试时注入 `httpx.MockTransport` 即可完全离线。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from loguru import logger

from .cookie import normalize_cookie_header
from .errors import CaptchaRequiredError, ParamError, WebApiError
from .observability import note_upstream, safe_headers, suppress_http
from .sniff import image_dimensions, sniff_file

# 站点约定
PAGE = "/zh/ai-video-generator"
LIST_PAGE = "/zh/generations"
DEFAULT_VISITOR_ID = "f29ee26edcb4e8b96ee17e277a384f6f"
# 建任务用的 procedure。单列成常量：trace 里要据此把「这次调用的返回值就是 taskId」
# 认出来（`trpc` 是通用的，不看 procedure 就得猜）。
CREATE_PROCEDURE = "ai.minimaxH3"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# tRPC 的"无参数"编码成 null + meta 标记 undefined。auth.user / credits.getCredits
# 要的是**位置式**那种（`values: ["undefined"]`）；listModel 用的是具名式。
VOID_INPUT: dict[str, Any] = {"0": {"json": None, "meta": {"values": ["undefined"], "v": 1}}}

TERMINAL = frozenset({"succeed", "success", "completed", "failed", "error", "cancelled", "canceled"})
SUCCESS = frozenset({"succeed", "success", "completed"})


def parse_sse(text: str) -> dict | None:
    """取 SSE 里的第一个 `data: {...}` 帧。"""
    for line in (text or "").split("\n"):
        t = line.strip()
        if not t.startswith("data:"):
            continue
        payload = t[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    return None


class WebClient:
    """网页端会话客户端。`cookie` 至少要有 `auth_session`。"""

    def __init__(
        self,
        cookie: str,
        base_url: str = "https://aivideomaker.ai",
        # None → 读 AVM_USER_ID（历史行为）；"" → 明确"不指定"，交给
        # get_user_id() 从会话里读。**透传时必须传 ""**：把默认账号的 userId 配到
        # 调用方自己的会话上，会让列表类接口按错误的 userId 查（"任务存在却查不到"）。
        user_id: str | None = None,
        visitor_id: str = "",
        timeout: float = 30.0,
        # 探测类**只读**请求的超时与重试（见 `trpc` 的说明）。出口代理的 TLS 握手
        # 实测抖到 10s+，一发卡住的探测会把闸门重试循环占满一整个默认 30s。
        probe_timeout: float = 8.0,
        probe_retries: int = 2,
        # 铸造服务客户端（可选）。闸门开着且调用方没带 token 时用它取一个；
        # 没配 / 取不到 ⇒ 如实抛 CaptchaRequiredError（绝不静默提交）。
        minter: object | None = None,
        trust_env: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        # 裸 token 也接受：自动补 `auth_session=` 前缀并告警（见 ark_compat/cookie.py）。
        # 在构造函数里兜一次，这样直接 `WebClient(cookie=...)` 的调用方也不会踩坑。
        cookie = normalize_cookie_header(cookie)
        if not cookie:
            raise ValueError("WebClient 需要 AVM_COOKIE（至少含 auth_session=...）")
        self.cookie = cookie
        self.base_url = base_url.rstrip("/")
        if user_id is None:
            user_id = os.environ.get("AVM_USER_ID", "")
        self.user_id = str(user_id).strip()
        # 服务端**不校验** visitorId —— 随便一个 32 位十六进制都行
        self.visitor_id = visitor_id or os.environ.get("AVM_VISITOR_ID") or DEFAULT_VISITOR_ID
        self.probe_timeout = float(probe_timeout)
        self.probe_retries = max(0, int(probe_retries))
        self.minter = minter
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=trust_env,
            transport=transport,
            follow_redirects=True,
        )

    # ------------------------------------------------------------ lifecycle --

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- plumbing --

    def _headers(self, referer: str = PAGE, extra: dict | None = None) -> dict[str, str]:
        h = {
            "user-agent": USER_AGENT,
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": f"{self.base_url}{referer}",
            "cookie": self.cookie,
        }
        h.update(extra or {})
        return h

    def trpc(self, procedure: str, inp: Any = None, *, method: str = "GET",
             referer: str = PAGE, meta: dict | None = None,
             timeout: float | None = None, retries: int | None = None) -> Any:
        """调一个 tRPC procedure，解开 `result.data.json`；出错抛 `WebApiError`。

        `json.dumps` 用紧凑分隔符：tRPC 的 input 会被百分号编码进 query，
        紧凑写法避免空格被编码成 `+`／`%20` 的歧义。

        **重试策略（2026-09-14 实测后加的）**：

        - `GET` 是**只读** ⇒ 传输层错误（代理 502 / `ConnectError` / 各种 Timeout）
          按 `probe_retries`（默认 2）**立即**重试。实测这类错误多数下一发就成功；
          而"失败后等 10s 再试"会白烧掉一整个闸门窗口（实测：一次真提交被代理吞掉
          ⇒ 那个窗口作废 ⇒ 该条整体延后 393s）。
        - `POST` 是**写**，而这条线**没有幂等键** ⇒ **绝不自动重试**：响应丢失时重试
          可能重复建任务（计费线上就是重复扣费）。写失败只能由调用方决定怎么处理。
        - `timeout=None` ⇒ 用 client 默认（长）；探测类显式传 `probe_timeout`（短）。
        """
        payload = json.dumps(meta if meta is not None else {"0": {"json": inp}}, separators=(",", ":"))
        url = f"/api/{procedure}"
        params = {"batch": "1", "input": payload}
        extra = {"content-type": "application/json", "accept": "*/*"}
        hdrs = self._headers(referer, extra)
        # 请求原文进 trace：input 就是 tRPC 的语义载荷（含 token 等参数）；
        # 请求头走 safe_headers —— cookie 在这一步就被摘掉，不看脱敏开关
        req_view = {"procedure": procedure, "method": method, "input": inp, "headers": safe_headers(hdrs)}
        started = time.perf_counter()

        # 只有只读请求（GET）自动重试；写入（POST）一次都不重试 —— 没有幂等键。
        planned = self.probe_retries if retries is None else max(0, int(retries))
        attempts = 1 if method == "POST" else planned + 1
        # ⚠️ httpx 的 `timeout=None` 表示**关闭超时**（不是"用默认值"），所以 None 时
        # 必须**不传**这个参数，否则一个卡住的请求会永远挂着。
        tkw = {} if timeout is None else {"timeout": timeout}

        for attempt in range(attempts):
            try:
                if method == "POST":
                    extra["origin"] = self.base_url
                    r = self._http.post(url, params=params, headers=hdrs,
                                        json={"0": {"json": inp}}, **tkw)
                else:
                    r = self._http.get(url, params=params, headers=hdrs, **tkw)
                break
            except httpx.HTTPError as e:
                if attempt + 1 < attempts:
                    # 立即重试：退避只用于错开极短抖动，不做秒级等待
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                note_upstream(
                    procedure,
                    upstream="web",
                    request=req_view,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                raise WebApiError(procedure, f"{type(e).__name__}: {e}") from None
        else:  # pragma: no cover —— 循环要么 break 要么 raise
            raise WebApiError(procedure, "transport error (no attempt completed)")

        elapsed = (time.perf_counter() - started) * 1000
        try:
            parsed = json.loads(r.text)
        except json.JSONDecodeError:
            note_upstream(
                procedure,
                upstream="web",
                request=req_view,
                response={"http_status": r.status_code, "body": r.text[:500]},
                status="error",
                error=f"non-JSON response ({r.status_code})",
                duration_ms=elapsed,
            )
            raise WebApiError(procedure, f"non-JSON response ({r.status_code}) {r.text[:200]}") from None

        first = parsed[0] if isinstance(parsed, list) and parsed else None
        if isinstance(first, dict) and first.get("error"):
            body = first["error"].get("json") or {}
            data = body.get("data") or {}
            note_upstream(
                procedure,
                upstream="web",
                request=req_view,
                response={"http_status": r.status_code, "body": parsed},
                status="error",
                error=str(body.get("message") or "unknown tRPC error"),
                duration_ms=elapsed,
            )
            raise WebApiError(
                procedure,
                str(body.get("message") or "unknown tRPC error"),
                code=data.get("code"),
                http_status=data.get("httpStatus") or r.status_code,
            )
        if isinstance(first, dict):
            result = first.get("result") or {}
            data = result.get("data") or {}
            value = data.get("json")
        else:
            value = None
        note_upstream(
            procedure,
            upstream="web",
            request=req_view,
            response={"http_status": r.status_code, "body": parsed},
            task_id=str(value) if procedure == CREATE_PROCEDURE and value else None,
            duration_ms=elapsed,
        )
        return value

    # ------------------------------------------------------------ session ----

    def get_user(self) -> dict | None:
        return self.trpc("auth.user", None, meta=VOID_INPUT)

    def get_user_id(self) -> str:
        """列表接口需要 userId；没配就读一次会话。"""
        if self.user_id:
            return self.user_id
        user = self.get_user() or {}
        self.user_id = str(user.get("id") or "")
        return self.user_id

    def get_credits(self) -> int | None:
        """权威余额：`credits.getCredits` → `totalRemaining`。

        网页端与官方 API **共用同一份积分池**（实测与 `/api/v1/account` 的
        `currentBalance` 相等），所以这个数也等于官方侧的余额。
        """
        r = self.trpc("credits.getCredits", None, meta=VOID_INPUT, timeout=self.probe_timeout)
        return (r or {}).get("totalRemaining")

    def get_subscription(self) -> dict | None:
        """订阅详情（`billing.subscription`，只读）。

        站点返回的是 `{id, userId, customerId, status, planId, price, variantId,
        nextPaymentDate, startDate}` —— **没有套餐名**（"premium"/"pro" 只存在于站点
        自己的商品描述里，见 app._PLAN_ID_CONCURRENCY 的注释）。

        ⚠️ 返回值里有 `userId`/`customerId`，上报时**只取套餐与日期字段**，
        账号身份一律用指纹（见 `observability.record_account`）。
        """
        r = self.trpc("billing.subscription", None, meta=VOID_INPUT, timeout=self.probe_timeout)
        return r if isinstance(r, dict) else None

    def get_permission(self) -> dict | None:
        """账号权限/额度（`ai.queryUserPermission`，只读）。

        返回 `{hasPermission, planName, maxQueueLength, ...}` —— 其中
        **`maxQueueLength` 就是该账号的并发额度**（premium 2 / pro 4，站点原文同）。
        用它来给提交闸门定槽位，而不是把并发写死在配置里。
        """
        r = self.trpc("ai.queryUserPermission", {}, timeout=self.probe_timeout)
        return r if isinstance(r, dict) else None

    def needs_captcha(self) -> bool:
        """这个账号现在要不要过 Turnstile？

        **动态风控开关，不是账号属性** —— 每次提交前都要重新问，不要缓存。
        """
        return bool(
            self.trpc(
                "model.needsCaptcha",
                {"userId": self.get_user_id()},
                timeout=self.probe_timeout,
            )
        )

    # ------------------------------------------------------------- create ----

    def _minter_configured(self) -> bool:
        return bool(self.minter is not None and getattr(self.minter, "configured", False))

    def _mint_token_from_service(self) -> str | None:
        """向铸造服务取一个 token；未配置或失败都返回 None（调用方降级）。"""
        if not self._minter_configured():
            return None
        tok = self.minter.mint()
        if tok:
            logger.info("已从铸造服务取得 Turnstile token（len={}）", len(tok))
        return tok

    def _minter_failure_hint(self) -> str:
        """铸造失败后的**归因提示**（E2E-AVM-008）。

        "minter configured but unavailable" 有两种完全不同的成因，修法南辕北辙：
        ① **网络层不可达**（`last_error` 带 `unreachable`）—— 配置全对也取不到，
           minter 侧 `served` 纹丝不动。host 网络的 minter 下这几乎总是**宿主防火墙**
           丢了「桥接容器 → 宿主端口」的包（E2E-AVM-008：3 条 429 全部源于此），
           直接给放行命令与验证方法；
        ② **可达但铸造失败** —— 指向 minter 自己（看它的 /healthz.last_failure）。
        归因取自 `TokenMinter.last_error`（替身没有该属性时如实说 unknown）。
        """
        last = getattr(self.minter, "last_error", None) or "unknown"
        if "unreachable" in str(last):
            return (
                "token minter configured but UNREACHABLE from this container "
                f"(last_error={last}). With a host-network minter this is usually the "
                "HOST FIREWALL dropping bridge-to-host traffic (E2E-AVM-008): allow the "
                "container subnet to the minter port, e.g. `ufw allow proto tcp from "
                "172.16.0.0/12 to any port 8899`, and verify with "
                "`python3 tools/compose_wiring_check.py --probe`. "
            )
        return f"token minter configured but mint failed (last_error={last}) — "

    def create(self, params: dict, token: str | None = None) -> str:
        """创建视频任务，返回站点 taskId。

        `token` 是调用方自带的真实 Turnstile token（BYO）。闸门关闭时可为 None。
        闸门开启且没带 token 时**不提交**，直接抛 `CaptchaRequiredError` ——
        上游此时会把 `token: null` 静默拒掉（返回空串），既不报错也不建任务，
        所以我们必须自己把它变成显式失败。
        """
        if self.needs_captcha() and not token:
            # 闸门开着：若接了铸造服务就先要一个（这是把吞吐从 ~20 条/小时抬到
            # ~120 条/小时的那把钥匙）。**取不到就如实失败** —— 没 token 也提交会被
            # 上游静默拒（返回空串），那看起来像成功，是最难查的一类故障。
            token = self._mint_token_from_service()
            if not token:
                tried = bool(self.minter is not None and getattr(self.minter, "configured", False))
                last = getattr(self.minter, "last_error", None) if tried else None
                raise CaptchaRequiredError(
                    "account currently requires a Turnstile captcha (model.needsCaptcha=true). "
                    "This is a dynamic, velocity-based gate, not an account property. "
                    + (self._minter_failure_hint() if tried else "")
                    + "Supply a fresh Turnstile token, wait for it to decay, or spread the submissions out.",
                    # 归因原文随异常上行 ⇒ app 层提升为结构化 span 属性（E2E-AVM-008）
                    minter_last_error=str(last) if last else None,
                )

        body = {
            "content": params.get("content"),
            "imageUrl": params.get("imageUrl"),
            "lastFrameUrl": params.get("lastFrameUrl"),
            "referenceImageUrls": params.get("referenceImageUrls") or [],
            "referenceVideoUrl": params.get("referenceVideoUrl"),
            "referenceAudioUrls": params.get("referenceAudioUrls") or [],
            "aspectRatio": params.get("aspectRatio") or "16:9",
            "duration": params.get("duration") or 5,
            "resolution": params.get("resolution") or "480p",
            "tier": params.get("tier") or "turbo",
            "promptEnrichment": bool(params.get("promptEnrichment")),
            "visitorId": self.visitor_id,
            "token": token,
        }
        task_id = self.trpc(CREATE_PROCEDURE, body, method="POST")
        if not task_id and self._minter_configured():
            # 站点用**空串**表示拒绝（不报错）。两种常见成因：
            #   ① 手上这个 token 已过期/已被用过（池子里的 token 会自然老死）；
            #   ② 探测时闸门是关的、提交前刚好被顶开（竞态）。
            # 两者都能被"**换一个新 token 重试一次**"救回来。空串意味着上游没建任务，
            # 所以重试不会重复建（已用 token 的情况也一样 —— 它根本没被接受）。
            fresh = self._mint_token_from_service()
            if fresh:
                body["token"] = fresh
                task_id = self.trpc(CREATE_PROCEDURE, body, method="POST")
                if task_id:
                    logger.info("用新铸的 token 重试成功（上一个 token 已过期/已用，或闸门刚被顶开）")
        if not task_id:
            # 站点用空串表示"拒绝"，不报错 —— 必须当成显式失败
            raise WebApiError(
                CREATE_PROCEDURE,
                "create returned no task id — the request was rejected (captcha gate or invalid parameters)",
            )
        return str(task_id)

    # --------------------------------------------------------------- tasks ----

    def list(self, offset: int = 0, limit: int = 40, sort: str = "desc") -> dict:
        """账号的生成记录（分页）。"""
        payload = {
            "0": {
                "json": {
                    "createdAt": None,
                    "offset": offset,
                    "limit": limit,
                    "createAtSort": sort,
                    "userId": self.get_user_id(),
                },
                # `createdAt` 用 undefined-with-meta 表示"无游标"
                "meta": {"values": {"createdAt": ["undefined"]}, "v": 1},
            }
        }
        url = "/api/model.listModel"
        r = self._http.get(
            url,
            params={"batch": "1", "input": json.dumps(payload, separators=(",", ":"))},
            headers=self._headers(LIST_PAGE, {"accept": "*/*", "content-type": "application/json"}),
        )
        try:
            j = json.loads(r.text)
        except json.JSONDecodeError:
            raise WebApiError("model.listModel", f"non-JSON ({r.status_code})") from None
        first = j[0] if isinstance(j, list) and j else None
        if isinstance(first, dict) and first.get("error"):
            body = first["error"].get("json") or {}
            raise WebApiError("model.listModel", str(body.get("message")), code=(body.get("data") or {}).get("code"))
        result = (first or {}).get("result") or {}
        return ((result.get("data") or {}).get("json")) or {}

    def get_model(self, task_id: str) -> dict | None:
        """单条任务。**三种读法里最好的一种**：一次请求、权威、不依赖分页位置。"""
        return self.trpc("model.getModel", {"id": task_id})

    def find_in_list(self, task_id: str, limit: int = 40) -> dict | None:
        data = self.list(limit=limit)
        for m in data.get("models") or []:
            if m.get("id") == task_id:
                return m
        return None

    def query_queue(self, task_id: str) -> dict | None:
        return self.trpc("model.queryQueueByModel", {"id": task_id})

    def mint_token(self, task_id: str) -> str:
        """换取状态流令牌（`expiresInSec` 约 300）。

        ⚠️ 高频调用会 429，别拿它当轮询用。
        """
        r = self._http.get(
            "/api/model-status/token",
            params={"id": task_id, "visitorId": self.visitor_id},
            headers=self._headers(PAGE, {"accept": "*/*", "cache-control": "no-cache", "pragma": "no-cache"}),
        )
        if r.status_code >= 400:
            raise WebApiError("model-status/token", f"{r.status_code}: {r.text[:200]}", http_status=r.status_code)
        j = r.json()
        token = j.get("token")
        if not token:
            raise WebApiError("model-status/token", f"no token in response: {json.dumps(j)[:200]}")
        return str(token)

    def get_task(self, task_id: str, *, sse_timeout: float = 12.0, prefer_list: bool = True) -> dict:
        """读完整任务记录，逐级回落。

        三种读法的实测取舍：

        ==================  =========  ==========================================
        `model.getModel`    1 次请求   最权威，不需要 userId —— **首选**
        `model.listModel`   1 次请求   要 userId，且任务得在请求的那一页里
        `model-status` SSE  2 次请求   (a) 上游还没接单时**不发帧也不断开 → 挂死**
                                      (b) `/token` 会 429
        ==================  =========  ==========================================
        """
        try:
            rec = self.get_model(task_id)
            if rec:
                return rec
        except WebApiError as e:
            if e.code == "NOT_FOUND":
                raise

        if prefer_list:
            try:
                rec = self.find_in_list(task_id)
                if rec:
                    return rec
            except Exception:  # noqa: BLE001  —— 回落到 SSE
                pass

        try:
            token = self.mint_token(task_id)
            r = self._http.get(
                "/api/model-status",
                params={"id": task_id, "token": token, "visitorId": self.visitor_id},
                headers=self._headers(
                    PAGE, {"accept": "text/event-stream", "cache-control": "no-cache", "pragma": "no-cache"}
                ),
                timeout=sse_timeout,  # 上游未接单时会挂死，必须自己加超时
            )
            if r.status_code >= 400:
                raise WebApiError("model-status", f"{r.status_code}: {r.text[:200]}", http_status=r.status_code)
            frame = parse_sse(r.text)
            if frame:
                return frame.get("model") or frame
        except WebApiError as e:
            if e.code == "NOT_FOUND":
                raise
        except Exception:  # noqa: BLE001  —— 429 / 超时 / 5xx / 无帧，都往下走
            pass

        try:
            rec = self.find_in_list(task_id)
        except Exception:  # noqa: BLE001
            rec = None
        if not rec:
            raise WebApiError("model.getModel", f"task {task_id} not found", code="NOT_FOUND", http_status=404)
        return rec

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float = 600.0,
        interval: float = 10.0,
        on_poll=None,
    ) -> dict:
        """轮询到终态。

        🔴 **每一次轮询 GET 都包在 `suppress_http()` 里**（闸门 1：产生层）。
        站点查询是只读且被高频重复的：按"一次调用一条 span"记账，一个任务就是几十上百条
        内容几乎相同的 span。明细改由调用方（`web_queue._watch`）在**一条**覆盖全程的
        span 上以「事件 + 指标」承载 —— 量从"每次一条"降到"每任务一条"。

        `on_poll(status)` 每次轮询后被调一次（含还没进终态的那些）。
        🔴 **它抛异常不允许影响盯梢**：埋点坏掉不许把"放槽位"一起赔进去 —— 槽位是
        闸门，卡住就是整条线停摆（实测过的故障模式）。
        """
        t0 = time.time()
        last: dict | None = None
        while time.time() - t0 < timeout:
            try:
                with suppress_http():
                    last = self.get_task(task_id)
            except WebApiError as e:
                if e.code == "NOT_FOUND":
                    raise
                last = None
            status = str((last or {}).get("taskStatus") or "").lower()
            if on_poll is not None:
                try:
                    on_poll(status)
                except Exception as e:  # noqa: BLE001 —— 埋点失败绝不影响业务
                    logger.debug(f"on_poll 回调失败（不影响盯梢）：{e}")
            if status in TERMINAL:
                return {
                    "done": True,
                    "ok": status in SUCCESS,
                    "status": status,
                    "task": last,
                    "ms": int((time.time() - t0) * 1000),
                }
            time.sleep(interval)
        return {
            "done": False,
            "ok": False,
            "status": str((last or {}).get("taskStatus") or "") or None,
            "task": last,
            "ms": int((time.time() - t0) * 1000),
        }

    # -------------------------------------------------------------- upload ----

    def upload_file(self, source: bytes | str, *, name: str | None = None, permanent: bool = False) -> dict:
        """把媒体转存到站点自己的 CDN，返回 `publicUrl` 等元信息。

        为什么必须转存：`ai.minimaxH3` 会拒绝 Content-Type 不在白名单里的外链
        （`Unsupported upload content type`）。走 `uploads.getPresignedUrl`
        （注意路由名是**复数** `uploads`）拿到的 `static.img2video.ai` 地址一定被接受。

        预签名 URL 只有 **60 秒**有效期 —— 拿到就传，别缓存。
        """
        if isinstance(source, (bytes, bytearray)):
            buf = bytes(source)
            base = re.sub(r"\.[^.]+$", "", name or "file")
        elif isinstance(source, str) and re.match(r"^https?://", source, re.I):
            started = time.perf_counter()
            try:
                r = self._http.get(source, timeout=60.0)
            except httpx.HTTPError as e:
                note_upstream(
                    "download",
                    upstream="web",
                    request={"url": source},
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                raise
            note_upstream(
                "download",
                upstream="web",
                request={"url": source},
                response={"bytes": len(buf)},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            tail = source.split("?")[0].rstrip("/").split("/")[-1]
            base = re.sub(r"\.[^.]+$", "", unquote(tail) or "file") or "file"
        else:
            raise ParamError("upload_file: source must be bytes or an http(s) URL")

        info = sniff_file(buf)  # 按真实字节，不信扩展名
        width, height = image_dimensions(buf) if info["kind"] == "image" else (0, 0)
        file_name = f"{base}.{info['ext']}"

        pre = self.trpc(
            "uploads.getPresignedUrl",
            {"fileName": file_name, "contentType": info["content_type"], "fileSize": len(buf), "permanent": permanent},
            method="POST",
        ) or {}

        max_bytes = pre.get("maxBytes")
        if max_bytes and len(buf) > max_bytes:
            raise ParamError(
                f"{info['kind']} too large: {len(buf)} bytes > maxBytes {max_bytes} "
                f"({int(max_bytes) // 1048576}MB for {info['content_type']})",
                "input_reference",
            )

        upload_url = pre.get("uploadUrl")
        if not upload_url:
            raise WebApiError("uploads.getPresignedUrl", f"no uploadUrl in response: {json.dumps(pre)[:200]}")

        put_view = {"url": upload_url, "fileSize": len(buf), "contentType": info["content_type"]}
        started = time.perf_counter()
        try:
            put = self._http.put(
                upload_url,
                headers={
                    **(pre.get("headers") or {}),
                    "Content-Type": info["content_type"],
                    "Content-Length": str(len(buf)),
                },
                content=buf,
                timeout=120.0,
            )
        except httpx.HTTPError as e:
            note_upstream(
                "uploads.PUT",
                upstream="web",
                request=put_view,
                status="error",
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise WebApiError("uploads.PUT", f"{type(e).__name__}: {e}") from None
        note_upstream(
            "uploads.PUT",
            upstream="web",
            request=put_view,
            response={
                "http_status": put.status_code,
                "body": put.text[:300],
                "publicUrl": pre.get("publicUrl"),
            },
            status="ok" if put.status_code < 400 else "error",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        if put.status_code >= 400:
            raise WebApiError("uploads.PUT", f"{put.status_code}: {put.text[:300]}", http_status=put.status_code)

        return {
            "publicUrl": pre.get("publicUrl"),
            "contentType": info["content_type"],
            "kind": info["kind"],
            "size": len(buf),
            "fileName": file_name,
            "width": width,
            "height": height,
        }

    def upload_image(self, source: bytes | str, **opts) -> dict:
        r = self.upload_file(source, **opts)
        if r["kind"] != "image":
            raise ParamError(f"upload_image: expected an image, got {r['contentType']}")
        return r

    # ------------------------------------------------------------ download ----

    def download(self, url: str, out_path: str) -> str:
        try:
            r = self._http.get(url, timeout=300.0)
        except httpx.HTTPError as e:
            raise WebApiError("download", f"{type(e).__name__}: {e}") from None
        if r.status_code >= 400:
            raise WebApiError("download", f"{r.status_code} for {url[:120]}", http_status=r.status_code)
        Path(out_path).write_bytes(r.content)
        return out_path
