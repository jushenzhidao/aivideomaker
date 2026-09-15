#!/usr/bin/env python3
"""上线助手（**宿主机侧**）：把 E2E-AVM-008 那条手工步骤变成一条幂等命令。

## 它解决的问题

minter 走 `network_mode: host` ⇒ ark-compat（桥接）访问它是**出网到宿主**，受宿主
**INPUT** 链管辖。宿主 DROP 掉这条路时，症状极具迷惑性：minter 照常铸造、`/healthz`
全绿、池子满着，只有"取 token"永远失败（`served` 恒 0），闸门一翻全是 429
「token minter configured but UNREACHABLE … ConnectTimeout」。而宿主上
`curl 127.0.0.1:8899` 是通的 —— **宿主能连 ≠ 容器能连**。

⚠️ 判定链是 **INPUT**，不是 `DOCKER-USER`：目的地址是本机的包走"本地投递"，而
`DOCKER-USER` 只看**转发**流量（容器去外网/去别的容器）。放错链等于没放。

## 放行"谁"：按**接口**，不按写死的网段

    iptables: -i br-+  （docker 自建桥接接口 `br-xxxxxxxx`）+ -i docker0

接口式的好处是**与子网无关**：Docker 换地址池（`default-address-pools` 配成 10.x /
192.168.x）、网络重建后拿到新网段，规则都照样命中 —— 这就是"172.16.0.0/12 是动态字段"
的正解。**别**为了"通用"去放行整个 `192.168.0.0/16`：那等于"谁在同一个局域网谁就能领
过闸凭证"（8899 = 谁能连上谁就能领 token）。

ufw **不支持接口通配符**（`in on br-+` 落不了地）⇒ ufw 主机上改为**枚举 docker 真实
桥接网段**再逐条放行（`docker network ls --filter driver=bridge` → inspect 取 Subnet）：

    ufw allow proto tcp from 172.18.0.0/16 to any port 8899     # 每个 docker 网段一条
    ⚠️ 之后**新建网络**（新 compose 项目/新 network）会拿到新网段 ⇒ 重跑本工具即可补齐。

## 四步（顺序不能反）

    0. 接线校验（`compose_wiring_check.py --resolve`）—— 不通就别上线
    1. `docker compose up -d`              **只有 `--apply` 会做**；让网络与 minter 就绪
    2. **读事实**：minter 的 `PORT`、ark-compat 的 `AVM_MINTER_URL`（从 `docker inspect`
       读，**不猜**；ufw 模式下再枚举 docker 真实桥接网段）
    3. 幂等放行：接口式（iptables，默认）或网段式（ufw）
    4. 在容器内实测「包能到」；不通过就把**本次新增**的规则删回去（不留半改状态）

## 用法

    python3 tools/host_preflight.py        # **纯只读**：体检 + 打印将要执行的规则
    sudo python3 tools/host_preflight.py --apply          # 起服务 + 放行 + 实测（失败自动回滚）
    sudo python3 tools/host_preflight.py --apply --no-up  # 只对齐防火墙，不动服务
    sudo python3 tools/host_preflight.py --rollback       # 删掉本工具这条拓扑该有的规则
    python3 tools/host_preflight.py --subnet 172.18.0.0/16     # 只放这一个网段（覆盖自动枚举）
    python3 tools/host_preflight.py --interfaces br-+,docker0  # 换接口集（默认就是这两个）
    python3 tools/host_preflight.py --port 8899                # 读不到容器时显式给端口

退出码：`0` 就绪 ｜ `2` 未通过（含"拿不到端口事实，拒绝猜"）。

## 它刻意**不**做的事

- **不自己提权**：`--apply/--rollback` 需要 root，不是 root 就明确要求你加 `sudo`。
- **不写"谁都能连"的规则**：源限定在 docker 桥接接口或 docker 真实网段。
- **不静默**：拿不到端口就报错；每次动作都先打印再执行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compose_wiring_check as wiring  # noqa: E402

DEFAULT_COMPOSE = wiring.DEFAULT_COMPOSE
APP = wiring.APP            # ark-compat
MINTER = wiring.MINTER      # minter

# docker 自建桥接接口：用户自定义网络是 `br-<12hex>`，默认 bridge 是 `docker0`。
# iptables 里 `br-+` 表示"任何以 br- 开头的接口" ⇒ 与子网/地址池无关。
DOCKER_BRIDGE_IFACES = ("br-+", "docker0")


def run_cmd(cmd: list[str], timeout: int = 300):
    """真实执行（测试里注入替身，见 tests/test_host_preflight.py）。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _fail(msgs: list[str]) -> int:
    print("[fatal] 上线助手未通过：", file=sys.stderr)
    for m in msgs:
        print("  - " + m, file=sys.stderr)
    return 2


# ------------------------------------------------------------ 读事实（不猜） ----


def _container_id(run, compose: Path, service: str) -> str:
    res = run(["docker", "compose", "-f", str(compose), "ps", "-q", service])
    cid = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not cid:
        raise RuntimeError(
            f"取不到 {service} 的容器 id（`docker compose ps -q {service}` 无输出）⇒ "
            "先 `docker compose up -d`，或用 `--port` 显式给出端口。"
        )
    return cid[0].strip()


def container_env(run, compose: Path, service: str) -> dict:
    """容器内的环境变量（**事实**，不是从 compose/.env 推的）。"""
    cid = _container_id(run, compose, service)
    res = run(["docker", "inspect", "--format", "{{json .Config.Env}}", cid])
    if res.returncode != 0:
        raise RuntimeError(f"docker inspect {service} 失败：{(res.stderr or '').strip()[:200]}")
    try:
        items = json.loads((res.stdout or "[]").strip() or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"读 {service} 的 env 失败（不是 JSON）：{e}") from e
    out = {}
    for item in items:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def minter_target(run, compose: Path) -> tuple[str, str]:
    """返回 `(端口, 探测用的完整 URL)` —— 两样都取自已运行的容器，并**交叉核对**。

    端口来自 minter 的 `PORT`，URL 来自 ark-compat 的 `AVM_MINTER_URL`：两者在 host
    网络下是"同一个数字"，分叉的后果与 E2E-AVM-008 同族（服务全好、包却到不了）。
    """
    env = container_env(run, compose, MINTER)
    port = str(env.get("PORT", "")).strip()
    if not port.isdigit():
        raise RuntimeError(f"minter 容器里的 PORT 不是数字（{port!r}）⇒ 用 `--port` 显式给出。")

    url = str(container_env(run, compose, APP).get("AVM_MINTER_URL", "")).strip()
    if not url:
        raise RuntimeError(
            "ark-compat 容器的 AVM_MINTER_URL 为空 ⇒ 铸造能力被关掉了（见 compose 文件头），"
            "先把它配起来再放行防火墙。"
        )
    _host, url_port = wiring.split_netloc(url)
    if url_port and url_port != port:
        raise RuntimeError(
            f"端口分叉：minter 监听 {port}，而 ark-compat 打的是 {url}（{url_port}）⇒ "
            "挪端口要三处同改（minter 的 PORT、AVM_MINTER_URL、防火墙规则）。"
        )
    return port, url


def docker_bridge_subnets(run) -> list:
    """枚举 docker 自建桥接网络的**真实**子网（含自定义地址池与默认 bridge）。

    ufw 不支持接口通配符，所以 ufw 主机上只能用网段 —— 那就用**真实枚举**取代写死的
    `172.16.0.0/12`：既不漏（换地址池也在内），也不宽（局域网网段不会被顺手放进来）。
    """
    res = run(["docker", "network", "ls", "--filter", "driver=bridge", "--format", "{{.ID}}"])
    if res.returncode != 0:
        raise RuntimeError(
            f"`docker network ls` 失败：{(res.stderr or '').strip()[:200]} ⇒ "
            "用 `--subnet` 显式给出要放行的网段。"
        )
    out: list = []
    for nid in (res.stdout or "").split():
        r2 = run(["docker", "network", "inspect", nid, "--format", "{{json .IPAM.Config}}"])
        try:
            cfgs = json.loads((r2.stdout or "[]").strip() or "[]")
        except json.JSONDecodeError:
            continue
        for cfg in cfgs:
            sub = str(cfg.get("Subnet", "")).strip()
            if sub and sub not in out:
                out.append(sub)
    if not out:
        raise RuntimeError(
            "枚举不到任何 docker 桥接网段（还没建过网络？）⇒ 先 `docker compose up -d`，"
            "或用 `--subnet` 显式给出。"
        )
    return out


# ------------------------------------------------------------------ 防火墙 ----


def firewall_mode(run) -> tuple[str, str]:
    """`("ufw"|"iptables", 说明)`。ufw 在场但未启用 ⇒ 退回 iptables（ufw 加规则也不会生效）。"""
    if not shutil.which("ufw"):
        return "iptables", "宿主机没有 ufw ⇒ 用 INPUT 链 + 接口匹配"
    res = run(["ufw", "status"])
    text = (res.stdout or "") + (res.stderr or "")
    if "Status: active" in text:
        return "ufw", "ufw 已启用（不支持接口通配符 ⇒ 按枚举出的 docker 网段放行）"
    if "Status: inactive" in text:
        return "iptables", "ufw 在场但**未启用** ⇒ 用 INPUT 链（加到 ufw 里不会生效）"
    return "iptables", "读不到 ufw 状态（多半不是 root）⇒ 用 INPUT 链"


def allow_plan(mode: str, port: str, *, subnets=(), interfaces=DOCKER_BRIDGE_IFACES):
    """`(检查命令, 放行命令, 回滚命令)` —— 三个**等长列表**，下标一一对应。

    · iptables：按**接口**（`br-+` / `docker0`）放行 ⇒ 与子网无关，天然抗"网段动态化"；
      显式给了 `subnets` 就再补上按网段的规则。
    · ufw：只能按**网段**（ufw 不支持 `in on br-+`）⇒ 每个 docker 真实网段一条。

    ⚠️ iptables 分支插的是 **INPUT**：桥接容器发给宿主自己的包走"本地投递"，
    `DOCKER-USER` 只看转发流量 —— 放错链等于没放（这正是要自动化掉的那类手滑）。
    """
    checks: list = []
    allows: list = []
    backs: list = []

    if mode == "ufw":
        if not subnets:
            raise RuntimeError(
                "ufw 模式必须给出网段（ufw **不支持**接口通配符 `in on br-+`）⇒ "
                "用 `--subnet`，或让工具枚举 docker 桥接网段（不给 --subnet 即为自动枚举）。"
            )
        for sub in subnets:
            allows.append(["ufw", "allow", "proto", "tcp", "from", sub, "to", "any", "port", port])
            backs.append(["ufw", "delete", "proto", "tcp", "from", sub, "to", "any", "port", port])
        return checks, allows, backs

    for iface in interfaces:
        base = ["-i", iface, "-p", "tcp", "--dport", port, "-j", "ACCEPT"]
        checks.append(["iptables", "-C", "INPUT", *base])
        allows.append(["iptables", "-I", "INPUT", "1", *base])
        backs.append(["iptables", "-D", "INPUT", *base])
    for sub in subnets:
        base = ["-s", sub, "-p", "tcp", "--dport", port, "-j", "ACCEPT"]
        checks.append(["iptables", "-C", "INPUT", *base])
        allows.append(["iptables", "-I", "INPUT", "1", *base])
        backs.append(["iptables", "-D", "INPUT", *base])
    return checks, allows, backs


def ensure_allow(run, mode: str, port: str, *, subnets=(), interfaces=DOCKER_BRIDGE_IFACES) -> list:
    """幂等放行。返回 `[(放行命令, 回滚命令)]` —— **只含本次真的新增的**。"""
    checks, allows, backs = allow_plan(mode, port, subnets=subnets, interfaces=interfaces)
    added: list = []
    for i, add in enumerate(allows):
        chk = checks[i] if i < len(checks) else None
        if chk is not None and run(chk).returncode == 0:
            print(f"[skip] 规则已存在，不动：{' '.join(add)}")
            continue
        res = run(add)
        text = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0:
            raise RuntimeError(f"放行失败（rc={res.returncode}）：{text.strip()[:300]}")
        if mode == "ufw" and "Skipping" in text:
            print(f"[skip] ufw 报『已存在』⇒ 未新增：{text.strip()[:120]}")
            continue
        print(f"[ok] 已放行：{' '.join(add)}")
        added.append((add, backs[i]))
    return added


def delete_rules(run, pairs: list) -> bool:
    """按 `(放行命令, 回滚命令)` 对里的回滚命令逐条删除。"""
    ok = True
    for _add, back in pairs:
        res = run(back)
        good = res.returncode == 0
        ok = ok and good
        print(("[ok] 已回滚：" if good else "[warn] 回滚失败，请手工删除：") + " ".join(back))
    return ok


# --------------------------------------------------------------------- main --


def _flag_value(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def resolve_match(argv: list[str], mode: str, run) -> tuple:
    """算出要放行的 `(接口集, 网段集)`。

    · `--interfaces` 覆盖接口集（默认 `br-+` / `docker0`）
    · `--subnet` 显式给网段 ⇒ 只放它（iptables 下与接口规则叠加，ufw 下就是唯一来源）
    · ufw 且没给 `--subnet` ⇒ **枚举 docker 真实桥接网段**（替代写死的 172.16/12）
    """
    ifaces = tuple(
        s.strip() for s in (_flag_value(argv, "--interfaces") or "").split(",") if s.strip()
    ) or DOCKER_BRIDGE_IFACES
    explicit = _flag_value(argv, "--subnet")
    if explicit:
        return ifaces, (explicit,)
    if mode == "ufw":
        return ifaces, tuple(docker_bridge_subnets(run))
    return ifaces, ()


def main(argv: list[str], run=run_cmd) -> int:
    do_apply = "--apply" in argv
    do_rollback = "--rollback" in argv
    no_up = "--no-up" in argv
    compose = Path(_flag_value(argv, "-f") or _flag_value(argv, "--compose") or DEFAULT_COMPOSE)
    compose = compose.resolve()
    if not compose.is_file():
        return _fail([f"找不到 compose 文件：{compose}"])

    if (do_apply or do_rollback) and os.geteuid() != 0:
        return _fail([
            "改宿主防火墙需要 root —— 请用 `sudo` 重跑（本工具**不自己提权**）：",
            f"  sudo python3 tools/host_preflight.py {'--rollback' if do_rollback else '--apply'}",
        ])

    # 0) 接线校验：不通就别上线（复用既有工具，不另写一份判据）
    check = run(
        [sys.executable, str(Path(__file__).resolve().parent / "compose_wiring_check.py"),
         "-f", str(compose), "--resolve"],
        timeout=300,
    )
    if check.returncode != 0:
        print((check.stderr or "").strip()[-2000:], file=sys.stderr)
        return _fail(["接线校验未通过（见上面的 [fatal] 明细）⇒ 先修它，再谈防火墙。"])

    # 1) 起服务（网络与 minter 就绪）—— **只有 `--apply` 会动它**：不带参数时全程只读，
    #    否则"dry-run"就成了"顺手把服务起了"，那不是一个可依赖的只读承诺。
    if do_apply and not no_up and not do_rollback:
        print(f"[go] docker compose -f {compose} up -d")
        up = run(["docker", "compose", "-f", str(compose), "up", "-d"], timeout=900)
        if up.returncode != 0:
            return _fail([f"`docker compose up -d` 失败：{((up.stderr or up.stdout) or '').strip()[:400]}"])

    # 2) 读事实（**不猜**：端口拿不到就报错并给逃生口）
    port = _flag_value(argv, "--port")
    url = ""
    try:
        fact_port, url = minter_target(run, compose)
        port = port or fact_port
    except RuntimeError as e:
        if not port:
            return _fail([str(e)])
        print(
            f"[warn] {e}\n       ⇒ 按你给的 --port {port} 继续；**跳过容器内实测**"
            "（读不到 AVM_MINTER_URL，就没法在容器里探那条路）。",
            file=sys.stderr,
        )

    mode, why = firewall_mode(run)
    print(f"[info] minter 端口={port} ｜ 探测地址={url or '（未知）'} ｜ 防火墙={mode}（{why}）")

    # 3) 先算计划并**打印**，再决定是否动手
    try:
        ifaces, subnets = resolve_match(argv, mode, run)
        checks, allows, backs = allow_plan(mode, port, subnets=subnets, interfaces=ifaces)
    except RuntimeError as e:
        return _fail([str(e)])

    what = ("网段 " + "、".join(subnets)) if subnets else ("接口 " + "、".join(ifaces))
    print(f"[plan] 匹配方式：{what}（共 {len(allows)} 条规则）")
    for add, back in zip(allows, backs):
        print(f"[plan] 将要执行：{' '.join(add)}")
        print(f"[plan] 回滚命令（记下来）：{' '.join(back)}")
    if not do_apply and not do_rollback:
        print("[dry-run] 未做任何改动。要真正执行：sudo python3 tools/host_preflight.py --apply")
        return 0

    if do_rollback:
        print("[go] 回滚本工具这条拓扑该有的规则")
        return 0 if delete_rules(run, list(zip(allows, backs))) else 2

    # 4) 幂等放行
    try:
        added = ensure_allow(run, mode, port, subnets=subnets, interfaces=ifaces)
    except RuntimeError as e:
        return _fail([str(e)])

    # 5) 实测「包能到」；不通过就把**本次新增**的删回去。没有 URL 就如实说明未实测。
    if not url:
        print(
            "[warn] 没有 AVM_MINTER_URL ⇒ 本次**未实测**「包能到」。宿主能连 ≠ 容器能连，"
            "别把这一步当成已验过（用 `compose_wiring_check.py --probe` 补一次）。",
            file=sys.stderr,
        )
    else:
        problems = wiring.probe_minter(compose, url)
        if problems:
            for p in problems:
                print("[probe] " + p, file=sys.stderr)
            if added:
                print("[go] 探测未通过 ⇒ 回滚本次新增的规则（不留半改状态）")
                delete_rules(run, added)
            return _fail(["容器内仍打不到 minter（见上面的 [probe]）。宿主上 curl 通不代表这条路通。"])
        print("[ok] 容器内实测通过：闸门开启时能取到 token。")

    if mode == "iptables":
        print(
            "[warn] 走的是裸 iptables ⇒ **重启不保留**：用 `netfilter-persistent save`"
            "（或发行版对应方式）落盘，否则重启后 E2E-AVM-008 会复发。"
        )
    else:
        print(
            "[note] ufw 规则是按**当前**docker 网段写的：以后新建网络会拿到新网段 ⇒ 重跑本工具补齐。"
            "想彻底摆脱网段，可把接口式规则写进 /etc/ufw/before.rules"
            f"（`-A ufw-before-input -i br-+ -p tcp --dport {port} -j ACCEPT`）—— 它随 ufw 一起加载、"
            "且不受地址池变化影响。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
