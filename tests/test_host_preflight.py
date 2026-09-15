#!/usr/bin/env python3
"""`tools/host_preflight.py` 的门禁 —— 它动的是**宿主防火墙**，所以判据要比别的工具更硬。

守六件事（每条都对应一个真实踩过的坑）：

1. **只读承诺**：不带 `--apply` 时既不碰防火墙、也**不起服务**。
2. **别放错链**：桥接容器 → 宿主自己的包走"本地投递"⇒ 判定链是 **INPUT**；
   `DOCKER-USER` 只看转发流量，放错等于没放。
3. **别放太宽**：iptables 用**接口** `br-+` / `docker0`（与子网无关），或按显式给出的网段；
   ufw 用**枚举出来的真实 docker 网段**。任何情况下都不许出现 `from any` / `0.0.0.0/0`
   （8899 = 谁能连上谁就能领"过闸凭证"）。
4. **动态优先**：默认计划里**不出现写死的 `172.16.0.0/12`**；ufw 模式换成枚举结果。
5. **不猜**：读不到端口就拒绝执行（`--port` 是逃生口）。
6. **失败要回滚**：容器内探测不过 ⇒ 只删"本次新增"的那条，**不碰既有规则**。

运行：python3 -m unittest tests.test_host_preflight
"""

import importlib.util
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
COMPOSE = str(ROOT / "docker-compose.yml")


def _load_tool():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("host_preflight", TOOLS / "host_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def has(*needles: str):
    """判定：拼起来的命令行里包含**全部**子串。

    ⚠️ 必须拼成字符串再 `in` —— 写 `"Config.Env" in cmd` 是**列表元素相等**
    （元素实为 `'{{json .Config.Env}}'`），永远为假，替身会静默返回空结果，
    把"工具读不到事实"伪装成"工具坏了"（本文件第一版就这么栽的）。
    """

    def pred(cmd) -> bool:
        line = " ".join(str(x) for x in cmd)
        return all(n in line for n in needles)

    return pred


class FakeRun:
    def __init__(self):
        self.calls: list = []
        self.handlers: list = []

    def add(self, pred, rc=0, out="", err="", first=False):
        item = (pred, rc, out, err)
        self.handlers.insert(0, item) if first else self.handlers.append(item)
        return self

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        for pred, rc, out, err in self.handlers:
            if pred(cmd):
                return Res(rc, out, err)
        return Res(0, "", "")

    def lines(self) -> list:
        return [" ".join(str(x) for x in c) for c in self.calls]

    def ran(self, *needles: str) -> bool:
        return any(all(n in ln for n in needles) for ln in self.lines())


def stack_run(*, port="8899", url="http://host.docker.internal:8899", ufw="active",
              bridge_nets=(), existing_rules=()) -> FakeRun:
    """一台"接线正常"的宿主机替身。

    `bridge_nets`: `[(网名, 子网), ...]` —— ufw 模式靠它枚举；默认空 = 枚举不到。
    `existing_rules`: 已在的规则特征串（命中则 `iptables -C` 返回 0）。
    """
    r = FakeRun()
    r.add(has("compose", "ps", "-q", "ark-compat"), out="cid-ark\n")
    r.add(has("compose", "ps", "-q", "minter"), out="cid-minter\n")
    r.add(has("Config.Env", "cid-ark"),
          out=json.dumps([f"AVM_MINTER_URL={url}", "ARK_PORT=8808"]) + "\n")
    r.add(has("Config.Env", "cid-minter"),
          out=json.dumps([f"PORT={port}", "TZ=Asia/Shanghai"]) + "\n")
    r.add(has("compose_wiring_check.py"), rc=0)
    r.add(has("ufw status"), out="Status: active\n" if ufw == "active" else "Status: inactive\n")
    if bridge_nets:
        ids = " ".join(n for n, _s in bridge_nets)
        r.add(has("network", "ls", "driver=bridge"), out=ids + "\n")
        for name, sub in bridge_nets:
            r.add(has("network", "inspect", name), out=json.dumps([{"Subnet": sub}]) + "\n")

    def _check(cmd):
        line = " ".join(str(x) for x in cmd)
        return any(pat in line for pat in existing_rules)

    # ⚠️ 两条要成对：先给"命中的规则"rc=0，再给其余 `-C` 兜底 rc=1。
    #    只写前者的话，未命中的 `-C` 会掉进 FakeRun 的默认 rc=0 ⇒ 所有规则都被当成
    #    "已存在"，幂等/回滚测试会**假绿**（本文件第二版就这么栽的）。
    r.add(lambda c: "iptables -C" in " ".join(c), rc=1)
    r.add(lambda c: "iptables -C" in " ".join(c) and _check(c), rc=0, first=True)
    return r


def call(tool, argv, run, *, root=True, probe=None, ufw_binary=True):
    with mock.patch.object(tool.os, "geteuid", lambda: 0 if root else 1000), \
         mock.patch.object(tool.shutil, "which",
                           lambda name: "/usr/sbin/ufw" if ufw_binary else None), \
         mock.patch.object(tool.wiring, "probe_minter", probe if probe else (lambda c, u: [])):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = tool.main(argv, run=run)
    return rc, out.getvalue(), err.getvalue()


class TestReadOnlyDryRun(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_prints_plan_without_touching_anything(self):
        run = stack_run(ufw="inactive")
        rc, out, _err = call(self.tool, [], run, ufw_binary=False)
        self.assertEqual(rc, 0)
        self.assertIn("iptables -I INPUT 1 -i br-+", out, "默认按接口放行，必须打印出来")
        self.assertIn("回滚命令", out)
        self.assertIn("dry-run", out)
        self.assertFalse(run.ran("iptables -I"), "dry-run 不得真的放行")
        self.assertFalse(run.ran("iptables -D"), "dry-run 不得删规则")
        self.assertFalse(run.ran("ufw allow"), "dry-run 不得真的放行")
        self.assertFalse(run.ran("up -d"), "dry-run **不得顺手把服务起了**（只读承诺）")


class TestInterfaceMode(unittest.TestCase):
    """第 3、4 条：接口式放行，与网段无关（这就是"网段是动态字段"的解法）。"""

    def setUp(self):
        self.tool = _load_tool()

    def test_plan_is_interface_based_and_dynamic(self):
        _c, allows, backs = self.tool.allow_plan("iptables", "8899")
        joined = "\n".join(" ".join(c) for c in allows)
        self.assertIn("-i br-+ -p tcp --dport 8899 -j ACCEPT", joined)
        self.assertIn("-i docker0 -p tcp --dport 8899 -j ACCEPT", joined)
        self.assertNotIn("172.16.0.0/12", joined, "不许写死默认地址池网段")
        for c in allows:
            self.assertEqual(c[:5], ["iptables", "-I", "INPUT", "1", "-i"], "插 INPUT 第一条")
        self.assertEqual(len(backs), len(allows))

    def test_no_subnet_needed_at_all(self):
        """接口模式**不需要**读任何 docker 网段 —— 网段怎么变都不影响。"""
        run = stack_run(ufw="inactive")
        rc, out, _err = call(self.tool, ["--apply", "--no-up"], run, ufw_binary=False)
        self.assertEqual(rc, 0)
        self.assertFalse(run.ran("network inspect"), "接口模式不该去查网段")
        self.assertIn("接口 br-+、docker0", out)

    def test_interfaces_can_be_overridden(self):
        _c, allows, _b = self.tool.allow_plan("iptables", "8899", interfaces=("br-+", "br-custom"))
        joined = "\n".join(" ".join(c) for c in allows)
        self.assertIn("-i br-custom", joined)
        self.assertNotIn("docker0", joined)

    def test_source_is_never_wildcard(self):
        """任何模式下都不许出现"谁都能连"的源。"""
        cases = (("iptables", {}), ("iptables", {"subnets": ("172.18.0.0/16",)}),
                 ("ufw", {"subnets": ("172.18.0.0/16",)}))
        for mode, kw in cases:
            _c, allows, _b = self.tool.allow_plan(mode, "8899", **kw)
            for cmd in allows:
                line = " ".join(cmd)
                self.assertNotIn("-s 0.0.0.0/0", line)
                self.assertNotIn("from any", line)
                self.assertNotIn("0.0.0.0/0", line)

    def test_explicit_subnet_is_used_verbatim(self):
        _c, allows, _b = self.tool.allow_plan("iptables", "8899", subnets=("10.7.0.0/16",))
        self.assertIn("-s 10.7.0.0/16", "\n".join(" ".join(c) for c in allows))


class TestUfwEnumeratesRealSubnets(unittest.TestCase):
    """第 4 条：ufw 不支持接口通配符 ⇒ 用**真实枚举**取代写死的 172.16/12。"""

    def setUp(self):
        self.tool = _load_tool()

    def test_enumerates_every_bridge_subnet(self):
        run = stack_run(bridge_nets=[("netA", "10.7.0.0/16"), ("netB", "172.18.0.0/16")])
        self.assertEqual(self.tool.docker_bridge_subnets(run), ["10.7.0.0/16", "172.18.0.0/16"])

    def test_dedupes_subnets(self):
        run = stack_run(bridge_nets=[("netA", "172.18.0.0/16"), ("netB", "172.18.0.0/16")])
        self.assertEqual(self.tool.docker_bridge_subnets(run), ["172.18.0.0/16"])

    def test_ufw_plan_has_one_rule_per_subnet(self):
        run = stack_run(bridge_nets=[("netA", "10.7.0.0/16"), ("netB", "172.18.0.0/16")])
        rc, out, _err = call(self.tool, [], run)
        self.assertEqual(rc, 0)
        self.assertIn("ufw allow proto tcp from 10.7.0.0/16 to any port 8899", out)
        self.assertIn("ufw allow proto tcp from 172.18.0.0/16 to any port 8899", out)
        self.assertNotIn("172.16.0.0/12", out, "不许写死默认地址池网段")

    def test_ufw_without_subnets_refuses(self):
        run = stack_run(bridge_nets=[])           # 枚举不到
        rc, _out, err = call(self.tool, [], run)
        self.assertEqual(rc, 2)
        self.assertIn("--subnet", err)
        self.assertFalse(run.ran("ufw allow"), "拒绝执行时不得已经放行")

    def test_ufw_plan_requires_subnets(self):
        with self.assertRaises(RuntimeError) as cm:
            self.tool.allow_plan("ufw", "8899")
        self.assertIn("不支持", str(cm.exception))

    def test_ufw_accepts_explicit_subnet(self):
        _c, allows, _b = self.tool.allow_plan("ufw", "8899", subnets=("172.18.0.0/16",))
        self.assertEqual(allows, [["ufw", "allow", "proto", "tcp", "from", "172.18.0.0/16",
                                   "to", "any", "port", "8899"]])


class TestChainAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_targets_input_not_docker_user(self):
        for kw in ({}, {"subnets": ("10.0.0.0/16",)}):
            _c, allows, backs = self.tool.allow_plan("iptables", "8899", **kw)
            for cmd in allows + backs:
                self.assertIn("INPUT", cmd)
                self.assertNotIn("DOCKER-USER", cmd,
                                 "DOCKER-USER 只看转发流量；桥接→宿主的包走 INPUT，放错链等于没放")

    def test_existing_rule_is_not_duplicated(self):
        run = stack_run(existing_rules=("br-+",))
        added = self.tool.ensure_allow(run, "iptables", "8899")
        self.assertFalse(run.ran("iptables -I", "br-+"), "br-+ 已在，不该重复插")
        self.assertTrue(run.ran("iptables -I", "docker0"), "docker0 缺，应补上")
        self.assertEqual(len(added), 1, "只应记录真正新增的那一条")

    def test_all_existing_means_nothing_added(self):
        run = stack_run(existing_rules=("br-+", "docker0"))
        self.assertEqual(self.tool.ensure_allow(run, "iptables", "8899"), [])
        self.assertFalse(run.ran("iptables -I"))

    def test_ufw_skip_output_counts_as_not_added(self):
        run = FakeRun().add(has("ufw allow"), out="Skipping adding existing rule\n")
        self.assertEqual(self.tool.ensure_allow(run, "ufw", "8899",
                                                subnets=("172.18.0.0/16",)), [])


class TestRefuseToGuess(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_port_split_is_refused(self):
        run = stack_run(port="8899", url="http://host.docker.internal:8900")
        with self.assertRaises(RuntimeError) as cm:
            self.tool.minter_target(run, pathlib.Path(COMPOSE))
        self.assertIn("三处同改", str(cm.exception))

    def test_non_numeric_port_is_refused(self):
        run = stack_run(port="auto")
        with self.assertRaises(RuntimeError) as cm:
            self.tool.minter_target(run, pathlib.Path(COMPOSE))
        self.assertIn("--port", str(cm.exception))

    def test_empty_minter_url_is_refused(self):
        run = stack_run(url="")
        with self.assertRaises(RuntimeError) as cm:
            self.tool.minter_target(run, pathlib.Path(COMPOSE))
        self.assertIn("铸造能力被关掉", str(cm.exception))

    def test_main_refuses_without_port_facts(self):
        run = stack_run(port="auto")
        rc, _out, err = call(self.tool, ["--apply", "--no-up"], run, ufw_binary=False)
        self.assertEqual(rc, 2)
        self.assertIn("--port", err)
        self.assertFalse(run.ran("iptables -I"), "拒绝执行时不得已经放行")


class TestProbeAndRollback(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()
        self.bad = (lambda c, u: ["[probe] 容器内打不到 minter（ConnectTimeout）"])

    def test_failure_rolls_back_only_the_new_rule(self):
        run = stack_run(ufw="inactive", existing_rules=("br-+",))
        rc, _out, err = call(self.tool, ["--apply", "--no-up"], run, probe=self.bad,
                             ufw_binary=False)
        self.assertEqual(rc, 2)
        self.assertTrue(run.ran("iptables -I", "docker0"), "应当先补上缺的那条")
        self.assertTrue(run.ran("iptables -D", "docker0"), "新增的那条必须回滚")
        self.assertFalse(run.ran("iptables -D", "br-+"), "既有规则不许删")
        self.assertIn("容器内仍打不到 minter", err)

    def test_success_does_not_roll_back(self):
        run = stack_run(ufw="inactive")
        rc, out, _err = call(self.tool, ["--apply", "--no-up"], run, ufw_binary=False)
        self.assertEqual(rc, 0)
        self.assertIn("容器内实测通过", out)
        self.assertFalse(run.ran("iptables -D"))

    def test_rollback_subcommand_deletes_the_plan(self):
        run = stack_run(ufw="inactive")
        rc, out, _err = call(self.tool, ["--rollback"], run, ufw_binary=False)
        self.assertEqual(rc, 0)
        self.assertTrue(run.ran("iptables -D", "br-+"))
        self.assertTrue(run.ran("iptables -D", "docker0"))
        self.assertIn("已回滚", out)

    def test_missing_url_skips_the_probe_honestly(self):
        run = stack_run(url="", ufw="inactive")
        rc, _out, err = call(self.tool, ["--apply", "--no-up", "--port", "8899"], run,
                             ufw_binary=False)
        self.assertEqual(rc, 0)
        self.assertIn("未实测", err)


class TestRootAndDetection(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_apply_without_root_refuses_before_doing_anything(self):
        run = stack_run(ufw="inactive")
        rc, _out, err = call(self.tool, ["--apply"], run, root=False, ufw_binary=False)
        self.assertEqual(rc, 2)
        self.assertIn("sudo", err)
        self.assertFalse(run.ran("up -d"), "非 root 时不得先起服务再报错")
        self.assertFalse(run.ran("iptables -I"))

    def test_ufw_inactive_falls_back_to_iptables(self):
        run = stack_run(ufw="inactive")
        with mock.patch.object(self.tool.shutil, "which", lambda n: "/usr/sbin/ufw"):
            mode, why = self.tool.firewall_mode(run)
        self.assertEqual(mode, "iptables")
        self.assertIn("未启用", why)

    def test_ufw_active_is_used(self):
        with mock.patch.object(self.tool.shutil, "which", lambda n: "/usr/sbin/ufw"):
            mode, _why = self.tool.firewall_mode(stack_run(ufw="active"))
        self.assertEqual(mode, "ufw")

    def test_ufw_status_unreadable_falls_back(self):
        """非 root 时 `ufw status` 读不到状态 —— 不能因此当成"已启用"。"""
        run = FakeRun().add(has("ufw status"), out="ERROR: You need to be root\n")
        with mock.patch.object(self.tool.shutil, "which", lambda n: "/usr/sbin/ufw"):
            mode, why = self.tool.firewall_mode(run)
        self.assertEqual(mode, "iptables")
        self.assertIn("root", why)

    def test_no_ufw_binary_falls_back_to_iptables(self):
        with mock.patch.object(self.tool.shutil, "which", lambda n: None):
            mode, why = self.tool.firewall_mode(FakeRun())
        self.assertEqual(mode, "iptables")
        self.assertIn("没有 ufw", why)


if __name__ == "__main__":
    unittest.main()
