"""成片对外出口：把上游直链换成本服务自己的下载地址，取用时**流式代理**转发。

为什么必须存在
==============

上游（网页端）返回的成片是一个**公开直链**，实测形态（2026-09-16 取证）：

    https://static2.img2video.ai/1789479777409-…-1635002_0_minimax_h3_1635002.mp4
            └────── 域名：上游服务商 ──────┘                 └─ 上游实际模型名 ─┘

原样透传，等于一次泄露三样东西：

① **域名** —— 直接指向上游服务商；
② **路径里的文件名** —— 内嵌 `minimax_h3`，即上游**实际执行**的模型
   （站点对调用方请求的模型名只是回显，见 `translate.normalize_web_task`）；
③ 🔴 **响应头 `content-disposition`** —— 上游回的是
   `attachment; filename="1635002_0_minimax_h3_1635002.mp4"`。这一条最隐蔽：
   **即使只把 URL 换成自己的，只要还在转发上游响应头，模型名就跟着每一次下载暴露**。
   所以本模块对响应头做**白名单**：`server` / `cf-ray` / `nel` / `report-to` 这些
   同样能指认上游的头一律不透传。

对外形态
========

    {AVM_PUBLIC_BASE}/v/cgt-20260916-a1b2c3d4.mp4

- 基址是本服务的地址，路径里是**本服务的任务 id** —— 读不出任何上游信息；
- 链接**立即有效**，不依赖任何搬运动作是否完成（任务照实报 `succeeded`，
  不引入"succeeded 但拿不到链接"的自相矛盾中间态）；
- 下载时才回源（用户 2026-09-16 口径："下载才触发转存"）：没被取用的成片
  不占任何存储与出口带宽。

⚠️ **为什么不加密拼接 token**：脱敏的要害是"URL 里不含上游信息"，不透明 id 已经
做到；而加密要额外付三笔成本 —— **不可撤销**（除非轮换密钥，那会作废全部已发链接）、
**无法与鉴权绑定**、**密钥即全局命门**（泄漏即全量泄漏）。它唯一换来的"无状态"
在本服务已有一张任务表（`store.py`，7 天窗口）时没有价值。将来若真要做成
**不能持久化**的独立服务，才需要回到 token 方案。

⚠️ **绝不提供"传任意 URL 给我换一个链接"的接口**：那等于开放代理 + SSRF，
还白送别人用你的带宽与出口 IP 做转存。下载地址只能由服务**自己**在观测到任务
成功时给出（`app._apply_media_gate`），调用方无法指定源地址。
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlsplit

import httpx
from loguru import logger

DEFAULT_PATH = "/v"
DEFAULT_EXT = ".mp4"
_FALLBACK_CONTENT_TYPE = "video/mp4"
_USER_AGENT = "aivideomaker-media-proxy/1"

# 只透传这几个头 —— 白名单而非黑名单：上游以后新增什么头都不会顺着这条管子漏给
# 调用方（`cf-ray` / `nel` / `report-to` / `server` 都曾在上游响应里出现过）。
_PASS_THROUGH = (
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
)


# 只认这几个后缀。⚠️ 刻意**不是**"取到什么就用什么"：这个值会出现在**对外 URL** 里
# （`/v/{ark_id}{ext}`），从上游地址派生出一个任意后缀（`.php` / `.exe`）既可能被
# 客户端当成别的东西，也让对外地址多带一分上游痕迹。白名单之外一律归 `.mp4`。
_ALLOWED_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv")


def extension_of(url: str) -> str:
    """从上游地址取扩展名 —— **只认白名单后缀**，其余归 `.mp4`。

    绝不复用上游文件名：那里面就有模型名（`…_0_minimax_h3_….mp4`）。
    """
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        if ext in _ALLOWED_EXTS:
            return ext
    return DEFAULT_EXT


class MediaSourceError(Exception):
    """回源失败（连不上 / DNS / 超时 / 上游报错）。

    刻意**不**把上游原文往上抛：出口那一跳只报一句通用描述。`transport` 只留给
    trace 与日志（与 `WebApiError.transport` 同一条纪律 —— 调用方需要知道"是超时
    还是连不上"，但那句话里不该有内部标识）。
    """

    def __init__(self, message: str, *, transport: str = "", http_status: int = 0):
        super().__init__(message)
        self.transport = transport
        self.http_status = http_status


class UpstreamStream:
    """一次上游读取。`close()` 幂等 —— 生成器 `finally` 与响应背景任务都会调它。"""

    def __init__(self, client: httpx.Client, ctx, resp: httpx.Response):
        self._client = client
        self._ctx = ctx
        self._resp = resp
        self._closed = False

    @property
    def status_code(self) -> int:
        return self._resp.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self._resp.headers

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._resp.iter_bytes(64 * 1024)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 关闭失败不该冒到调用方
            pass
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


class MediaProxy:
    """成片下载出口。未配置 `public_base` 时 `enabled` 为 False ⇒ 行为与从前一致。"""

    def __init__(
        self,
        *,
        public_base: str = "",
        path: str = DEFAULT_PATH,
        trust_env: bool = True,
        connect_timeout: float = 15.0,
        read_timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.public_base = str(public_base or "").strip().rstrip("/")
        # 仅为测试注入（`httpx.MockTransport`）。生产恒为 None ⇒ 真实网络。
        # 可测性在这里是必需的：下载端点会发绝对 URL 的出站请求，而"把上游指到死端口"
        # 那套挡不住它（见 tools/egress_audit.py 的说明）。
        self._transport = transport
        self.path = "/" + str(path or DEFAULT_PATH).strip().strip("/")
        self.trust_env = bool(trust_env)
        # ⚠️ 不用"总超时"：成片可能有几百 MB，一个大文件完全可能超过任何固定总时长
        #    （那会变成"文件越大越容易失败"）。用**空闲超时**：两次读到字节之间超过
        #    `read_timeout` 才算卡死 —— 语义恰好是"连接没在动"。
        self.timeout = httpx.Timeout(
            connect=max(1.0, float(connect_timeout)),
            read=max(1.0, float(read_timeout)),
            write=30.0,
            pool=10.0,
        )

    # ---- 配置 --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.public_base)

    def describe(self) -> dict:
        """`/healthz` 用。只报形态，不含任何凭据。"""
        return {
            "enabled": self.enabled,
            "public_base": self.public_base,
            "path": self.path,
        }

    # ---- 地址 --------------------------------------------------------------

    def download_url(self, ark_id: str, source_url: str = "") -> str:
        """对外地址：`{public_base}{path}/{ark_id}{ext}` —— 三段全是我们自己的。"""
        ext = extension_of(source_url) if source_url else DEFAULT_EXT
        return f"{self.public_base}{self.path}/{ark_id}{ext}"

    def matches(self, url: str) -> bool:
        return bool(self.public_base) and str(url or "").startswith(
            f"{self.public_base}{self.path}/"
        )

    # ---- 取用 --------------------------------------------------------------

    def open(self, source_url: str, *, range_header: str = "") -> UpstreamStream:
        """连上游并返回可分块读取的流。`Range` 原样透传（播放器会发它）。

        异常（连不上 / DNS / 超时）由 `httpx` 抛给调用方 —— 出口那一跳负责转成
        对外的网关错误，且**不把上游原文带出去**。
        """
        headers = {"user-agent": _USER_AGENT}
        if range_header:
            headers["range"] = range_header
        client = httpx.Client(
            timeout=self.timeout,
            trust_env=self.trust_env,
            follow_redirects=True,
            headers=headers,
            transport=self._transport,
        )
        try:
            ctx = client.stream("GET", source_url)
            resp = ctx.__enter__()
        except httpx.HTTPError as e:
            # 传输层异常（连不上 / DNS / 超时）：类名交给上层归因，报文保持通用
            client.close()
            raise MediaSourceError(
                "media source unreachable", transport=type(e).__name__
            ) from None
        except BaseException:
            client.close()
            raise
        return UpstreamStream(client, ctx, resp)

    def out_headers(self, stream: UpstreamStream, *, ark_id: str, ext: str = DEFAULT_EXT) -> dict:
        """对外响应头。**白名单**：既脱敏，也顺手甩掉与调用方无关的噪声。"""
        upstream_type = str(stream.headers.get("content-type") or "").strip()
        out = {
            # 上游给的是 `video/mp4` 这类通用类型，不含标识信息，可用；否则按扩展名兜底。
            "content-type": upstream_type or _FALLBACK_CONTENT_TYPE,
            # ★ 脱敏第三面：上游的 `content-disposition` 里带着模型名，我们自己写一份。
            "content-disposition": f'attachment; filename="{ark_id}{ext}"',
        }
        for name in _PASS_THROUGH:
            value = stream.headers.get(name)
            if value:
                out[name] = value
        return out
