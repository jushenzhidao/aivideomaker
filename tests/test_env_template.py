""".env.example 模板的门禁：**双向**把关，且不得重复声明。

方向一（声明 → 代码）：声明的变量必须真的被代码读取。
方向二（代码 → 声明）：**生产代码**读取的变量必须被声明。

为什么两条都要（这不是对称性洁癖，是两类都真实发生过的事故）：

方向一的事故（2026-09-13，实测踩过）—— 本模板有三个"名字写错"的项，全部
**不报错、静默不生效**：用户照着模板配好了，功能却毫无变化，且没有任何提示
指向真正的原因：

| 模板里写的 | 代码实际读的 | 后果 |
|---|---|---|
| `AVM_COOKIE_FILE` | `COOKIES_FILE` | 指向的 cookie jar 被忽略 |
| `AVM_PORT` | `PORT` | 端口配置无效 |
| `AVM_GATE_KEY`（写了两条） | `AVM_GATE_KEY` | 后一条**静默覆盖**前一条 |

方向二的事故（2026-09-14 审计发现）—— `AVM_CONCURRENCY_DIVISOR`（多 worker 下
分摊账号额度的关键项）与 7 个 `AVM_GUNICORN_*` 一直在被代码读取，却从未登记进
模板：运维照模板配 `.env` 时**根本不知道这些开关存在**，更谈不上正确配置。
两个方向同属"配置与代码脱节"，只是一个多写、一个漏写。

这类错误肉眼极难发现（变量名都"看起来对"），所以做成可执行的门禁。

运行：python3 tests/test_env_template.py
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENV_EXAMPLE = ROOT / ".env.example"

SUFFIXES = {".py", ".mjs"}
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git"}

# 方向二的扫描范围：**只算生产面**。
# `src/web-adapter/` 是最初的逆向参考实现（已不再部署），把它的变量也要求登记，
# 等于逼着模板去描述一条不运行的代码路径 —— 那只会制造噪音，最终让人忽略门禁。
# 生产路径 = ark_compat 包 + 它的三个启动/装配入口。
PRODUCTION_MODULES = ("ark_compat",)
PRODUCTION_FILES = {"ark_server.py", "asgi_app.py", "gunicorn_conf.py", "watch_captcha.py"}

# 本项目自有配置的命名形态。不按这个圈定范围的话，"代码里的任意全大写字符串"
# 都会被当成配置项（例如枚举值、常量名），门禁会被噪音淹没。
CONFIG_PREFIXES = ("AVM_", "ARK_")
CONFIG_BARE = {"PORT", "COOKIES_FILE", "LOGFIRE_TOKEN"}

# 明确**不该**登记进模板的项（每一条都要写清理由，否则就成了"随手加白名单"）。
NOT_IN_TEMPLATE = {
    # gunicorn master 在 auto 模式下自己注入，模板里已按"勿手工设置"登记
    # （注意：它也**在**模板里，这里仅作说明位，不再重复登记）
}

DECLARED = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$", re.M)


def read_patterns(name: str) -> tuple:
    """一个变量的「被读取」写法。

    只认两种形态，刻意**不**试图穷举所有读取函数：

    1. 变量名作为**字符串字面量**出现 —— `env.get("X")`、`_env_flag(env, "X")`、
       `os.environ.get("X")` 全都涵盖（本项目这三种写法都存在，最初只按
       `.get("X")` 写会漏掉 `_env_flag`）。
    2. JS 的**点号访问** `process.env.X` —— 这种形式没有引号，必须单独列。

    判据强度：这是**弱判据**。它只能证明"变量名不只是出现在注释里"，不能证明
    语义接线正确。但它恰好覆盖真实发生过的两类错误（代码里根本没有该字符串 /
    只出现在注释里），而成本为零。
    """
    n = re.escape(name)
    return (
        rf"""["']{n}["']""",
        rf"""process\.env\.{n}(?![A-Z0-9_])""",
    )


def is_read(name: str, code: str) -> bool:
    return any(re.search(p, code) for p in read_patterns(name))


def _strip_comments(paths) -> str:
    """若干文件拼接成源码全文，但**剔除纯注释行** —— 注释里的提及不等于接线。

    实例：`ark_server.py` 有一行注释写着「用 ARK_* 而不是 AVM_PORT/PORT」。
    若不剔注释，`AVM_PORT` 会被误判为已接线，门禁就失去了判别力。
    """
    lines = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("#", "//", "*", "/*")):
                continue
            lines.append(line)
    return "\n".join(lines)


def all_source_files() -> list:
    return [
        p for p in sorted(SRC.rglob("*"))
        if p.is_file() and p.suffix in SUFFIXES
        and not any(part in SKIP_DIRS for part in p.parts)
    ]


def non_comment_source() -> str:
    """全仓库源码（含逆向参考目录），供方向一使用。"""
    return _strip_comments(all_source_files())


def production_files() -> list:
    files = [
        p for p in all_source_files()
        if any(part in PRODUCTION_MODULES for part in p.parts)
        or (p.parent == SRC and p.name in PRODUCTION_FILES)
    ]
    assert files, "没有扫描到任何生产面文件 —— 门禁的扫描范围写错了"
    # 防**范围漂移**：这两个文件是环境变量读取的权威来源（settings.py 是唯一
    # 集中解析处，gunicorn_conf.py 是进程启动前的装配处）。若扫描范围漏掉它们，
    # 门禁会退化成"只盯着冷门文件"，而漏报恰恰发生在这两个文件里。
    # 实测：把 PRODUCTION_MODULES 写错时，仅靠"数量下界"是拦不住的（剩下的
    # 文件仍能凑够 10 个名字）—— 所以这里按**具体文件**钉住。
    required = {SRC / "ark_compat" / "settings.py", SRC / "gunicorn_conf.py"}
    missing = sorted(str(p.relative_to(ROOT)) for p in required - set(files))
    assert not missing, f"生产面扫描漏掉了配置权威文件 {missing} —— 范围写错了"
    return files


def production_source() -> str:
    """生产面源码，供方向二使用。"""
    return _strip_comments(production_files())


def configured_names(code: str) -> list:
    """源码里以配置形态出现的变量名（字符串字面量 + JS 点号访问）。"""
    names = set(re.findall(r"""["']([A-Z][A-Z0-9_]{2,})["']""", code))
    names |= set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", code))
    return sorted(
        n for n in names
        if n.startswith(CONFIG_PREFIXES) or n in CONFIG_BARE
    )


class TestEnvTemplate(unittest.TestCase):
    def test_every_declared_var_is_actually_read(self):
        declared = [m.group(1) for m in DECLARED.finditer(
            ENV_EXAMPLE.read_text(encoding="utf-8")
        )]
        self.assertTrue(declared, ".env.example 里没有解析出任何变量声明")

        code = non_comment_source()
        unread = [n for n in declared if not is_read(n, code)]
        self.assertEqual(
            unread,
            [],
            "以下变量在 .env.example 里声明了，但**代码从不读取** —— "
            "照着配置不会生效，而且不会报错：\n  " + "\n  ".join(unread),
        )

    def test_no_duplicate_declarations(self):
        names = [m.group(1) for m in DECLARED.finditer(
            ENV_EXAMPLE.read_text(encoding="utf-8")
        )]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(
            dupes,
            [],
            "以下变量在 .env.example 里被声明了多次 —— 同一个 .env 文件中"
            "**后写的会静默覆盖前面的**：\n  " + "\n  ".join(dupes),
        )

    def test_every_production_var_is_declared(self):
        """方向二：生产代码读到的每个配置项都必须在模板里登记。

        漏登记的后果与写错名字**一样静默** —— 运维不知道有这个开关，
        于是永远用默认值跑，而且不觉得少了什么。
        """
        declared = {m.group(1) for m in DECLARED.finditer(
            ENV_EXAMPLE.read_text(encoding="utf-8")
        )}
        code = production_source()
        names = configured_names(code)
        # 防**空转**：若扫描范围写错（例如目录改名、后缀漏写），names 会变空，
        # 断言 [] == [] 照样通过 —— 那是最隐蔽的虚假绿灯。这里先钉一个下界。
        self.assertGreaterEqual(
            len(names), 10,
            f"方向二只扫到 {len(names)} 个配置项，明显偏少 ⇒ 扫描范围大概率写错了",
        )
        undeclared = [
            n for n in names
            if n not in declared and n not in NOT_IN_TEMPLATE
        ]
        self.assertEqual(
            undeclared,
            [],
            "以下变量**生产代码会读取**，但 .env.example 里没有登记 —— "
            "运维照模板配 .env 时无从知道它们存在：\n  " + "\n  ".join(undeclared),
        )


class TestBlanksMeanUnset(unittest.TestCase):
    """模板里 `VAR=` 的语义必须真的是"没设" —— 否则**照模板声明反而会把默认值清空**。

    这类陷阱比"名字写错"更阴：名字是对的、门禁也过了，只是值被空串顶掉。
    2026-09-14 发现 settings 里有两处用 `.get(name, DEFAULT)`（其余 20 多项都用
    `or DEFAULT`）⇒ `AVM_SERVICE_NAME=` 会把服务名清成空串、`AVM_BASE_URL=` 会让
    base_url 变成空。既然模板现在会声明这些项，就必须先钉住"空串 = 没设"。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "src"))

    def test_blank_service_name_falls_back_to_default(self):
        from ark_compat.settings import SERVICE_NAME, Settings

        s = Settings.from_env({"AVM_SERVICE_NAME": ""})
        self.assertEqual(s.service_name, SERVICE_NAME)
        # 未设与留空必须同义
        self.assertEqual(s.service_name, Settings.from_env({}).service_name)

    def test_blank_base_url_falls_back_to_default(self):
        from ark_compat.settings import Settings

        s = Settings.from_env({"AVM_BASE_URL": ""})
        self.assertTrue(s.base_url.startswith("http"), f"base_url 被清空了：{s.base_url!r}")
        self.assertEqual(s.base_url, Settings.from_env({}).base_url)

    def test_blank_optional_numbers_fall_back(self):
        from ark_compat.settings import Settings

        s = Settings.from_env({
            "AVM_CONCURRENCY_DIVISOR": "",
            "AVM_MAX_CONCURRENT": "",
            "AVM_ACCOUNT_REPORT_SECONDS": "",
        })
        self.assertEqual(s.concurrency_divisor, 1)
        self.assertEqual(s.max_concurrent, 2)
        self.assertEqual(s.account_report_seconds, 300)


if __name__ == "__main__":
    unittest.main()
