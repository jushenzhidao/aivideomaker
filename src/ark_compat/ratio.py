"""比例（ratio）↔ 像素（WxH）的通用映射层。

为什么单独成层
--------------

1. **档位表不是全局常量。** 现在只有一条 web 站点线，站点 UI 给六档；将来接入别的
   模型 / 上游，各自可选的比例档位**大概率不同**（正如分辨率档位本来就不同）。把档位
   表做成 `RatioSpec` 实例：换上游 = 换一个 spec，而不是去改一个全局常量。

2. **输入形态只会越来越多。** 今天是 `16:9` / `1920x1080` / `5:4`，明天可能是
   `portrait`、平台预设名、别的 SDK 的枚举。用 **resolver 链**：每种形态一个函数，
   按优先级依次尝试，第一个认领的胜出。加一种形态 = 加一个 resolver，其余不动；
   判定顺序也因此是**显式**的，而不是散落在 if/elif 里。

3. **比例与像素是两张表。** 档位表决定"能出什么比例"，像素表决定"这档实际出多大"
   （调用方真正关心的是后者）。像素表**只填实测值**，没测过的一律返回 None ——
   编造一个尺寸比不给尺寸糟糕得多。

判定的几种出口（`Resolution.mode`）
-----------------------------------

    exact       精确命中档位表 / 非比例枚举 —— 不留痕
    alias       别名映射（keep_ratio → adaptive）
    preset      语义预设（portrait → 9:16）
    dimension   WxH 像素约分后**正好**命中档位
    proportion  W:H 比例串（可读，但不在档位表里）→ 就近吸附
    snapped     有比例值但约不进档位 → 就近吸附（有损，必写偏差）
    fallback    连比例都读不出来 → 兜底档
    absent      没传（= 没指定，交给上游默认，不是兜底）

后六种**一律留痕**：它们都改变了调用方想要的结果。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Callable, Mapping

# 宽 x 高（OpenAI Sora / 图像 API 风格的 size，如 `1920x1080`、`1024x1792`）。
WXH_RE = re.compile(r"^(\d{1,6})\s*[xX×*]\s*(\d{1,6})$")

# `W:H` 形态的**比例串**（`5:4`、`9:21`、`2.35:1`）。与 `WXH_RE` 的分工必须分清：
#   · `WXH_RE` 认**尺寸**（`1920x1080` = 像素宽高）⇒ 先约分，再拿约分结果比档位；
#   · 本正则认**比例本身**（`5:4` ⇒ 1.25）⇒ 直接拿数值比远近，不需要约分。
# 允许小数是必要的：`2.35:1`（宽银幕）是真实写法，强行约分只会得出怪分数而无益。
PROPORTION_RE = re.compile(r"^\s*(\d{1,6}(?:\.\d+)?)\s*:\s*(\d{1,6}(?:\.\d+)?)\s*$")


def ratio_value(ratio: str) -> float:
    """`16:9` → 1.777…（供**远近比较**用，不参与对外输出）。"""
    w, _, h = str(ratio).partition(":")
    return float(w) / float(h)


def simplify_wxh(text: Any) -> str | None:
    """`1920x1080` → `16:9`；`1024x1792` → `4:7`。非 WxH 形态返回 None。"""
    m = WXH_RE.match(str(text or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    f = Fraction(w, h)
    return f"{f.numerator}:{f.denominator}"


def parse_proportion(text: Any) -> float | None:
    """`5:4` → 1.25；非「数值:数值」形态（`abc`、`16`、`16:9:1`、`16/9`）返回 None。

    只认**冒号两侧都是正数**的形态：`0:9` / `16:0` 返回 None —— 它们不是比例，是坏
    输入；放行会让除零/无穷把任意值都吸到同一档（`inf` 的最近邻恒为最宽那档）。
    """
    m = PROPORTION_RE.match(str(text or ""))
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return w / h


# ==================== 档位表 ====================


@dataclass(frozen=True)
class RatioSpec:
    """一份「上游能出哪些比例」的描述。

    `order` 是**有序**的：就近吸附靠它做平手时的确定性 tie-break（平手取靠前者）。
    `extra` 放**非比例**的枚举（如 `adaptive` = 跟随输入图），它们不参与吸附比较。
    """

    order: tuple[str, ...]
    extra: tuple[str, ...] = ()
    fallback: str = "16:9"
    name: str = "default"

    @property
    def all_values(self) -> frozenset[str]:
        return frozenset(self.order) | frozenset(self.extra)

    def snap(self, value: float) -> str:
        """把任意宽高比吸附到本档位表里**相对距离**最近的一档。

        用相对距离（对数差）而不是绝对差：比例域跨越 0.56~2.33，横屏那一端的绝对差
        天然被放大，只有乘性比较才能让横屏与竖屏共用一把尺子。
        """
        return min(
            self.order,
            key=lambda r: (abs(math.log(ratio_value(r) / value)), self.order.index(r)),
        )

    def drift_pct(self, value: float, landed: str) -> float:
        """`value` 相对**落点**的偏差百分比。

        以落点为分母 ⇒ 语义是"实际出的比你想要的窄/宽 X%"，比用较小值做分母
        （`exp(|log 差|)-1`，会系统性高估）更贴近肉眼感受。
        """
        out = ratio_value(landed)
        return abs(value - out) / out * 100

    def with_fallback(self, fallback: str) -> "RatioSpec":
        """换掉兜底档位、其余不变 —— 换策略不必重建整个 spec。"""
        return replace(self, fallback=fallback)


#: 站点 UI 的 `Aspect Ratio` 选择器（截图取证 2026-09-18）：六档，16:9 默认选中。
#: 兜底取 16:9 正是为了与这个默认一致 —— 兜底不该制造"比上游默认更意外"的落差。
SITE_UI_SPEC = RatioSpec(
    order=("16:9", "4:3", "1:1", "3:4", "9:16", "21:9"),
    extra=("adaptive",),
    fallback="16:9",
    name="site-ui",
)


#: 语义预设 → 档位。**给不给这张表由调用方决定**（`ResolveOptions.presets`）：
#:  OpenAI 面的 `size` 收这些词很自然；方舟线是官方契约，不往里加非标准值。
#: ⚠️ 加词 = 往这里加一项，判定逻辑一行不用动 —— 这正是 resolver 链要的效果。
SIZE_PRESETS: dict[str, str] = {
    "portrait": "9:16",
    "landscape": "16:9",
    "square": "1:1",
    "ultrawide": "21:9",
}


# ==================== 实测像素表 ====================
#
# 各档位**真实出片**的像素尺寸。判据是成片实际分辨率，不是文档或推算。
# ⚠️ **只填实测过的值** —— 没测过的组合返回 None，绝不靠"按比例推算"补表：
#    推算值看着合理，但站点常常取整/对齐到偶数或 8 的倍数，推算会**稳定地错一档**。
#    需要哪个组合就去实测一条，把数字填进来。
#
# 想补齐：**按免费组合实测一条即可**（`/v1/videos` + 480p，实测 `billed=False`），
# 出片后 ffprobe 读分辨率填进本表。
MEASURED_PIXELS: dict[tuple[str, str], tuple[int, int]] = {
    # E2E-AVM-019 实测（2026-09-18，`/v1/videos` minimaxH3，成片 ffprobe）
    ("480p", "16:9"): (864, 480),
    ("480p", "4:3"): (640, 480),
    ("480p", "3:4"): (480, 640),
    ("480p", "21:9"): (1120, 480),
    ("720p", "16:9"): (1248, 704),
    ("720p", "4:3"): (928, 704),     # ⚠️ 见下方"反例"
    ("720p", "1:1"): (704, 704),
    ("720p", "3:4"): (704, 928),
    ("720p", "9:16"): (704, 1248),
    ("1080p", "16:9"): (1904, 1080),  # ⚠️ 见下方"反例"
    ("1080p", "1:1"): (1080, 1080),
    ("1080p", "9:16"): (1080, 1904),
}
# ⚠️ **"按比例推算"的两个反例（本表存在的理由）** —— 站点把边长对齐到 **16 的倍数**：
#    · 4:3  @720p ：从 480p 的 `640x480` 按比例算得 `960x720`，实测是 **`928x704`**；
#    · 16:9 @1080p：按 16:9 精确算是 `1920x1080`，实测是 **`1904x1080`**。
#    两个推算值都"看着合理"，两个都错。**没测过的一律返回 None，绝不推算。**
#
# 尚未实测（提交时被 `429 CAPTCHA_REQUIRED` 速率门挡回，非比例问题）：
#   480p 的 `1:1` / `9:16`；720p 的 `21:9`；1080p 的 `4:3` / `3:4` / `21:9`。
#   补法：想办法绕过速率门（拉长间隔或带 Turnstile token）后实测一条填进来。


def pixels_for(ratio: str, resolution: str) -> tuple[int, int] | None:
    """该分辨率下该比例**实测**的出片尺寸；未实测 → None（不推算、不编造）。"""
    return MEASURED_PIXELS.get((str(resolution).strip().lower(), str(ratio).strip()))


def ratio_of(w: int, h: int, spec: RatioSpec = SITE_UI_SPEC) -> str:
    """像素尺寸 → 档位（约分后命中就用它，否则就近吸附）。"""
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid dimensions: {w}x{h}")
    f = Fraction(int(w), int(h))
    exact = f"{f.numerator}:{f.denominator}"
    if exact in spec.all_values:
        return exact
    return spec.snap(float(f.numerator) / float(f.denominator))


# ==================== 解析结果与 resolver 链 ====================


@dataclass(frozen=True)
class Resolution:
    """一次解析的完整结果 —— 带 `mode` 是为了让**留痕**由调用方统一决定。"""

    ratio: str
    mode: str
    note: str = ""

    @property
    def silent(self) -> bool:
        """exact / absent 不该产生噪声：健康请求被告警淹没就没人看了。"""
        return self.mode in ("exact", "absent")


@dataclass(frozen=True)
class ResolveOptions:
    """解析时的可选开关 —— 都是"表"，加一项不需要动判定逻辑。"""

    #: 别名 → 档位（`keep_ratio` → `adaptive`）。传空表即关闭该能力。
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: 语义预设 → 档位（`portrait` → `9:16`）。默认空表 ⇒ 不改变既有行为。
    presets: Mapping[str, str] = field(default_factory=dict)
    #: True = 认不出就抛（Ark 线严格契约）；False = 落到 `spec.fallback`（OpenAI 面）。
    strict: bool = True
    #: 是否接受 `W:H` 比例串（`5:4`、`2.35:1`）。**默认收**（2026-09-18 用户口径：
    #: "等比 或者按比例 传都可以" —— 调用方写哪种形态不该由我们挑）。
    #: ⚠️ 曾只在 OpenAI 面收、Ark 线一律 400；那条门槛是本层自己加的、并非上游约束，
    #:    已撤 ⇒ **两线都认**，需要区分时由调用方显式传 False。
    allow_proportion: bool = True
    #: 吸附偏差**超过**该百分比时升级措辞（0 = 不升级）。
    #: 🔴 越线**一律不拒绝** —— "出一档相近的片"远好过"整个请求 400"；
    #:    代价是画面构成可能明显不同，所以把偏差写清楚并给出可替换的写法。
    warn_drift: float = 0.0
    #: 档位 → 一个可直接替换的 WxH 写法，供偏差过大时指路。
    examples: Mapping[str, str] = field(default_factory=dict)


Resolver = Callable[[str, "RatioSpec", "ResolveOptions"], "Resolution | None"]


def _res_enum(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    if s in spec.all_values:
        return Resolution(s, "exact")
    return None


def _res_alias(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    mapped = opt.aliases.get(s)
    if mapped is None:
        return None
    if mapped == s:
        return Resolution(mapped, "exact")
    return Resolution(mapped, "alias",
                      f'"{s}" maps to "{mapped}" upstream (a single behavior there)')


def _res_preset(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    mapped = opt.presets.get(s)
    if mapped is None:
        return None
    return Resolution(mapped, "preset", f'"{s}" is a preset for "{mapped}"')


def _res_dimension(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    wxh = simplify_wxh(s)
    if wxh is None:
        return None
    if wxh in spec.all_values:
        return Resolution(wxh, "dimension", f'"{s}" reduces to "{wxh}"')
    value = ratio_value(wxh)
    landed = spec.snap(value)
    return Resolution(landed, "snapped",
                      _snap_note(s, wxh, landed, spec.drift_pct(value, landed), opt))


def _res_proportion(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    if not opt.allow_proportion:
        return None
    value = parse_proportion(s)
    if value is None:
        return None
    landed = spec.snap(value)
    return Resolution(landed, "proportion",
                      _snap_note(s, "", landed, spec.drift_pct(value, landed), opt))


def _snap_note(raw: str, as_text: str, landed: str, drift: float,
               opt: ResolveOptions) -> str:
    """吸附说明。偏差**必须写出来**：差 1.6% 看不出来，差 25% 是另一个画面 ——
    只说"改了"不说"改了多少"，调用方无法判断该接受还是该换尺寸。
    """
    #  `as_text` 只在"原始写法与约分结果不同"时才给（`1024x1792` (4:7)）；比例串
    #  自己就是比例文本（`5:4`），再括一遍会是 `size="5:4" (5:4)` 这种赘述。
    shown = f" ({as_text})" if as_text else ""
    if opt.warn_drift and drift > opt.warn_drift:
        example = opt.examples.get(landed, "")
        tail = (
            f'supported ratio "{landed}" (differs by {drift:.1f}%, well beyond '
            f'{opt.warn_drift:g}%) — the output will look noticeably different; '
            f'consider "{example}" instead'
        )
    else:
        tail = (
            f'supported ratio "{landed}" (differs by {drift:.1f}%) — the output aspect ratio '
            f'will not be exactly what you asked for'
        )
    return f'"{raw}"{shown} is not one of the upstream ratios; snapped to the nearest ' + tail


def _res_fallback(s: str, spec: RatioSpec, opt: ResolveOptions) -> Resolution | None:
    if opt.strict:
        return None  # 交给 `resolve` 抛出
    return Resolution(
        spec.fallback, "fallback",
        f'"{s}" is not a recognized aspect ratio (expected one of '
        f'{"/".join(spec.order)}'
        + (f', {"/".join(spec.extra)}' if spec.extra else "")
        + ', a WxH size like 1920x1080, or a W:H proportion like 16:9); '
        f'fell back to "{spec.fallback}"',
    )


#: 判定顺序**显式**列出。加一种输入形态 = 在这里插一个函数，其余不动。
RESOLVERS: tuple[Resolver, ...] = (
    _res_enum,
    _res_alias,
    _res_preset,
    _res_dimension,
    _res_proportion,
)


class UnknownRatioError(ValueError):
    """认不出的比例（`strict` 模式下抛出）。"""


def resolve(
    raw: Any,
    spec: RatioSpec = SITE_UI_SPEC,
    options: ResolveOptions | None = None,
) -> Resolution:
    """把任意写法的 size / ratio 解析成上游档位。

    返回 `Resolution`（含 `mode` 与 `note`）—— 留痕由**调用方**决定怎么组织，本层
    只负责说清"改了什么、为什么"。
    """
    opt = options or ResolveOptions()
    s = str(raw or "").strip()
    if not s:
        return Resolution("", "absent")

    for resolver in RESOLVERS:
        got = resolver(s, spec, opt)
        if got is not None:
            return got

    got = _res_fallback(s, spec, opt)
    if got is not None:
        return got
    raise UnknownRatioError(f'invalid ratio value "{s}"')
