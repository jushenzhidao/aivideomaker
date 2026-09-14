#!/usr/bin/env python3
"""compose 接线校验（**宿主机侧**）—— 补 minter-preflight 的盲区。

## 为什么需要它（2026-09-14 报告 AVM12-PF-08 的根因）

`minter-preflight` 是个一次性容器，它比的是**自己那份 env**：

    MINTER_KEY:     ${AVM_MINTER_KEY:-}     # "minter 服务读到的值"
    ARK_MINTER_KEY: ${AVM_MINTER_KEY:-}     # "ark-compat 读到的值"

两侧同源 ⇒ **永远相等**。于是把 **minter 服务**那一行写死成字面量
（`MINTER_KEY: sk-hardcoded-minter-only`，`docker compose config` 已确认生效）之后，
preflight 照样报 `[ok]`。容器里既没有 docker CLI，也看不见别的服务的 env —— 这个盲区
**在容器内无法修**，只能搬到宿主机侧。

三类检查 + 一个实测探针，粒度不同、互相补位：

1. **静态**（默认，零依赖、不需要 docker）：解析 `docker-compose.yml`，检查两个服务给
   同一个键写的**插值表达式是否同源**。P-08 那种"只改一边（改成字面量）"必然被拦下。
   它看不见 `.env` 里的值，所以也拦不住"两侧都改了、但改成了不同的字面量"。
2. **解析后**（`--resolve`，需要 `docker compose`）：跑 `docker compose config`，拿**两个
   服务实际会拿到的 env** 逐项比对 —— 这是"事实"层面的检查，也是 P-08 的正解。
   顺带把 minter 的 fail-closed 守卫、`ARK_HOST`、闸门与透传互斥一起按真实值复核一遍。
3. **路径提示**（默认就会打 `[note]`）：minter 走 host 网络后，"compose 内部访问"并不
   存在 —— ark-compat（桥接）访问它是**出网到宿主**，受宿主防火墙 INPUT 链管辖。这件事
   从 YAML 里**验不出来**（宿主防火墙不在 compose 里），所以只提示不判失败，并指向 `--probe`。
4. **包能到**（`--probe`，需要 docker 且镜像已在本地）：起一个一次性容器（`docker compose
   run --rm`，与 ark-compat 同网络同 env），在**容器内**真实打一次 minter 的 `/healthz`。
   E2E-AVM-008 的教训：历轮接线验证都只验到「名字能解析」，没验到「包能到」—— node064
   的 `iptables -P INPUT DROP` 丢掉这条路，闸门一翻 3 条提交全部 429（minter `served`
   恒 1），而宿主侧 `curl` 全通。**宿主能连 ≠ 容器能连。**

## 用法

    python3 tools/compose_wiring_check.py                # 静态校验（CI 用这条）
    python3 tools/compose_wiring_check.py --resolve       # 部署前用这条（更强）
    python3 tools/compose_wiring_check.py --probe         # 最强：resolve + 容器内真实探测

退出码：`0` 通过 ｜ `2` 未通过（含无法解析、探测不到）。
⚠️ 它**不**替代容器内的 `minter-preflight` —— 那条守的是"镜像里的守卫本身能不能跑起来"，
两条都要留。

本轮（E2E-AVM-006）新增的检查：**minter 的 `TZ` 必须在场**。它和 `AVM_MINTER_URL`
同属"配了才可能对、不配也照样起"的项 —— 缺它时容器按 UTC 跑，CF 判定「时区/会话不一致」
⇒ 下发交互式挑战 ⇒ **铸造恒失败**，而服务照常 ready、`/healthz` 一片正常。
E2E-AVM-008 新增：minter 必须 `network_mode: host`（bridge 的 NAT 改写 TCP 指纹 ⇒
CF 判机器人 ⇒ 铸造整体失效，实测同机同 Chrome 宿主 3.7s ✅ / bridge ❌）+ 路径提示与探针。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPOSE = ROOT / "docker-compose.yml"

APP = "ark-compat"
MINTER = "minter"
PREFLIGHT = "minter-preflight"

# 同一个"接线键"在两个服务里的变量名 —— 这正是必须成对相等的那一对。
KEY_PAIRS = (
    ("MINTER_KEY", "AVM_MINTER_KEY"),
)

_INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-?([^}]*))?\}")


def _strip_inline_comment(value: str) -> str:
    """去掉 YAML 行内注释（` # …`）。本文件的值里没有出现过字面 ` #`。"""
    return value.split(" #", 1)[0].strip() if " #" in value else value.strip()


def parse_services(text: str) -> dict:
    """零依赖解析出 `services.*` 的 environment **原始值**与 `network_mode`（不做插值）。

    刻意不依赖 pyyaml：这个工具要在**任何**宿主机上能跑，包括没装 pyyaml 的运维机。
    只认本项目 compose 的写法（`services:` → 缩进 2 的服务名 → 缩进 4 的键），写法一变
    就会被"抽到 0 个服务"当场暴露，而不是静默放行。
    `network_mode`（E2E-AVM-008 起需要）：environment 之外的顶层标量键只认这一个，
    存放在服务字典的 `"network_mode"` 键下 —— environment 的键全是大写，不会撞名。
    """
    services: dict = {}
    in_services = False
    cur: str | None = None
    in_env = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            in_services = line == "services:"
            cur, in_env = None, False
            continue
        if not in_services:
            continue
        if indent == 2 and line.endswith(":") and " " not in line[:-1]:
            cur = line[:-1]
            services.setdefault(cur, {})
            in_env = False
            continue
        if cur is not None and indent == 4 and not in_env:
            if line == "environment:":
                in_env = True
                continue
            m2 = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
            if m2 and m2.group(1) == "network_mode":
                val = _strip_inline_comment(m2.group(2))
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                services[cur]["network_mode"] = val
            continue
        if in_env and cur is not None:
            if indent <= 4:            # 块结束（volumes / healthcheck / 下一个键…）
                in_env = False
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
            if m:
                val = _strip_inline_comment(m.group(2))
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                services[cur][m.group(1)] = val
    return services


def interp_default(expr: str) -> tuple[str, str | None] | None:
    """`${VAR:-default}` → `(VAR, "default")`；`${VAR}` → `(VAR, None)`；非插值 → `None`。"""
    m = _INTERP.fullmatch(expr.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


# 只有 `${VAR:-非空默认值}` 这一种写法能保证「.env 里留空」≠「能力被关掉」：
#   `${VAR}`      —— 没给默认值，变量没设就是空 ⇒ 静默关掉；
#   `${VAR-def}`  —— 只在**未设**时用默认值；`.env` 里写 `VAR=`（设了、值为空）照样是空。
# 这正是 0.0.10 那次事故的形状：把 `:-` 改回 `-`，容器都在跑、key 也配了，适配层却不取 token。
_FULL_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


def capability_problem(expr: str, what: str, effect: str = "") -> str | None:
    """检查一个"留空即静默失效"的变量的写法。返回问题描述或 `None`。

    `effect` 是"真的留空之后会发生什么"的落地描述 —— 判据共用，后果各不相同
    （地址变空 ⇒ 不去取 token；时区变空 ⇒ 铸造恒失败），把后果参数化是为了让报错
    直接指向症状，而不是让运维自己翻译一遍。
    """
    consequence = effect or ("能力被静默关掉（容器都在跑、key 也配了，适配层却根本不去取 token）")
    e = expr.strip()
    if not e:
        return f"{what} 为空 ⇒ {consequence}"
    if "${" not in e:
        return None                       # 写死的字面量：没有"留空即失效"的风险
    m = _FULL_DEFAULT.fullmatch(e)
    if not m:
        return (f"{what}={e} 不是 `${{VAR:-默认值}}` 形态。写成 `${{VAR}}` / `${{VAR-默认值}}` 时，"
                "用户把那一项**留空**会让它变空 ⇒ " + consequence)
    if not m.group(2).strip():
        return f"{what}={e} 的默认值是**空的** ⇒ 那一项留空就会失效（{consequence}）"
    return None


# ---------------------------------------------------------------- 静态检查 ----


def check_static(services: dict) -> list[str]:
    """不解析插值，只看**表达式**。返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    app, minter, pre = services.get(APP, {}), services.get(MINTER, {}), services.get(PREFLIGHT, {})

    # ① 两个服务给"同一个键"写的表达式必须**同源** —— P-08 的正解之一：
    #    任一侧被写成字面量（不是 ${…}）就说明有人手工接线只改了一边。
    for minter_var, app_var in KEY_PAIRS:
        left, right = minter.get(minter_var, ""), app.get(app_var, "")
        if not left or not right:
            problems.append(
                f"[接线] {MINTER}.{minter_var} / {APP}.{app_var} 有一个没写（"
                f"{MINTER}.{minter_var}={left!r}, {APP}.{app_var}={right!r}）—— "
                "两侧必须都从同一个变量插值，否则取 token 会 401。"
            )
            continue
        pi, ai = interp_default(left), interp_default(right)
        if pi is None or ai is None:
            bad = MINTER if pi is None else APP
            problems.append(
                f"[接线] {bad} 侧的 {minter_var if pi is None else app_var} 被写成了**字面量**"
                f"（{left if pi is None else right!r}）⇒ 它不再跟随 .env 里的 "
                f"AVM_MINTER_KEY，两侧会静默分叉（这正是 minter-preflight 看不见的那种改法）。"
                "改回 ${AVM_MINTER_KEY:-}。"
            )
            continue
        if pi[0] != ai[0]:
            problems.append(
                f"[接线] 两侧插值的**变量都不同**：{MINTER}.{minter_var} 用 ${{{pi[0]}}}、"
                f"{APP}.{app_var} 用 ${{{ai[0]}}} ⇒ 无论 .env 怎么填都不会相等。"
            )

    # ② 铸造能力的"静默关掉"契约：`AVM_MINTER_URL` 必须是字面量、或带**非空**默认值的
    #    插值（`${VAR:-http…}`）。见 `capability_problem` 里三种写法的差别。
    url_expr = app.get("AVM_MINTER_URL", "")
    if not url_expr:
        problems.append(f"[接线] {APP} 没有 AVM_MINTER_URL ⇒ 适配层不会去取 token。")
    else:
        p = capability_problem(url_expr, f"{APP}.AVM_MINTER_URL")
        if p:
            problems.append("[接线] " + p)

    # ③ 容器内必须显式绑 0.0.0.0 —— 绑回环会让端口映射形同虚设（本项目踩过的老坑）。
    host = app.get("ARK_HOST", "")
    if host != "0.0.0.0":
        problems.append(
            f"[接线] {APP}.ARK_HOST={host!r} ⇒ 容器内绑回环，宿主侧端口映射收不到连接。"
            "必须显式 0.0.0.0。"
        )

    # ④ P-07 的契约：preflight 必须拿到**原始**的 ALLOW_INSECURE，否则分不清"显式设置"
    #    与"compose 默认值"，归因又会不实（回环放行被报成"显式放行"）。
    if PREFLIGHT in services and "MINTER_ALLOW_INSECURE_RAW" not in pre:
        problems.append(
            f"[提示] {PREFLIGHT} 缺少 MINTER_ALLOW_INSECURE_RAW 插值 ⇒ 它无法区分"
            "「显式设了 ALLOW_INSECURE」与「取 compose 默认值」，归因会不实（报告 AVM12-PF-07）。"
        )

    # ⑤ 时区（E2E-AVM-006 单变量实验定案）：缺 `TZ` ⇒ 容器按 UTC 跑 ⇒ CF 判定「时区/会话
    #    不一致」⇒ 下发**交互式挑战** ⇒ 铸造恒失败。它和 AVM_MINTER_URL 属于同一类：
    #    "配了才可能对、不配也照样起"（不需要代码、不影响启动、/healthz 也照常 ready），
    #    只能靠部署前校验拦 —— 本轮为此白跑了三轮实验（合 5 小时以上）。
    #    判据复用同一份写法判定；⚠️ 这里**允许**写死字面量（手工接线时合理），
    #    仓库侧的 tests/test_minter_timezone.py 更严（要求从 AVM_MINTER_TZ 插值，
    #    因为文件头的清单声称有这个旋钮）—— 两者严格度不同是有意的，别对齐成一样。
    tz_expr = minter.get("TZ", "")
    if not tz_expr:
        problems.append(
            f"[接线] {MINTER} 没有 TZ ⇒ 容器按 UTC 跑，CF 会下发交互式挑战、**铸造恒失败**"
            "（E2E-AVM-006）。写成 `TZ: ${AVM_MINTER_TZ:-Asia/Shanghai}`。"
        )
    else:
        p = capability_problem(
            tz_expr, f"{MINTER}.TZ",
            "时区静默退化成 UTC ⇒ CF 下发交互式挑战、铸造恒失败（E2E-AVM-006）",
        )
        if p:
            problems.append("[接线] " + p)

    # ⑥ host 网络契约（E2E-AVM-008 起钉死）：minter 必须 `network_mode: host` ——
    #    bridge + NAT 会改写 TCP MSS/指纹 ⇒ CF 判机器人 ⇒ `render()` 全部吃满预算超时、
    #    0 成功（同机同 Chrome：宿主 3.7s ✅ / bridge 容器 ❌，实测）。删掉或改掉这一行，
    #    铸造就整体失效，而 /healthz 照样 ready —— 又是"配了不生效"的形态。
    minter_nm = minter.get("network_mode", "")
    if minter_nm != "host":
        problems.append(
            f"[接线] {MINTER}.network_mode={minter_nm!r} ⇒ 不走 host 网络时 NAT 会改写 "
            "TCP 指纹，CF 判机器人 ⇒ 铸造整体失效（实测同机同 Chrome：宿主 3.7s ✅ / "
            "bridge ❌）。必须保留 `network_mode: host`。"
        )
    return problems


# ---------------------------------------------------------------- 路径提示 ----


def host_path_notes(services: dict) -> list[str]:
    """**不判失败**、只在部署输出里喊出来的路径类提示（E2E-AVM-008）。

    minter 走 host 网络后，"compose 内部访问"并不存在 —— ark-compat（桥接）访问它是
    **出网到宿主**，受宿主防火墙 INPUT 链管辖。这件事从 YAML 里**验不出来**（宿主防火墙
    不在 compose 里），所以只提示 + 提供 `--probe` 实测。E2E-AVM-008 的 3 条 429 全部
    源于它（`iptables -P INPUT DROP` 丢包，而 minter `served` 恒 1 —— 配置全对）。
    """
    notes: list[str] = []
    if not services:
        return notes
    minter_nm = (services.get(MINTER) or {}).get("network_mode", "")
    app_nm = (services.get(APP) or {}).get("network_mode", "")
    if minter_nm == "host" and app_nm != "host":
        notes.append(
            "[路径] minter 走 host 网络 ⇒ ark-compat（桥接）访问它是**出网到宿主**，受宿主"
            "防火墙 INPUT 链管辖。宿主若 DROP（如 `iptables -P INPUT DROP` / ufw 默认拒），"
            "闸门翻起时适配层只能如实 429（E2E-AVM-008：3 条 429 全部源于此，而 minter "
            "served 恒为 1）。放行容器网段 → minter 端口（仅容器私网，不放公网）："
            "`ufw allow proto tcp from 172.16.0.0/12 to any port 8899`"
            "（回滚 `ufw delete allow proto tcp from 172.16.0.0/12 to any port 8899`），"
            "并用 --probe 验证「包能到」—— 宿主能连 ≠ 容器能连。"
        )
    return notes


# ---------------------------------------------------------------- 连通性探针 ----


def probe_snippet() -> str:
    """容器内执行的连通性探针源码（stdlib-only）。

    为什么用 stdlib 而不是 httpx：探针要能在**任何**镜像里跑，stdlib 最稳。
    `ProxyHandler({})` 显式清空代理 —— 环境里的 `HTTP_PROXY` 会把内网/回环地址也拐走
    （本站踩过：返回的是代理网关的错误体，不是 connection refused，极误导）。
    """
    return textwrap.dedent("""\
        import json, sys, urllib.request
        urls = [u.strip() for u in (sys.argv[1] or "").split(",") if u.strip()]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        out, ok_all = [], True
        for u in urls:
            rec = {"url": u}
            try:
                with opener.open(u.rstrip("/") + "/healthz", timeout=6) as r:
                    body = json.loads((r.read() or b"{}").decode("utf-8", "replace"))
                    rec.update(ok=True, status=r.status, ready=body.get("ready"))
            except Exception as e:
                rec.update(ok=False, error=type(e).__name__ + ": " + str(e)[:120])
                ok_all = False
            out.append(rec)
        print("PROBE_JSON=" + json.dumps(out, ensure_ascii=False))
        sys.exit(0 if ok_all else 1)
        """)


def interpret_probe(returncode: int, stdout: str, stderr: str) -> list[str]:
    """把探针输出翻成问题清单。**没有结果本身就是问题** —— 连通性未证实 ≠ 通过。"""
    problems: list[str] = []
    line = next((l for l in stdout.splitlines() if l.startswith("PROBE_JSON=")), None)
    if line is None:
        problems.append(
            f"[probe] 容器内探针没有给出结果（退出码 {returncode}）⇒ 连通性**未证实**，"
            "不能当成通过。stderr: " + ((stderr or "").strip()[-200:] or "(空)")
        )
        return problems
    try:
        records = json.loads(line[len("PROBE_JSON="):])
    except json.JSONDecodeError as e:
        problems.append(f"[probe] 探针输出不可解析（{e}）⇒ 连通性未证实。")
        return problems
    for rec in records:
        if rec.get("ok"):
            continue
        m = re.search(r":(\d+)", str(rec.get("url", "")))
        port = m.group(1) if m else "8899"
        problems.append(
            f"[probe] 容器内连不上 minter：{rec.get('url')} —— {rec.get('error')}。"
            "这正是 E2E-AVM-008 那 3 条 429 的成因形态：宿主能连 ≠ 容器能连，桥接容器 → "
            "宿主端口的包被宿主防火墙 INPUT 链丢弃。放行容器网段（docker 默认网段都在 "
            "172.16/12 内）：`ufw allow proto tcp from 172.16.0.0/12 to any port "
            f"{port}`（回滚 `ufw delete allow …`）；放通后重跑本探针确认。"
        )
    return problems


def probe_minter(compose: Path, url: str) -> list[str]:
    """在**与 ark-compat 同网络同 env** 的一次性容器里真实探测 minter。

    用 `docker compose run --rm --no-deps`：不依赖 ark-compat 正在运行（只要镜像在本地），
    也不会牵起 minter；`-T` 禁掉 TTY 分配。命令以 argv 传递，snippet 不经 shell，无转义问题。
    """
    cmd = ["docker", "compose", "-f", str(compose), "run", "--rm", "--no-deps", "-T",
           APP, "python", "-c", probe_snippet(), url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, cwd=str(compose.parent),
                             timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"[probe] 无法在容器内执行探针（{type(e).__name__}: {e}）⇒ 连通性未证实。"]
    return interpret_probe(out.returncode, out.stdout, out.stderr)


# -------------------------------------------------------------- 解析后检查 ----


def resolved_services(compose: Path) -> dict:
    """`docker compose config --format json` → `{服务: {env 键: 真实值}}`。

    `config` **不联网、不拉镜像**，纯本地插值。缺 docker / 解析失败一律抛错（调用方判失败）：
    显式要了 `--resolve` 却把"没跑成"当成"没问题"，正是这类前置检查最典型的自我欺骗。
    """
    out = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
        capture_output=True, text=True, cwd=str(compose.parent),
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:400] or "docker compose config 失败")
    data = json.loads(out.stdout or "{}")
    services = {}
    for name, svc in (data.get("services") or {}).items():
        env = svc.get("environment") or {}
        if isinstance(env, list):   # `- KEY=value` 形态
            env = dict(x.split("=", 1) for x in env if "=" in x)
        services[name] = {str(k): ("" if v is None else str(v)) for k, v in env.items()}
    return services


def _flag(raw: str) -> bool:
    """与 `turnstile_service.env_flag` **同口径**的白名单判定（只认开启值）。"""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def check_resolved(services: dict) -> list[str]:
    """按**真实生效的值**复核接线。返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    app, minter = services.get(APP, {}), services.get(MINTER, {})
    if not app or not minter:
        return [f"[解析] compose 里找不到 {APP} / {MINTER} 服务 —— 解析结果不可信。"]

    for minter_var, app_var in KEY_PAIRS:
        left, right = minter.get(minter_var, ""), app.get(app_var, "")
        if left != right:
            problems.append(
                f"[接线] **实际生效值**不一致：{MINTER}.{minter_var}={left!r} 而 "
                f"{APP}.{app_var}={right!r} ⇒ 取 token 会全部 401（报告 AVM12-PF-08 的场景）。"
            )

    url = app.get("AVM_MINTER_URL", "")
    if not url.strip():
        problems.append(f"[接线] {APP}.AVM_MINTER_URL 解析后为空 ⇒ 铸造能力被静默关掉。")

    if app.get("ARK_HOST") != "0.0.0.0":
        problems.append(f"[接线] {APP}.ARK_HOST={app.get('ARK_HOST')!r} ⇒ 端口映射失效。")

    # 时区：解析后的**真实值**必须非空（部署机上那份 override 手工改动的概率最高）
    if not minter.get("TZ", "").strip():
        problems.append(
            f"[接线] {MINTER}.TZ 解析后为空 ⇒ 容器按 UTC 跑，铸造会恒 interactive"
            "（E2E-AVM-006 单变量实验定案）。"
        )

    # 真实守卫判据（复用镜像里那份代码，不重写一遍 —— 重写必然分叉）
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import turnstile_service as S
    except Exception as e:  # noqa: BLE001
        problems.append(f"[守卫] 无法导入 tools/turnstile_service.py（{type(e).__name__}: {e}）⇒ "
                        "无法按真实值复核 fail-closed 守卫。")
    else:
        err = S.bind_guard_error(
            minter.get("BIND_HOST", "127.0.0.1"),
            minter.get("MINTER_KEY", ""),
            _flag(minter.get("MINTER_ALLOW_INSECURE", "")),
        )
        if err:
            problems.append("[守卫] minter 会拒绝启动：\n      " + err.replace("\n", "\n      "))

    # 闸门与透传互斥（`Settings.validate()` 的同一条契约，这里提前一步拦住）
    if _flag(app.get("AVM_PASSTHROUGH_COOKIE", "")) and app.get("AVM_GATE_KEY", "").strip():
        problems.append(
            f"[接线] {APP} 同时开了 AVM_PASSTHROUGH_COOKIE 与非空 AVM_GATE_KEY ⇒ "
            "同一个 Authorization 不可能既是闸门密钥又是上游凭据，服务会拒绝启动。"
        )
    return problems


def main(argv: list[str]) -> int:
    do_resolve = "--resolve" in argv
    do_probe = "--probe" in argv
    compose = DEFAULT_COMPOSE
    if "-f" in argv:
        compose = Path(argv[argv.index("-f") + 1]).resolve()
    if not compose.is_file():
        print(f"[fatal] 找不到 compose 文件：{compose}", file=sys.stderr)
        return 2

    text = compose.read_text(encoding="utf-8")
    services = parse_services(text)
    if len(services) < 3:
        print(
            f"[fatal] 只解析出 {len(services)} 个服务（{sorted(services)}）⇒ 解析器与 compose "
            "写法脱节。把「解析不出来」当成「没问题」是这类检查最典型的自我欺骗，故判失败。",
            file=sys.stderr,
        )
        return 2

    problems = check_static(services)

    # 两条路互相校验：装了 pyyaml 就比对一次，防"行内解析悄悄腐烂"。
    try:
        import yaml
    except ImportError:
        pass
    else:
        data = yaml.safe_load(text) or {}
        y = (data.get("services", {}).get(APP) or {}).get("environment") or {}
        y_keys = {str(k) for k in (y if isinstance(y, dict) else [])}
        line_keys = set(services.get(APP, {}))
        if y_keys and y_keys != line_keys:
            problems.append(
                "[解析] pyyaml 与行内解析对 ark-compat 的 environment 抽出的键不一致："
                f"仅 pyyaml={sorted(y_keys - line_keys)}，仅行内={sorted(line_keys - y_keys)}"
            )

    # 路径提示只喊不拦（宿主防火墙不在 compose 里，YAML 验不出来）—— E2E-AVM-008。
    for note in host_path_notes(services):
        print(note, file=sys.stderr)

    resolved = None
    if do_resolve or do_probe:
        try:
            resolved = resolved_services(compose)
        except (OSError, RuntimeError, json.JSONDecodeError) as e:
            print(f"[fatal] --resolve/--probe 要求 `docker compose config` 能跑通：{e}",
                  file=sys.stderr)
            return 2
        problems += check_resolved(resolved)

    if do_probe:
        url = ((resolved or {}).get(APP, {}) or {}).get("AVM_MINTER_URL", "").strip()
        if not url:
            problems.append(
                f"[probe] {APP} 的 AVM_MINTER_URL 解析后为空 ⇒ 无处可探（先把铸造能力恢复，"
                "见前面的 [接线] 项）。"
            )
        else:
            problems += probe_minter(compose, url)

    if problems:
        print(f"[fatal] compose 接线校验未通过（{len(problems)} 项）：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print(
            "\n  修法：两侧的 key 都写成同一个变量的插值（`${AVM_MINTER_KEY:-}`），"
            "别在任何一侧写死字面量。细节见 docker-compose.yml 的 minter 段注释。",
            file=sys.stderr,
        )
        return 2

    modes = ["静态"]
    if do_resolve:
        modes.append("docker compose config 解析后")
    if do_probe:
        modes.append("容器内真实探测（包能到）")
    print(f"[ok] compose 接线校验通过（{' + '.join(modes)}；服务 {sorted(services)}）")
    if not do_resolve and not do_probe:
        print("     提示：部署前可加 --resolve 用**实际解析出的值**再复核一遍；"
              "minter 走 host 网络的部署再加 --probe 验证「容器 → 宿主」的包真的能到"
              "（E2E-AVM-008：宿主能连 ≠ 容器能连）。")
    return 0


if __name__ == "__main__":
    os.chdir(str(ROOT))
    raise SystemExit(main(sys.argv[1:]))
