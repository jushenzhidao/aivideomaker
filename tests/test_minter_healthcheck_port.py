#!/usr/bin/env python3
"""铸造容器的**健康检查地址**门禁（E2E-AVM-012 定案）。

## 为什么这条必须存在

镜像里曾把健康检查写成 `curl -fsS "http://127.0.0.1:8899/healthz"`，而 `PORT` 是
**部署可覆盖**的运行时旋钮。node064 的 compose override 只挪了端口（8895）⇒
服务好端端的、铸造照常、`/healthz:8895` 一片正常，`docker ps` 却恒 `unhealthy`
（E2E-AVM-010 定位、到 0.0.18 仍在）。同一个写死还在 `AVM_MINTER_BIND_HOST=172.17.0.1`
（文档里"开放但不挂公网"的收窄写法）下 100% 误报 —— 那时容器里根本没有套接字监听回环。

假"不健康"和假"健康"一样是事故：它会训练人忽略健康状态，也会卡住
`depends_on: condition: service_healthy` 这类编排（整个栈起不来）。

## 守什么（四层，每层都能被证伪）

1. **镜像不再自己写地址**：`HEALTHCHECK` 必须是 **exec 形态**、执行
   `turnstile_service.py --selfcheck` —— 出现字面 IP/端口，或退回 `curl …` 形态，即判失败。
2. **默认端口三处相等**：镜像 `ENV PORT` / `EXPOSE` / compose 的 `PORT`（外加服务代码里的
   fallback）—— 数字漂移正是这类"写死"的老家。判据抽成纯函数，变异自检与真实门禁**共用**。
3. **行为**：真起一个假 `/healthz`，用**从 Dockerfile 里抽出来的那条命令**去打它 ——
   `PORT` 指到哪就必须打到哪（指到死端口必须失败）。这一层能穿过"文本看着对、语义反了"
   的变异（文本断言做不到）。
4. **老命令必须被证明是坏的**：把 0.0.18 那句硬编码命令拿来打同一台服务，它必须**打不到**
   （`PORT` 对它毫无影响）—— 那一刻的假 unhealthy 由此变成一条可执行的断言。

全部离线：只在本机回环上起一个桩 `/healthz`，不连上游、不铸 token、零计费。
运行：python3 tests/test_minter_healthcheck_port.py
"""

import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
DOCKERFILE = ROOT / "Dockerfile.minter"
COMPOSE = ROOT / "docker-compose.yml"
SERVICE_PY = TOOLS / "turnstile_service.py"

MINTER = "minter"
DEFAULT_PORT = "8899"
SERVICE_NAME = "turnstile_service.py"

# 0.0.18 镜像里那句（假 unhealthy 的成因）。留着当**反例**：第 4 层要证明它打不到
# 被挪过端口的服务。
OLD_HEALTHCHECK_CMD = 'CMD curl -fsS "http://127.0.0.1:8899/healthz" >/dev/null || exit 1'


# ---------------------------------------------------------------- 文本解析 --


def joined(text: str) -> str:
    """拼回 `\\` 续行 —— 不拼的话 `HEALTHCHECK ... CMD [...]` 会被跨行截断。"""
    return re.sub(r"\\\n\s*", " ", text)


def healthcheck_argv(text: str) -> list | None:
    """抽出 `HEALTHCHECK` 的 `CMD [...]` → argv；**shell 形态返回 `None`**。

    必须按**指令行**取、不能全文搜地址：本文件与 Dockerfile 的注释里都写了
    `http://127.0.0.1:8899`（解释这次为什么改）—— 全文搜会捞到注释，
    于是"把命令改回硬编码"的变异照样全绿（门禁被自己写的文档骗过，本项目踩过两次）。
    """
    m = re.search(r"^HEALTHCHECK\b[^\n]*?\bCMD\s+(\[.*?\])\s*$", joined(text), re.M)
    if not m:
        return None
    try:
        argv = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return argv if isinstance(argv, list) and argv else None


def dockerfile_env(text: str) -> dict:
    """Dockerfile 的 `ENV` → `{键: 值}`（只在 ENV 指令行取，注释一律不算）。"""
    out: dict = {}
    for line in joined(text).splitlines():
        if not line.startswith("ENV "):
            continue
        for tok in line[4:].split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k] = v
    return out


def dockerfile_expose(text: str) -> str:
    """`EXPOSE` 声明的端口（文档性信息，但要与默认端口一致 —— 否则读的人先被误导）。"""
    m = re.search(r"^EXPOSE\s+(\d+)", joined(text), re.M)
    return m.group(1) if m else ""


def compose_minter_env(text: str, key: str) -> str:
    """取 compose `minter` 服务 `environment:` 下某个键的原始值（空串 = 没写）。"""
    m = re.search(r"^  minter:$", text, re.M)
    assert m, "compose 里找不到 minter 服务"
    rest = text[m.end():]
    nxt = re.search(r"^  [a-z][a-z0-9-]*:$", rest, re.M)
    block = text[m.start(): m.end() + (nxt.start() if nxt else len(rest))]
    in_env = False
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent <= 4 and line == "environment:":
            in_env = True
            continue
        if in_env:
            if indent <= 4:
                in_env = False
                continue
            m2 = re.match(rf"{re.escape(key)}\s*:\s*(.*)$", line)
            if m2:
                return m2.group(1).split(" #", 1)[0].strip()
    return ""


# ---------------------------------------------------------------- 判据（纯函数）--


_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_LITERAL_PORT = re.compile(r":\d{2,5}\b")


def healthcheck_problems(text: str) -> list:
    """镜像健康检查的判据。返回问题清单（空 = 通过）。

    与变异自检**共用这一份**：否则很容易变成"变异断言了另一个写法"，全绿却与真实门禁无关。
    """
    argv = healthcheck_argv(text)
    if argv is None:
        return ["HEALTHCHECK 不是 exec 形态的 `CMD [...]`：shell 形态里只能靠字面地址"
                "（0.0.18 的假 unhealthy 正是这么来的），且会被 shell 插值语义左右。"]
    problems: list[str] = []
    flat = " ".join(argv)
    if _IPV4.search(flat):
        problems.append(f"健康检查里出现字面 IP：{flat!r} ⇒ 地址必须由服务自己从 BIND_HOST 推导"
                        "（收到 `AVM_MINTER_BIND_HOST=172.17.0.1` 时容器内没有回环监听）")
    if _LITERAL_PORT.search(flat):
        problems.append(f"健康检查里出现字面端口：{flat!r} ⇒ 端口必须由服务自己从 PORT 读"
                        "（`PORT` 可被部署覆盖，写死即假 unhealthy）")
    if "--selfcheck" not in argv:
        problems.append("健康检查没走服务自己的 `--selfcheck`（判据会与 /healthz 分叉）")
    if not argv or not os.path.basename(argv[0]).startswith("python"):
        problems.append("健康检查应以解释器启动服务自检（镜像里 python3 一定在场）")
    elif len(argv) < 2 or os.path.basename(argv[1]) != SERVICE_NAME:
        problems.append(f"健康检查要执行 {SERVICE_NAME}（当前第二项是 {argv[1:]!r}）")
    return problems


def _clean(value: str) -> str:
    """去掉 compose 值外层的引号（`"8899"` → `8899`）。"""
    v = value.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def port_default_problems(dockerfile_text: str, compose_text: str, service_port: str) -> list:
    """默认端口四处必须相等：镜像 ENV / EXPOSE / compose / 服务代码 fallback。"""
    env = dockerfile_env(dockerfile_text)
    values = {
        "镜像 ENV PORT": env.get("PORT", ""),
        "镜像 EXPOSE": dockerfile_expose(dockerfile_text),
        f"compose {MINTER}.PORT": _clean(compose_minter_env(compose_text, "PORT")),
        f"{SERVICE_NAME} 的 fallback": service_port,
    }
    distinct = {v for v in values.values()}
    if len(distinct) == 1 and "" not in distinct:
        return []
    return ["默认端口不一致：" + "、".join(f"{k}={v!r}" for k, v in values.items()) +
            " ⇒ 四处必须一样（漂移出来的那个数字迟早会被写回健康检查里）"]


def with_healthcheck_cmd(text: str, cmd: str) -> str:
    """只换掉 `HEALTHCHECK` 的 CMD（保留 `--interval` 等旗标）—— 变异自检用。"""
    flags = re.search(r"^HEALTHCHECK\b((?:\s+--[a-z-]+=[^\s]+)*)", joined(text), re.M)
    assert flags, "Dockerfile 里找不到 HEALTHCHECK 指令"
    return re.sub(r"^HEALTHCHECK\b[^\n]*$", f"HEALTHCHECK{flags.group(1)} {cmd}",
                  joined(text), count=1, flags=re.M)


# ---------------------------------------------------------------- 桩服务 --


class _Stub:
    """本机回环上的假 `/healthz`（记录收到的路径，便于断言"真的打到了这里"）。"""

    def __init__(self, body: bytes = b'{"ready": false}', status: int = 200):
        self.body, self.status, self.paths = body, status, []
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                stub.paths.append(self.path)
                self.send_response(stub.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(stub.body)))
                self.end_headers()
                self.wfile.write(stub.body)

            def log_message(self, *a):     # 静音
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def free_port() -> int:
    """取一个**确认没人监听**的端口（用完即关，测试里当"死端口"用）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env(**overrides) -> dict:
    """子进程环境：清掉本机的 PORT / BIND_HOST / 代理，再按需覆盖。

    代理必须清 —— 本站的 `HTTP_PROXY` 会把**回环**地址也拐走（返回代理网关的错误体，
    不是 connection refused，极误导）；被拐走的话"打到 8895"这类断言会变成假阳性。
    """
    drop = {"PORT", "BIND_HOST", "TZ", "HTTP_PROXY", "http_proxy",
            "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(TOOLS), env.get("PYTHONPATH", "")]))
    env.update(overrides)
    return env


def run(cmd: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def local_healthcheck_argv() -> list:
    """把 Dockerfile 里那条命令翻成**本机可跑**的形态（只换解释器与脚本路径，语义不变）。

    刻意仍然**从 Dockerfile 抽**、而不是在测试里手写一遍：手写就等于测试另一条命令，
    镜像真被改回 `curl` 时它照样绿。
    """
    argv = healthcheck_argv(DOCKERFILE.read_text(encoding="utf-8"))
    assert argv is not None, "Dockerfile 的 HEALTHCHECK 不是可解析的 exec 形态"
    out = [sys.executable]
    for a in argv[1:]:
        out.append(str(SERVICE_PY) if os.path.basename(a) == SERVICE_NAME else a)
    return out


def service_port_fallback() -> str:
    """服务代码在 `PORT` 未设时的 fallback（**独立进程**跑，避免继承本测试进程的 PORT）。"""
    code = (f"import sys; sys.path.insert(0, r'{TOOLS}');"
            "import turnstile_service as S; print(S.PORT)")
    env = clean_env()
    out = run([sys.executable, "-c", code], env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class TestHealthcheckContract(unittest.TestCase):
    """第 1、2 层：文本契约 + 默认端口一致性（含变异自证）。"""

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.compose = COMPOSE.read_text(encoding="utf-8")

    # ---- 正向 ----
    def test_real_dockerfile_passes(self):
        self.assertEqual(healthcheck_problems(self.dockerfile), [])

    def test_real_default_ports_are_consistent(self):
        self.assertEqual(
            port_default_problems(self.dockerfile, self.compose, service_port_fallback()), [])

    def test_healthcheck_argv_is_executable_shape(self):
        """抽出来的 argv 必须真的是那条自检命令（防止解析器"抽到个空壳还报通过"）。"""
        argv = healthcheck_argv(self.dockerfile)
        self.assertIsNotNone(argv)
        self.assertTrue(os.path.basename(argv[0]).startswith("python"), argv)
        self.assertEqual(os.path.basename(argv[1]), SERVICE_NAME, argv)
        self.assertEqual(argv[2:], ["--selfcheck"], argv)

    # ---- 变异自证（每一条都对应一类真实错法）----
    def test_mutation_old_hardcoded_curl_is_rejected(self):
        """★ 0.0.18 那句本身（假 unhealthy 的成因）必须被判失败。"""
        mut = with_healthcheck_cmd(self.dockerfile, OLD_HEALTHCHECK_CMD)
        problems = healthcheck_problems(mut)
        self.assertTrue(problems, "把健康检查改回硬编码的 curl 竟然没被拦下")
        self.assertTrue(any("exec 形态" in p for p in problems), problems)

    def test_mutation_literal_address_in_exec_form_is_rejected(self):
        """换个形态照样得拦：exec 形态 + 字面地址（端口/回环都会被写死）。"""
        mut = with_healthcheck_cmd(
            self.dockerfile, 'CMD ["curl", "-fsS", "http://127.0.0.1:8899/healthz"]')
        problems = healthcheck_problems(mut)
        self.assertTrue(any("字面 IP" in p for p in problems), problems)
        self.assertTrue(any("字面端口" in p for p in problems), problems)

    def test_mutation_missing_selfcheck_flag_is_rejected(self):
        mut = with_healthcheck_cmd(
            self.dockerfile, f'CMD ["python3", "/app/tools/{SERVICE_NAME}"]')
        self.assertTrue(any("--selfcheck" in p for p in healthcheck_problems(mut)))

    def test_mutation_wrong_script_is_rejected(self):
        mut = with_healthcheck_cmd(
            self.dockerfile, 'CMD ["python3", "/app/tools/other_service.py", "--selfcheck"]')
        self.assertTrue(any(SERVICE_NAME in p for p in healthcheck_problems(mut)))

    def test_mutation_port_drift_between_the_four_places_is_rejected(self):
        """默认端口漂移（这里改 EXPOSE）必须被拦 —— 漂移出去的数字迟早写回健康检查。"""
        mut = self.dockerfile.replace("EXPOSE 8899", "EXPOSE 8900", 1)
        problems = port_default_problems(mut, self.compose, service_port_fallback())
        self.assertTrue(any("默认端口不一致" in p for p in problems), problems)

    def test_mutation_compose_port_drift_is_rejected(self):
        mut = self.compose.replace('      PORT: "8899"', '      PORT: "8900"', 1)
        problems = port_default_problems(self.dockerfile, mut, service_port_fallback())
        self.assertTrue(any("默认端口不一致" in p for p in problems), problems)


class TestSelfcheckBehavior(unittest.TestCase):
    """第 3、4 层：真起桩服务，用 Dockerfile 里那条命令去打 —— 端口必须跟着 `PORT` 走。"""

    def setUp(self):
        self.stub = _Stub()
        self.addCleanup(self.stub.close)

    def test_selfcheck_succeeds_against_the_port_from_PORT(self):
        """★ 核心：端口由 `PORT` 决定（桩在随机端口上，命令必须打到那里）。"""
        out = run(local_healthcheck_argv(), clean_env(PORT=str(self.stub.port)))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[ok]", out.stdout)
        self.assertEqual(self.stub.paths, ["/healthz"], "自检没有打 /healthz")
        self.assertIn(str(self.stub.port), out.stdout, "输出里应带上实际用的端口")

    def test_selfcheck_fails_on_a_dead_port(self):
        """死端口必须失败（否则"永远健康"，比假 unhealthy 更糟）。"""
        port = free_port()
        out = run(local_healthcheck_argv(), clean_env(PORT=str(port)))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn(str(port), out.stderr, "失败信息必须点名**尝试过的**端口，否则排障全靠猜")

    def test_selfcheck_rejects_a_200_that_is_not_our_healthz(self):
        """200 但不是本服务 ⇒ 判失败（打到别的服务/网关的情形，本轮踩过的代理错误体同族）。"""
        self.stub.close()
        self.stub = _Stub(body=b"<html>hello</html>")
        out = run(local_healthcheck_argv(), clean_env(PORT=str(self.stub.port)))
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_selfcheck_rejects_json_without_the_ready_key(self):
        self.stub.close()
        self.stub = _Stub(body=b'{"ok": true}')
        out = run(local_healthcheck_argv(), clean_env(PORT=str(self.stub.port)))
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_selfcheck_treats_warming_up_as_healthy(self):
        """`ready=false`（Chrome 预热中）**仍然算健康** —— 判据与原来等价，本次只改地址。"""
        self.stub.close()
        self.stub = _Stub(body=b'{"ready": false, "minting": false}')
        out = run(local_healthcheck_argv(), clean_env(PORT=str(self.stub.port)))
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_selfcheck_is_not_hijacked_by_http_proxy(self):
        """★ 环境里的代理必须被显式清空：`HTTP_PROXY` 会把回环地址也拐走。"""
        out = run(local_healthcheck_argv(), clean_env(
            PORT=str(self.stub.port),
            HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.stub.paths, ["/healthz"])

    def test_old_hardcoded_command_ignores_PORT(self):
        """★ 第 4 层的反例自证：老命令对 `PORT` 无感 ⇒ 服务被挪端口后它必然打不到。

        不断言它的退出码：本机 8899 上可能真有一个 minter（Mac 上就是宿主机直跑）——
        断言的是**我们这台桩服务一个请求都没收到**，与环境无关。
        """
        env = clean_env(PORT=str(self.stub.port))
        run(["sh", "-c", OLD_HEALTHCHECK_CMD.replace("CMD ", "", 1)], env)
        self.assertEqual(self.stub.paths, [],
                         f"老命令竟然打到了 :{self.stub.port} —— 说明反例的前提已经变了")


class TestHealthUrlDerivation(unittest.TestCase):
    """地址推导：`PORT` 与 `BIND_HOST` 都必须参与（两者都是部署可改的）。"""

    def _health_url(self, **env) -> str:
        code = (f"import sys; sys.path.insert(0, r'{TOOLS}');"
                "import turnstile_service as S; print(S.health_url())")
        out = run([sys.executable, "-c", code], clean_env(**env))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_defaults_to_loopback_on_the_default_port(self):
        self.assertEqual(self._health_url(), f"http://127.0.0.1:{DEFAULT_PORT}/healthz")

    def test_follows_port(self):
        self.assertEqual(self._health_url(PORT="8895"), "http://127.0.0.1:8895/healthz")

    def test_wildcard_bind_falls_back_to_loopback(self):
        for host in ("0.0.0.0", "::"):
            with self.subTest(bind_host=host):
                self.assertEqual(self._health_url(PORT="8895", BIND_HOST=host),
                                 "http://127.0.0.1:8895/healthz")

    def test_specific_bind_host_is_dialed_directly(self):
        """收到具体地址时**不能**改打回环 —— 那个地址下容器里没有回环监听（100% 假 unhealthy）。"""
        self.assertEqual(self._health_url(PORT="8895", BIND_HOST="172.17.0.1"),
                         "http://172.17.0.1:8895/healthz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
