"""防漂移门禁：散落在多处源码里的 UA / visitorId 字面量必须保持一致。

为什么需要这条测试：UA 曾在 4 个源码里各写一份，其中 3 处同版本、1 处不同 ——
是靠人肉 grep 才发现的。同一类「散落的字面量」只要没有门禁就会再次分叉，
所以这里把它做成可执行的断言，而不是文档里的一句提醒。

两条刻意的设计：

1. **覆盖清单断言**：若只断言「所有值都相同」，那么删掉某处定义后，剩下的样本
   仍然相同 ⇒ 测试**静默通过**。这正是典型的虚假绿灯。覆盖清单让「少写一处」
   也变成红灯。
2. **剔除注释行**：注释里提到 `Chrome/140` 不是"又一处 UA 定义"。若把注释也算进去，
   门禁会对着文档措辞报错，最终被人加 `noqa` 关掉 —— 那还不如没有。
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# 被扫的源码后缀；跳过依赖目录（node_modules 里满是无关的 Chrome/NNN 字样）
SUFFIXES = {".py", ".mjs"}
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git"}

# 期望包含 UA 定义的文件（相对仓库根，posix 风格）。
# 新增一处 UA 定义时**必须**同步加进来，否则覆盖断言会失败 —— 这是刻意的。
UA_FILES = {
    "src/ark_compat/web_client.py",
    "src/web-adapter/client.mjs",
    "src/web-adapter/tools/session-diagnose.mjs",
    "src/web_session.py",
}

# visitorId 默认值散落三处。首版测试只列了两处、漏掉 session-diagnose.mjs ——
# 补上覆盖清单后，「某一处被删」才会被断言抓到。
VISITOR_ID_FILES = (
    "src/ark_compat/web_client.py",
    "src/web-adapter/client.mjs",
    "src/web-adapter/tools/session-diagnose.mjs",
)

CHROME_VERSION = re.compile(r"Chrome/(\d+)\.\d+\.\d+\.\d+")
VISITOR_ID = re.compile(r"""["']([0-9a-f]{32})["']""")
FULL_UA = re.compile(r"Mozilla/5\.0\(Macintosh;IntelMacOSX.*?Safari/537\.36")


def flat(text: str) -> str:
    """去掉引号 / 加号 / 全部空白。

    Python 的隐式字符串拼接与 JS 的跨行书写都会被打平，于是**跨行写的 UA
    也能作为一整串**被提取出来 —— 否则正则只能匹配到半截。
    """
    return re.sub(r"""["'`+\s]""", "", text)


def code_only(path: pathlib.Path) -> str:
    """文件内容，剔除纯注释行 —— 注释里的提及不等于"这里定义了一个 UA"。"""
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(("#", "//", "*", "/*")):
            continue
        kept.append(line)
    return "\n".join(kept)


def source_files():
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


class TestLiteralConsistency(unittest.TestCase):
    def test_every_expected_file_still_declares_a_ua(self):
        missing = [f for f in sorted(UA_FILES) if not (ROOT / f).exists()]
        self.assertEqual(missing, [], f"UA 覆盖清单指向的文件已不存在：{missing}")

        found = {f for f in UA_FILES if FULL_UA.search(flat(code_only(ROOT / f)))}
        self.assertEqual(
            found,
            UA_FILES,
            "以下文件不再包含可识别的 UA 字面量（定义被删？写法变了？）：\n  "
            + "\n  ".join(sorted(UA_FILES - found))
            + "\n若确实要移除某处 UA，请同步更新本测试的 UA_FILES 清单。",
        )

    def test_full_ua_string_is_identical_everywhere(self):
        by_ua = {}
        for f in sorted(UA_FILES):
            for ua in FULL_UA.findall(flat(code_only(ROOT / f))):
                by_ua.setdefault(ua, []).append(f)
        self.assertEqual(
            len(by_ua),
            1,
            "UA 完整串在不同文件间分叉了：\n"
            + "\n".join(f"  {ua}\n    <- {files}" for ua, files in by_ua.items()),
        )

    def test_chrome_major_version_is_identical_everywhere(self):
        """兜底扫描：即使 UA 写法变了、FULL_UA 抓不到，Chrome 主版本也必须唯一。"""
        versions = {}
        for path in source_files():
            rel = path.relative_to(ROOT).as_posix()
            for m in CHROME_VERSION.finditer(code_only(path)):
                versions.setdefault(m.group(1), set()).add(rel)
        self.assertTrue(versions, "整个 src/ 里找不到任何 Chrome/<version> 字样")
        self.assertEqual(
            len(versions),
            1,
            "UA 的 Chrome 主版本分叉了："
            + "; ".join(f"{v} <- {sorted(fs)}" for v, fs in versions.items()),
        )

    def test_visitor_id_defaults_match(self):
        by_value = {}
        for f in VISITOR_ID_FILES:
            values = VISITOR_ID.findall(code_only(ROOT / f))
            self.assertTrue(
                values,
                f"{f} 不再包含 32 位十六进制字面量（visitorId 默认值被删或写法变了）；"
                "若确实已移除，请同步更新本测试的 VISITOR_ID_FILES 清单。",
            )
            for value in values:
                by_value.setdefault(value, []).append(f)
        self.assertEqual(
            len(by_value),
            1,
            "visitorId 默认值不一致 —— 多账号场景下，不同实现会发出不同的访客标识："
            + "; ".join(f"{v} <- {fs}" for v, fs in by_value.items()),
        )


if __name__ == "__main__":
    unittest.main()
