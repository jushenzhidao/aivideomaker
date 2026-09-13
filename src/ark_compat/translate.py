"""Ark ↔ aivideomaker 网页端内部接口的纯翻译层。

**全部是纯函数，零外部依赖、零网络。** 这是整个兼容层的核心逻辑，也是测试
真正该压的地方——FastAPI / logfire 只是把它包成服务。

翻译方向：
    Ark create body  →  web {params}（站点 tRPC 接口的字段形状）
    站点任务记录       →  Ark task object

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
DURATION_ALLOWED: dict[str, list[int]] = {
    "480p": list(range(5, 21)),
    "720p": list(range(5, 21)),
    "1080p": list(range(5, 21)),
}
FREE_MAX_DURATION = 8  # 免费窗口的上界：turbo 且不超过它就不计费

# Seedance 2.5 的「全能参考」任务类型。
#   auto / reference  普通全模态生成与参考驱动 —— 站点侧有对应能力
#   edit / extend     视频编辑与延长 —— 需要上游具备编辑/延长能力，
#                     而 web 线只有 minimax-H3 的文生/图生/参考
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

# 提示词里的素材占位引用：`@图像1` / `@视频2` / `@音频1`。
# 编号是 **1-based**，对应 `content[]` 里同类素材的出现顺序。
_REF_TOKEN_RE = re.compile(r"@\s*(图像|视频|音频)\s*(\d+)")


def snap_duration(duration: Any, resolution: str, prefer_free: bool = False) -> int:
    """把时长吸附到该分辨率的合法区间 `[5, 20]`。

    站点的约束是"连续秒数 + 上限"，所以绝大多数请求**原样透传**（没有离散档位），
    只有越界值才会被就近钳制。

    `prefer_free=True` 是**省钱开关**，语义为"别越过免费线"：凡是超过
    `FREE_MAX_DURATION` 的合法时长（例如 12s）都会被主动拉回 8s —— 而不只是
    在越界时才生效。
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


def translate_create(body: Mapping[str, Any]) -> dict:
    """Ark 创建任务请求体 → 完整翻译结果（纯函数，不联网）。

    返回 requested / effective / warnings / unsupported / incompatible / web_params。

    `incompatible` 装的是**上游明确不具备的能力**（例如 2.5 的视频编辑）。
    它们不影响纯翻译与 dry-run，但真实提交前必须被拒 —— 见 `app.py`。
    """
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
                f'omni_reference_task_type="{omni}" requires upstream video edit/extend, which the web line '
                "does not provide (it is minimax-H3 text-to-video / image-to-video / reference only) "
                '— use "reference" or "auto"'
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

    # 计费口径**不在这里定死**，由 billing_view() 统一渲染（见下），这里只给默认值。
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
            # 请求值。**实际产出**的档位以任务记录里的 `kelingKeyId` 为准
            # （实测：请求 720p 实际得 704p），见 normalize_web_task。
            "resolution": resolution,
            # 上游实际产出的容器（站点实测成片一律 .mp4），不是请求值
            "output_format": UPSTREAM_OUTPUT_FORMAT,
            "billed": params["tier"] == "base" or duration > FREE_MAX_DURATION,
        },
        "warnings": warnings,
        "unsupported": sorted(set(unsupported)),
        # 上游明确不具备的能力：dry-run 下照常返回（零成本可见），真实提交前必须被拒
        "incompatible": incompatible,
        "web_params": params,
        "captcha_token": extra.get("aivideomaker_captcha_token"),
    }


def billing_note() -> str:
    """一句话说清这条上游怎么计费。`/healthz` 与 `effective` 都引用它。"""
    return f"web upstream: tier=base is always billed; tier=turbo is free up to {FREE_MAX_DURATION}s"


def billing_view(plan: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """渲染计费口径。

    `tier=base` 一律计费；`tier=turbo` 且 ≤8s **免费**。

    返回 `(effective, warnings)`。判据只有一个：`effective.billed`
    —— 站点任务记录里的 `paid` 才是最终事实（`credits` 与它反相，别用它判断）。
    """
    eff = dict(plan.get("effective") or {})
    web_params = plan.get("web_params") or {}
    tier = web_params.get("tier", "turbo")
    duration = eff.get("duration") or 0
    eff["billed"] = tier == "base" or duration > FREE_MAX_DURATION
    eff["tier"] = tier
    # 分辨率/比例以**站点实际收到的参数**为准（web_params 是真正发给上游的那份）。
    if web_params.get("resolution"):
        eff["resolution"] = web_params["resolution"]
    if web_params.get("aspectRatio"):
        eff["aspectRatio"] = web_params["aspectRatio"]
    eff["billing_note"] = billing_note()
    return eff, list(plan.get("warnings") or [])


def _epoch(v: Any) -> int | None:
    """ISO8601 → epoch 秒。"""
    if not v:
        return None
    try:
        return int(_dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# 站点侧 taskStatus 的词表（succeed / processing / queueing…）
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
