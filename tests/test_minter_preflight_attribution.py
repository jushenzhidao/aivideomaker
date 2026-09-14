#!/usr/bin/env python3
"""`minter-preflight` 的**判据与归因**门禁（报告 AVM12-PF-01…P-09 的固化）。

## 为什么直接从 compose 里抽脚本跑

preflight 不是独立脚本，而是 `docker-compose.yml` 里 `minter-preflight` 服务的 `command`
（一段内联 Python）。把它手抄一份进测试 = 守着一份会漂移的副本 —— 本项目已经反复吃过
"手维护清单必漏"的亏（见 `test_docs_billing_sync.py` 的两轮范围修正）。所以这里**解析
compose、抽出那段源码、在受控 env 下真跑一遍**，守的是"实际会执行的东西"。

## 这一版重点守什么

**归因必须与真实放行原因对得上**（报告 AVM12-PF-07）。第一版的判据是
`if MINTER_KEY … elif allow_insecure … else 回环` —— 只挑一条，而 `MINTER_ALLOW_INSECURE`
在 compose 默认值下恒为 True ⇒ **回环那条分支永远轮不到**：`BIND_HOST=127.0.0.1` 放行时
会打印"显式放行 MINTER_ALLOW_INSECURE"，把人引去查一个**根本没设过**的开关。
修法两条（都有对应用例）：列出**所有**成立的充分理由；以及用
`MINTER_ALLOW_INSECURE_RAW` 把"显式设置"与"取默认值"分开。

运行：python3 tests/test_minter_preflight_attribution.py
"""

import contextlib
import io
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))      # 让内联脚本的 `import turnstile_service` 落到实处

COMPOSE = ROOT / "docker-compose.yml"
SERVICE = "minter-preflight"

# compose 注入给 preflight 的变量 —— 这些就是**脚本自己会读**的全部 env。
# 每个用例都必须把它们显式给定，否则测到的是"宿主机恰好存在的环境"，结论无效。
MANAGED = (
    "MINTER_KEY", "ARK_MINTER_KEY", "MINTER_ALLOW_INSECURE", "MINTER_ALLOW_INSECURE_RAW",
    "BIND_HOST", "ARK_MINTER_URL",
)

# 默认档 = compose 在用户什么都没配时的实际注入值
DEFAULT_ENV = {
    "MINTER_KEY": "",
    "ARK_MINTER_KEY": "",
    "MINTER_ALLOW_INSECURE": "1",
    "MINTER_ALLOW_INSECURE_RAW": "",
    "BIND_HOST": "0.0.0.0",
    "ARK_MINTER_URL": "http://host.docker.internal:8899",
}


def preflight_source() -> str:
    """从 compose 里**机械抽出** preflight 的内联 Python（零依赖的行内解析）。

    不依赖 pyyaml：这段源码要被"实际执行"，抽取路径也必须永远可用。抽不到就 AssertionError
    —— 宁可测试红，也不要静默地什么都不测。
    """
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next((i for i, raw in enumerate(lines) if raw.rstrip() == f"  {SERVICE}:"), None)
    assert start is not None, f"compose 里找不到服务 {SERVICE}"

    body: list = []
    seen_command = False
    indent = None
    for raw in lines[start + 1:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cur = len(raw) - len(raw.lstrip())
        if cur == 2 and raw.strip().endswith(":"):      # 下一个服务 ⇒ 块结束
            break
        if indent is None:
            if not seen_command and raw.strip() == "command:":
                seen_command = True
            elif seen_command and raw.strip() == "- |":
                indent = cur + 2
            continue
        if cur < indent:
            break
        body.append(raw[indent:])
    assert body, f"没能从 compose 抽出 {SERVICE} 的内联脚本"
    return "\n".join(body)


def run_preflight(**overrides) -> tuple:
    """在受控 env 下执行 preflight，返回 `(退出码, stdout, stderr)`。

    ⚠️ 脚本把 `[ok]` 打到 **stdout**、把 `[warn]/[fatal]` 打到 **stderr** ⇒ 断言"归因"时
    必须两边都看（只看 stderr 会拿到空串，然后"什么都没说"与"说错了"就分不出来了）。
    """
    env = dict(DEFAULT_ENV)
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    clean = {k: v for k, v in os.environ.items() if k not in MANAGED}
    clean.update(env)

    code = compile(preflight_source(), f"<{SERVICE} from docker-compose.yml>", "exec")
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, clean, clear=True):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(code, {"__name__": "__main__"})
            except SystemExit as e:
                return int(e.code or 0), out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


def run(**overrides) -> tuple:
    """`(退出码, 合并输出, 归因串)` —— 归因串是 `[ok] …（这里）` 的括号内容。"""
    code, out, err = run_preflight(**overrides)
    merged = out + err
    m = re.search(r"\[ok\] minter 前置校验通过（(.+)）", merged)
    return code, merged, (m.group(1) if m else "")


class TestPreflightSourceIsReal(unittest.TestCase):
    def test_the_extracted_script_is_actually_the_preflight(self):
        src = preflight_source()
        self.assertIn("bind_guard_error", src)
        self.assertIn("/app/tools", src)

    def test_extraction_is_not_silently_empty_or_truncated(self):
        """防**空转**：抽到的必须是一段能编译的完整脚本，且覆盖判据的每一支。"""
        src = preflight_source()
        compile(src, "<inline>", "exec")
        for anchor in ("MINTER_ALLOW_INSECURE_RAW", "LOOPBACK_HOSTS", "ARK_MINTER_URL"):
            self.assertIn(anchor, src, f"抽到的脚本里没有 {anchor} —— 抽取被截断了")

    def test_the_entrypoint_matches_the_service(self):
        """抽取的锚点与 compose 里的变量名必须对得上（变量名改了而这里没改 ⇒ 静默测空）。"""
        for name in MANAGED:
            with self.subTest(var=name):
                self.assertIn(name, preflight_source(), f"compose 里已经不看 {name} 了？")


class TestAttribution(unittest.TestCase):
    """★ PF-07：归因必须与真实放行原因一致。"""

    def test_loopback_binding_is_attributed_to_the_binding(self):
        """核心回归：回环绑定放行时，**不许**说成"显式放行 ALLOW_INSECURE"。

        默认档下 `MINTER_ALLOW_INSECURE=1` 是 compose 给的默认值，没人显式设过它；
        真正让它过的是回环绑定。旧判据在这一幕会指向一个不存在的开关。
        """
        code, merged, why = run(BIND_HOST="127.0.0.1")
        self.assertEqual(code, 0)
        self.assertIn("回环", why, f"归因没指向真正的理由：{why!r}")
        self.assertNotIn("显式设了", why, f"默认档下不该出现『显式设了』，归因不实：{why!r}")
        self.assertNotIn("显式放行", why, f"默认档下不该出现『显式放行』，归因不实：{why!r}")
        # 回环绑定必须**不**再喊"开放模式"（外部根本连不上，跟着喊只会让人对告警脱敏）
        self.assertNotIn("开放模式", merged)

    def test_default_config_is_attributed_to_the_compose_default_not_to_a_user_choice(self):
        code, merged, why = run()
        self.assertEqual(code, 0)
        self.assertIn("默认", why, f"默认档应归因到『取默认值』：{why!r}")
        self.assertNotIn("显式设了", why)
        self.assertNotIn("显式放行", why)
        self.assertIn("开放模式", merged, "默认档（无 key + 非回环）必须喊出开放模式")

    def test_explicit_allow_insecure_is_attributed_to_the_flag(self):
        code, _, why = run(MINTER_ALLOW_INSECURE_RAW="1")
        self.assertEqual(code, 0)
        self.assertIn("显式设了 MINTER_ALLOW_INSECURE", why)

    def test_key_is_attributed_to_the_key(self):
        code, merged, why = run(MINTER_KEY="k", ARK_MINTER_KEY="k")
        self.assertEqual(code, 0)
        self.assertIn("MINTER_KEY 已设", why)
        self.assertNotIn("开放模式", merged)

    def test_all_sufficient_reasons_are_listed_not_just_one(self):
        """三条理由同时成立时就该都列出来 —— 只挑一条正是 PF-07 的根因。"""
        code, _, why = run(
            MINTER_KEY="k", ARK_MINTER_KEY="k", BIND_HOST="127.0.0.1", MINTER_ALLOW_INSECURE_RAW="1"
        )
        self.assertEqual(code, 0)
        for part in ("MINTER_KEY 已设", "回环", "显式设了"):
            self.assertIn(part, why, f"归因漏了成立的理由 {part!r}：{why!r}")

    def test_undecidable_raw_marker_is_admitted_instead_of_guessed(self):
        """少了 RAW 就别猜：如实说"分不清显式设置还是默认值"。"""
        code, _, why = run(MINTER_ALLOW_INSECURE_RAW=None)
        self.assertEqual(code, 0)
        self.assertIn("分不清", why)

    def test_the_old_lying_wording_never_comes_back(self):
        """把被删掉的措辞钉死 —— 它一旦回来，就说明判据又被改成了"只挑一条"。"""
        self.assertNotIn("显式放行 MINTER_ALLOW_INSECURE", preflight_source())


class TestVerdicts(unittest.TestCase):
    """判据本身（放行 / 拦住）不能因为改归因而被改坏。"""

    def test_fail_closed_is_still_restored_when_the_flag_is_zero(self):
        code, merged, _ = run(MINTER_ALLOW_INSECURE="0", MINTER_ALLOW_INSECURE_RAW="0")
        self.assertEqual(code, 2)
        self.assertIn("前置校验未通过", merged)

    def test_mismatched_keys_are_fatal(self):
        """P-05：`-e MINTER_KEY=…` 只喂一侧 ⇒ 仍然必须拦住。"""
        code, merged, _ = run(MINTER_KEY="sk-only-minter")
        self.assertEqual(code, 2)
        self.assertIn("接线错误", merged)
        # 同时要如实说明这条自检的**边界**（P-08），否则读者会以为它守着服务侧
        self.assertIn("compose_wiring_check.py", merged)

    def test_empty_minter_url_warns_about_the_silently_disabled_capability(self):
        code, merged, _ = run(ARK_MINTER_URL="")
        self.assertEqual(code, 0)
        self.assertIn("铸造能力被静默关掉", merged)

    def test_a_wrong_key_on_the_ark_side_is_also_caught(self):
        """反向：只改 ark 侧的注入值同样要拦（两侧都必须一致）。"""
        code, merged, _ = run(MINTER_KEY="k", ARK_MINTER_KEY="other")
        self.assertEqual(code, 2)
        self.assertIn("接线错误", merged)


if __name__ == "__main__":
    unittest.main(verbosity=2)
