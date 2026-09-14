#!/usr/bin/env python3
"""零外发审计 + **门禁不得被跳过**：跑全量单测，拦下所有**非回环** TCP 连接并打印调用栈。

为什么需要它：单测的纪律之一是"零外发"（不碰真上游、不产生额度消耗、
不依赖外部服务可用性），但这条纪律**靠人自觉**。2026-09-14 复跑时抓到反例：

    >>> [EGRESS] connect ('220.181.116.101', 443)
      File ".../src/ark_compat/upstreams.py", line 60, in create
      File ".../src/ark_compat/upstreams.py", line 73, in _rehost
      File ".../src/ark_compat/upstreams.py", line 91, in _upload_one
      File ".../src/ark_compat/web_client.py", line 558, in upload_file

根因：**把上游 base_url 指向死端口挡不住媒体转存** —— 转存外链素材走的是
`httpx.get(绝对 URL)`，不受 `base_url` 约束，于是会真的去公网下载。那个用例
每次运行都向文档站 TOS 发一次真实 HTTPS 请求（已修：改用死端口素材 URL）。

教训：**"指向死端口"只覆盖 base_url 那条路径**；凡是不经 base_url 的绝对 URL
（媒体下载、回调、探测）都必须单独确认。

## 第二职责：`skipped == 0`（2026-09-14 新增）

**跳过 ≠ 通过**，而且输出几乎一样（只多一句 `OK (skipped=1)`）—— 这是最隐蔽的假绿。
实测（报告 AVM12-OPEN-PYYAML）：`requirements.txt`、`requirements-dev.txt`、CI 三处都不装
pyyaml ⇒ `test_compose_env_injection` 的对照路**在流水线里从不执行**，而全程是绿的。
所以这里把"有用例被跳过"直接判失败，并在报告里点名是哪一条、为什么。真正的修法是补齐
测试期依赖（见 `requirements-dev.txt`），不是把这条断言删掉。

用法：
    python3 tools/egress_audit.py            # 退出码非 0 即有非回环出站 / 有用例被跳过
"""
import socket
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HITS: list = []

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_loopback(addr) -> bool:
    if not isinstance(addr, tuple) or not addr:
        return True
    host = str(addr[0])
    return host.startswith("127.") or host in ("::1", "localhost", "0.0.0.0", "")


def _report(kind: str, addr) -> None:
    if _is_loopback(addr):
        return
    HITS.append((kind, addr))
    sys.stderr.write(f"\n>>> [EGRESS] {kind} {addr}\n")
    traceback.print_stack(file=sys.stderr)


def _connect(self, addr):
    _report("connect", addr)
    return _real_connect(self, addr)


def _connect_ex(self, addr):
    _report("connect_ex", addr)
    return _real_connect_ex(self, addr)


def main() -> int:
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex

    # 与项目自己的命令同口径：`python -m unittest discover -s tests`（在仓库根跑）。
    # 刻意**不**传 top_level_dir —— tests/ 没有 __init__.py，传了会被
    # unittest 判为"不可导入目录"而直接 ImportError。
    import os
    os.chdir(ROOT)
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    ok = result.wasSuccessful()

    sys.stderr.write(f"\n=== 非回环出站合计：{len(HITS)} 次 ===\n")
    for kind, addr in HITS:
        sys.stderr.write(f"  {kind} {addr}\n")
    if not ok:
        sys.stderr.write("（单测本身有失败，先修失败再看外发）\n")
        return 1
    if HITS:
        sys.stderr.write(
            "⇒ 违反「零外发」纪律。常见根因：素材/回调/探测用了绝对 URL ——\n"
            "   base_url 指向死端口**挡不住**这类请求。请把 URL 换成死端口地址。\n"
        )
        return 1
    if result.skipped:
        # 跳过 ≠ 通过。这是最隐蔽的一类假绿：输出只多了一句 `OK (skipped=1)`，
        # 而"被跳过的门禁"与"跑过且通过的门禁"在报告里长得一模一样。
        # 实测（2026-09-14 报告 AVM12-OPEN-PYYAML）：没装 pyyaml ⇒
        # `test_compose_env_injection` 的对照路整条不执行，而流水线全程绿。
        sys.stderr.write(
            f"⇒ 有 {len(result.skipped)} 条用例被**跳过**（跳过不等于通过）：\n"
        )
        for case, why in result.skipped:
            sys.stderr.write(f"    {case.id()}\n      理由：{why}\n")
        sys.stderr.write(
            "  常见根因：测试期依赖没装 —— 见 requirements-dev.txt"
            "（pyyaml / websocket-client / pytest）。\n"
            "  确实需要跳过时，请把这条断言与理由一起改掉，别让它静默累积。\n"
        )
        return 1
    sys.stderr.write("⇒ 零外发 ✔（且没有用例被跳过）\n")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
