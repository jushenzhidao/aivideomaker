#!/usr/bin/env python3
"""门禁：`X-Channel-Options.model_map` 的键规则（**2026-09-17 通配降级后重写**）。

## 变更记录（读之前先看这条）

前一版钉的是"任意通配模式 + 特异性排序 + 与书写顺序无关"三条承诺，对应模块里的
`_glob_regex` / `_specificity` / `_wildcard_hit`。2026-09-17 的决定是**把通配降级为
唯一一条 `*` 兜底**、其余一律精确键 ⇒ 那套多模式机制**整体拆掉**，本文件随之按新语义重写。

路径与 `TestVerifiedSlotMatchesProcedureConstant` 类名**刻意保留**：`channel_options.py`
的 docstring 指着这个类名（改名的代价是那条指针失效）。降级的动因与两项目对比见
`docs/channel-options-wildcard-compare.md`。

## 现在钉什么

1. `*` 是**唯一**被识别的通配形态，且**至多一条**；任何**非 `*` 却含 `*`** 的键 ⇒ 渠道配置错误；
2. 判定顺序：**精确** > 名字本身是槽位 > **`*` 兜底** > **钉住** > 400；
3. `*` 兜底**不改写**已知槽位名（**已决定的规则**，见模块 docstring —— 不是缺口）；
4. 值域 / 空键 / 重复键（含大小写折叠）/ 别名冲突 / 后缀与 `provider/` 剥离。

## 怎么证伪（变异测试）

| 变异 | 期望变红 |
|---|---|
| 去掉"非 `*` 通配一律拒绝"那条校验 | `test_non_catchall_wildcards_are_rejected` |
| 把 `*` 兜底排到"名字本身是槽位"**之前** | `test_catchall_does_not_rewrite_a_slot_name` |
| 去掉 `*` 兜底分支 | `test_catchall_maps_unlisted_names` |
| 去掉值域校验 | `test_unknown_slot_is_rejected` |
| 不剥分辨率后缀 | `test_suffix_is_stripped_before_matching` |

> ⚠️ 前身留下的一条教训，**保留**：**"写了门禁"与"门禁真的会拦"是两件事** ——
> 上一版四条变异里第一次有三条没红（用例选得不对）。改动本文件时请同样逐条跑变异。

**纯函数、零网络、零计费。**
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import web_client  # noqa: E402
from ark_compat.channel_options import (  # noqa: E402
    CATCH_ALL,
    KNOWN_SLOTS,
    MODEL_MAP_ALIAS,
    MODEL_MAP_KEY,
    SOURCE_MODEL_MAP,
    SOURCE_PASSTHROUGH,
    VERIFIED_SLOTS,
    resolve_model,
)
from ark_compat.errors import ParamError  # noqa: E402


def resolve(model, table=None, pin=None):
    opts = {}
    if table is not None:
        opts[MODEL_MAP_KEY] = table
    if pin is not None:
        opts["model"] = pin
    return resolve_model(model, opts)


class TestCatchAllOnly(unittest.TestCase):
    """`*` 是唯一通配形态（降级后的核心：多命中不可能发生）。"""

    def test_catchall_maps_unlisted_names(self):
        r = resolve("doubao-seedance-2-5-260628", {CATCH_ALL: "wan27"})
        self.assertEqual(r.slot, "wan27")
        self.assertEqual(r.source, SOURCE_MODEL_MAP)

    def test_catchall_only_fires_when_nothing_else_matches(self):
        """精确键优先于兜底 —— 同一条请求命中精确键时，兜底不许插手。"""
        r = resolve("doubao-seedance-2-0-260128", {"doubao-seedance-2-0-260128": "wan27",
                                                   CATCH_ALL: "seedance20"})
        self.assertEqual(r.slot, "wan27")

    def test_catchall_does_not_rewrite_a_slot_name(self):
        """已知槽位名优先于兜底（**已决定的规则**）。

        ⚠️ 这条是刻意选的代价：配了 `{"*": X}` 也**不会**改写调用方明确写出的槽位名。
        想强制一档 ⇒ 别让调用方写槽位名，或改用 `model` 钉住（那时冲突会**报错**）。
        """
        r = resolve("seedance20", {CATCH_ALL: "wan27"})
        self.assertEqual(r.slot, "seedance20", "槽位名被兜底改写了？那是规则变了")
        self.assertEqual(r.source, SOURCE_PASSTHROUGH)

    def test_catchall_is_warned(self):
        """兜底改写了模型值 ⇒ 必须留痕（静默改模型的账单差异是本项目最贵的一类缺陷）。"""
        self.assertTrue(any("fallback" in w for w in resolve("nope-1", {CATCH_ALL: "wan27"}).warnings))

    def test_non_catchall_wildcards_are_rejected(self):
        """任何**非 `*`** 却含 `*` 的键 ⇒ 渠道配置错误（报文要给出两条出路）。"""
        for bad in ("doubao-seedance-*", "a*b", "*pro", "doubao-*"):
            with self.subTest(key=bad):
                with self.assertRaises(ParamError) as ctx:
                    resolve("x", {bad: "wan27"})
                self.assertIn("catch-all", str(ctx.exception))
                self.assertEqual(ctx.exception.param, "X-Channel-Options", "报错要指向渠道配置")


class TestPrecedence(unittest.TestCase):
    """精确 > 槽位名 > 兜底 > 钉住 > 400。"""

    def test_exact_beats_everything(self):
        r = resolve("a-foo", {"a-foo": "wan27", CATCH_ALL: "seedance20"}, pin=None)
        self.assertEqual(r.slot, "wan27")

    def test_pin_applies_when_nothing_matches(self):
        r = resolve("nope-1", None, pin="wan27")
        self.assertEqual(r.slot, "wan27")

    def test_pin_conflict_is_a_channel_error(self):
        """钉住与解析结果不同 ⇒ 报错，**不静默改模型**。"""
        with self.assertRaises(ParamError) as ctx:
            resolve("seedance20", None, pin="wan27")
        self.assertEqual(ctx.exception.param, "X-Channel-Options")

    def test_unknown_without_any_fallback_is_the_callers_fault(self):
        with self.assertRaises(ParamError) as ctx:
            resolve("doubao-seedance-2-5-260628")
        self.assertEqual(ctx.exception.param, "model", "调用方的错要指向 model")
        self.assertIn("model_map", str(ctx.exception), "报文要给出下一步怎么改")


class TestInputNormalisation(unittest.TestCase):
    def test_suffix_is_stripped_before_matching(self):
        r = resolve("seedance20_1080p", None)
        self.assertEqual(r.slot, "seedance20")

    def test_provider_prefix_is_stripped(self):
        r = resolve("somevendor/seedance20", None)
        self.assertEqual(r.slot, "seedance20")


class TestConfigValidation(unittest.TestCase):
    """配置错误必须**拒绝且说清**（值域 / 空键 / 重复 / 别名冲突）。"""

    def test_unknown_slot_is_rejected(self):
        with self.assertRaises(ParamError) as ctx:
            resolve("m", {CATCH_ALL: "not-a-slot"})
        self.assertIn("known upstream slot", str(ctx.exception))
        self.assertEqual(ctx.exception.param, "X-Channel-Options")

    def test_empty_key_or_value_is_rejected(self):
        for bad in ({"": "seedance20"}, {CATCH_ALL: ""}):
            with self.subTest(table=bad):
                with self.assertRaises(ParamError):
                    resolve("m", bad)

    def test_case_variant_duplicate_patterns_are_rejected(self):
        """大小写变体视为重复 —— JSON 同名键会静默覆盖，"哪条生效"直接决定账单。"""
        with self.assertRaises(ParamError):
            resolve("m", {"Wan27": "wan27", "wan27": "seedance20"})

    def test_alias_is_accepted_and_conflict_is_rejected(self):
        r = resolve_model("doubao-1", {MODEL_MAP_ALIAS: {CATCH_ALL: "wan27"}})
        self.assertEqual(r.slot, "wan27", "别名 upstream_model_map 必须等价")
        with self.assertRaises(ParamError):
            resolve_model("m", {MODEL_MAP_KEY: {CATCH_ALL: "wan27"},
                                MODEL_MAP_ALIAS: {CATCH_ALL: "seedance20"}})


class TestVerifiedSlotMatchesProcedureConstant(unittest.TestCase):
    """`channel_options.py` 的 docstring 指着这个类名 —— 别改名。"""

    def test_every_verified_slot_has_a_live_procedure_constant(self):
        """已实测槽位必须与 `web_client.CREATE_PROCEDURE` 同源（否则"实测"是假的）。"""
        seg = web_client.CREATE_PROCEDURE.split(".", 1)[-1]
        self.assertIn(seg, VERIFIED_SLOTS, "实测槽位集合与 procedure 常量脱节了")

    def test_known_slots_cover_site_keys(self):
        """值域必须覆盖站点那 11 个模型键 —— 少一个就等于把一个合法名字变成 400。"""
        for key in ("seedance20", "wan27", "seedance25", "kling3", "veo31Fast"):
            with self.subTest(key=key):
                self.assertIn(key, KNOWN_SLOTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
