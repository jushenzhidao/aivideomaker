#!/usr/bin/env python3
"""铸造器 Chrome 生命周期守卫：**绝不叠两个实例**（2026-09-14 容器实测根因）。

现场：容器里 8 小时前的旧 Chrome（`--user-data-dir=/root/avm-chrome`）没被
`pkill -f "remote-debugging-port=9222"` 杀掉 ⇒ **双实例同时监听 9222**（IPv4 + IPv6），
`cdp_ok()` 连到旧实例、页面开在旧进程 ⇒ 渲染越拖越慢 ⇒ 间歇 45s TIMEOUT；
而每次失败后只杀掉新实例 ⇒ 新旧永远叠着跑。

三条必须钉住：
1. **按进程名通杀**（`pkill -x chrome` 等，模式不含自身 ⇒ 一定匹配），再补杀按端口；
2. 杀完必须等 **9222 真正释放**才放行（老探活会连到将死实例）；
3. 释放不了就报错（SystemExit），**绝不带着旧实例继续跑**。

运行：python3 tests/test_minter_chrome_guard.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import turnstile_minter as M  # noqa: E402


class TestKillAllChrome(unittest.TestCase):
    def _patch(self, cdp_ok_values):
        """subprocess.run 记下调用；cdp_ok 按序列返回（耗尽后用最后一项）。"""
        runs = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return mock.Mock()

        it = iter(cdp_ok_values)
        last = cdp_ok_values[-1]

        def fake_cdp_ok():
            try:
                return next(it)
            except StopIteration:
                return last

        for target in ():
            pass
        mock.patch.object(M.subprocess, "run", side_effect=fake_run).start()
        mock.patch.object(M, "cdp_ok", side_effect=fake_cdp_ok).start()
        self.addCleanup(mock.patch.stopall)
        return runs

    def test_kills_by_exact_name_then_by_port(self):
        runs = self._patch([False])
        M.kill_all_chrome()
        plain = [c for c in runs if "-x" in c]
        self.assertEqual(len(plain), 4, "必须按 4 个进程名逐个通杀")
        names = {c[c.index("-x") + 1] for c in plain}
        self.assertEqual(names, {"chrome", "google-chrome", "chromium", "chromium-browser"})
        by_port = [c for c in runs if "remote-debugging-port" in " ".join(c)]
        self.assertTrue(by_port, "必须再按端口补杀一次")

    def test_returns_promptly_when_port_freed(self):
        # 端口在轮询第 2 秒释放 ⇒ 正常返回（不拖满也不抛）
        M.kill_all_chrome()  # cdp_ok 序列 [True, False] 由 _patch 提供

    def test_raises_when_port_never_frees(self):
        self._patch([True] * 40)  # 永远探活成功 = 端口一直没释放
        with self.assertRaises(SystemExit):
            M.kill_all_chrome()

    def test_start_chrome_kills_stale_first(self):
        # 即使此时 9222 是通的（旧实例活着），KEEP=0 也必须先通杀再起新的
        runs = []
        mock.patch.object(
            M.subprocess, "run",
            side_effect=lambda cmd, **kw: (runs.append(cmd), mock.Mock())[1],
        ).start()
        mock.patch.object(M, "clear_profile_locks", return_value=["SingletonLock"]).start()
        mock.patch.object(M.subprocess, "Popen",
                          side_effect=lambda cmd, **kw: mock.Mock()).start()
        # cdp_ok：第一次调用（kill 前探活）=True（旧实例活着）→ kill 轮询序列 → 起完探测
        # 起完后的探测序列：前 1 次 False（新实例还没就绪）→ 第 2 次 True（就绪，退出循环）
        cdp_seq = [True, True, False, False, False, False, False, True]
        mock.patch.object(
            M, "cdp_ok",
            side_effect=lambda: cdp_seq.pop(0) if cdp_seq else True,
        ).start()
        # http_json 必须一起 mock：cdp_ok 为 False 时 start_chrome 不碰网络，
        # 但 ensure() 里起完 Chrome 后会 http_json 拉 /json/version —— 这里只测 start_chrome，
        # 且本机 9222 可能真有别的 Chrome（不能让它真连）。
        mock.patch.object(
            M, "http_json",
            return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/0"},
        ).start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(M, "chrome_bin", return_value="/fake/chrome"):
            M.start_chrome()
        pkill_calls = [c for c in runs if c and "-9" in c]
        self.assertTrue(pkill_calls, "start_chrome 必须开枪杀旧实例")


class TestModuleContract(unittest.TestCase):
    def test_chrome_bin_resolves_env(self):
        with mock.patch.dict(M.os.environ, {"CHROME_BIN": "/custom/chrome"}, clear=False):
            self.assertEqual(M.chrome_bin(), "/custom/chrome")

    def test_clear_profile_locks_removes_stale_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            for name in ("SingletonLock", "SingletonCookie"):
                (Path(d) / name).touch()
            with mock.patch.object(M, "PROFILE_DIR", d):
                cleared = M.clear_profile_locks()
            self.assertEqual(set(cleared), {"SingletonLock", "SingletonCookie"})
            self.assertFalse((Path(d) / "SingletonLock").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)