"""Ark ↔ aivideomaker 官方 API 的纯翻译层。

**全部是纯函数，零外部依赖、零网络。** 这是整个兼容层的核心逻辑，也是测试
真正该压的地方——FastAPI / logfire 只是把它包成服务。

翻译方向：
    Ark create body  →  official {model, payload}
    官方 Task 信封     →  Ark task object

三个官方模型对同一语义字段的类型要求并不一致，这是官方线 INVALID_PAYLOAD
最常见的原因：

    model        duration   resolution              ratio 字段
    seedance20   number     480 | 720   (number)    ratio
    minimax      integer    "720p" | "1080p"        aspectRatio
    t2v / i2v    string     —                       aspectRatio
    wan27        string     "720P" | "1080P"        ratio
    happyhorse   string     "720P" | "1080P"        （无）

Seedance 2.5「全能参考」的三条专属字段（`omni_reference_task_type` /
`output_format` / `generate_audio`）在这里是**建模**的，不是丢进 unsupported
了事：能承接的承接、上游明确不具备的进 `incompatible`（真实提交前 400、
dry-run 仍可零成本看到）、只是弱化的进 `warnings`。

参考素材按上游标称上限**截断**（图 4 / 视频 1 / 音频 2），但**每一次截断都留痕**：
告警里写明丢了几项、丢的是哪些 URL，并点名因此悬空的提示词占位符（`@视频n`）。
截断而不留痕，是本项目最贵的一类缺陷 —— 调用方会以为整份素材都生效了。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Mapping

from .errors import ParamError

# 官方 supportedModels（GET /api/v1/account）
OFFICIAL_MODELS: tuple[str, ...] = (
    "t2v", "i2v", "minimax", "t2v_v3", "i2v_v3", "seedance20", "wan27", "happyhorse",
)

ARK_RATIOS = frozenset({"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"})
ARK_RESOLUTIONS = frozenset({"480p", "720p", "1080p"})

# 站点侧时长约束 = **连续秒数 + 上限**，不是离散档位。依据（全部零成本取证）：
#   1. 站点 UI 文案 `videoDurationMaxWarnTip` = "Video duration must not exceed {seconds} seconds."
#      ⇒ 站点自己描述的是"不得超过 N 秒"，而非"只能取某几个值"；
#   2. 站点侧实测：720p 的 5/6/7/8/9/10/11/14 秒**全部成功**（连续整数，见 TESTCASES §10）；
#   3. 计费文案按秒计（"15/20 秒按 3 积分/秒"），进一步说明时长是连续量。
#
# ⚠️ 曾经把 480p 写成 `[5, 10, 15, 20]`（一张来源不明的"档位表"）—— 那会让 `480p/8s`
#    （本就在免费线内）被就近吸附到 **10s**，从而**跨进计费区**。这类"静默改档 + 变贵"
#    正是适配层最该避免的。已按站点真实行为改成连续区间。
# 官方 seedance20 的真实区间未验证，沿用此表并给出提示。
DURATION_ALLOWED: dict[str, list[int]] = {
    "480p": list(range(5, 21)),
    "720p": list(range(5, 21)),
    "1080p": list(range(5, 21)),
}
FREE_MAX_DURATION = 8  # web 线的免费边界；官方线不存在，仅用于沿用档位时提示

# Seedance 2.5 的「全能参考」任务类型。
#   auto / reference  普通全模态生成与参考驱动 —— 站点侧有对应能力
#   edit / extend     视频编辑与延长 —— 需要上游具备编辑/延长能力，
#                     而两条上游都没有（web 线是 minimax-H3 文生/图生/参考）
OMNI_TASK_TYPES = ("auto", "reference", "edit", "extend")
OMNI_TYPES_NEEDING_ABSENT_CAPS = ("edit", "extend")

# 输出容器。站点实测成片 URL 一律 `.mp4`；`mov` 是 2.5 才引入的高色深容器。
OUTPUT_FORMATS = ("mp4", "mov")
UPSTREAM_OUTPUT_FORMAT = "mp4"

# 上游站点为参考素材标称的上限（中英双份一致，取自
# docs/web-reverse/captured/docs-{zh,en}.html）：
#     "参考图片（最多 4 张）" / "Reference images (up to 4)"
#     "参考视频（最多 1 个）" / "Reference video (up to 1)"
#     "参考音频（最多 2 条）" / "Reference audio (up to 2)"
# **适配层按这些上限截断**：超出部分发过去也不生效。但截断**必须留痕** ——
# 每一项被丢弃的素材都要出现在 warnings 里（含 URL），绝不静默丢。
UPSTREAM_REF_LIMITS: dict[str, tuple[str, int]] = {
    "reference_image": ("参考图片/reference_image", 4),
    "reference_video": ("参考视频/reference_video", 1),
    "reference_audio": ("参考音频/reference_audio", 2),
}

# Ark 参数里站点侧无法承接的，列进 unsupported 而不是静默丢弃。
# 注意：`generate_audio` / `omni_reference_task_type` / `output_format` 已**移出**本表 ——
# 它们现在是被建模的字段（见 translate_create），不再只是"回显但无效"。
PASSTHROUGH_UNSUPPORTED = (
    "watermark", "seed", "camera_fixed", "return_last_frame",
    "draft", "service_tier", "priority", "callback_url", "safety_identifier",
    "tools", "execution_expires_after",
)

# 官方 TaskStatus → Ark 状态枚举
STATUS_TO_ARK = {
    "submitted": "queued",
    "progress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancel": "cancelled",
    "cancelled": "cancelled",
}

BUDGET_REQUIRED_MESSAGE = (
    "official upstream is billed on submit — refuse to guess. Provide a spend cap via "
    "`extra_body.aivideomaker_max_credits` (per request) or `AVM_OFFICIAL_MAX_CREDITS` (env). "
    "Both are forwarded as X-Max-Credits and are checked BEFORE billing."
)

# 提示词里的素材占位引用：`@图像1` / `@视频2` / `@音频1`。
# 编号是 **1-based**，对应 `content[]` 里同类素材的出现顺序。
_REF_TOKEN_RE = re.compile(r"@\s*(图像|视频|音频)\s*(\d+)")


def pick_official_model(ark_model: str, override: str | None = None) -> str:
    """Ark/Seedance 模型名 → 官方模型名。

    默认落 `seedance20`：这条线的用途就是用 Ark/Seedance 形状驱动官方 API，
    而 seedance20 是官方唯一暴露的 Seedance 入口。
    可用 `extra_body.aivideomaker_official_model` 或环境变量 `AVM_OFFICIAL_MODEL` 覆盖。
    """
    if override:
        return str(override).strip()
    m = str(ark_model or "").strip()
    if re.search(r"seedance", m, re.I):
        return "seedance20"
    if re.search(r"hailuo|minimax", m, re.I):
        return "minimax"
    if re.search(r"wan", m, re.I):
        return "wan27"
    if re.search(r"happyhorse", m, re.I):
        return "happyhorse"
    if re.match(r"^i2v", m, re.I):
        return "i2v"
    if re.match(r"^t2v", m, re.I):
        return "t2v"
    return "seedance20"


def _numeric_resolution(v: Any) -> int | None:
    m = re.search(r"(\d{3,4})", str(v or ""))
    return int(m.group(1)) if m else None


def snap_duration(duration: Any, resolution: str, prefer_free: bool = False) -> int:
    """把时长吸附到该分辨率的合法区间 `[5, 20]`。

    站点的约束是"连续秒数 + 上限"，所以绝大多数请求**原样透传**（没有离散档位），
    只有越界值才会被就近钳制。

    `prefer_free=True` 是**省钱开关**，语义为"别越过免费线"：凡是超过
    `FREE_MAX_DURATION` 的合法时长（例如 12s）都会被主动拉回 8s —— 而不只是
    在越界时才生效。官方线没有免费区，该开关只影响档位选择。
    """
    allowed = DURATION_ALLOWED.get(resolution) or [5]
    try:
        d = int(duration)
    except (TypeError, ValueError):
        d = 5
    if prefer_free and d > FREE_MAX_DURATION:
        fits = [a for a in allowed if a <= FREE_MAX_DURATION]
        if fits:
            return max(fits)
    if d in allowed:
        return d
    return min(allowed, key=lambda a: (abs(a - d), a))


def ref_indices_in_prompt(prompt: str) -> dict[str, list[int]]:
    """抽出提示词里的 `@图像n` / `@视频n` / `@音频n` 引用编号（1-based，去重保序）。

    上游按 `content[]` 里同类素材的**出现顺序**解析这些占位符。素材被截断后，
    超出范围的编号会指向不存在的素材；调用方写错编号也有同样后果 —— 两种都必须
    被点名，否则就是"参考了错误的素材"，且极难归因。
    """
    out: dict[str, list[int]] = {"图像": [], "视频": [], "音频": []}
    for m in _REF_TOKEN_RE.finditer(str(prompt or "")):
        kind, num = m.group(1), int(m.group(2))
        if num not in out[kind]:
            out[kind].append(num)
    return out


def _prompt_ref_warnings(prompt: str, groups: Mapping[str, list[str]]) -> list[str]:
    """提示词引用了不存在的素材 → 明确告警（否则素材会"参考错"，极难归因）。"""
    labels = {"图像": "reference_image", "视频": "reference_video", "音频": "reference_audio"}
    found = ref_indices_in_prompt(prompt)
    warnings: list[str] = []
    for kind, nums in found.items():
        available = len(groups.get(labels[kind]) or [])
        dangling = sorted({n for n in nums if n < 1 or n > available})
        if dangling:
            shown = ", ".join(f"@{kind}{n}" for n in dangling)
            warnings.append(
                f"prompt references {shown} but only {available} {labels[kind]} asset(s) are forwarded — "
                "those placeholders cannot resolve"
            )
    return warnings


def _truncate_references(urls: list[str], kind: str) -> tuple[list[str], list[str]]:
    """按上游标称上限截断参考素材，返回 `(保留, 被丢弃)`。

    上限取自 `UPSTREAM_REF_LIMITS`（站点 UI 文案，中英双份一致）。这里只做纯粹的
    分割 —— **告警由调用方负责写**，且必须写，见 `translate_create`。
    """
    limit = UPSTREAM_REF_LIMITS[kind][1]
    return urls[:limit], urls[limit:]


def to_official_body(model: str, params: Mapping[str, Any], warnings: list[str]) -> dict:
    """web 形状的中间参数 → 官方模型期望的请求体。"""
    resolution = str(params.get("resolution") or "720p").strip()
    num_res = _numeric_resolution(resolution)
    duration = params.get("duration", 5)
    ratio = params.get("aspectRatio")
    if ratio == "auto":
        ratio = None

    if model == "seedance20":
        r = num_res if num_res is not None else 720
        if r > 720:
            warnings.append(f"seedance20 only supports resolution 480/720; {resolution} downgraded to 720")
            r = 720
        elif r not in (480, 720):
            warnings.append(f"seedance20 resolution {r} is outside {{480,720}}; forwarded as-is")
        body: dict[str, Any] = {
            "prompt": str(params.get("content") or ""),
            "duration": int(duration),
            "resolution": r,
        }
        if ratio:
            body["ratio"] = ratio
        # 官方 OpenAPI 只详述了 minimax 的 schema，seedance20 的图像字段名未经实证。
        # 按 i2v 的命名转发并明确告警，而不是静默丢弃。
        if params.get("imageUrl"):
            body["image"] = params["imageUrl"]
            warnings.append("seedance20 image field is unverified (published schema covers minimax only)")
        if params.get("lastFrameUrl"):
            body["lastFrameImage"] = params["lastFrameUrl"]
            warnings.append("seedance20 lastFrameImage field is unverified")
        refs = params.get("referenceImageUrls") or []
        if refs:
            # 素材已在 translate_create 按上游上限截断，这里不再二次裁剪。
            body["referenceImages"] = list(refs)
            warnings.append("seedance20 referenceImages field is unverified")
        # 参考视频：原来完全没有转发（一并被丢掉）。按单槽位字段转发，并标注字段名
        # 未经实证 —— 官方 API 的 seedance20 schema 是黑盒。
        if params.get("referenceVideoUrl"):
            body["referenceVideoUrl"] = params["referenceVideoUrl"]
            warnings.append(
                "seedance20 referenceVideoUrl field is unverified (published schema covers minimax only)"
            )
        warnings.append("duration/resolution 档位沿用的是站点侧实测表；官方 seedance20 的真实档位未验证")
        return body

    if model == "minimax":
        lower = resolution.lower()
        if lower not in ("720p", "1080p"):
            warnings.append(f"minimax accepts 720p/1080p only; {resolution} -> 720p")
        body = {
            "content": str(params.get("content") or ""),
            "duration": int(duration),
            "resolution": lower if lower in ("720p", "1080p") else "720p",
            "tier": "base" if params.get("tier") == "base" else "turbo",
        }
        if ratio:
            body["aspectRatio"] = ratio
        for src in ("imageUrl", "lastFrameUrl", "referenceVideoUrl"):
            if params.get(src):
                body[src] = params[src]
        refs = params.get("referenceImageUrls") or []
        if refs:
            body["referenceImageUrls"] = list(refs)
        auds = params.get("referenceAudioUrls") or []
        if auds:
            body["referenceAudioUrls"] = list(auds)
        return body

    if model in ("t2v", "t2v_v3"):
        # duration 在这个入口是字符串（实测，见 docs/official/api-reference.md §3）
        body = {"prompt": str(params.get("content") or ""), "duration": str(int(duration))}
        if ratio:
            body["aspectRatio"] = ratio
        if params.get("imageUrl") or params.get("lastFrameUrl"):
            warnings.append(f"{model} is text-to-video; the supplied image was dropped")
        if params.get("tier") == "base":
            warnings.append(f"{model} has no tier switch; tier was dropped")
        return body

    if model in ("i2v", "i2v_v3"):
        image = params.get("imageUrl") or params.get("lastFrameUrl")
        if not image:
            raise ParamError(f'{model} requires a first-frame image (Ark content[] with role="first_frame")')
        body = {"image": image, "duration": str(int(duration))}
        if params.get("content"):
            body["prompt"] = str(params["content"])
        if ratio:
            body["aspectRatio"] = ratio
        return body

    if model == "wan27":
        body = {
            "prompt": str(params.get("content") or ""),
            "duration": str(int(duration)),
            "resolution": resolution.upper(),
        }
        if ratio:
            body["ratio"] = ratio
        if body["resolution"] not in ("720P", "1080P"):
            warnings.append(f'wan27 accepts 720P/1080P only; "{body["resolution"]}" forwarded as-is')
        return body

    if model == "happyhorse":
        body = {
            "prompt": str(params.get("content") or ""),
            "duration": str(int(duration)),
            "resolution": resolution.upper(),
        }
        if ratio:
            warnings.append("happyhorse has no ratio field; ratio was dropped")
        return body

    raise ParamError(f'unknown official model "{model}" (expected one of: {", ".join(OFFICIAL_MODELS)})')


def _parse_content_items(items: list) -> tuple[list[str], list, list, list, list, list, list[str]]:
    """拆 content[] 的 6 类输入 + 不认识的 type。"""
    texts: list[str] = []
    first: list = []
    last: list = []
    ref_img: list = []
    ref_vid: list = []
    ref_aud: list = []
    unknown: list[str] = []

    for it in items:
        if not isinstance(it, dict):
            unknown.append("content[] (non-object)")
            continue
        t = str(it.get("type") or "")
        if t == "text":
            if it.get("text"):
                texts.append(str(it["text"]))
            continue
        url = None
        for key in ("image_url", "video_url", "audio_url"):
            holder = it.get(key)
            if isinstance(holder, dict) and holder.get("url"):
                url = holder["url"]
                break
        role = str(it.get("role") or "").strip()
        if t == "image_url":
            if role == "first_frame":
                first.append(url)
            elif role == "last_frame":
                last.append(url)
            else:
                ref_img.append(url)  # reference_image，或无 role
        elif t == "video_url":
            ref_vid.append(url)
        elif t == "audio_url":
            ref_aud.append(url)
        else:
            unknown.append(f'content[].type="{t}"')

    return texts, first, last, ref_img, ref_vid, ref_aud, unknown


def _pick(body: Mapping[str, Any], extra: Mapping[str, Any], key: str) -> Any:
    """取参数：顶层优先，其次 `extra_body`（Ark SDK 把未建模字段塞在这里）。"""
    v = body.get(key)
    if v is None:
        v = extra.get(key)
    return v


def translate_create(body: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict:
    """Ark 创建任务请求体 → 完整翻译结果（纯函数，不联网）。

    返回 requested / effective / warnings / unsupported / incompatible /
    official_model / official_payload / max_credits / idempotency_key。

    `incompatible` 装的是**上游明确不具备的能力**（例如 2.5 的视频编辑）。
    它们不影响纯翻译与 dry-run，但真实提交前必须被拒 —— 见 `app.py`。
    """
    env = env if env is not None else {}
    if not isinstance(body, Mapping):
        raise ParamError("body must be a JSON object")

    warnings: list[str] = []
    unsupported: list[str] = []
    incompatible: list[str] = []

    model = str(body.get("model") or "").strip()
    if not model:
        raise ParamError("model is required", "model")

    items = body.get("content")
    if not isinstance(items, list) or not items:
        raise ParamError("content is required", "content")

    texts, first, last, ref_img, ref_vid, ref_aud, unknown = _parse_content_items(items)
    unsupported.extend(unknown)

    # 帧输入与参考素材互斥（站点硬约束）
    has_frame = bool(first or last)
    has_ref = bool(ref_img or ref_vid or ref_aud)
    if has_frame and has_ref:
        raise ParamError("frame inputs (imageUrl/lastFrameUrl) and reference assets are mutually exclusive")

    # Ark SDK 会把"未建模字段"塞进 extra_body，真正发出时合并到顶层 —— 两处都要看，
    # 否则调用方以为生效了（例如 extra_body.watermark），实际被丢。
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    for k in PASSTHROUGH_UNSUPPORTED:
        if body.get(k) is not None or extra.get(k) is not None:
            unsupported.append(k)

    ratio = str(body.get("ratio") or "").strip()
    if ratio and ratio not in ARK_RATIOS:
        raise ParamError(f'ratio: invalid enum value "{ratio}"', "ratio")
    aspect = None
    if ratio and ratio != "adaptive":
        aspect = ratio

    resolution = "720p"
    if body.get("resolution") not in (None, ""):
        r = str(body["resolution"]).strip()
        if r not in ARK_RESOLUTIONS:
            raise ParamError(f'resolution: invalid enum value "{r}"', "resolution")
        resolution = r

    raw_duration = body.get("duration")
    duration = None
    if raw_duration not in (None, ""):
        d = float(raw_duration)
        if d == -1:
            duration = 5
            warnings.append("duration=-1 (intelligent) mapped to 5s")
        else:
            duration = d
    elif body.get("frames") is not None:
        duration = max(1, round(float(body["frames"]) / 24))
        warnings.append(f'frames={body["frames"]} converted to {duration}s at 24fps')

    prefer_free = extra.get("aivideomaker_prefer_free") is True
    requested_duration = duration
    duration = snap_duration(duration if duration is not None else 5, resolution, prefer_free)
    if requested_duration is not None and requested_duration != duration:
        msg = f"duration {requested_duration:g}s snapped to {duration}s (site limit for {resolution})"
        if duration > FREE_MAX_DURATION >= requested_duration:
            msg += (
                "; this crosses into the billed range — set "
                "extra_body.aivideomaker_prefer_free=true to snap down instead"
            )
        warnings.append(msg)

    if aspect and first and ratio != "adaptive":
        warnings.append('Ark requires ratio="adaptive" when role=first_frame; ratio is derived from the image anyway')

    # -------------------------------------------------------- Seedance 2.5 专属字段 --

    # omni_reference_task_type —— 声明这是哪一类参考任务。
    omni: str | None = None
    omni_raw = _pick(body, extra, "omni_reference_task_type")
    if omni_raw not in (None, ""):
        omni = str(omni_raw).strip()
        if omni not in OMNI_TASK_TYPES:
            raise ParamError(
                f'omni_reference_task_type: invalid enum value "{omni}" '
                f'(expected one of: {", ".join(OMNI_TASK_TYPES)})',
                "omni_reference_task_type",
            )
        if omni in OMNI_TYPES_NEEDING_ABSENT_CAPS:
            incompatible.append(
                f'omni_reference_task_type="{omni}" requires upstream video edit/extend, which neither '
                "configured upstream provides (the web line is minimax-H3 text-to-video / image-to-video / "
                'reference only) — use "reference" or "auto"'
            )
        if omni == "edit":
            if ratio and ratio != "adaptive":
                warnings.append('omni_reference_task_type="edit" requires ratio="adaptive" upstream')
            if raw_duration not in (None, "", -1):
                warnings.append('omni_reference_task_type="edit" requires duration=-1 upstream')

    # output_format —— 容器。上游实测成片一律 mp4。
    output_format: str | None = None
    fmt_raw = _pick(body, extra, "output_format")
    if fmt_raw not in (None, ""):
        output_format = str(fmt_raw).strip().lower()
        if output_format not in OUTPUT_FORMATS:
            raise ParamError(
                f'output_format: invalid enum value "{output_format}" '
                f'(expected one of: {", ".join(OUTPUT_FORMATS)})',
                "output_format",
            )
        if output_format != UPSTREAM_OUTPUT_FORMAT:
            warnings.append(
                f'output_format="{output_format}" requested, but the upstream renders '
                f"{UPSTREAM_OUTPUT_FORMAT} only — the delivered file will be "
                f"{UPSTREAM_OUTPUT_FORMAT}, not {output_format}"
            )

    # generate_audio —— 站点没有这个开关，音画由模型自行决定。
    generate_audio: bool | None = None
    ga_raw = _pick(body, extra, "generate_audio")
    if ga_raw is not None:
        if not isinstance(ga_raw, bool):
            raise ParamError("generate_audio must be a boolean", "generate_audio")
        generate_audio = ga_raw
        warnings.append(
            "generate_audio is not a station-side switch — the upstream decides the audio track; "
            "the requested value is recorded and echoed back, but audio presence is never guaranteed"
        )

    # ------------------------------------------------------------------ 素材组装 --

    # 参考素材按上游标称上限截断：图 ≤4 / 视频 ≤1 / 音频 ≤2（站点 UI 文案，中英双份一致）。
    # **每一次截断都必须留痕** —— 告警里写明丢了几项、丢的是哪些 URL；被丢弃的素材对应的
    # 提示词占位符（`@视频2`…）随后由 `_prompt_ref_warnings` 点名。
    ref_images_in = [u for u in (first[1:] + ref_img) if u]
    ref_videos_in = [u for u in ref_vid if u]
    ref_audios_in = [u for u in ref_aud if u]

    ref_images, dropped_images = _truncate_references(ref_images_in, "reference_image")
    ref_videos, dropped_videos = _truncate_references(ref_videos_in, "reference_video")
    ref_audios, dropped_audios = _truncate_references(ref_audios_in, "reference_audio")

    for label, kept, dropped in (
        ("reference_image", ref_images, dropped_images),
        ("reference_video", ref_videos, dropped_videos),
        ("reference_audio", ref_audios, dropped_audios),
    ):
        if dropped:
            warnings.append(
                f"{label}: {len(kept) + len(dropped)} supplied, upstream accepts at most {len(kept)} — "
                f"{len(dropped)} dropped: " + ", ".join(dropped)
            )

    if ref_videos:
        warnings.append(
            "reference_video is forwarded as referenceVideoUrl (accepted upstream; verified live on the "
            "480p/5s free tier)"
        )
    if ref_audios:
        warnings.append(
            "reference_audio is forwarded as referenceAudioUrls (accepted upstream; verified live on the "
            "480p/5s free tier)"
        )

    params: dict[str, Any] = {
        "content": "\n".join(texts).strip(),
        "imageUrl": first[0] if first else None,
        "lastFrameUrl": last[0] if last else None,
        # 多张 first_frame 的多余图与 reference_image 合并
        "referenceImageUrls": ref_images,
        "referenceVideoUrl": ref_videos[0] if ref_videos else None,
        "referenceAudioUrls": ref_audios,
        "duration": duration,
        "resolution": resolution,
        "tier": "turbo",
    }
    if aspect:
        params["aspectRatio"] = aspect

    # 提示词里的 @图像n / @视频n / @音频n 必须能落到**真实发出去的**素材上。
    # 截断后超出范围的占位符（以及调用方本来写错的编号）都必须点名 ——
    # "参考错了素材"极难归因，正是本适配层最该拦住的一类。
    warnings.extend(
        _prompt_ref_warnings(
            params["content"],
            {"reference_image": ref_images, "reference_video": ref_videos, "reference_audio": ref_audios},
        )
    )

    override = extra.get("aivideomaker_tier")
    if override in ("turbo", "base"):
        params["tier"] = override
    elif override:
        warnings.append(f'extra_body.aivideomaker_tier="{override}" ignored (expected turbo|base)')

    official_model = pick_official_model(
        model, extra.get("aivideomaker_official_model") or env.get("AVM_OFFICIAL_MODEL")
    )
    # 未知模型直接报错（错误信息里带可用清单），而不是塞进 unsupported 静默放行。
    # 官方专属的提示（分辨率降级、字段未验证…）单独收集 —— 混进 web 线的输出会让
    # 调用方以为站点也不支持 1080p、或者以为用的就是官方线。
    official_warnings: list[str] = []
    official_payload = to_official_body(official_model, params, official_warnings)

    max_credits = None
    try:
        max_credits = resolve_max_credits(body, env)
    except ParamError as e:
        warnings.append(str(e))

    # 计费口径**不在这里定死**：两条线的免费规则不同（web 有免费窗口、官方线一律计费），
    # 由 billing_view() 按上游渲染。这里只给出 web 口径的默认值。
    return {
        "requested": {
            "model": model,
            "content": items,
            "ratio": ratio or None,
            "resolution": body.get("resolution"),
            "duration": body.get("duration"),
            "frames": body.get("frames"),
            "output_format": output_format,
            "generate_audio": generate_audio,
            "omni_reference_task_type": omni,
            "watermark": body.get("watermark"),
            "seed": body.get("seed"),
        },
        "effective": {
            "aspectRatio": aspect or "auto(from image)",
            "duration": duration,
            "resolution": effective_resolution(official_payload),
            # 上游实际产出的容器（站点实测成片一律 .mp4），不是请求值
            "output_format": UPSTREAM_OUTPUT_FORMAT,
            "billed": params["tier"] == "base" or duration > FREE_MAX_DURATION,
        },
        "warnings": warnings,
        "unsupported": sorted(set(unsupported)),
        # 上游明确不具备的能力：dry-run 下照常返回（零成本可见），真实提交前必须被拒
        "incompatible": incompatible,
        # 两条上游各自要的东西，一次翻译都产出，由上游层自取
        "web_params": params,
        "official_model": official_model,
        "official_payload": official_payload,
        "official_warnings": official_warnings,
        "max_credits": max_credits,
        "idempotency_key": extra.get("aivideomaker_idempotency_key"),
        "captcha_token": extra.get("aivideomaker_captcha_token"),
    }


_FREE_WINDOW_RE = re.compile(r"free window|free duration|billed range", re.I)


def billing_note(upstream: str) -> str:
    """一句话说清这条上游怎么计费。`/healthz` 与 `effective` 都引用它。"""
    if upstream == "web":
        return f"web upstream: tier=base is always billed; tier=turbo is free up to {FREE_MAX_DURATION}s"
    return "official upstream: every submit is billed — X-Max-Credits is the only guard"


def billing_view(plan: Mapping[str, Any], upstream: str) -> tuple[dict, list[str]]:
    """按上游线渲染计费口径 —— 两条线的免费规则**不同，不能混**。

    ==========  ==========================================================
    `web`       `tier=base` 一律计费；`tier=turbo` 且 ≤8s **免费**
    `official`  **一律计费**，没有免费窗口；`X-Max-Credits` 是唯一闸门
    ==========  ==========================================================

    返回 `(effective, warnings)`。把 web 口径的提示原样端给官方线的调用方，
    会让它以为这次不花钱 —— 这是本项目最贵的一类 bug。
    """
    eff = dict(plan.get("effective") or {})
    web_params = plan.get("web_params") or {}
    shared = list(plan.get("warnings") or [])
    official_only = list(plan.get("official_warnings") or [])

    if upstream == "web":
        tier = web_params.get("tier", "turbo")
        duration = eff.get("duration") or 0
        eff["billed"] = tier == "base" or duration > FREE_MAX_DURATION
        eff["tier"] = tier
        # ⚠️ 分辨率要取**站点实际会用的值**。官方 payload 可能把它降到 720，那是官方线
        # 的限制（seedance20 只到 720），站点本身是支持 1080p 的 —— 报错值会误导。
        if web_params.get("resolution"):
            eff["resolution"] = web_params["resolution"]
        if web_params.get("aspectRatio"):
            eff["aspectRatio"] = web_params["aspectRatio"]
        eff["billing_note"] = billing_note("web")
        return eff, shared

    warnings = [w for w in shared + official_only if not _FREE_WINDOW_RE.search(w)]
    eff.pop("tier", None)
    eff["billed"] = True
    eff["billing_note"] = billing_note("official")
    return eff, warnings


def effective_resolution(payload: Mapping[str, Any] | None) -> str:
    """官方 payload 实际会用的分辨率（可能是降级后的值）。"""
    if not isinstance(payload, Mapping):
        return "720p"
    r = payload.get("resolution")
    if isinstance(r, int):
        return f"{r}p"
    if isinstance(r, str) and re.match(r"^\d{3,4}\s*[pP]$", r.strip()):
        return f"{int(re.match(r'(\d+)', r).group(1))}p"
    return "720p"


def resolve_max_credits(body: Mapping[str, Any], env: Mapping[str, str] | None = None) -> int | None:
    """解析 X-Max-Credits。返回 None 表示调用方没给上限 → 上层必须拒绝提交。"""
    env = env if env is not None else {}
    extra = body.get("extra_body") if isinstance(body, Mapping) and isinstance(body.get("extra_body"), dict) else {}
    raw = extra.get("aivideomaker_max_credits")
    if raw is not None and raw != "":
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ParamError("extra_body.aivideomaker_max_credits must be a non-negative integer")
        return raw
    envv = str(env.get("AVM_OFFICIAL_MAX_CREDITS", "")).strip()
    if envv:
        try:
            n = int(envv)
        except ValueError:
            raise ParamError("AVM_OFFICIAL_MAX_CREDITS must be a non-negative integer") from None
        if n < 0:
            raise ParamError("AVM_OFFICIAL_MAX_CREDITS must be a non-negative integer")
        return n
    return None


def _epoch(v: Any) -> int | None:
    """ISO8601 → epoch 秒。"""
    if not v:
        return None
    try:
        return int(_dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def normalize_task(raw: Mapping[str, Any] | None) -> dict:
    """官方 Task 信封 → Ark 任务对象。"""
    raw = raw if isinstance(raw, Mapping) else {}
    status = str(raw.get("status") or "SUBMITTED").lower()
    ark_status = STATUS_TO_ARK.get(status, "running")
    charged = int(raw.get("creditsCharged") or 0)
    refunded = int(raw.get("creditsRefunded") or 0)
    inp = raw.get("input") if isinstance(raw.get("input"), Mapping) else {}
    out = raw.get("output") if isinstance(raw.get("output"), Mapping) else {}
    resolution = inp.get("resolution", inp.get("size"))
    num_res = _numeric_resolution(resolution)

    return {
        "id": raw.get("id"),
        "model": raw.get("model"),
        "status": ark_status,
        "error": (
            {"code": "GenerationFailed", "message": out.get("error") or "video generation failed"}
            if ark_status == "failed"
            else None
        ),
        "content": {
            "video_url": out.get("url") if ark_status == "succeeded" else None,
            "last_frame_url": None,
            "file_url": None,
        },
        "usage": {
            "completion_tokens": 0,
            "total_tokens": 0,
            "credits": charged - refunded,
            "credits_charged": charged,
            "credits_refunded": refunded,
            "paid": charged > 0 and refunded < charged,
        },
        "frames": None,
        "framespersecond": 24,
        "created_at": _epoch(raw.get("createdAt")),
        "updated_at": _epoch(raw.get("completedAt") or raw.get("createdAt")),
        "seed": -1,
        "service_tier": "default",
        "execution_expires_after": 172800,
        "generate_audio": False,
        "duration": inp.get("duration"),
        "ratio": inp.get("ratio") or inp.get("aspectRatio"),
        "output_format": UPSTREAM_OUTPUT_FORMAT,
        "resolution": f"{num_res}p" if num_res else None,
        "draft": False,
        "draft_task_id": None,
        "official": dict(raw),
    }


# 站点侧 taskStatus 的词表与官方的 TaskStatus **不同**（succeed / processing / queueing…），
# 所以单独一张表，别复用 STATUS_TO_ARK。
_WEB_STATUS_TO_ARK = {
    "submitted": "queued",
    "pending": "queued",
    "preparing": "queued",
    "queueing": "queued",
    "queued": "queued",
    "processing": "running",
    "running": "running",
    "in_progress": "running",
    "succeed": "succeeded",
    "success": "succeeded",
    "completed": "succeeded",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def normalize_web_task(raw: Mapping[str, Any] | None) -> dict:
    """站点任务记录 → Ark 任务对象。

    站点记录的字段名自成一套（`taskStatus` / `aiModel` / `url` / `credits` /
    `kelingKeyId`），其中 **`kelingKeyId` 才是真实分辨率**（`"480"`、`"1080"`）——
    请求里写的分辨率未必等于实际产出的档位。
    """
    raw = raw if isinstance(raw, Mapping) else {}
    status = str(raw.get("taskStatus") or "submitted").lower()
    ark_status = _WEB_STATUS_TO_ARK.get(status, "queued")
    # ⚠️ 站点会把**没有产出 URL** 的任务也留在 `succeed`（`taskStatusMsg="not found url"`）。
    # 照搬映射会让调用方看到"succeeded 但 content.video_url 为 null"这种自相矛盾的状态，
    # 下游据此判成功、拿到空链接 ⇒ 直接按失败处理，原因沿用 `taskStatusMsg`。
    if ark_status == "succeeded" and not raw.get("url"):
        ark_status = "failed"
    succeeded = ark_status == "succeeded"
    res_key = str(raw.get("kelingKeyId") or "").strip()

    return {
        "id": raw.get("id"),
        "model": raw.get("aiModel"),
        "status": ark_status,
        "error": (
            {"code": "GenerationFailed", "message": raw.get("taskStatusMsg") or "video generation failed"}
            if ark_status == "failed"
            else None
        ),
        "content": {
            "video_url": raw.get("url") if succeeded else None,
            "last_frame_url": None,
            "file_url": None,
        },
        "usage": {
            "completion_tokens": 0,
            "total_tokens": 0,
            # ⚠️ 站点侧 `credits` 与 `paid` **不是一回事**：paid=false 时记 credits=1。
            # 判断这次是否花钱只看 `paid`。
            "credits": raw.get("credits"),
            "paid": bool(raw.get("paid")),
        },
        "frames": None,
        "framespersecond": 24,
        "created_at": _epoch(raw.get("createdAt")),
        "updated_at": _epoch(raw.get("completedAt") or raw.get("createdAt")),
        "seed": -1,
        "service_tier": "default",
        "execution_expires_after": 172800,
        "generate_audio": False,
        "duration": raw.get("duration"),
        "ratio": raw.get("aspectRatio"),
        "output_format": UPSTREAM_OUTPUT_FORMAT,
        "resolution": f"{res_key}p" if res_key.isdigit() else None,
        "draft": False,
        "draft_task_id": None,
        "cover": raw.get("cover"),
        "upstream": dict(raw),
    }
