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

两类检查，粒度不同、互相补位：

1. **静态**（默认，零依赖、不需要 docker）：解析 `docker-compose.yml`，检查两个服务给
   同一个键写的**插值表达式是否同源**。P-08 那种"只改一边（改成字面量）"必然被拦下。
   它看不见 `.env` 里的值，所以也拦不住"两侧都改了、但改成了不同的字面量"。
2. **解析后**（`--resolve`，需要 `docker compose`）：跑 `docker compose config`，拿**两个
   服务实际会拿到的 env** 逐项比对 —— 这是"事实"层面的检查，也是 P-08 的正解。
   顺带把 minter 的 fail-closed 守卫、`ARK_HOST`、闸门与透传互斥一起按真实值复核一遍。

## 用法

    python3 tools/compose_wiring_check.py                # 静态校验（CI 用这条）
    python3 tools/compose_wiring_check.py --resolve       # 部署前用这条（最强）

退出码：`0` 通过 ｜ `2` 未通过（含无法解析）。
⚠️ 它**不**替代容器内的 `minter-preflight` —— 那条守的是"镜像里的守卫本身能不能跑起来"，
两条都要留。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
    """零依赖解析出 `services.*.environment` 的**原始值字符串**（不做插值）。

    刻意不依赖 pyyaml：这个工具要在**任何**宿主机上能跑，包括没装 pyyaml 的运维机。
    只认本项目 compose 的写法（`services:` → 缩进 2 的服务名 → 缩进 4 的 `environment:` →
    缩进 ≥6 的 `KEY: value`），写法一变就会被"抽到 0 个服务"当场暴露，而不是静默放行。
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
            in_env = line == "environment:"
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


def capability_problem(expr: str, what: str) -> str | None:
    """检查一个"留空即静默关掉能力"的地址变量写法。返回问题描述或 `None`。"""
    e = expr.strip()
    if not e:
        return f"{what} 为空 ⇒ 能力被静默关掉。"
    if "${" not in e:
        return None                       # 写死的字面量地址：没有"留空即关掉"的风险
    m = _FULL_DEFAULT.fullmatch(e)
    if not m:
        return (f"{what}={e} 不是 `${{VAR:-默认值}}` 形态。写成 `${{VAR}}` / `${{VAR-默认值}}` 时，"
                "用户把 .env 里那一项**留空**会让地址变空 ⇒ 能力被静默关掉"
                "（容器都在跑、key 也配了，适配层却根本不去取 token）。")
    if not m.group(2).strip():
        return f"{what}={e} 的默认值是**空的** ⇒ .env 里留空会把能力静默关掉。"
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
    return problems


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

    if do_resolve:
        try:
            resolved = resolved_services(compose)
        except (OSError, RuntimeError, json.JSONDecodeError) as e:
            print(f"[fatal] --resolve 要求 `docker compose config` 能跑通：{e}", file=sys.stderr)
            return 2
        problems += check_resolved(resolved)

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

    mode = "静态 + docker compose config 解析后" if do_resolve else "静态"
    print(f"[ok] compose 接线校验通过（{mode}；服务 {sorted(services)}）")
    if not do_resolve:
        print("     提示：部署前可加 --resolve 用**实际解析出的值**再复核一遍（更强）。")
    return 0


if __name__ == "__main__":
    os.chdir(str(ROOT))
    raise SystemExit(main(sys.argv[1:]))
