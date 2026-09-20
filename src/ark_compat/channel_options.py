"""渠道级选项头 `X-Channel-Options` 的**模型**键：模型映射 / 默认透传。

本模块是这几个键的**唯一解析点**。键名与语义与视频适配层（`video-adapter`，
`docs/decisions/ADR-012-model-name-passthrough.md`）**逐字同形** —— 两边共用一套口径，
改这里必须同步改那边（约定原文见本仓库 `docs/channel-options-model.md`）。

| 键 | 语义 |
| --- | --- |
| `model_map` | 模型名 → 上游槽位 的映射表：**精确键**（要覆盖一族名字就逐条写）+ **至多一条 `*` 兜底**。别名 `upstream_model_map`（两个都写且不同 ⇒ 拒绝） |
| ~~`model`~~ | 🔴 **已撤除**（2026-09-17）：原先的"渠道钉住槽位"。遗留它 ⇒ 渠道配置错误，**不静默忽略**（见 `_reject_removed_keys`） |

**默认 = 上游 model 透传**：不配任何键时，调用方写的名字**原样当作上游槽位**。本站的"上游槽位"
就是站点 procedure 名里那一段（`ai.minimaxH3` ⇒ 槽位 `minimaxH3`）—— 站点把模型编进了
procedure 路径，创建请求体里**没有** model 字段（`web_client.create` 的 body 键是白名单）。

🔴 **不留任何名字猜测表**：曾经有一张"名字里含 seedance 就走 seedance20"的正则表，它把 5 个不同
代次的原生 ID 静默压进同一个槽位且不回告警（实测账单差 7.3 倍）。已删除，**不要加回来** ——
"哪个名字落哪个槽位"是**控制面知识**，只能由渠道用 `model_map` 声明。

## 判定顺序

    ① 映射表**精确**键命中            → slot = 表值                source=model_map
    ② 映射表的 **`*` 兜底**命中        → slot = 表值                source=model_map
    ③ 名字本身是已知上游槽位（剥后缀后）→ slot = 名字（**逐字透传**）  source=passthrough
    ④ 其余                            → **400**（报文列出已知槽位 + 表里已声明的键 + 怎么改）

**② 排在 ③ 前面是刻意的**（2026-09-17 用户裁定）：**兜底＝渠道声明"除我明确列出的以外，一切名字都落这一档"**
⇒ 它**要**能覆盖调用方写出的槽位名，否则运维"这个渠道只跑某一档"的意图会落空。
这也是本层**唯一**的"强制一档"手段（`model` 钉住键已撤除）：**要强制就配兜底**。
代价：调用方点名别的模型会被改写 —— 因此每次改写都进 `warnings` 并留 `model_source=model_map` 证据，
**不静默**；调用方若真要别的模型，运维得把那个名字放进**精确表**（① 优先于 ②）。

## 通配：**唯一一条 `*` 兜底**（2026-09-17 降级）

原先支持任意通配模式（`doubao-seedance-*`），于是需要"多命中怎么排"的规则，而两个项目在这一点上
各写了一套（`docs/channel-options-wildcard-compare.md` 记了那场对比）。现在**整套多模式机制拆掉**：
`*` 是**唯一**被识别的通配形态、**至多一条**，其余键一律当精确键。

- 好处：**不可能出现重叠** ⇒"优先级 / 最长匹配 / 歧义报错"这一整类问题消失；
- 代价：要覆盖一族名字（`doubao-seedance-*`）**必须逐条写精确键** —— 而那些名字是有限的、
  已知的（官方模型 ID 就那几个），所以这不是负担；
- 校验：任何**非 `*` 却含 `*`** 的键（`doubao-seedance-*` / `a*b`）一律 `channel_config_error`，
  报文给出两条出路（逐条写精确键，或保留唯一的 `*` 兜底）。

## `model`（钉住）为什么撤除，以及"强制一档"怎么办

它当时只做一致性断言（与本次解析结果不一致就报错），**不承担任何名字转换** —— 与 `model_map`
职责重复。撤除之后"强制一档"**由 `*` 兜底承接**（② 优先于 ③，见上）：渠道配 `{"*": X}`
就是"本渠道一律跑 X"。⚠️ 语义与钉住**不等价**：兜底是**改写**（请求照样放行，命中写进
`model_source` 与 `warnings`），钉住是**拦下请求**。

⚠️ 遗留 `model` 键**必须响亮失败**，不能静默忽略：以为它还生效的运维会以为这个渠道只跑某个槽位，
而实际上调用方写的任何合法槽位名都会被逐字发到上游。

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

MODEL_MAP_KEY = "model_map"
#: `model_map` 的别名：`docs/03_引擎架构.md` §4.2 早先登记过这个名字（当时并未实现），
#: 运维可能照那份文档配 —— 两个名字都接受，但**不替配置方挑一个**（见 `_model_map`）。
MODEL_MAP_ALIAS = "upstream_model_map"

_SUPPORTED_KEYS = (MODEL_MAP_KEY, MODEL_MAP_ALIAS)

#: **撤除过**的渠道键：出现即报错（见 `_reject_removed_keys`），**不静默忽略**。
#: `model` 曾经的语义是"渠道钉住槽位"（一致性断言，不承担翻译）——
#: 以为它还生效的运维会以为这个渠道只跑某个槽位，而实际上调用方写的任何合法槽位名都会被逐字发出。
_REMOVED_KEYS = ("model",)

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
#: **面级策略**强制落定的槽位（见 `resolve_model(force_slot=...)`）。目前只有一个使用者：
#: `/v1/videos` 的"只跑免费线"（2026-09-20 用户口径），槽位由 `openai_videos.FREE_ONLY_SLOT` 给出。
#: ⚠️ 它与已撤除的渠道键 `X-Channel-Options.model` **不是一回事**：那个是**渠道配置**要钉住槽位
#: （会让"配了不生效"难以察觉），这个是**面自己的对外契约**、由代码传入、每次改写都留痕。
SOURCE_FREE_ONLY = "free_only"


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
    """头原文 → dict。**只做结构校验**，语义校验在 `_reject_removed_keys` / `_model_map`。

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
            '{"model_map": {"doubao-seedance-2-0-260128": "seedance20"}}'
        ) from exc
    if not isinstance(obj, dict):
        raise _config_error(
            "X-Channel-Options must be a JSON object, not "
            f"{type(obj).__name__} — supported keys: {', '.join(_SUPPORTED_KEYS)}"
        )
    return obj


def _reject_removed_keys(options: Mapping[str, Any]) -> None:
    """撤除过的渠道键一旦出现 ⇒ 渠道配置错误（**不静默忽略**）。

    静默忽略是更坏的选择：那个键的语义是"本渠道只服务某个槽位"，运维据此认为
    "调用方传错名字会被拦住"；一旦它不再生效却仍被接受，那层保护就无声消失了。
    """
    for key in _REMOVED_KEYS:
        if key in options:
            raise _config_error(
                f"X-Channel-Options.{key} was removed on 2026-09-17: the channel-level pinned slot "
                "is gone. Use model_map instead — either an exact key (the name the caller sends → "
                'the upstream slot name) or the single "*" catch-all — or have callers send the slot '
                f'name verbatim. Remove the "{key}" key from the channel configuration.'
            )


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


def _resolve_by_name(name: str, table: Mapping[str, str]) -> tuple[str | None, str, str]:
    """按名字解析（老规则）→ `(槽位 | None, 来源, 告警文本)`。

    判定顺序：① 映射表**精确**命中 → ② 映射表的 `*` 兜底 → ③ 名字本身是已知上游槽位
    （**透明转发**）→ ④ `None`（表外、且不是已知槽位；由调用方决定"拒绝"还是"面级策略覆盖"）。

    抽成函数只有一个理由：**面级策略要能如实说出"它改掉了什么"**。判定链写两份必然漂，
    而这条链的产物直接决定"落到哪个模型"= 计费档位。
    """
    if name in table:
        slot = table[name]
        return slot, SOURCE_MODEL_MAP, (
            f'model "{name}" → upstream slot "{slot}" (exact hit in the channel model_map)'
        )
    if CATCH_ALL in table:
        slot = table[CATCH_ALL]
        return slot, SOURCE_MODEL_MAP, (
            f'model "{name}" → upstream slot "{slot}" (the channel\'s model_map "{CATCH_ALL}" '
            "fallback overrides every name it does not list explicitly)"
        )
    if name in KNOWN_SLOTS:
        # 透明转发：请求值 = 执行值，没有可留痕的改写（与老实现一致：不发声）
        return name, SOURCE_PASSTHROUGH, ""
    return None, SOURCE_PASSTHROUGH, ""


def resolve_model(
    model: Any, options: Mapping[str, Any] | None, *, force_slot: str | None = None
) -> ModelResolution:
    """调用方模型名 → `(槽位, 来源, 是否已实测, 告警)`。**不猜、不兜底**（判定顺序见模块 docstring）。

    ⚠️ **两条已决定的规则**（不是缺口）：

    1. **配了 `*` 兜底时，它会覆盖调用方写出的槽位名** —— 兜底＝渠道声明"除我列出的以外
       一律落这一档"，这正是本层唯一的"强制一档"手段（`model` 钉住键已撤除）。
       每次改写都进 `warnings` 且留 `model_source=model_map` 证据，**不静默**；
    2. **精确键永远优先于兜底**（① > ②）—— 想让某个名字走别的槽位，就把它写进精确表。

    `force_slot`：**面级策略**（不是渠道配置）要求本次请求只落这一个槽位。给了它，就**跳过**
    ②③④三条判定 —— 连"不认识的名字"也不会 400（那正是"全部兜底到免费档"的含义）；
    槽位改写成它、来源标 `source=free_only`，而**调用方的名字一字不改**（`requested.model`
    仍是原值，证据不丢），凡是没落的就进 `warnings`。唯一的调用者是 `/v1/videos` 的
    "只跑免费线"（`openai_videos.FREE_ONLY_SLOT`）。
    ⚠️ **渠道配置的校验（撤除键 / 非法通配）在它之前照旧执行** —— 强制槽位不该让一份坏配置
    静默通过。它与已撤除的 `X-Channel-Options.model` 不是一回事：那个是渠道配置想钉住槽位，
    这个是面自己的对外契约（见 `SOURCE_FREE_ONLY`）。
    """
    options = options or {}
    _reject_removed_keys(options)
    name = bare_model_name(model)
    if not name:
        raise ParamError("model is required", "model")

    table = _model_map(options)
    warnings: list[str] = []
    # 先算出"**本来**会落到哪"（老规则：映射表精确命中 / `*` 兜底 / 已知槽位透传 / 未命中 ⇒ 拒）。
    # 抽出来是为了让面级策略能**如实说出它改掉了什么** —— 两处各写一份判定必然漂。
    intended, intended_source, intended_note = _resolve_by_name(name, table)

    if force_slot:
        slot, source = force_slot, SOURCE_FREE_ONLY
        if intended != force_slot:
            # 🔴 覆盖必须留痕，且要说清被覆盖的是**哪一种**：调用方点名的别的槽位、渠道映射
            #    指到的槽位、还是"本来会被拒"的未知名。运维要靠这条判断"我配的映射为什么
            #    没生效"（不静默换模型是本项目一贯的口径）。
            what = (
                f'would have gone to upstream slot "{intended}"'
                + (f" ({intended_note})" if intended_note else " (passthrough)")
                if intended is not None
                else "would have been refused (unknown upstream slot)"
            )
            warnings.append(
                f'model "{name}" {what}, but this face only runs one upstream slot: the request '
                f'goes to "{force_slot}" instead'
            )
    elif intended is None:
        hint = (
            f" The channel model_map declares: {', '.join(sorted(table))}."
            if table
            else " No model_map is configured on this channel."
        )
        raise ParamError(
            f'unknown model "{name}". This layer forwards the model name verbatim, so it must be '
            f"one of the upstream slots: {', '.join(KNOWN_SLOTS)}.{hint} Add an exact entry (or a "
            f'single "{CATCH_ALL}" catch-all) to the channel\'s X-Channel-Options.model_map.',
            "model",
        )
    else:
        slot, source = intended, intended_source
        if intended_note:
            warnings.append(intended_note)

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
