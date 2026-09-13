""".env.example 模板的门禁：声明的变量必须真的被代码读取，且不得重复声明。

为什么需要这两条断言：本模板此前有三个"名字写错"的项，全部**不报错、静默不生效**
—— 用户照着模板配好了，功能却毫无变化，且没有任何提示指向真正的原因：

| 模板里写的 | 代码实际读的 | 后果 |
|---|---|---|
| `AVM_COOKIE_FILE` | `COOKIES_FILE` | 指向的 cookie jar 被忽略 |
| `AVM_PORT` | `PORT` | 端口配置无效 |
| `AVM_GATE_KEY`（写了两条） | `AVM_GATE_KEY` | 后一条**静默覆盖**前一条 |

这类错误肉眼极难发现（变量名都"看起来对"），所以做成可执行的门禁。
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENV_EXAMPLE = ROOT / ".env.example"

SUFFIXES = {".py", ".mjs"}
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git"}

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


def non_comment_source() -> str:
    """源码全文，但**剔除纯注释行** —— 注释里的提及不等于接线。

    实例：`ark_server.py` 有一行注释写着「用 ARK_* 而不是 AVM_PORT/PORT」。
    若不剔注释，`AVM_PORT` 会被误判为已接线，门禁就失去了判别力。
    """
    lines = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("#", "//", "*", "/*")):
                continue
            lines.append(line)
    return "\n".join(lines)


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


if __name__ == "__main__":
    unittest.main()
