#!/usr/bin/env python3
"""`tools/compose_wiring_check.py` 的门禁 —— 并且要求它**能被证伪**。

守的是什么（报告 AVM12-PF-08）：容器里的 `minter-preflight` 比的是**自己那份 env**
（两侧同源于 `AVM_MINTER_KEY` ⇒ 永远相等），于是"把 minter 服务那一行写死成字面量"这种
最常见的接线改法**它看不见**。真正的校验搬到宿主机侧后，最怕的失败形态反过来：
**它自己静默失效** —— 解析器与 compose 写法脱节、抽到 0 个服务却报"通过"。
所以这里不满足于"对当前 compose 通过"，而是逐条**变异**（只改 compose 文本，不碰磁盘上的
真文件），确认每一类错误都被拦下。反例清单与报告一一对应。

运行：python3 tests/test_compose_wiring_check.py
"""

import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "compose_wiring_check", ROOT / "tools" / "compose_wiring_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _service_block(text: str, service: str) -> tuple:
    """把 `text` 按 `  <service>:` 切成 (前, 该服务块, 后)。

    必须**按服务块**做变异：同一个 `.env` 键在多个服务里都出现（`MINTER_KEY` 在 minter 与
    minter-preflight 里各一份），全局 `replace(..., 1)` 会改到"错的那一个" —— 本次就踩过，
    变异改的是 preflight、而检查的是 minter，于是**假绿**。
    """
    m = re.search(rf"^  {re.escape(service)}:$", text, re.M)
    assert m, f"compose 里找不到服务 {service}"
    rest = text[m.end():]
    nxt = re.search(r"^  [a-z][a-z0-9-]*:$", rest, re.M)
    end = m.end() + (nxt.start() if nxt else len(rest))
    return text[: m.start()], text[m.start():end], text[end:]


def _mutate(text: str, service: str, old: str, new: str) -> str:
    head, body, tail = _service_block(text, service)
    assert old in body, f"{service} 块里找不到锚点：{old!r}"
    return head + body.replace(old, new, 1) + tail


class TestComposeWiringCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        cls.compose = COMPOSE.read_text(encoding="utf-8")

    def _problems(self, text: str) -> list:
        return self.tool.check_static(self.tool.parse_services(text))

    # ---------------------------------------------------------------- 正向 --
    def test_real_compose_passes(self):
        self.assertEqual(self._problems(self.compose), [])

    def test_parser_finds_all_three_services(self):
        """防**空转**：解析器抽不到服务时，`check_static` 会对空字典报"没问题"。"""
        services = self.tool.parse_services(self.compose)
        self.assertEqual(sorted(services), ["ark-compat", "minter", "minter-preflight"])
        for name, env in services.items():
            with self.subTest(service=name):
                self.assertTrue(env, f"{name} 的 environment 抽成了空的 ⇒ 解析器已脱节")
        self.assertIn("AVM_MINTER_KEY", services["ark-compat"])

    def test_parser_does_not_silently_degrade_on_broken_input(self):
        """结构被写坏时必须"抽不到"（由调用方判失败），不能"抽到一部分还报通过"。"""
        broken = self.compose.replace("services:", "servicesX:", 1)
        self.assertEqual(self.tool.parse_services(broken), {})

    # ---------------------------------------------------------------- 变异 --
    def test_catches_key_hardcoded_on_the_minter_service(self):
        """★ P-08 的那个改法本身：只改 **minter 服务**那行，写成字面量。

        容器里的 preflight 对这一幕**必然**报 [ok]（它看不到服务侧的值）。
        """
        mut = _mutate(
            self.compose, "minter",
            "      MINTER_KEY: ${AVM_MINTER_KEY:-}\n",
            "      MINTER_KEY: sk-hardcoded-minter-only\n",
        )
        problems = self._problems(mut)
        self.assertTrue(problems, "把服务侧的 key 写死成字面量竟然没被拦下")
        self.assertTrue(any("字面量" in p for p in problems), problems)

    def test_catches_the_two_sides_reading_different_variables(self):
        mut = _mutate(
            self.compose, "minter",
            "      MINTER_KEY: ${AVM_MINTER_KEY:-}\n",
            "      MINTER_KEY: ${AVM_MINTER_KEY_OTHER:-}\n",
        )
        self.assertTrue(any("变量都不同" in p for p in self._problems(mut)))

    def test_catches_minter_url_without_the_colon_form(self):
        """`:-` 改回 `-`（或去掉默认值）⇒ .env 里留空会把铸造能力静默关掉。"""
        for bad in ("${AVM_MINTER_URL-}", "${AVM_MINTER_URL}", "${AVM_MINTER_URL:-}"):
            with self.subTest(expr=bad):
                mut = _mutate(
                    self.compose, "ark-compat",
                    "      AVM_MINTER_URL: ${AVM_MINTER_URL:-http://host.docker.internal:8899}",
                    f"      AVM_MINTER_URL: {bad}",
                )
                self.assertTrue(any("静默关掉" in p for p in self._problems(mut)), bad)

    def test_catches_ark_host_bound_to_loopback(self):
        mut = _mutate(self.compose, "ark-compat", "      ARK_HOST: 0.0.0.0", "      ARK_HOST: 127.0.0.1")
        self.assertTrue(any("ARK_HOST" in p for p in self._problems(mut)))

    # ---- 时区（E2E-AVM-006）：缺它 ⇒ 铸造恒 interactive，而服务照常 ready --------------
    def test_catches_missing_tz_on_the_minter_service(self):
        """缺 TZ 的部署"看起来完全正常"（不需要代码、不影响启动、healthz 也 ready），
        只有铸造永远失败 —— 部署前必须拦下。"""
        mut = _mutate(
            self.compose, "minter",
            "      TZ: ${AVM_MINTER_TZ:-Asia/Shanghai}\n",
            "",
        )
        problems = self._problems(mut)
        self.assertTrue(any("TZ" in p for p in problems), problems)

    def test_catches_tz_without_the_colon_form(self):
        """`:-` 改回 `-`（或去掉默认值）：.env 里留空 ⇒ 时区静默退化成 UTC。"""
        for bad in ("${AVM_MINTER_TZ-Asia/Shanghai}", "${AVM_MINTER_TZ}", "${AVM_MINTER_TZ:-}"):
            with self.subTest(expr=bad):
                mut = _mutate(
                    self.compose, "minter",
                    "      TZ: ${AVM_MINTER_TZ:-Asia/Shanghai}",
                    f"      TZ: {bad}",
                )
                self.assertTrue(any("TZ" in p for p in self._problems(mut)), bad)

    def test_accepts_a_literal_timezone(self):
        """手工接线时写死 `Asia/Shanghai` 是**允许**的（这台工具只看"留空会不会失效"）。

        ⚠️ 与仓库侧的 tests/test_minter_timezone.py 严格度不同是有意的：那条还要求
        从 AVM_MINTER_TZ 插值（因为 compose 文件头的清单声称有这个旋钮），
        这条只守"部署机上的 override 别把时区搞丢"。
        """
        mut = _mutate(
            self.compose, "minter",
            "      TZ: ${AVM_MINTER_TZ:-Asia/Shanghai}",
            "      TZ: Asia/Shanghai",
        )
        self.assertEqual(self._problems(mut), [])

    def test_catches_the_missing_raw_flag_that_made_attribution_lie(self):
        """★ P-07 的契约：少了 RAW，preflight 就分不清"显式设置"与"取默认值"。"""
        mut = _mutate(
            self.compose, "minter-preflight",
            "      MINTER_ALLOW_INSECURE_RAW: ${AVM_MINTER_ALLOW_INSECURE:-}\n",
            "",
        )
        self.assertTrue(any("ALLOW_INSECURE_RAW" in p for p in self._problems(mut)))

    def test_accepts_a_literal_url(self):
        """写死一个字面量地址是**可以**的（没有"留空即关掉"的风险）—— 别误报。"""
        mut = _mutate(
            self.compose, "ark-compat",
            "      AVM_MINTER_URL: ${AVM_MINTER_URL:-http://host.docker.internal:8899}",
            "      AVM_MINTER_URL: http://10.0.0.5:8899",
        )
        self.assertEqual(self._problems(mut), [])


    # ---- host 网络契约与「包能到」（E2E-AVM-008）----
    def test_parser_captures_network_mode(self):
        """解析器必须能看见 `network_mode`（host 网络契约与路径提示都建立在它上面）。"""
        services = self.tool.parse_services(self.compose)
        self.assertEqual(services["minter"].get("network_mode"), "host")
        self.assertNotIn("network_mode", services["ark-compat"],
                         "ark-compat 没有 network_mode ⇒ 键必须缺席（别把别的键误捕进来）")

    def test_catches_minter_off_host_network(self):
        """★ 变异：删掉 `network_mode: host` ⇒ bridge 的 NAT 改写 TCP 指纹 ⇒ 铸造整体失效，
        而 /healthz 照样 ready —— 必须被静态检查拦下。"""
        mut = _mutate(self.compose, "minter", "    network_mode: host\n", "")
        problems = self._problems(mut)
        self.assertTrue(any("host 网络" in p for p in problems), problems)

    def test_host_path_note_present_for_real_compose(self):
        """真实 compose（minter=host、ark-compat=桥接）必须给出防火墙路径提示。"""
        notes = self.tool.host_path_notes(self.tool.parse_services(self.compose))
        self.assertTrue(any("ufw allow" in n and "8899" in n for n in notes), notes)

    def test_host_path_note_mutation_proof(self):
        """提示的触发条件必须可证伪：两侧条件各变异一次，提示都要消失。"""
        # ① ark-compat 也上 host 网络 ⇒ 同在宿主网络栈，无跨界流量 ⇒ 不再提示
        mut = _mutate(self.compose, "ark-compat",
                      "    container_name: ark-compat\n",
                      "    container_name: ark-compat\n    network_mode: host\n")
        self.assertEqual(self.tool.host_path_notes(self.tool.parse_services(mut)), [])
        # ② minter 离开 host 网络（该情形另由 check_static 判失败）⇒ 提示也不适用
        mut2 = _mutate(self.compose, "minter", "    network_mode: host\n", "")
        self.assertEqual(self.tool.host_path_notes(self.tool.parse_services(mut2)), [])

    def test_probe_snippet_is_valid_python_and_blocks_proxies(self):
        """探针源码会被塞进 `docker run … python -c`：语法必须合法；代理必须被显式清空
        （本站踩过 HTTP_PROXY 把内网地址拐走）；必须打 /healthz 且带超时。"""
        snippet = self.tool.probe_snippet()
        compile(snippet, "probe", "exec")
        self.assertIn("ProxyHandler({})", snippet)
        self.assertIn("/healthz", snippet)
        self.assertIn("timeout=", snippet)
        self.assertIn("PROBE_JSON=", snippet)

    def test_interpret_probe_flags_unreachable_with_ufw_hint(self):
        import json as _json

        rec = [{"url": "http://host.docker.internal:8899", "ok": False,
                "error": "URLError: timed out"}]
        problems = self.tool.interpret_probe(1, "PROBE_JSON=" + _json.dumps(rec), "")
        self.assertTrue(problems)
        self.assertTrue(any("ufw allow" in p and "8899" in p for p in problems), problems)

    def test_interpret_probe_passes_on_ok(self):
        import json as _json

        rec = [{"url": "http://host.docker.internal:8899", "ok": True,
                "status": 200, "ready": True}]
        self.assertEqual(self.tool.interpret_probe(0, "PROBE_JSON=" + _json.dumps(rec), ""), [])

    def test_interpret_probe_without_output_is_a_problem(self):
        """★ 探针没给出结果（容器起不来/镜像缺失）⇒ 连通性**未证实**，绝不能当成通过。"""
        problems = self.tool.interpret_probe(1, "", "docker: error …")
        self.assertTrue(any("未证实" in p for p in problems))


class TestPortCoherence(unittest.TestCase):
    """`AVM_MINTER_URL` 里的端口 与 minter 的 `PORT` 必须一致（E2E-AVM-012）。

    host 网络下 minter 监听的就是宿主 `PORT`，而适配层通过 URL 里的**同一个数字**去找它。
    compose 的插值**不支持嵌套**（`${A:-${B:-x}}` 非法）⇒ 这个数字只能写两遍 ⇒
    "改了端口忘了改地址"是完全静态的分叉（症状：服务全好、健康检查也绿，铸造却恒 429）。
    """

    def setUp(self):
        self.tool = _load_tool()
        self.base = {
            "ark-compat": {"AVM_MINTER_URL": "http://host.docker.internal:8899",
                           "ARK_HOST": "0.0.0.0", "AVM_MINTER_KEY": "k"},
            "minter": {"MINTER_KEY": "k", "PORT": "8899", "BIND_HOST": "127.0.0.1",
                       "TZ": "Asia/Shanghai"},
        }

    def _resolved(self, service: str, key: str, value: str) -> list:
        svc = {k: dict(v) for k, v in self.base.items()}
        svc[service][key] = value
        return self.tool.check_resolved(svc)

    # ---- 正向：真实 compose 的**默认值**也必须自洽（不需要 docker）----
    def test_shipped_compose_default_url_port_matches_minter_port(self):
        services = self.tool.parse_services(COMPOSE.read_text(encoding="utf-8"))
        expr = services["ark-compat"]["AVM_MINTER_URL"]
        parsed = self.tool.interp_default(expr)
        self.assertIsNotNone(parsed, f"AVM_MINTER_URL 的写法变了：{expr!r}")
        host, url_port = self.tool.split_netloc(parsed[1])
        self.assertIn(host, self.tool.SELF_HOSTS, host)
        self.assertEqual(url_port, services["minter"]["PORT"],
                         f"compose 里 URL 默认端口 {url_port} 与 minter.PORT "
                         f"{services['minter']['PORT']} 不一致（改了端口要两处同改）")

    # ---- 解析后的值（部署机上那份 override 才是分叉的高发地）----
    def test_aligned_ports_pass(self):
        self.assertEqual(self.tool.check_resolved(self.base), [])

    def test_catches_url_port_diverging_from_minter_port(self):
        problems = self._resolved("ark-compat", "AVM_MINTER_URL",
                                  "http://host.docker.internal:8895")
        self.assertTrue(any("端口分叉" in p for p in problems), problems)

    def test_catches_a_non_integer_minter_port(self):
        """空/非整数的 PORT ⇒ `int("")` 直接抛错，容器崩溃循环。"""
        problems = self._resolved("minter", "PORT", "")
        self.assertTrue(any("不是整数" in p for p in problems), problems)

    def test_does_not_flag_a_deliberately_remote_minter(self):
        """★ 反证：指向**另一台主机**是合法的（那台机器上的 minter），本项必须闭嘴。

        本工具的判据只在"地址指向本机"时才成立 —— 误报会让人开始忽略这个门禁，
        与假 unhealthy 是同一种伤害。
        """
        self.assertEqual(self._resolved("ark-compat", "AVM_MINTER_URL",
                                        "http://10.0.0.5:8895"), [])

    def test_skips_when_the_url_has_no_port(self):
        self.assertEqual(self._resolved("ark-compat", "AVM_MINTER_URL",
                                        "https://minter.example.com"), [])

    def test_split_netloc_handles_ipv6_and_credentials(self):
        self.assertEqual(self.tool.split_netloc("http://user:pw@h:8895/x"), ("h", "8895"))
        self.assertEqual(self.tool.split_netloc("http://[::1]:8895"), ("::1", "8895"))
        self.assertEqual(self.tool.split_netloc("http://h"), ("h", ""))
        self.assertEqual(self.tool.split_netloc("not-a-url"), ("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
