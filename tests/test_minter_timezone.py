#!/usr/bin/env python3
"""铸造服务的**时区**门禁（E2E-AVM-006 定案后的收口）。

## 为什么这条必须存在（三轮实验白跑的教训）

冷 profile 首铸失败先后被归因为「冷启动慢（≈46s）」「预算不够（45s→120s→600s）」
「profile 太新」，E2E-AVM-004/005/006 三轮（合计 5 小时以上）之后才定案：

    容器环境里**根本没有 TZ**（默认 UTC）⇒ CF 判定「时区/会话不一致」⇒ 下发**交互式
    挑战** ⇒ 铸造恒失败。同机单变量矩阵：无 TZ 时冷卷 interactive ×5、热卷 interactive ×3；
    补上 TZ 后热卷首铸 2.96s、全新冷卷首铸 5.20s，各 4/4 成功。

而这个变量此前在**部署里压根不存在** —— 不需要代码改动、不影响启动、`/healthz` 照常
`ready=true`，失败只体现在「铸造不出 token」这一个远端行为上。**这类"配了才可能对、
不配也照样起"的项只能靠门禁钉住**，否则下一个人删掉 compose 那行时不会有任何提示。

## 守什么（五层，每层都能被证伪）

1. **compose 注入了 TZ**，写法是 `${VAR:-非空默认值}` —— 写成 `${VAR}` / `${VAR-…}`
   会让 `.env` 留空静默退化成 UTC（与 `AVM_MINTER_URL` 那次「能力被静默关掉」同族）。
2. **镜像自带 ENV TZ + tzdata** —— 手工 `docker run` 不经 compose 也要拿到同一份环境，
   且 `TZ` 不能只在浏览器侧生效、被 libc 静默当成 UTC。
3. **Chrome 继承进程环境**（AST 断言 `Popen` 没传 `env=`）—— 一旦有人为了"干净"给
   Chrome 传白名单 env，TZ 会**静默**丢掉，而第 1、2 层全绿。这是机制层那一环。
4. **`/healthz` 如实报出生效时区**（env 原文 / libc 偏移 / zoneinfo 是否在场）。
5. **不做隐式兜底**：服务自己 `setdefault("TZ", …)` 会让「部署漏配」变得不可见 ——
   正是这次白跑两轮的成因，因此反着钉住。

全部离线：不真起 Chrome、不发真实挑战。
运行：python3 tests/test_minter_timezone.py
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "Dockerfile.minter"
MINTER_PY = TOOLS / "turnstile_minter.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "src"))

import turnstile_service as S  # noqa: E402

MINTER_SERVICE = "minter"
TZ_VAR = "AVM_MINTER_TZ"
TZ_DEFAULT = "Asia/Shanghai"

# `${VAR:-default}` —— 只有这一种写法能保证「.env 里留空」≠「时区退化成 UTC」
_FULL_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


# ------------------------------------------------------------------ 解析辅助 --


def service_block(text: str, service: str) -> str:
    """切出 `  <service>:` 到下一个顶层服务的文本块。"""
    m = re.search(rf"^  {re.escape(service)}:$", text, re.M)
    assert m, f"compose 里找不到服务 {service}"
    rest = text[m.end():]
    nxt = re.search(r"^  [a-z][a-z0-9-]*:$", rest, re.M)
    return text[m.start(): m.end() + (nxt.start() if nxt else len(rest))]


def env_value(block: str, key: str) -> str:
    """从服务块里取 `environment:` 下某个键的原始值字符串（空串 = 没写）。"""
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
            m = re.match(rf"{re.escape(key)}\s*:\s*(.*)$", line)
            if m:
                return m.group(1).split(" #", 1)[0].strip()
    return ""


def dockerfile_env(text: str) -> dict:
    """Dockerfile 的 `ENV` 指令 → {键: 值}（先拼续行，再逐行取；**不认注释**）。

    必须绕开注释：本文件在注释里多次写过 `TZ=Asia/Shanghai`（记录实测数据），
    用一行正则去搜会捞到注释里那句 —— 那是"门禁被自己写的文档骗过"的典型形状。
    """
    joined = re.sub(r"\\\n\s*", " ", text)          # 拼回 ENV 的续行
    out: dict = {}
    for line in joined.splitlines():
        if not line.startswith("ENV "):
            continue
        for tok in line[4:].split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k] = v
    return out


def popen_env_kwargs(tree: ast.AST) -> list:
    """`start_chrome()` 里每个 `Popen(...)` 收到的关键字集合（用于断言不裁剪环境）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "start_chrome":
            return [
                {k.arg for k in n.keywords if k.arg}
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "Popen"
            ]
    raise AssertionError("tools/turnstile_minter.py 里找不到 start_chrome()")


def tz_expression_problem(expr: str) -> str:
    """compose 里 `TZ` 的值**写法**判定 —— 真实门禁与变异自检**共用同一份判据**。

    分成"同一条判据 + 返回原因"而不是散落的断言：否则变异自检很容易变成
    "断言另一个写法"，全绿却与真实门禁无关（假自证）。
    """
    m = _FULL_DEFAULT.fullmatch(expr)
    if not m:
        return (f"TZ={expr!r} 不是 `${{VAR:-默认值}}` 形态（写死字面量会让 .env 里的 "
                f"{TZ_VAR} 静默失效，与本文件头的清单自相矛盾；`${{VAR}}` / `${{VAR-默认值}}` "
                "则会在 .env 留空时静默退化成 UTC）")
    if m.group(1) != TZ_VAR:
        return f"TZ 应从 {TZ_VAR} 插值，当前插的是 ${{{m.group(1)}}}"
    if not m.group(2).strip():
        return f"TZ={expr!r} 的默认值是空的 ⇒ .env 留空即失效（服务照常起、铸造一直失败）"
    return ""


def apt_packages(text: str) -> set:
    """`apt-get install` 指令里**真正装了**的包名集合（注释一律不算）。

    为什么要这么绕：本文件在注释里也写了 "tzdata"（解释它为什么装），用
    `assertIn("tzdata", 全文)` 去判会让**把包从 apt 行删掉**的变异照样全绿 ——
    门禁被自己写的文档骗过（实测踩到：变异自证时那一条是绿的）。
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    pkgs: set = set()
    for m in re.finditer(r"apt-get install[^;]*?(?=&&|;|\Z)", joined):
        chunk = m.group(0).split("--no-install-recommends", 1)[-1]
        pkgs |= {t for t in chunk.split() if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", t)}
    return pkgs


def compose_only_vars(text: str) -> set:
    """**机械推导**出"compose 专属变量"：compose 里插值用到、但 .env.example 未声明的 `AVM_*`。

    为什么不让它靠人维护清单：文件头那句「**N 个** compose 专属变量」在本项目已经漂移过一次
    （清单从 3 补到 6 时数字忘了改）。同理，"新加一个变量却忘了登记"是完全静默的 ——
    运维根本不知道有这个旋钮，而且没有任何症状。
    """
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
    declared = {m.group(1) for m in re.finditer(
        r"^([A-Z][A-Z0-9_]*)=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)}
    return {v for v in used if v.startswith("AVM_") and v not in declared}


def documented_compose_only_vars(text: str) -> set:
    """文件头清单里**登记了**的变量（`#   AVM_XXX  说明` 形态，与正文引用区分开）。"""
    header = text[: text.index("\nservices:")]
    return set(re.findall(r"^#\s+(AVM_[A-Z0-9_]+)\s+\S", header, re.M))


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def health_tz_in_subprocess(env: dict) -> dict:
    """在**独立进程**里导入服务并取 `health()['tz']`（避免继承本测试进程的 TZ）。"""
    code = ("import sys, json; sys.path.insert(0, r'%s');"
            "import turnstile_service as S;"
            "print(json.dumps(S.health()['tz'], ensure_ascii=False))" % TOOLS)
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def main_log_in_subprocess(env: dict) -> str:
    """跑一遍 `main()` 的启动段（**不真起服务、不真起 Chrome**），取 stdout。

    必须真的跑一遍而不是断言源码里有那行告警：告警写在 `if` 的哪一支、变量名对不对，
    只有执行才知道（源码级断言在"逻辑写反了"时照样绿）。
    """
    code = (
        "import sys, io, contextlib;"
        "sys.path.insert(0, r'%s');"
        "import turnstile_service as S;"
        "S.refill_loop = lambda: None;"
        "S.threading.Thread = type('T', (), {'__init__': lambda s,*a,**k: None,"
        "'start': lambda s: None});"
        "S.ThreadingHTTPServer = type('H', (), {'__init__': lambda s,*a,**k: None,"
        "'serve_forever': lambda s: None});"
        "buf = io.StringIO();"
        "\nwith contextlib.redirect_stdout(buf):\n"
        "    S.main()\n"
        "print(buf.getvalue())" % TOOLS
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout


def env_without_tz(**extra) -> dict:
    return {**{k: v for k, v in os.environ.items() if k != "TZ"}, **extra}


class TestComposeInjectsTimezone(unittest.TestCase):
    """第 1 层：compose 必须把 TZ 交给 minter，且留空不能静默退化成 UTC。"""

    @classmethod
    def setUpClass(cls):
        cls.text = COMPOSE.read_text(encoding="utf-8")
        cls.block = service_block(cls.text, MINTER_SERVICE)

    def test_the_block_split_is_not_the_whole_file(self):
        """防**空转**：块切分若退化成返回全文，后面几条断言就都失去判别力。"""
        self.assertLess(len(self.block), len(self.text))
        self.assertTrue(self.block.startswith(f"  {MINTER_SERVICE}:"))
        self.assertIn("environment:", self.block)

    def test_tz_is_declared_on_the_minter_service(self):
        self.assertTrue(
            env_value(self.block, "TZ"),
            f"{MINTER_SERVICE} 服务的 environment 里没有 TZ ⇒ 容器按 UTC 跑 ⇒ 铸造恒 interactive"
            "（E2E-AVM-006 的单变量矩阵定案）",
        )

    def test_tz_expression_keeps_a_nonempty_default(self):
        """★ 与 `AVM_MINTER_URL` 同族的契约：`:-` + 非空默认值（判据见 `tz_expression_problem`）。

        `TZ: ${AVM_MINTER_TZ}`（无默认值）时，`.env` 里那个键**留空**即退化成 UTC ——
        服务照常启动、`/healthz` 照常 ready，只有铸造一直失败。这正是最难查的形状。
        """
        expr = env_value(self.block, "TZ")
        self.assertEqual(tz_expression_problem(expr), "", f"TZ={expr!r} 的写法不合契约")

    def test_default_matches_the_documented_value(self):
        expr = env_value(self.block, "TZ")
        self.assertEqual(_FULL_DEFAULT.fullmatch(expr).group(2), TZ_DEFAULT)

    def test_the_header_documents_the_variable(self):
        """compose 文件头的「compose 专属变量」清单是运维唯一的发现入口（模板里没有它）。"""
        self.assertIn(TZ_VAR, documented_compose_only_vars(self.text),
                      "文件头清单里没有登记 AVM_MINTER_TZ ⇒ 运维无从知道有这时区旋钮")

    def test_the_header_list_matches_the_facts(self):
        """★ 清单必须与**机械推导**一致（两个方向都查），别让它靠人手维护：

        * 漏登记 ⇒ 运维不知道有这个旋钮（完全静默）；
        * 多登记 ⇒ 清单开始骗人，读的人会去找一个不存在的变量。
        本轮正是"新增 AVM_MINTER_TZ 却可能忘了改清单"的场景 —— 这条门禁就是为此。
        """
        derived, documented = compose_only_vars(self.text), documented_compose_only_vars(self.text)
        self.assertGreaterEqual(len(derived), 5, "推导出的变量太少 ⇒ 解析器已脱节（断言会空转）")
        self.assertEqual(derived - documented, set(), "以下 compose 专属变量没进文件头清单")
        self.assertEqual(documented - derived, set(), "以下清单项在 compose 里根本没用（清单漂移）")

    def test_the_claimed_count_matches_the_list(self):
        """「**N 个** compose 专属变量」这句话里的数字必须等于清单条数（本项目漂移过一次）。"""
        header = self.text[: self.text.index("\nservices:")]
        m = re.search(r"\*\*([一二三四五六七八九十]+)个\*\*\s*compose 专属变量", header)
        self.assertIsNotNone(m, "文件头那句「N 个 compose 专属变量」没找到 ⇒ 断言失效")
        claimed = _CN_NUM[m.group(1)]
        listed = documented_compose_only_vars(self.text)
        self.assertEqual(claimed, len(listed),
                         f"文件头声称 {claimed} 个，实际登记了 {len(listed)} 个：{sorted(listed)}")


class TestImageCarriesTimezoneByDefault(unittest.TestCase):
    """第 2 层：镜像自己也要有正确默认 —— 手工 `docker run` 不经 compose。"""

    @classmethod
    def setUpClass(cls):
        cls.text = DOCKERFILE.read_text(encoding="utf-8")
        cls.env = dockerfile_env(cls.text)

    def test_the_env_parser_actually_found_the_block(self):
        """防**空转**：解析器抽空时"值不对"与"没抽到"会混为一谈。"""
        self.assertIn("PORT", self.env, "Dockerfile 的 ENV 没解析出来 ⇒ 解析器已脱节")
        self.assertIn("CHROME_PROFILE", self.env)

    def test_env_tz_is_baked_in(self):
        self.assertIn("TZ", self.env, "Dockerfile.minter 的 ENV 里没有 TZ ⇒ 手工起容器拿到 UTC")
        self.assertTrue(self.env["TZ"].strip(), "ENV TZ 的值是空的")

    def test_the_mirrored_defaults_agree(self):
        """镜像默认值、compose 默认值、服务代码里的假定默认值，三处必须同值。"""
        self.assertEqual(self.env["TZ"], TZ_DEFAULT)
        self.assertEqual(S.TZ_DEFAULT, TZ_DEFAULT,
                         "turnstile_service.TZ_DEFAULT 与 compose/镜像不一致（三处同值）")

    def test_tzdata_is_installed(self):
        """没有它 libc 会把 `TZ=Asia/Shanghai` 静默当成 UTC（浏览器走 ICU，不受影响）。

        严格说这不是「铸造能否成功」的因子（E2E-AVM-006 的决定性实验就是在没有它的
        镜像上跑通的），但少了它 `TZ` 会变成"只对浏览器生效"的半吊子配置 ——
        本项目最忌讳的那类「配了像没配」。
        """
        pkgs = apt_packages(self.text)
        self.assertIn("xvfb", pkgs, "apt 包清单没解析出来 ⇒ 解析器已脱节（断言会空转）")
        self.assertIn("tzdata", pkgs, "apt 清单里没有 tzdata（注释里写着不算装上了）")


class TestChromeInheritsTheProcessEnvironment(unittest.TestCase):
    """第 3 层（机制）：TZ 是靠**环境继承**传给 Chrome 的。

    第 1、2 层全绿而这一层被破坏时，TZ 会在启动 Chrome 的那一刻**静默丢掉**：
    给 `Popen` 传一个自建的 `env=` 白名单就会这样（看起来还更「干净」）。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = MINTER_PY.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_popen_is_found_at_all(self):
        """防**空转**：找不到 Popen 时"没有 env= 关键字"会变成空话。"""
        self.assertTrue(popen_env_kwargs(self.tree),
                        "start_chrome() 里没有 Popen ⇒ 断言已失去判别力")

    def test_chrome_is_launched_without_a_private_env(self):
        for kw in popen_env_kwargs(self.tree):
            self.assertNotIn(
                "env", kw,
                "Chrome 启动时传了 env= ⇒ 环境不再继承，**TZ 会被静默丢掉**"
                "（E2E-AVM-006 定案的铸造前提）。要加变量请改进程环境，别裁剪子进程环境。",
            )


class TestServiceReportsTimezoneHonestly(unittest.TestCase):
    """第 4 层：`/healthz` 如实报出。第 5 层：不隐式兜底。"""

    def test_health_exposes_the_effective_timezone(self):
        tz = S.health()["tz"]
        for k in ("env", "name", "offset", "zoneinfo_present", "assumed_default"):
            self.assertIn(k, tz, f"health.tz 缺字段 {k} —— 排障时要能一次看全")

    def test_health_reports_the_env_verbatim_when_set(self):
        tz = health_tz_in_subprocess({**os.environ, "TZ": "Europe/Berlin"})
        self.assertEqual(tz["env"], "Europe/Berlin", "设了 TZ 就必须如实报出，不能报默认值")
        self.assertIsNone(tz["assumed_default"], "TZ 在场时不该同时报「用了默认值」")
        # 柏林的偏移按夏令时是 +0200 / 冬令时 +0100 —— 关键断言是「不是 +0000」：
        # 它证明 libc 真的按 IANA 名字解析了，而不是把 TZ 当无效值丢成 UTC。
        self.assertIn(tz["offset"], ("+0100", "+0200"), f"偏移异常：{tz['offset']}")

    def test_missing_tz_is_reported_as_missing_not_as_the_default(self):
        tz = health_tz_in_subprocess(env_without_tz())
        self.assertIsNone(tz["env"], "缺 TZ 必须如实报 None —— 报成默认值就掩盖了漏配")
        self.assertEqual(tz["assumed_default"], TZ_DEFAULT,
                         "要同时说明「这是代码假定的默认值」，让人一眼看出部署漏配")

    def test_the_service_does_not_silently_default_the_timezone(self):
        """★ 反兜底：导入本模块**不得**改写 `TZ`。

        `os.environ.setdefault("TZ", …)` 能让服务"恰好跑对"，代价是**下一个人再也看不到**
        「部署没配 TZ」这件事 —— 正是这次白跑两轮的成因。宁可让它响亮地失败。
        """
        code = ("import sys, os; sys.path.insert(0, r'%s');"
                "import turnstile_service;"
                "print(repr(os.environ.get('TZ')))" % TOOLS)
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, env=env_without_tz())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            out.stdout.strip(), "None",
            "导入 turnstile_service 改写了 TZ ⇒ 部署漏配会被代码悄悄补上，"
            "失败现象从「一眼可见」退化成「永远查不到」")

    def test_startup_log_shouts_when_tz_is_missing(self):
        """缺 TZ 的表现是「服务照常 ready、铸造永远失败」—— 必须在**启动日志**里喊出来。"""
        log = main_log_in_subprocess(env_without_tz(BIND_HOST="127.0.0.1"))
        self.assertIn("TZ 未设置", log, f"缺 TZ 时启动日志没有告警：\n{log}")

    def test_startup_log_is_quiet_when_tz_is_set(self):
        """反向断言：TZ 在场时不许再喊 —— 否则告警会因噪音被忽略（对告警脱敏）。"""
        log = main_log_in_subprocess({**os.environ, "TZ": TZ_DEFAULT, "BIND_HOST": "127.0.0.1"})
        self.assertNotIn("TZ 未设置", log, f"TZ 已设却仍在告警：\n{log}")
        self.assertIn(f"TZ={TZ_DEFAULT}", log, "启动日志应报出生效时区")


class TestGateIsFalsifiable(unittest.TestCase):
    """门禁自身的**可证伪性**：变异真实文件，确认每层都会红。

    不做这一步，"全绿"可能只是断言写错了 —— 这类门禁最典型的失败形态。
    """

    def test_missing_tz_on_the_minter_service_is_caught(self):
        block = service_block(COMPOSE.read_text(encoding="utf-8"), MINTER_SERVICE)
        self.assertTrue(env_value(block, "TZ"))
        self.assertEqual(env_value(block.replace("      TZ: ", "      # TZ: ", 1), "TZ"), "",
                         "变异没生效 ⇒ 门禁的锚点写错了（它必须拦得住自己造的变异）")

    def test_bad_tz_expressions_are_rejected(self):
        """四种坏写法逐条必须被**同一份判据**拒掉（含"看起来最像对的"那个）。"""
        for bad in ("${%s:-}" % TZ_VAR,            # 空默认值
                    "${%s}" % TZ_VAR,              # 无默认值 ⇒ 留空即空
                    "${%s-Asia/Shanghai}" % TZ_VAR,   # 少一个冒号：留空时照样是空
                    "Asia/Shanghai",               # 写死字面量 ⇒ .env 里的旋钮静默失效
                    "${OTHER_TZ:-Asia/Shanghai}"):    # 插错变量
            with self.subTest(expr=bad):
                self.assertTrue(tz_expression_problem(bad), f"坏写法竟被判为合规：{bad}")
        self.assertEqual(tz_expression_problem("${%s:-Asia/Shanghai}" % TZ_VAR), "")

    def test_a_whitelisted_chrome_env_is_caught(self):
        """变异真实源码：给 Chrome 的 Popen 加 `env=` ⇒ 第 3 层必须红。"""
        mutated = self._add_env_kwarg(MINTER_PY.read_text(encoding="utf-8"))
        self.assertIn("env", popen_env_kwargs(ast.parse(mutated))[0],
                      "变异没生效 ⇒ 第 3 层断言写错了")

    @staticmethod
    def _add_env_kwarg(src: str) -> str:
        anchor = "    subprocess.Popen(cmd, stdout=open(\"/tmp/chrome-run.log\", \"ab\"),"
        assert anchor in src, "Popen 的写法变了 ⇒ 变异锚点要跟着更新"
        return src.replace(anchor, anchor + "\n                     env={},", 1)

    def test_dropping_tzdata_from_the_apt_line_is_caught(self):
        """★ 这条门禁曾真实失效过：注释里也写着 tzdata，全文 `assertIn` 让变异照样全绿。"""
        src = DOCKERFILE.read_text(encoding="utf-8")
        mutated = src.replace("dbus-x11 tzdata \\", "dbus-x11 \\", 1)
        self.assertNotEqual(mutated, src, "变异锚点失效（apt 行改写了）")
        self.assertIn("tzdata", src, "注释里的提及仍在（说明判据不能只看全文是否存在）")
        self.assertNotIn("tzdata", apt_packages(mutated), "删掉包之后解析结果仍在 ⇒ 判据失效")

    def test_header_list_drift_is_caught(self):
        """变异：把 AVM_MINTER_TZ 从文件头清单里删掉（compose 里仍在用）⇒ 必须红。

        这正是本项目真实发生过的漂移形态（清单补项而数字/条目对不上），
        所以这条自检不能只查"清单里有没有提到"，必须查**两个方向的集合相等**。
        """
        text = COMPOSE.read_text(encoding="utf-8")
        mutated = re.sub(r"^#\s+AVM_MINTER_TZ\s+\S.*$", "#   （已删）", text, count=1, flags=re.M)
        self.assertNotEqual(mutated, text, "变异锚点失效（清单行改写了）")
        self.assertIn(TZ_VAR, compose_only_vars(mutated))
        self.assertNotIn(TZ_VAR, documented_compose_only_vars(mutated))

    def test_the_dockerfile_env_parser_ignores_comments(self):
        """本文件在注释里写过 `TZ=Asia/Shanghai` —— 解析器不许把它当 ENV 指令。"""
        sample = "# 注释：TZ=UTC\nENV A=1 \\\n    TZ=Asia/Shanghai\n"
        self.assertEqual(dockerfile_env(sample), {"A": "1", "TZ": "Asia/Shanghai"})
        self.assertEqual(dockerfile_env("# 只有注释 TZ=UTC\n"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
