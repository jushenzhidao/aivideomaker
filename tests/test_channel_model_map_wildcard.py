#!/usr/bin/env python3
"""门禁：`X-Channel-Options.model_map` 的**通配键**（`*`）。

## 为什么需要它

`model_map` 的通配支持落在 `src/ark_compat/channel_options.py`
（`_glob_regex` / `_specificity` / `_wildcard_hit`）。该模块 docstring 里承诺了三条性质：

1. **整串匹配**（不是前缀/子串匹配）；
2. **特异性**：被多个通配键命中时取"模式里**非通配字符更多**"的那个；
3. **与书写顺序无关**（"JSON 对象顺序不该决定计费档位"）。

承诺 ≠ 事实。本门禁把它们逐条钉成断言。

## 怎么证伪（变异测试）—— **本表已逐条实跑确认**

| 变异 | 期望变红 | 为什么必须用这个用例 |
|---|---|---|
| `_glob_regex` 去掉 `\\Z`（退化成前缀匹配） | `test_full_match_not_prefix` | ⚠️ **`doubao-*` 抓不到它** —— 那种情况的失配发生在**起始锚**上。必须用**通配符后还有字面量**的模式（`*pro` vs `pro-x`），失配才发生在**结尾锚**上 |
| `_specificity` 改成数**模式串总长** | `test_specificity_is_not_total_length` | 需要一对"字面量多但更短" vs "字面量少但更长"的模式；**真实模式名几乎不会让两者冲突**，故用退化但合法的 `**a**`（字面量 1 / 总长 5）对 `ab*`（字面量 2 / 总长 3） |
| `_wildcard_hit` 的比较 `>` 改成 `>=` | `test_result_is_independent_of_key_order` | 必须让映射表里存在**同分**且都命中的两条；只有"字面量各不相同"的模式集时，`>` 与 `>=` 结果一样 |
| `WILDCARD` 改成 `"**"` | 全部通配用例 | — |

**这四条第一次写的时候有三条没红** —— 因为用例选得不对（示例看不住锚点、模式集无同分项）。
留着这段记录：**"写了门禁"和"门禁真的会拦"是两件事**。

**纯函数、零网络、零计费。**
"""

import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ark_compat import web_client  # noqa: E402
from ark_compat.channel_options import (  # noqa: E402
    KNOWN_SLOTS,
    MODEL_MAP_ALIAS,
    MODEL_MAP_KEY,
    SOURCE_MODEL_MAP,
    SOURCE_PASSTHROUGH,
    VERIFIED_SLOTS,
    _glob_regex,
    _specificity,
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


class TestGlobSemantics(unittest.TestCase):
    """`*` 的匹配语义（docstring 第 1 条）。"""

    def test_star_matches_any_and_empty(self):
        for name in ("doubao-seedance-1-0-pro", "doubao-seedance-x", "doubao-seedance-"):
            with self.subTest(name=name):
                self.assertTrue(_glob_regex("doubao-seedance-*").match(name))

    def test_full_match_not_prefix(self):
        """整串匹配 —— 失配必须发生在**结尾**，所以模式里得有"通配符之后的字面量"。

        ⚠️ 反面教材：只测 `doubao-*` vs `x-doubao-1` 是**看不住**这条的 ——
        那种失配发生在起始锚上，去掉结尾锚（`\\Z`）照样不匹配 ⇒ 门禁不会红。
        """
        # 通配符在中间/开头，字面量在**末尾**：这才压到结尾锚
        self.assertIsNone(_glob_regex("*pro").match("pro-x"), "前缀匹配了 ⇒ 结尾锚失效")
        self.assertIsNone(_glob_regex("a*1").match("a-1-x"))
        self.assertIsNotNone(_glob_regex("*pro").match("x-pro"))
        # 端到端：不命中就该 400，绝不许"静默用了某个槽位"
        with self.assertRaises(ParamError) as ctx:
            resolve("pro-x", {"*pro": "seedance20"})
        self.assertIn("unknown model", str(ctx.exception))
        self.assertIn("*pro", str(ctx.exception), "报文必须列出表里已声明的键，便于排障")

    def test_only_star_is_a_wildcard(self):
        """只支持 `*`（模块明确"不做正则"）—— `?` `.` `[` 一律按**字面量**。

        防的是：一条写错的键（如 `a[bc]d`）被当成正则，静默指到别的槽位。
        """
        self.assertIsNone(_glob_regex("a?b").match("axb"))
        self.assertIsNotNone(_glob_regex("a?b").match("a?b"))
        self.assertIsNone(_glob_regex("a.c").match("abc"))
        self.assertIsNotNone(_glob_regex("a.c").match("a.c"))
        self.assertIsNone(_glob_regex("a[bc]d").match("abd"))

    def test_catch_all_is_last_resort(self):
        r = resolve("anything", {"*": "minimaxH3"})
        self.assertEqual((r.slot, r.source), ("minimaxH3", SOURCE_MODEL_MAP))
        r2 = resolve("doubao-x", {"doubao-*": "seedance20", "*": "minimaxH3"})
        self.assertEqual(r2.slot, "seedance20")


class TestSpecificity(unittest.TestCase):
    """特异性规则（docstring 第 2 条）：比**非通配字符个数**，不比模式串总长。"""

    def test_more_literal_chars_wins(self):
        r = resolve("abc", {"a*": "seedance20", "ab*": "kling3"})
        self.assertEqual(r.slot, "kling3", "非通配字符更多的模式应胜出")

    def test_specificity_is_not_total_length(self):
        """这两条模式的**两种排序方向相反** ⇒ 谁胜出可判别实现用的是哪一种。

        `ab*`      : 字面量 2、总长 3
        `**a**`    : 字面量 1、总长 5
        正确的实现（按字面量）判 `ab*` 胜；按总长则会判 `**a**` 胜。
        """
        assert _specificity("ab*")[0] > _specificity("**a**")[0]
        assert len("ab*") < len("**a**")
        r = resolve("abc", {"**a**": "kling3", "ab*": "seedance20"})
        self.assertEqual(r.slot, "seedance20", "特异性被算成了模式串总长")

    def test_tie_is_broken_deterministically(self):
        """同分时按模式串字典序 —— 必须**确定**，不能依赖遍历顺序。"""
        a = resolve("abc", {"a*c": "seedance20", "ab*": "kling3"})
        b = resolve("abc", {"ab*": "kling3", "a*c": "seedance20"})
        self.assertEqual(a.slot, b.slot, "同分时的裁决随书写顺序变了")


class TestOrderIndependence(unittest.TestCase):
    """docstring 第 3 条：结果与 JSON 键序**无关**（全排列性质测试）。

    ⚠️ 模式集里**必须含一对同分且都命中的键**（`a*c` 与 `ab*`，字面量各 2）——
    否则 `>` 与 `>=` 行为一致，把比较写错也测不出来。
    """

    PAIRS = [("a*c", "seedance20"), ("ab*", "kling3"), ("*", "minimaxH3")]

    def test_result_is_independent_of_key_order(self):
        results = set()
        for perm in itertools.permutations(self.PAIRS):
            results.add(resolve("abc", dict(perm)).slot)
        self.assertEqual(
            len(results), 1,
            f"键序不同的映射表给出了**不同**槽位：{results} —— 「对象顺序不该决定计费档位」被破坏",
        )

    def test_the_set_actually_contains_a_tie(self):
        """前提断言：上面那条之所以能判别 `>`/`>=`，靠的是这一对同分。"""
        self.assertEqual(_specificity("a*c")[0], _specificity("ab*")[0])
        self.assertTrue(_glob_regex("a*c").match("abc") and _glob_regex("ab*").match("abc"))


class TestPrecedence(unittest.TestCase):
    """判定顺序：① 精确 ② 透传 ③ 通配 ④ 钉住 ⑤ 400。"""

    def test_exact_beats_wildcard(self):
        r = resolve("doubao-x", {"doubao-*": "kling3", "doubao-x": "seedance20"})
        self.assertEqual(r.slot, "seedance20")
        self.assertIn("exact hit", " ".join(r.warnings))

    def test_passthrough_beats_wildcard(self):
        """② 排在 ③ 前是**刻意**的（docstring 明确）：调用方指名已知槽位时不被通配改写。"""
        r = resolve("seedance20", {"seedance*": "kling3"})
        self.assertEqual((r.slot, r.source), ("seedance20", SOURCE_PASSTHROUGH))

    def test_pin_applies_when_nothing_matches(self):
        r = resolve("no-such-model", pin="minimaxH3")
        self.assertEqual(r.slot, "minimaxH3")

    def test_wildcard_match_is_warned(self):
        """通配命中**必须**留痕 —— 名字猜测表造成 7.3 倍账单差是本项目最贵的一类缺陷。"""
        r = resolve("doubao-seedance-1-0-pro", {"doubao-seedance-*": "seedance20"})
        blob = " ".join(r.warnings)
        self.assertIn("doubao-seedance-*", blob, "告警里必须点名是哪个模式命中的")
        self.assertIn("doubao-seedance-1-0-pro", blob, "告警里必须带上调用方写的名字")

    def test_suffix_and_provider_are_stripped_before_matching(self):
        for name in ("doubao-seedance-1-0-pro_1080p", "doubao-seedance-1-0-pro_720P",
                     "openai/doubao-seedance-1-0-pro"):
            with self.subTest(name=name):
                self.assertEqual(resolve(name, {"doubao-seedance-*": "seedance20"}).slot, "seedance20")

    def test_wildcards_never_see_the_resolution_suffix(self):
        """后缀在匹配**之前**被剥掉 ⇒ `*_1080p` 这种键永远命中不了（应 400 而非静默走别的槽位）。"""
        with self.assertRaises(ParamError) as ctx:
            resolve("m_1080p", {"*_1080p": "seedance20"})
        self.assertIn("unknown model", str(ctx.exception))


class TestPassthroughTakesPrecedence(unittest.TestCase):
    """判定顺序「② 透传 ③ 通配」是**刻意的**：调用方指名已知槽位时不被通配改写。

    🔴 **这里只钉"不变式"（谁胜出），刻意不钉告警文案。**
    原因：`channel_options.py` 此刻正被**另一个会话实时修改** —— 23:52 / 23:54 / 23:58
    连续三次改动，其中「通配被压过时给告警」这条在同一小时内**出现过又消失了**。
    对着移动靶钉文案，只会制造一条随时变红的假告警。

    待该文件稳定后再补两条（届时删掉本段说明）：
      · 被压过时**必须**给告警，且告警里点名「哪个模式」「它本会指向哪个槽位」；
      · 没有通配命中时**不许**出这条告警（否则告警变噪音、进而被脱敏）。
    """

    def test_passthrough_wins_over_wildcard(self):
        r = resolve("seedance20", {"seedance*": "kling3"})
        self.assertEqual((r.slot, r.source), ("seedance20", SOURCE_PASSTHROUGH))

    def test_exact_key_wins_over_wildcard(self):
        """精确键必须压过通配键（① 在 ③ 之前）—— 这是"要用映射就用精确键"的逃生口。"""
        r = resolve("seedance20", {"seedance*": "kling3", "seedance20": "kling3"})
        self.assertEqual((r.slot, r.source), ("kling3", SOURCE_MODEL_MAP))
        self.assertIn("exact hit", " ".join(r.warnings))


class TestConfigValidation(unittest.TestCase):
    """配置错误必须**拒绝且说清**（值域 / 空键 / 重复 / 别名冲突）。"""

    def test_unknown_slot_is_rejected(self):
        with self.assertRaises(ParamError) as ctx:
            resolve("m", {"m*": "not-a-slot"})
        self.assertIn("known upstream slot", str(ctx.exception))

    def test_empty_key_or_value_is_rejected(self):
        for bad in ({"": "seedance20"}, {"m*": ""}):
            with self.subTest(table=bad):
                with self.assertRaises(ParamError):
                    resolve("m", bad)

    def test_case_variant_duplicate_patterns_are_rejected(self):
        """大小写变体视为重复 —— JSON 同名键会静默覆盖，"哪条生效"直接决定账单。"""
        with self.assertRaises(ParamError) as ctx:
            resolve("m", {"X-*": "seedance20", "x-*": "kling3"})
        self.assertIn("duplicate", str(ctx.exception))

    def test_alias_is_accepted_and_conflict_is_rejected(self):
        self.assertEqual(
            resolve_model("m", {MODEL_MAP_ALIAS: {"m*": "seedance20"}}).slot, "seedance20"
        )
        with self.assertRaises(ParamError):
            resolve_model("m", {MODEL_MAP_KEY: {"m*": "seedance20"},
                                MODEL_MAP_ALIAS: {"m*": "kling3"}})


class TestVerifiedSlotMatchesProcedureConstant(unittest.TestCase):
    """`VERIFIED_SLOTS` 必须与 `web_client.CREATE_PROCEDURE` 的模型段一致。

    ⚠️ `channel_options.py` 的 docstring 把这条门禁写在
    `tests/test_channel_model_options.py::TestVerifiedSlotMatchesProcedureConstant`，
    但那个文件**不存在** ⇒ 这里补上；类名**逐字相同**，两边合并时不会出现两份。
    """

    def test_every_verified_slot_has_a_live_procedure_constant(self):
        proc = web_client.CREATE_PROCEDURE
        prefix = "ai."
        self.assertTrue(proc.startswith(prefix), f"CREATE_PROCEDURE 形态变了：{proc!r}")
        self.assertEqual(
            {proc[len(prefix):]}, set(VERIFIED_SLOTS),
            "已实测槽位集合与 CREATE_PROCEDURE 的模型段对不上 —— 其中一边是过期的",
        )

    def test_known_slots_cover_site_keys(self):
        for slot in ("minimaxH3", "seedance20", "wan27", "veo3Fast"):
            self.assertIn(slot, KNOWN_SLOTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
