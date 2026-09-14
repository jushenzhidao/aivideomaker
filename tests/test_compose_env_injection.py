#!/usr/bin/env python3
"""模板里声明的 env 必须真的**注入容器** —— 否则在 .env 里改它毫无作用。

为什么需要这条（2026-09-14 env 三向比对实测发现）：
  `tests/test_env_template.py` 守的是「模板声明 ⇄ 生产代码读取」两个方向，但它
  **看不见「代码读取 ⇄ 编排注入」这一层**。实测：模板里 **18 个键**容器内的代码确实会读，
  而 `docker-compose.yml` 的 `environment:` 里**从没列过它们** —— 其中还包括 README
  明确推荐调的 `AVM_GUNICORN_TIMEOUT` / `AVM_GUNICORN_GRACEFUL_TIMEOUT`。
  ⇒ 照模板配 `.env` **完全不生效**，不报错、不告警，是最典型的"配了没用"。

  根因不是笔误：compose **只注入显式列出的变量**（本文件刻意不用 `env_file`，为的是让
  写错名字立刻暴露成空值），所以"模板声明"与"容器可见"本来就是两件事，必须有门禁把两者钉在一起。

做法：把 `.env.example` 声明的每个键拿去 compose 的 ark-compat `environment:` 里找；
找不到就必须在 `NOT_INJECTED` 里**逐个点名 + 写明理由**（唯一豁免通道，留痕可 review）。

解析走**两条独立路径**：`pyyaml`（若装了）与**行内解析**（零第三方依赖）。两条路都钉了
数量下界 —— 解析器一旦跟不上 compose 的写法就会红，而不会"两边都抽空所以通过"。
另有 `test_two_parsers_agree` 让两条路互相校验，防止兜底那条悄悄腐烂。

运行：python3 tests/test_compose_env_injection.py
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"

# 被检的服务：模板描述的变量都由它（及其容器内的 gunicorn）消费
APP_SERVICE = "ark-compat"

DECLARED = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$", re.M)

# 模板声明了、但**故意不注入** ark-compat 的键。每条必须写理由 —— 它是"让门禁闭嘴"的
# 唯一通道，所以必须留下可被 review 的痕迹。新加一条前先问：这真的是有意的吗？
NOT_INJECTED = {
    "AVM_CONCURRENCY_DIVISOR":
        "由 gunicorn master 在 auto + 多 worker 时自行 os.environ 下发（推导值，不是输入）",
    "COOKIES_FILE":
        "属**已不再部署**的 Node web-adapter（src/web-adapter/adapter.mjs 读它）",
    "PORT":
        "同上：web-adapter 的 HTTP 端口；本容器用 ARK_PORT（gunicorn bind）",
}

# 数量下界：防"解析器抽空 ⇒ 没问题"的假绿。改 compose 结构后若这两条红了，
# 先确认解析器是否还跟得上写法，再调数字。
_MIN_ENV_KEYS = 25
_MIN_DECLARED = 40


def env_keys_yaml(text: str) -> set:
    """用 pyyaml 解析出 ark-compat 的 environment 键集合。"""
    import yaml  # 局部导入：只有走这条路时才需要它

    data = yaml.safe_load(text) or {}
    env = (data.get("services", {}).get(APP_SERVICE) or {}).get("environment")
    if isinstance(env, dict):
        return {str(k) for k in env}
    if isinstance(env, list):   # `- KEY=value` 形态（本项目没这么写，但别误报成空）
        return {str(x).split("=", 1)[0].strip() for x in env}
    return set()


def env_keys_linebased(text: str) -> set:
    """零依赖的行内解析（pyyaml 不可用时的兜底）。

    只认本项目 compose 的写法：`  <svc>:` → `    environment:` → 缩进 ≥6 的 `KEY: …`，
    遇到缩进 ≤4 的行即认为该块结束。刻意不做通用 YAML —— 但**必须能被下界证伪**：
    写法一变、抽到的键变少，测试会因 `_MIN_ENV_KEYS` 而红，而不是静默变绿。
    """
    keys, in_svc, in_env = set(), False, False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 2 and line.endswith(":") and " " not in line[:-1]:
            in_svc = line[:-1] == APP_SERVICE      # 进了别的服务就停（各服务各有 environment）
            in_env = False
            continue
        if in_svc and indent == 4 and line == "environment:":
            in_env = True
            continue
        if in_env:
            if indent <= 4:                        # 块结束（volumes / healthcheck / 下一个键…）
                in_env = False
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                keys.add(m.group(1))
    return keys


def declared_keys() -> set:
    return {m.group(1) for m in DECLARED.finditer(ENV_EXAMPLE.read_text(encoding="utf-8"))}


def injected_keys() -> tuple:
    """(键集合, 来源说明)。pyyaml 优先，缺失则回落行内解析。"""
    text = COMPOSE.read_text(encoding="utf-8")
    try:
        return env_keys_yaml(text), "pyyaml"
    except ImportError:
        return env_keys_linebased(text), "行内解析（未装 pyyaml）"


class TestComposeInjectsDeclaredEnv(unittest.TestCase):
    def test_every_declared_key_is_injected_or_excused(self):
        declared = declared_keys()
        injected, source = injected_keys()
        self.assertGreaterEqual(
            len(declared), _MIN_DECLARED,
            f"模板只解析出 {len(declared)} 个键，明显偏少 ⇒ 解析或模板本身出问题了",
        )
        self.assertGreaterEqual(
            len(injected), _MIN_ENV_KEYS,
            f"{source}：ark-compat 的 environment 只抽到 {len(injected)} 个键，明显偏少 "
            "⇒ 解析器与 compose 写法脱节（不是「没配错」）",
        )
        missing = sorted(k for k in declared if k not in injected and k not in NOT_INJECTED)
        self.assertEqual(
            missing, [],
            "以下变量在 .env.example 里声明，但 compose 的 ark-compat environment **没有注入** "
            "⇒ 在 .env 里改它们完全不生效（静默无效配置）：\n  " + "\n  ".join(missing)
            + "\n  修法：① 在 compose 里加 `<KEY>: ${<KEY>:-}`（空串 = 没设，代码侧有默认值）；"
              "② 确实不该注入就写进 NOT_INJECTED 并说明理由。",
        )
        # 反向：豁免清单**过期**会掩盖真问题（某个键其实已经注入了，却还挂着豁免）
        stale = sorted(k for k in NOT_INJECTED if k in injected)
        self.assertEqual(stale, [], f"这些键其实已经注入了，豁免清单该删掉：{stale}")
        # 豁免清单里的键必须真的在模板里 —— 否则它是悬空备注，读的人会以为模板有这一项
        orphan = sorted(k for k in NOT_INJECTED if k not in declared)
        self.assertEqual(orphan, [], f"这些豁免项在模板里根本不存在（清单漂移）：{orphan}")

    def test_two_parsers_agree(self):
        """行内解析是 pyyaml 的兜底 —— 让两条路互相校验，防兜底那条悄悄腐烂。

        （未装 pyyaml 时这条会 skip：主门禁仍由兜底路径执行，不会出现"整条门禁没跑"。）
        """
        text = COMPOSE.read_text(encoding="utf-8")
        try:
            from_yaml = env_keys_yaml(text)
        except ImportError:
            self.skipTest("未装 pyyaml —— 只跑了行内解析那条路（主门禁不受影响）")
        from_line = env_keys_linebased(text)
        self.assertEqual(
            from_yaml, from_line,
            "两条解析路径对 ark-compat 的 environment 抽出的键不一致 ⇒ 二者的差异要当场查清"
            "（多/少的那几个键正是漏检点）：\n"
            f"  仅 pyyaml : {sorted(from_yaml - from_line)}\n"
            f"  仅行内解析: {sorted(from_line - from_yaml)}",
        )


if __name__ == "__main__":
    unittest.main()
