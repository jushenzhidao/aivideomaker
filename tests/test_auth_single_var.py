#!/usr/bin/env python3
"""鉴权**单变量**（`AVM_AUTH`）门禁：三态解析、旧变量拒绝、以及"不可表达性"。

## 为什么收成一个变量（0.0.20）

闸门与透传**互斥** —— 单一 `Authorization: Bearer` 不可能既当闸门密钥又当上游凭据。
以前用两个变量（`AVM_GATE_KEY` + `AVM_PASSTHROUGH_COOKIE`）表达，那个"必然 401"的组合
只能靠 `validate()` 拦下来；而它的两个失效方向都是**静默**的：

  * 忽略一个非空的 `AVM_GATE_KEY` ⇒ 闸门无声消失（服务对任何调用方开放）；
  * 忽略 `AVM_PASSTHROUGH_COOKIE=1` ⇒ 透传无声失效（调用方的 Bearer 被当成闸门密钥）。

所以旧变量**只要还有行为就拒绝启动**（不是警告），并把迁移写法打在报错里。
"只写了关值"（`=0`、空串）的旧部署与留空**语义完全一致**，故不拦、只记一条提示 ——
否则 compose 里那句 `AVM_GATE_KEY: ${AVM_GATE_KEY:-}` 会让每次启动都刷一条噪音。

## 守什么（五层，每层都能被证伪）

1. **三态解析**：留空/`open`/`none` ｜ `passthrough` ｜ `key:<密钥>`；大小写与空白不敏感；
   密钥自身可含 `:`。
2. **不猜意图**：裸密钥、`key:` 空密钥、`gate:` 之类近似写法一律拒绝（猜错的代价不对称 ——
   把"我想开闸门"猜成别的模式，结果就是闸门静默消失）。
3. **废弃变量拒绝 + 精确迁移映射**，且**不回显密钥**（报错要能贴进工单/日志）。
4. **不可表达性**：穷举 env 组合，任何能构造出来的 Settings 都不会同时是闸门与透传。
5. **接线**：真实进程环境读得到；`/healthz` 如实报出模式；CI 冒烟用的是新写法
   （否则下一次发版会在冒烟那一步红）。

全部离线：不连上游、不发真实请求、零计费。
运行：python3 tests/test_auth_single_var.py
"""

import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENV_EXAMPLE = ROOT / ".env.example"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient  # noqa: E402

from ark_compat.app import create_app  # noqa: E402
from ark_compat.settings import (  # noqa: E402
    AUTH_FORMS,
    AUTH_GATE,
    AUTH_OPEN,
    AUTH_PASSTHROUGH,
    LEGACY_AUTH_VARS,
    Settings,
    legacy_auth_usage,
    parse_auth,
)

DEAD_UPSTREAM = "http://127.0.0.1:9"
COOKIE = "auth_session=" + "a" * 40


def app_settings(**kw) -> Settings:
    """与 test_passthrough_cookie 同款最小夹具：不开 logfire、不落库、上游指死端口。"""
    base = dict(
        base_url=DEAD_UPSTREAM,
        log_level="WARNING",
        enable_logfire=False,
        trust_env=False,
        task_store="memory",
    )
    base.update(kw)
    return Settings(**base)


# ------------------------------------------------------------ 取值写法（第 1、2 层）--


class TestParseAuth(unittest.TestCase):
    """判据只此一份：真实门禁与变异自检**共用** `parse_auth`（避免"断言另一个写法"）。"""

    def test_three_modes(self):
        self.assertEqual(parse_auth(None), (AUTH_OPEN, "", ""))
        self.assertEqual(parse_auth(""), (AUTH_OPEN, "", ""))
        self.assertEqual(parse_auth("open"), (AUTH_OPEN, "", ""))
        self.assertEqual(parse_auth("none"), (AUTH_OPEN, "", ""))
        self.assertEqual(parse_auth("passthrough"), (AUTH_PASSTHROUGH, "", ""))
        self.assertEqual(parse_auth("key:sk-x"), (AUTH_GATE, "sk-x", ""))

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(parse_auth("  PASSTHROUGH  ")[0], AUTH_PASSTHROUGH)
        self.assertEqual(parse_auth(" KEY:sk-x ")[:2], (AUTH_GATE, "sk-x"))

    def test_secret_may_contain_colons_and_keeps_its_case(self):
        self.assertEqual(parse_auth("key:sk:a:B")[:2], (AUTH_GATE, "sk:a:B"))

    def test_bare_secret_is_refused(self):
        """★ 裸密钥**不猜**：闸门必须显式 `key:`（否则与模式名分不开，猜错即闸门消失）。"""
        mode, key, problem = parse_auth("sk-abc")
        self.assertTrue(problem)
        self.assertEqual((mode, key), (AUTH_OPEN, ""), "非法取值绝不能带着密钥放行")

    def test_empty_gate_secret_is_refused(self):
        _, key, problem = parse_auth("key:")
        self.assertTrue(problem)
        self.assertEqual(key, "")

    def test_problem_message_lists_the_accepted_forms(self):
        for bad in ("gate:sk-x", "sk-abc", "key:"):
            with self.subTest(value=bad):
                _, _, problem = parse_auth(bad)
                for frag in ("open", "passthrough", "key:"):
                    self.assertIn(frag, problem, f"{bad!r} 的报错没有列全可选写法")

    def test_mutation_proof_every_plausible_wrong_value_is_caught(self):
        """变异自证：把旧写法/近似写法逐个喂进去，**每一个**都必须被拒且不带走密钥。"""
        for bad in ("1", "true", "yes", "on", "sk-abc", "key:", "key =x",
                    "gate:x", "passthrouh", "PASSTHROUGH=1"):
            with self.subTest(value=bad):
                mode, key, problem = parse_auth(bad)
                self.assertTrue(problem, f"{bad!r} 竟然被当成合法取值")
                self.assertEqual(key, "")
                self.assertEqual(mode, AUTH_OPEN)


# ------------------------------------------------------- 旧变量迁移（第 3 层）----


class TestLegacyRefusal(unittest.TestCase):
    def test_gate_key_is_refused_with_a_mapping(self):
        with self.assertRaises(ValueError) as ctx:
            Settings.from_env({"AVM_GATE_KEY": "sk-old"})
        msg = str(ctx.exception)
        self.assertIn("AVM_GATE_KEY", msg)
        self.assertIn("AVM_AUTH=key:", msg)

    def test_passthrough_switch_is_refused_with_a_mapping(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    Settings.from_env({"AVM_PASSTHROUGH_COOKIE": value})
                self.assertIn("AVM_AUTH=passthrough", str(ctx.exception))

    def test_refusal_never_echoes_the_secret(self):
        """报错会被贴进工单/日志 ⇒ 绝不能让闸门密钥顺着迁移提示漏出去。"""
        with self.assertRaises(ValueError) as ctx:
            Settings.from_env({"AVM_GATE_KEY": "sk-super-secret-value"})
        self.assertNotIn("sk-super-secret-value", str(ctx.exception))

    def test_both_legacy_vars_are_listed_with_both_mappings(self):
        with self.assertRaises(ValueError) as ctx:
            Settings.from_env({"AVM_GATE_KEY": "sk-old", "AVM_PASSTHROUGH_COOKIE": "1"})
        msg = str(ctx.exception)
        self.assertIn("AVM_AUTH=key:", msg)
        self.assertIn("AVM_AUTH=passthrough", msg)
        self.assertIn("删掉", msg)

    def test_new_and_old_together_still_refuses(self):
        """新变量写了但旧变量没删 ⇒ 仍拒绝：两个来源并存时"谁生效"只能靠猜。"""
        with self.assertRaises(ValueError):
            Settings.from_env({"AVM_AUTH": "key:new", "AVM_GATE_KEY": "old"})

    def test_explicit_off_value_is_allowed_but_flagged(self):
        """`=0` 与留空语义完全一致（那正是旧模板的默认值）⇒ 不拦，只记一条提示。"""
        s = Settings.from_env({"AVM_PASSTHROUGH_COOKIE": "0"})
        self.assertEqual(s.auth, AUTH_OPEN)
        self.assertEqual(s.auth_deprecated, ("AVM_PASSTHROUGH_COOKIE",))

    def test_blank_legacy_values_are_treated_as_unset(self):
        """空串 = 没设（本项目口径：模板里 `VAR=` 就是没设）。
        否则 compose 里那句 `AVM_GATE_KEY: ${AVM_GATE_KEY:-}` 会给每次启动都刷一条噪音。
        """
        s = Settings.from_env({"AVM_GATE_KEY": "", "AVM_PASSTHROUGH_COOKIE": ""})
        self.assertEqual(s.auth, AUTH_OPEN)
        self.assertEqual(s.auth_deprecated, ())

    def test_legacy_usage_classification(self):
        """分类判据本身（两个数组各自的含义），单独钉一遍。"""
        self.assertEqual(legacy_auth_usage({}), ((), ()))
        self.assertEqual(legacy_auth_usage({"AVM_GATE_KEY": "x"}), (("AVM_GATE_KEY",), ()))
        self.assertEqual(legacy_auth_usage({"AVM_PASSTHROUGH_COOKIE": "true"}),
                         (("AVM_PASSTHROUGH_COOKIE",), ()))
        self.assertEqual(legacy_auth_usage({"AVM_PASSTHROUGH_COOKIE": "0"}),
                         ((), ("AVM_PASSTHROUGH_COOKIE",)))
        self.assertEqual(legacy_auth_usage({"AVM_PASSTHROUGH_COOKIE": "  "}), ((), ()))

    def test_legacy_names_are_the_documented_ones(self):
        """迁移指引与拒绝逻辑必须盯同一组名字（漏一个 ⇒ 那个变量会静默失效）。"""
        self.assertEqual(set(LEGACY_AUTH_VARS), {"AVM_GATE_KEY", "AVM_PASSTHROUGH_COOKIE"})


# ------------------------------------------------- 不可表达性（第 4 层）----


class TestModeIsUnambiguous(unittest.TestCase):
    def test_no_env_combination_yields_gate_and_passthrough_together(self):
        """穷举：能构造出来的 Settings 里，闸门与透传**不可能同时为真**。"""
        auths = [None, "", "open", "none", "passthrough", "key:sk-x", "1", " nope "]
        legacies = [None, "", "0", "1"]
        for auth in auths:
            for legacy in legacies:
                env = {"AVM_COOKIE": COOKIE}
                if auth is not None:
                    env["AVM_AUTH"] = auth
                if legacy is not None:
                    env["AVM_PASSTHROUGH_COOKIE"] = legacy
                with self.subTest(auth=auth, legacy=legacy):
                    try:
                        s = Settings.from_env(env)
                    except ValueError:
                        continue        # 被拒也是合法结果（本测试只关心"能构造出来的"）
                    self.assertFalse(
                        s.gate_key and s.passthrough_cookie,
                        "闸门与透传同时为真 ⇒ 那个必然 401 的组合又变得可表达了",
                    )
                    self.assertEqual(s.passthrough_cookie, s.auth == AUTH_PASSTHROUGH)
                    self.assertEqual(bool(s.gate_key), s.auth == AUTH_GATE)

    def test_validate_still_guards_in_code_built_settings(self):
        """环境变量层已无法表达，但**代码内直接构造**仍可能（测试夹具/嵌入用法）——
        `validate()` 继续兜住，且报错要指向单变量写法。"""
        with self.assertRaises(ValueError) as ctx:
            Settings(cookie=COOKIE, gate_key="sk-g", passthrough_cookie=True).validate()
        self.assertIn("AVM_AUTH", str(ctx.exception))


# ------------------------------------------------------------ 接线（第 5 层）----


class TestWiring(unittest.TestCase):
    def test_real_process_env_is_read(self):
        """`from_env()` 不带参数时读的是**真实环境**（不是只看显式传入的字典）。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith("AVM_")}
        env["AVM_AUTH"] = "key:from-os-environ"
        code = (f"import sys; sys.path.insert(0, r'{SRC}');"
                "from ark_compat.settings import Settings;"
                "s = Settings.from_env(); print(s.auth, s.gate_key)")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        # `gate_key` 是**密钥本体**（不含 `key:` 前缀）—— 前缀只是写法约定
        self.assertEqual(out.stdout.split(), [AUTH_GATE, "from-os-environ"])

    def test_healthz_reports_each_mode(self):
        """`/healthz` 是排障入口：模式必须如实报出（旧三键保留，供既有巡检取用）。"""
        cases = [
            (app_settings(cookie=COOKIE), AUTH_OPEN, "open", False),
            (app_settings(cookie=COOKIE, gate_key="sk-g"), AUTH_GATE, "required", False),
            # 直接构造（不经 from_env）也必须报出**行为**对应的模式 —— `auth` 是派生属性
            (app_settings(passthrough_cookie=True), AUTH_PASSTHROUGH, "open", True),
        ]
        for st, auth, gate, pt in cases:
            with self.subTest(auth=auth):
                st.validate()
                j = TestClient(create_app(st)).get("/healthz").json()
                self.assertEqual(j["auth"], auth)
                self.assertEqual(j["gate"], gate)
                self.assertEqual(j["passthrough_cookie"], pt)
                self.assertEqual(j["credentials_from_caller"], pt)

    def test_template_declares_only_the_single_variable(self):
        text = "\n" + ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("\nAVM_AUTH=", text, "模板必须声明 AVM_AUTH（否则运维不知道有这个旋钮）")
        for legacy in LEGACY_AUTH_VARS:
            self.assertNotIn(f"\n{legacy}=", text,
                             f"{legacy} 已废弃，不能在模板里声明（注释里可以提到它）")

    def test_ci_smoke_uses_the_single_variable(self):
        """★ CI 冒烟若还留在旧写法，下一次发版会在冒烟那一步红 —— 提前在这里钉住。"""
        text = RELEASE_YML.read_text(encoding="utf-8")
        self.assertIn("AVM_AUTH=passthrough", text)
        for legacy in LEGACY_AUTH_VARS:
            self.assertNotIn(f"-e {legacy}=", text,
                             f"冒烟命令里不该再出现 {legacy}（容器会拒绝启动）")

    def test_compose_passes_the_single_variable_through(self):
        """compose 必须把 `AVM_AUTH` 转发进容器（漏了 ⇒ 配置在新部署上静默无效）。"""
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("AVM_AUTH: ${AVM_AUTH:-}", text)

    def test_docs_do_not_teach_the_deprecated_switches(self):
        """文档（README / 包 README）里不该再有"照着配就用旧变量"的指令。

        只查**赋值形态**（`AVM_GATE_KEY=` / `AVM_PASSTHROUGH_COOKIE=1`），
        因为解释迁移时提到的名字是必要的。
        """
        for rel in ("README.md", "src/ark_compat/README.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                for legacy, bad in (("AVM_GATE_KEY", "AVM_GATE_KEY="),
                                    ("AVM_PASSTHROUGH_COOKIE", "AVM_PASSTHROUGH_COOKIE=")):
                    self.assertNotIn(bad, text, f"{rel} 里还在教 {legacy} 的赋值写法")


if __name__ == "__main__":
    unittest.main(verbosity=2)
