"""OpenAI `/v1/videos` 兼容面的纯翻译层（零网络、零副作用）。

对齐的契约是 Chatfire（oneapis.apifox.cn）「OpenaiVideos格式 / Seedance 豆包即梦」
的两份 OpenAPI（369966278 创建 / 369966279 查询）：

    POST /v1/videos        表单或 JSON → 200 {id, object, status, created_at}
    GET  /v1/videos/{id}   → 200 {id, object, status, progress, video_url, created_at}

「务必一样」：对外**只**返回契约里声明的字段，一个不多 —— 调用方按 Chatfire 文档
写的解析代码必须原样可用。内部仍走 `translate.create_translate` 的完整翻译
（截断留痕 / 计费口径 / 前置拒绝），两条入口共享同一条纪律。

字段对应关系（OpenAI 形 → Ark 形）：

    model     → 原样保留为请求模型；`_480p/_720p/_1080p` 后缀额外决定分辨率档位
    prompt    → content[] 里的 text 项
    seconds   → duration（整数秒；越界由 translate 就近钳制并告警）
    size      → ratio（比例枚举原样；`keep_ratio` 与 `adaptive` 上游语义相同 = 跟随输入图）
    input_reference       → content[] 的 reference_image 项（1-4 张的上限截断由 translate 负责）
    first_frame_image     → content[] 的 first_frame 帧通道
    last_frame_image      → content[] 的 last_frame 帧通道

首帧 / 首尾帧 / 参考图三种场景互斥（上游硬约束），混用由 translate_create 前置 400。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from fractions import Fraction
from typing import Any, Mapping

from .errors import ParamError
from .sniff import sniff_file

OPENAI_VIDEOS_PATH = "/v1/videos"

# model 名自带分辨率档位（Chatfire 枚举：doubao-seedance-1-0-pro_1080p 一类）。
_RESOLUTION_SUFFIX_RE = re.compile(r"_(480p|720p|1080p)\s*$", re.I)

# size 枚举 → Ark ratio。keep_ratio / adaptive 都表示"由输入图决定"，上游不区分二者。
_SIZE_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")
_SIZE_ALIASES = {"adaptive": "adaptive", "keep_ratio": "adaptive"}

# 宽 x 高（OpenAI Sora 风格的 size，如 1920x1080）→ 最简整数比。
_WXH_RE = re.compile(r"^(\d{2,5})\s*[xX×]\s*(\d{2,5})$")

# Ark 状态 → OpenAI 状态词表（OpenAI Videos 家族：queued / in_progress / completed / failed）。
_STATUS_TO_OPENAI = {
    "queued": "queued",
    "running": "in_progress",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "failed",
}

KNOWN_CREATE_FIELDS = frozenset(
    {"model", "prompt", "seconds", "size", "input_reference", "first_frame_image", "last_frame_image"}
)


def decode_media_value(value: str, field: str, b64_mode: bool) -> str:
    """把调用方给的媒体值统一成 translate 认识的形态（URL 或 data URI）。

    - `http(s)://` / `data:` 原样放行；
    - `input-reference-format: b64` 时，裸值按 base64 解码，magic bytes 嗅探 MIME 后
      包成 data URI（转存管线会解出字节再传站点 CDN）；
    - 其余（既非 URL 也未声明 b64）原样透传，让上游给出明确的报错 —— 不猜。
    """
    s = str(value or "").strip()
    if not s:
        raise ParamError(f"{field} is empty", field)
    if s.startswith(("http://", "https://", "data:")):
        return s
    if b64_mode:
        try:
            buf = base64.b64decode(re.sub(r"\s+", "", s), validate=True)
        except (binascii.Error, ValueError):
            raise ParamError(f"{field}: invalid base64 payload", field) from None
        mime = sniff_file(buf)["content_type"]
        if mime == "application/octet-stream":
            mime = "image/png"
        return f"data:{mime};base64," + base64.b64encode(buf).decode()
    return s


def _ratio_from_wxh(size: str) -> str | None:
    """`1920x1080` → `16:9`。约不进 size 枚举的（如 12:5）返回 None，由调用方报错。"""
    m = _WXH_RE.match(size.replace("×", "x"))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    f = Fraction(w, h)
    ratio = f"{f.numerator}:{f.denominator}"
    return ratio if ratio in _SIZE_RATIOS else None


def size_to_ratio(size: str) -> tuple[str, list[str]]:
    """size 字段 → Ark ratio，附映射说明（被改写的一定留痕）。"""
    s = str(size or "").strip()
    if not s:
        return "", []
    if s in _SIZE_RATIOS:
        return s, []
    if s in _SIZE_ALIASES:
        mapped = _SIZE_ALIASES[s]
        if s == mapped:
            return mapped, []
        # keep_ratio → adaptive：两者对上游是同一件事（不设 aspectRatio = 跟随输入图）
        return mapped, [f'size="{s}" mapped to ratio="{mapped}" (the upstream has a single '
                        "image-derived behavior; the distinction does not exist upstream)"]
    wxh = _ratio_from_wxh(s)
    if wxh:
        return wxh, [f'size="{s}" mapped to ratio="{wxh}"']
    return s, []  # 未知值原样传入，translate 的 ratio 枚举校验会给出 400


def _seconds_to_duration(raw: Any) -> int | None:
    """seconds 字段 → 整数秒。表单里永远是字符串，JSON 里通常是整数。"""
    if raw is None or raw == "":
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        raise ParamError(f"seconds: expected an integer number of seconds, got {raw!r}", "seconds") from None
    if f != int(f):
        raise ParamError(f"seconds: expected an integer number of seconds, got {raw!r}", "seconds")
    return int(f)


def _url_of(item: Any) -> str:
    """input_reference 的元素形态兼容：url 字符串 / {url} / OpenAI 官方的 {image_url:{url}}。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        if item.get("url"):
            return str(item["url"]).strip()
        holder = item.get("image_url")
        if isinstance(holder, Mapping) and holder.get("url"):
            return str(holder["url"]).strip()
        if isinstance(holder, str) and holder:
            return holder.strip()
    return ""


def _listish(value: Any, field: str) -> Any:
    """把「数组被写成了字符串」救回来；救不回来就**明确拒**（2026-09-15）。

    Chatfire 的 curl 里很常见的写法是 `--form 'input_reference=["u1","u2"]'` —— 值是**一个
    JSON 字符串**而不是重复的部件。原样透传会变成 `referenceImageUrls` 里的**一个垃圾项**
    （既不是 URL 也不是 data URI），一路静默到转存阶段才炸，而调用方完全看不出是自己
    把数组写成了字符串（实测：`warnings` 里一个字都没有）。

    只认以 `[` / `{` 开头的串 —— URL 与裸 base64 都不会这么开头，所以不误伤。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s.startswith(("[", "{")):
        return value
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        raise ParamError(
            f"{field}: 看着像要传 JSON 数组/对象，但不是合法 JSON：{s[:80]!r} —— "
            f"多值请在表单里**重复该字段**（`--form a --form a`），或改用 JSON body",
            field,
        ) from None
    return parsed if isinstance(parsed, list) else [parsed]


def _frame_url_of(value: Any, field: str) -> str:
    """帧字段（`first_frame_image` / `last_frame_image`）的取值 —— 🔴 **不许静默丢素材**。

    `_url_of` 对认不出的形态（数组、数字、`{"foo":1}`）返回空串，而调用方那边看起来是
    "提交成功、只是没有首帧" —— **参考素材静默丢失是本项目的红线之一**（实测：给成数组时
    图片项为 0 且 notes 为空）。所以这里改成**明确拒**，并说清该给什么形态。
    """
    url = _url_of(value)
    if url:
        return url
    raise ParamError(
        f"{field}: 无法识别这个取值（{type(value).__name__}）—— 请给 URL 字符串、"
        f"{{'url': ...}} 或 {{'image_url': {{'url': ...}}}}；**帧字段不接受数组**"
        f"（多张参考图请用 input_reference）",
        field,
    )


def ark_body_from_openai(fields: Mapping[str, Any], *, reference_format: str = "") -> tuple[dict, list[str]]:
    """OpenAI /v1/videos 创建字段 → `(Ark 请求体, notes)`。

    `notes` 是"字段被改写/被忽略"的说明，调用方必须并进 warnings —— 映射不静默。
    不在这里调用 translate_create：它要重用 app 层同一份入口（计费、dry-run、
    前置拒绝都只在一处）。
    """
    notes: list[str] = []

    unknown = sorted(k for k in fields.keys() if str(k) not in KNOWN_CREATE_FIELDS)
    for k in unknown:
        notes.append(f'"{k}" is not part of the OpenAI /v1/videos (Seedance) contract and was ignored')

    model = str(fields.get("model") or "").strip()
    if not model:
        raise ParamError("model is required", "model")

    prompt = str(fields.get("prompt") or "").strip()
    if not prompt:
        raise ParamError("prompt is required", "prompt")

    # model 后缀决定分辨率档位（doubao-seedance-1-0-pro_1080p → 1080p）。
    resolution = None
    m = _RESOLUTION_SUFFIX_RE.search(model)
    if m:
        resolution = m.group(1).lower()

    duration = _seconds_to_duration(fields.get("seconds"))

    ratio, size_notes = size_to_ratio(fields.get("size"))
    notes.extend(size_notes)

    b64_mode = str(reference_format or "").strip().lower() == "b64"

    refs_raw = fields.get("input_reference")
    if refs_raw is None or refs_raw == "":
        refs_raw = []
    if isinstance(refs_raw, (str, Mapping)):
        refs_raw = [refs_raw]
    if not isinstance(refs_raw, list):
        refs_raw = [refs_raw]

    # 归一成一个**扁平**列表。三种输入形态都要能落地：
    #   · JSON body 给数组（最正常）
    #   · 表单里的**重复部件**（`input_reference` 天生就是一个列表）
    #   · 「数组写成了字符串」—— `--form 'input_reference=["u1","u2"]'`
    # 🔴 第三种可能出现在**顶层**（JSON body 里写成字符串）或**元素上**（表单里天生是列表），
    #    两处都要救、都要摊平、都要**留痕**。实测踩过：只救顶层时 form 路径照样把整串当成
    #    一个垃圾 URL 静默带走，而直接调翻译层的单测却是绿的（盲区就在这一层）。
    items: list = []
    rescued_entries = 0
    for i, item in enumerate(refs_raw):
        rescued = _listish(item, f"input_reference[{i}]")
        if isinstance(rescued, list):
            items.extend(rescued)
            rescued_entries += len(rescued)
        else:
            items.append(rescued)
    if rescued_entries:
        notes.append(
            f"input_reference was given as a JSON string and was parsed into {rescued_entries} "
            f"entries — prefer repeating the form field "
            f"(`--form input_reference=...` once per file) or a JSON body"
        )

    ref_urls: list[str] = []
    for i, item in enumerate(items):
        url = _url_of(item)
        if url:
            ref_urls.append(decode_media_value(url, f"input_reference[{i}]", b64_mode))

    def _frame_media(key: str) -> str:
        # 先救 JSON 字符串形态，再由 `_frame_url_of` **明确拒**认不出的形态（不静默丢）
        raw = _listish(fields.get(key), key)
        if raw in (None, "") or raw == []:
            return ""
        url = _frame_url_of(raw, key)
        return decode_media_value(url, key, b64_mode) if url else ""

    first_frame = _frame_media("first_frame_image")
    last_frame = _frame_media("last_frame_image")

    content: list[dict] = [{"type": "text", "text": prompt}]
    for u in ref_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})
    if first_frame:
        content.append({"type": "image_url", "role": "first_frame", "image_url": {"url": first_frame}})
    if last_frame:
        content.append({"type": "image_url", "role": "last_frame", "image_url": {"url": last_frame}})

    body: dict[str, Any] = {"model": model, "content": content}
    if resolution:
        body["resolution"] = resolution
    if ratio:
        body["ratio"] = ratio
    if duration is not None:
        body["duration"] = duration
    return body, notes


def openai_task_view(view: Mapping[str, Any], *, created_at_fallback: int | None = None) -> dict:
    """内部任务视图 → Chatfire `/v1/videos/{id}` 契约形状（**恰好六个字段**）。

    - status：queued / in_progress / completed / failed（OpenAI Videos 家族词表）；
    - progress：终态成功 100，其余 0 —— 上游没有可读的百分比，不编造中间值；
    - video_url：仅 completed 且真有产出时给值，其余为 null；
    - created_at：epoch 秒；上游取不到时回退到**本服务创建记录**的时间。
    """
    status = _STATUS_TO_OPENAI.get(str(view.get("status") or "queued"), "queued")
    completed = status == "completed"
    video_url = (view.get("content") or {}).get("video_url") if completed else None
    created_at = view.get("created_at")
    if not isinstance(created_at, int) or created_at <= 0:
        created_at = created_at_fallback
    return {
        "id": view.get("id"),
        "object": "video",
        "status": status,
        "progress": 100 if completed else 0,
        "video_url": video_url or None,
        "created_at": created_at,
    }
