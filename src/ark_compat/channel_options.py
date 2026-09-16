"""渠道级选项头 `X-Channel-Options` 的**模型**键：钉住槽位 / 模型映射 / 默认透传。

本模块是这几个键的**唯一解析点**。键名与语义与视频适配层（`video-adapter`，
`docs/decisions/ADR-012-model-name-passthrough.md`）**逐字同形** —— 两边共用一套口径，
改这里必须同步改那边（约定原文见本仓库 `docs/channel-options-model.md`）。

| 键 | 语义 |
| --- | --- |
| `model` | 渠道级**钉住槽位**：请求名解析不出槽位时以它为准；解析得出**且与它不同** ⇒ 渠道配置错误（不静默改模型） |
| `model_map` | 模型名 → 上游槽位 的映射表：**精确键**（要覆盖一族名字就逐条写）+ **至多一条 `*` 兜底**。别名 `upstream_model_map`（两个都写且不同 ⇒ 拒绝） |

**默认 = 上游 model 透传**：不配任何键时，调用方写的名字**原样当作上游槽位**。本站的"上游槽位"
就是站点 procedure 名里那一段（`ai.minimaxH3` ⇒ 槽位 `minimaxH3`）—— 站点把模型编进了
procedure 路径，创建请求体里**没有** model 字段（`web_client.create` 的 body 键是白名单）。

🔴 **不留任何名字猜测表**：曾经有一张"名字里含 seedance 就走 seedance20"的正则表，它把 5 个不同
代次的原生 ID 静默压进同一个槽位且不回告警（实测账单差 7.3 倍）。已删除，**不要加回来** ——
"哪个名字落哪个槽位"是**控制面知识**，只能由渠道用 `model_map` / `model` 声明。

## 判定顺序

    ① 映射表**精确**键命中            → slot = 表值                 source=model_map
    ② 名字本身是已知上游槽位（剥后缀后）→ slot = 名字（**逐字透传**）   source=passthrough
    ③ 映射表的 **`*` 兜底**命中        → slot = 表值                 source=model_map
    ④ 都没命中、但渠道**钉住**了槽位    → slot = 钉住值               source=pinned
    ⑤ 其余                            → **400**（报文列出已知槽位 + 表里已声明的键 + 怎么改）

**② 排在 ③ 前面是刻意的**：调用方**指名**了一个真槽位时，不该被兜底规则改写。
代价是"配了兜底、调用方又写槽位名"时兜底**不会生效** —— 这是**已决定的规则**，不是缺口：
想强制一档就别让调用方写槽位名（或改用 ④ 钉住 —— 那时两者冲突会**报错**，而不是静默择一）。

## 通配：**降级为唯一一条 `*` 兜底**（2026-09-17）

原先支持任意通配模式（`doubao-seedance-*`），于是需要"多命中怎么排"的规则，而两个项目在这一点上
各写了一套（`docs/channel-options-wildcard-compare.md` 记了那场对比）。现在**整套多模式机制拆掉**：
`*` 是**唯一**被识别的通配形态、**至多一条**，其余键一律当精确键。

- 好处：**不可能出现重叠** ⇒"优先级 / 最长匹配 / 歧义报错"这一整类问题消失；
- 代价：要覆盖一族名字（`doubao-seedance-*`）**必须逐条写精确键** —— 而那些名字是有限的、
  已知的（官方模型 ID 就那几个），所以这不是负担；
- 校验：任何**非 `*` 却含 `*`** 的键（`doubao-seedance-*` / `a*b`）一律 `channel_config_error`，
  报文给出两条出路（逐条写精确键，或保留唯一的 `*` 兜底）。

⚠️ 两处必须知道的边界：

1. **槽位值域来自站点事实，不是猜测**：`SITE_MODEL_KEYS` 是站点网页端 i18n 文案里的 11 个模型键
   （`docs/web-reverse/model-inventory.md`，采集 2026-09-13）；`VERIFIED_SLOTS` 是**已实测存在
   procedure** 的槽位。配置**允许**落在"站点有、procedure 未实测"的槽位上（否则映射表在拿到
   procedure 清单之前根本无法配置），但会在 `warnings` 与证据字段里标注 `verified=false` ——
   **允许但不静默**。
2. **调用方模型名可能自带分辨率后缀**（`/v1/videos` 面：`doubao-seedance-1-0-pro_1080p`）。
   匹配前**先剥后缀**，否则合法请求会因为名字对不上而被拒。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ParamError

#: 头名。Starlette 的 `request.headers` 查表大小写不敏感，这里给规范写法。
CHANNEL_OPTIONS_HEADER = "X-Channel-Options"

PIN_KEY = "model"
MODEL_MAP_KEY = "model_map"
#: `model_map` 的别名：`docs/03_引擎架构.md` §4.2 早先登记过这个名字（当时并未实现），
#: 运维可能照那份文档配 —— 两个名字都接受，但**不替配置方挑一个**（见 `_model_map`）。
MODEL_MAP_ALIAS = "upstream_model_map"

_SUPPORTED_KEYS = (PIN_KEY, MODEL_MAP_KEY, MODEL_MAP_ALIAS)

#: **唯一**被识别的通配形态（兜底键）。2026-09-17 起不再支持其它模式，见模块 docstring。
CATCH_ALL = "*"

#: 站点 procedure 的命名前缀（`ai.minimaxH3` ⇒ `ai.`）。前端两条线共用同一个站点。
PROCEDURE_PREFIX = "ai."

#: 站点网页端的 11 个模型键（站点 i18n 文案，`docs/web-reverse/model-inventory.md`）。
#: ⚠️ 它回答的是"**站点认识哪些模型名**"，**不回答**"哪条 procedure 存在"——两件事别混。
SITE_MODEL_KEYS: tuple[str, ...] = (
    "default",
    "HappyHorse",
    "seedance20",
    "wan27",
    "seedance2",
    "seedance25",
    "kling2_5",
    "kling3",
    "ltx23",
    "veo3Fast",
    "veo31Fast",
)

#: 已**实测**存在 procedure 的槽位。唯一证据：三次真实提交的成片 URL 里一律是 `minimax_h3`，
#: 且 `ai.minimaxH3` 是唯一被观测到的生成 procedure（报告 AVM12-OPEN-UPSTREAM）。
#: 🔴 本集合必须与 `web_client.CREATE_PROCEDURE` 的模型段一致 —— 门禁：
#:    `tests/test_channel_model_map_wildcard.py::TestVerifiedSlotMatchesProcedureConstant`。
VERIFIED_SLOTS: frozenset[str] = frozenset({"minimaxH3"})

#: 槽位值域 = 站点模型键 ∪ 已实测槽位（保序去重）。
KNOWN_SLOTS: tuple[str, ...] = tuple(dict.fromkeys((*SITE_MODEL_KEYS, *sorted(VERIFIED_SLOTS))))

#: 与 `openai_videos._RESOLUTION_SUFFIX_RE` 同一语义（两处各留一份：那一份决定分辨率，
#: 这一份决定"名字怎么比对"，耦合起来反而会把两件事绑死）。
_RESOLUTION_SUFFIX_RE = re.compile(r"_(480p|720p|1080p)\s*$", re.I)

#: 解析来源，进证据字段（`effective.model_source`）。
SOURCE_MODEL_MAP = "model_map"
SOURCE_PASSTHROUGH = "passthrough"
SOURCE_PINNED = "pinned"


@dataclass(frozen=True)
class ModelResolution:
    """一次请求的模型解析结果（**证据的载体**：调用方写了什么 → 上游拿到什么）。"""

    slot: str
    source: str
    verified: bool
    warnings: tuple[str, ...] = ()


def _config_error(message: str) -> ParamError:
    """**渠道配置错误**：运维的问题，不是调用方的。

    对外仍是 400 `InvalidParameter`（`code` 是白名单，不为它新造错误码），但
    `param` 指向头名、报文里写明"这是渠道配置问题" —— 否则调用方会在自己的
    `model` 字段上找半天，而问题在渠道那一侧。
    """
    return ParamError(message, CHANNEL_OPTIONS_HEADER)


def bare_model_name(model: Any) -> str:
    """剥掉 `provider/` 段与分辨率后缀，留下可与槽位比对的名字。"""
    name = str(model or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[1].strip()
    return _RESOLUTION_SUFFIX_RE.sub("", name).strip()


def parse_channel_options(raw: Any) -> dict[str, Any]:
    """头原文 → dict。**只做结构校验**，语义校验在 `_model_map` / `_pinned`。

    空串 = 没配。非 JSON / 非对象一律拒绝 —— 一个坏掉的头不该被当成"没配"（那会静默
    用默认透传跑掉，而运维以为自己的映射生效了）。
    """
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise _config_error(
            f"X-Channel-Options is not valid JSON ({exc}); it must be a JSON object, e.g. "
            '{"model": "minimaxH3"} or {"model_map": {"doubao-seedance-2-0-260128": "seedance20"}}'
        ) from exc
    if not isinstance(obj, dict):
        raise _config_error(
            "X-Channel-Options must be a JSON object, not "
            f"{type(obj).__name__} — supported keys: {', '.join(_SUPPORTED_KEYS)}"
        )
    return obj


def _model_map(options: Mapping[str, Any]) -> dict[str, str]:
    """渠道声明的映射表 `{"<调用方写的名字或 *>": "<上游槽位>"}`。

    · 别名 `upstream_model_map` 等价；两个都写且内容不同 ⇒ 拒绝（自相矛盾的配置不该由我们挑一个）；
    · 值必须落在 `KNOWN_SLOTS` 里：给一个上游不认识的槽位，等于把 400 推到**已经带上凭据的**
      上游请求之后；
    · 重复键（含大小写折叠）⇒ 拒绝：JSON 同名键会**静默覆盖**，而"哪一条生效"直接决定计费；
    · **通配只认字面 `*`、且至多一条**（2026-09-17 降级）：其它含 `*` 的模式一律拒绝，
      报文给出两条出路 —— 这让"多命中怎么排"这一整类问题**不可能发生**。
    """
    raw = options.get(MODEL_MAP_KEY)
    alias = options.get(MODEL_MAP_ALIAS)
    if raw is not None and alias is not None and raw != alias:
        raise _config_error(
            "X-Channel-Options.model_map and upstream_model_map are both set and differ; keep exactly "
            "one (they are two names for the same table)"
        )
    raw = raw if raw is not None else alias
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _config_error(
            "X-Channel-Options.model_map must be a JSON object of "
            '{"<name the caller sends>": "<upstream slot>"}'
        )
    out: dict[str, str] = {}
    folded: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        slot = str(value or "").strip()
        if not name or not slot:
            raise _config_error(
                f"model_map entries must be non-empty strings (got {key!r}: {value!r})"
            )
        if "*" in name and name != CATCH_ALL:
            raise _config_error(
                f"model_map key {name!r} uses a wildcard pattern; only the bare "
                f'"{CATCH_ALL}" catch-all is supported (at most one entry). Write the names out as '
                'exact keys (e.g. "doubao-seedance-2-0-260128"), or keep a single '
                f'"{CATCH_ALL}" entry as the fallback for names the table does not list.'
            )
        if slot not in KNOWN_SLOTS:
            raise _config_error(
                f"model_map[{name!r}]={slot!r} is not a known upstream slot (expected one of: "
                f"{', '.join(KNOWN_SLOTS)})"
            )
        low = name.lower()
        if low in folded:
            raise _config_error(
                f"model_map has duplicate entries for {name!r} and {folded[low]!r} "
                "(JSON would silently keep only one of them)"
            )
        folded[low] = name
        out[name] = slot
    return out


def _pinned(options: Mapping[str, Any]) -> str:
    """`X-Channel-Options.model` —— 渠道钉住的槽位（空串 = 没钉）。"""
    raw = options.get(PIN_KEY)
    if raw is None:
        return ""
    pin = str(raw).strip()
    if not pin:
        raise _config_error(
            "X-Channel-Options.model must be a non-empty string (drop the key instead of blanking it)"
        )
    if pin not in KNOWN_SLOTS:
        raise _config_error(
            f'X-Channel-Options.model="{pin}" is not a known upstream slot (expected one of: '
            f"{', '.join(KNOWN_SLOTS)})"
        )
    return pin


def resolve_model(model: Any, options: Mapping[str, Any] | None) -> ModelResolution:
    """调用方模型名 → `(槽位, 来源, 是否已实测, 告警)`。**不猜、不兜底**（判定顺序见模块 docstring）。

    钉住值若与 ①②③ 的结果**不同** ⇒ 渠道配置错误：一个渠道声明"我只跑 X"，而这次请求明确落到了
    Y，两边有一个是错的。这里**不静默改模型** —— 静默改模型的账单差异，是本项目定义为最贵的一类缺陷。

    ⚠️ **已决定的规则**（不是缺口）：② 命中的名字是**已知槽位**时，`*` 兜底**不会**改写它。
    于是"配了兜底 + 调用方写槽位名"时兜底静默不生效 —— 想强制一档就别让调用方写槽位名，
    或改用 ④ 钉住（那时冲突会**报错**，而不是静默择一）。
    """
    options = options or {}
    name = bare_model_name(model)
    if not name:
        raise ParamError("model is required", "model")

    table = _model_map(options)
    pinned = _pinned(options)
    warnings: list[str] = []

    if name in table:
        slot, source = table[name], SOURCE_MODEL_MAP
        warnings.append(
            f'model "{name}" → upstream slot "{slot}" (exact hit in the channel model_map)'
        )
    elif name in KNOWN_SLOTS:
        slot, source = name, SOURCE_PASSTHROUGH
    elif CATCH_ALL in table:
        slot, source = table[CATCH_ALL], SOURCE_MODEL_MAP
        warnings.append(
            f'model "{name}" is not an upstream slot; the channel\'s model_map "{CATCH_ALL}" '
            f'fallback maps it to upstream slot "{slot}"'
        )
    elif pinned:
        slot, source = pinned, SOURCE_PINNED
        warnings.append(
            f'model "{name}" is not an upstream slot; the channel pins upstream slot "{pinned}" '
            "(X-Channel-Options.model)"
        )
    else:
        hint = (
            f" The channel model_map declares: {', '.join(sorted(table))}."
            if table
            else " No model_map is configured on this channel."
        )
        raise ParamError(
            f'unknown model "{name}". This layer forwards the model name verbatim, so it must be '
            f"one of the upstream slots: {', '.join(KNOWN_SLOTS)}.{hint} Add an exact entry (or a "
            f'single "{CATCH_ALL}" catch-all) to the channel\'s X-Channel-Options.model_map, or pin '
            "one slot with X-Channel-Options.model.",
            "model",
        )

    if pinned and slot != pinned:
        raise _config_error(
            f'X-Channel-Options.model pins "{pinned}" but this request resolves to upstream slot '
            f'"{slot}" (source={source}); fix the channel pin or the caller\'s model — this layer '
            "will not silently substitute one model for another"
        )

    verified = slot in VERIFIED_SLOTS
    if not verified:
        warnings.append(
            f'upstream slot "{slot}" has a known site model but no verified procedure: it is sent as '
            f'"{procedure_for_slot(slot)}", which is inferred from the naming pattern and has not '
            "been confirmed against the site yet"
        )

    unknown = sorted(str(k) for k in options if k not in _SUPPORTED_KEYS)
    if unknown:
        warnings.append(
            f"X-Channel-Options keys {', '.join(unknown)} are not supported by this layer and were "
            f"ignored (supported: {', '.join(_SUPPORTED_KEYS)})"
        )
    return ModelResolution(slot=slot, source=source, verified=verified, warnings=tuple(warnings))


def procedure_for_slot(slot: str) -> str:
    """槽位 → 站点 tRPC procedure 名。

    🔴 **这里是一个未验证假设**：全站点只实测出 `ai.minimaxH3` 一条生成 procedure，其余槽位的
    procedure 名按"同一命名形态"推断。因此：

    · 已实测槽位（`VERIFIED_SLOTS`）走这里等于取真值；
    · 未实测槽位**照发**，但 `resolve_model` 会带告警、证据里标 `verified=false` ——
      拿到站点 procedure 清单后，**把推断换成表**（`docs/web-reverse/README.md` 的待办），
      并让"表里没有的槽位"直接被拒。
    """
    return f"{PROCEDURE_PREFIX}{slot}"
