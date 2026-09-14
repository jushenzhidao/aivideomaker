#!/usr/bin/env python3
"""铸造服务的**绑定安全门禁**：非回环 + 无凭据 ⇒ 拒绝启动（不得 fail-open）。

为什么这条门禁值得单独钉住（2026-09-14 审计发现，本文件即其修复）：
  铸造服务产出的 Turnstile token 能**直接过掉上游的提交闸门**，等价于"把过闸能力
  分发出去"。容器形态是 `BIND_HOST=0.0.0.0` ＋ `network_mode: host` ⇒ 0.0.0.0
  就是宿主公网地址。而原实现里 `MINTER_KEY` 未设时**只 print 一行 warn 就照常服务**
  ⇒ 匿名 `curl -X POST :8899/v1/turnstile/mint` 即可领 token、还能把池子刷干。

  这是全项目**唯一**的 fail-open 点：其余入口一律 fail-closed
  （`Settings.validate()` 拒绝启动、`ARK_HOST` 必须显式设、`AVM_TASK_STORE` 写错即抛错）。

三条必须钉住：
1. **非回环 + 无 key ⇒ 拒启动**（且必须真的不监听端口）；
2. 回环 + 无 key **照旧放行** —— 本机自用是最常见用法，不能把正常路径一起堵掉；
3. 拒绝信息必须给出**可执行的出路**（设 key / 显式自担风险 / 退回回环），
   否则运维只会看到"服务起不来"而无从下手。

运行：python3 tests/test_minter_bind_guard.py
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import turnstile_service as S  # noqa: E402  （模块级只读环境变量，不起 Chrome）


class TestBindGuard(unittest.TestCase):
    """直接对判定函数做穷举 —— 这是门禁的核心，必须逐格覆盖。"""

    def test_nonloopback_without_key_is_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.10", "minter.example.com"):
            with self.subTest(host=host):
                self.assertIsNotNone(
                    S.bind_guard_error(host, ""),
                    f"BIND_HOST={host} 且无 MINTER_KEY 必须拒绝启动",
                )

    def test_loopback_without_key_is_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertIsNone(
                    S.bind_guard_error(host, ""),
                    f"BIND_HOST={host} 是本机自用，必须放行",
                )

    def test_nonloopback_with_key_is_allowed(self):
        self.assertIsNone(S.bind_guard_error("0.0.0.0", "s3cret"))

    def test_whitespace_only_key_counts_as_missing(self):
        # 空白串不是凭据：`export MINTER_KEY=" "` 这类手误会让人以为配上了
        self.assertIsNotNone(
            S.bind_guard_error("0.0.0.0", "   "),
            "只有空白的 MINTER_KEY 必须按未设置处理",
        )
        self.assertIsNone(S.bind_guard_error("0.0.0.0", "   ", allow_insecure=True))

    def test_insecure_override_is_explicit_and_works(self):
        self.assertIsNone(
            S.bind_guard_error("0.0.0.0", "", allow_insecure=True),
            "显式自担风险的开关必须能放行（否则私有网络部署无路可走）",
        )

    def test_refusal_message_names_all_three_ways_out(self):
        msg = S.bind_guard_error("0.0.0.0", "") or ""
        # 只说"不行"的门禁会把问题推给运维；必须自带出路
        self.assertIn("MINTER_KEY", msg)
        self.assertIn("MINTER_ALLOW_INSECURE", msg)
        self.assertIn("127.0.0.1", msg)

    def test_insecure_flag_parsing_is_a_whitelist(self):
        """安全开关的解析必须走**白名单**：判错的代价不对称。

        把"关闭"误判成"开启" ⇒ 铸造能力直接暴露到公网；反之只是多设一个 key。
        所以任何不认识的写法（含拼错的 `ture`、`0`、空串）都必须判为关闭。
        """
        for raw in ("1", "true", "True", "TRUE", "yes", "on", " 1 ", "\ton\n"):
            with self.subTest(raw=raw):
                self.assertTrue(S.env_flag(raw), f"{raw!r} 应判为开启")
        for raw in ("", " ", "0", "false", "False", "no", "off",
                    "ture", "enable", "2", "nope"):
            with self.subTest(raw=raw):
                self.assertFalse(S.env_flag(raw), f"{raw!r} 必须判为关闭（白名单）")


class TestRefuseToStartForReal(unittest.TestCase):
    """端到端：真的起一次进程，断言**退出码非 0** 且**没有开始监听**。

    这是本门禁的关键一格 —— 只测函数返回值的话，"忘了在 main() 里调用它"
    会让测试全绿而缺陷原样存在（典型虚假绿灯）。
    """

    def _run(self, env_extra: dict, timeout: float = 15.0):
        env = {k: v for k, v in os.environ.items()
               if k not in ("MINTER_KEY", "MINTER_ALLOW_INSECURE", "BIND_HOST", "PORT")}
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(TOOLS / "turnstile_service.py")],
            capture_output=True, text=True, env=env, timeout=timeout,
            cwd=str(ROOT),
        )

    def test_exits_nonzero_and_warns_when_exposed_without_key(self):
        p = self._run({"BIND_HOST": "0.0.0.0", "PORT": "18899"})
        self.assertNotEqual(p.returncode, 0,
                            "暴露到非回环且无 key 时必须拒绝启动（不能 fail-open 继续服务）")
        self.assertIn("拒绝启动", p.stderr, "必须在 stderr 明确说明原因与出路")
        self.assertIn("MINTER_KEY", p.stderr)
        # 绝不能出现"已启动"那句 —— 那意味着它还是监听上了
        self.assertNotIn("已启动", p.stdout,
                         "拒绝启动的路径不得打印启动成功行（也不得真的绑定端口）")


if __name__ == "__main__":
    unittest.main()
