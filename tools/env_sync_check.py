#!/usr/bin/env python3
"""`.env` ⇄ `.env.example` 的同步核验（**本机开发工具，不是 CI 门禁**）。

为什么不做成 CI 门禁：`.env` 已被 `.gitignore` 排除（它含真实 token），CI 里根本没有
这个文件。所以本项目里「模板 ⇄ 代码」由 `tests/test_env_template.py` 把关、
「模板 ⇄ compose 注入」由 `tests/test_compose_env_injection.py` 把关，而
「模板 ⇄ **生效文件**」这一环只能在有 `.env` 的机器上检查 —— 就是本脚本。

它把三件事一次做完，外加一条交叉校验：

  ① **结构**：生效文件缺哪些模板键 / 多了哪些键；
  ② **取值**：同一个键在两边取值不同（容忍 `0` vs `False`、`30` vs `30.0` 这类表示差异）；
  ③ **消费方**：生效文件里那些「生产代码不读」的键，逐个归类（web-adapter 段 /
     compose 插值 / 第三方 SDK / 无消费方=死配置）；
  ④ **交叉校验**：②③ 报出来的每一项，必须能在 `.env` 抬头的「刻意差异」清单里找到 ——
     清单过期（声明了一个其实已经不存在的差异）与漏写同样有害，两者都要红。

用法：
    python3 tools/env_sync_check.py             # 有漂移则退出码 1
    python3 tools/env_sync_check.py --quiet     # 只输出问题

同步手法见 skill `env-template-sync`：以模板为母版**只搬结构/注释**，取值保留生效文件
的；改完立刻重跑本脚本，让清单与事实一致。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TPL_FILE = ROOT / ".env.example"
COMPOSE = ROOT / "docker-compose.yml"
HEADER_END = 4          # `.env` 前 N 行内出现第 2 条 `# ====` 分隔线即为抬头结束


def parse(path: pathlib.Path) -> dict:
    """`KEY=VALUE` → dict。行内注释（` # ...`）不算取值。

    ⚠️ 用 Python 解析而不是 shell `grep`：某些环境里 `grep` 会静默给空结果，把
    「有内容」误判成「没内容」，而这类核验最怕的就是静默。
    """
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        out[key.strip()] = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return out


def header_keys(text: str) -> set:
    """抬头「刻意差异」清单里提到的键名。"""
    lines, fences = [], 0
    for line in text.splitlines(keepends=True):
        if line.startswith("# ===="):
            fences += 1
            if fences > 1:
                break
        lines.append(line)
    return set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", "".join(lines)))


# 凭据类键名。🔴 **核验工具本身就是一个泄露面** —— 它要打印两份 env 的取值，顺手就会把
# 真实 token / key 打到终端、日志、以及任何被贴出去的报告里（本工具第一版就踩到了）。
# 一律隐去，只留长度：足够判断"是不是同一个值"，不泄露任何字节。
_SENSITIVE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|KEY", re.I)


def masked(name: str, value: str) -> str:
    """打印用的取值：凭据类只给长度，绝不回显。"""
    if not value:
        return repr(value)
    if _SENSITIVE.search(name) or value.startswith("key:"):
        return f"<已隐去：{len(value)} 字符>"
    return repr(value)


def same_value(a: str, b: str) -> bool:
    """容忍「同一语义的两种表示」：`0` / `False`、`30` / `30.0`、空串 / 缺失。"""
    if a == b:
        return True
    def norm(v: str):
        v = v.strip().strip('"\'')
        low = v.lower()
        if low in ("0", "false", "no", "off", ""):
            return ("flag", False) if low != "" else ("empty", "")
        if low in ("1", "true", "yes", "on"):
            return ("flag", True)
        try:
            return ("num", float(v))
        except ValueError:
            return ("str", v)
    return norm(a) == norm(b)


def consume_owner(name: str, compose_text: str) -> str:
    """生效文件里的键由谁消费（生产面之外的归类）。"""
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests"))
    import test_env_template as t

    if name in t.configured_names(t.production_source()):
        return "生产代码"
    if t.is_read(name, t.non_comment_source()):
        return "web-adapter 段（已不部署的代码路径）"
    # 值位插值：`SOME_KEY: ${NAME:-默认}` —— 键名在右边，不在行首
    if re.search(rf"\$\{{{re.escape(name)}\b", compose_text):
        return "compose 插值"
    if "TOKEN" in name:
        return "第三方 SDK 直读（待确认）"
    return "★ 死配置"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="只输出问题")
    args = ap.parse_args()

    for path in (ENV_FILE, TPL_FILE):
        if not path.exists():
            print(f"缺少 {path.name} —— 本工具需要两份文件同时在位", file=sys.stderr)
            return 2

    env_text = ENV_FILE.read_text(encoding="utf-8")
    env, tpl = parse(ENV_FILE), parse(TPL_FILE)
    compose_text = COMPOSE.read_text(encoding="utf-8") if COMPOSE.exists() else ""
    declared = header_keys(env_text)
    problems: list[str] = []

    only_env = sorted(set(env) - set(tpl))
    only_tpl = sorted(set(tpl) - set(env))
    print(f"[结构] 生效 {len(env)} 键 / 模板 {len(tpl)} 键；"
          f"只在生效 {len(only_env)}、只在模板 {len(only_tpl)}")
    for name in only_tpl:
        problems.append(f"生效文件**缺**模板键 {name}（照模板配的功能不会启用）")
        print(f"    ★ 只在模板: {name}")

    print("[取值] 两边不同的键：")
    for name in sorted(set(env) & set(tpl)):
        if not same_value(env[name], tpl[name]):
            mark = "已登记" if name in declared else "★ 未登记"
            if mark == "★ 未登记":
                problems.append(f"{name} 取值与模板不同，但抬头清单没写理由")
            print(f"    {name:<34} 生效={masked(name, env[name]):<28} "
                  f"模板={masked(name, tpl[name])}  [{mark}]")

    print("[消费方] 生产代码不读的键：")
    for name in sorted(env):
        owner = consume_owner(name, compose_text)
        if owner == "生产代码":
            continue
        if owner.startswith("★"):
            problems.append(f"{name} 没有任何消费方（死配置）")
        print(f"    {name:<34} → {owner}")

    print()
    if problems:
        print("✗ 发现未收口的漂移：")
        for p in problems:
            print(f"  · {p}")
        print("\n处置：按 skill `env-template-sync` 补齐结构 / 补写抬头清单，或把值收口成与模板一致。")
        return 1
    print("✓ 结构与取值均与模板对齐，且每一项差异都能在抬头清单里找到依据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
