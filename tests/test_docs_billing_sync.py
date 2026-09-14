#!/usr/bin/env python3
"""文档里的**免费窗口秒数**必须跟着代码常量走。

为什么需要这条门禁（2026-09-14 审计的直接产物）：
  免费窗口先是被写成 8 秒，用户按站点终态记录（`480p/10s/turbo` ⇒ `paid=False`）
  更正为 **10 秒**。更正时**代码与测试都改了，5 处文档漏了** —— 根 README 的分界表、
  实现文档的启动自述样例、口径表、`prefer_free` 说明、响应示例，全都还是旧数字。

  代码侧本来就有门禁（`tests/test_free_window.py` 钉住常量与 `billing_note()`），
  但**没有任何门禁守文档** —— 于是"凡是被更正的数值，文档必然漂移"。

  这条漂移不是纯文字问题：文档是调用方**决定发什么请求**的依据。按 8 秒理解，
  要么白白放弃 9~10 秒的免费档，要么误以为 15 秒免费而直接产生计费请求
  （判据是任务记录的 `paid`，花了就回不来）。

做法：把这些载体里所有"声明免费窗口秒数"的措辞当成契约，逐个抽出数字与
`FREE_MAX_DURATION` 比对。数字对不上就判失败，并指出是哪个文件哪一行。

两轮范围修正（都是"机械证明"的产物，且栽在同一处：**范围靠人记**）：
  第一轮 —— 把 `src/ark_compat/app.py` 收进 `DOCS`。它的模块 docstring 里就写着一处
  口径，收进来后门禁立刻变红并精确指到 `app.py:28`（那时还是 8 秒）。
  教训：口径载体不止"文档"，**代码 docstring 同样是调用方读到的口径**。
  第二轮 —— 全仓库普查发现 `src/web-adapter/README.md` 两处、`docs/web-reverse/TESTCASES.md`
  一处，旧数字一直活到现在。⇒ 手维护的文件清单**必漏**。故改成**机械发现**：
  `discover_carriers()` 扫全仓库找出所有载体，命中文件必须在 `DOCS` 里点名、
  或进 `NOT_A_CARRIER` 显式豁免（并写明理由）。"范围"不再由人记，由仓库现状推出。

运行：python3 tests/test_docs_billing_sync.py
"""

import os
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat.translate import FREE_MAX_DURATION  # noqa: E402

# 机械发现的扫描面。排除 `.workbuddy`：记忆文件会**引述**旧数字（复盘用），
# 那不是"口径载体"，扫进来只会制造噪音 —— 而噪音会让门禁被忽略。
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "avm-proxy", "assets", ".workbuddy",
})
SCAN_SUFFIXES = frozenset({
    ".md", ".py", ".yml", ".yaml", ".example", ".txt", ".html", ".mjs", ".json",
})

# 会被当作"口径声明"来逐处比对的载体。清单里每一项都在机械发现里出现过，
# 不是凭印象写的；漏登记会被 `test_no_unlisted_carrier` 拦下。
DOCS = (
    "README.md",
    "src/ark_compat/README.md",
    "src/ark_compat/app.py",
    "src/ark_compat/settings.py",
    ".env.example",
    "docker-compose.yml",
    "docs/pricing-enum-480p-720p.md",
    "docs/web-reverse/README.md",
    "docs/web-reverse/TESTCASES.md",
    "docs/web-reverse/session-runbook.md",
    "src/web-adapter/README.md",
    "tests/test_ark_compat.py",
    "tests/test_web_upstream.py",
)

# 机械发现命中、但**不该**按本项目口径比对的文件。加一条必须写清理由 ——
# 它是"让门禁闭嘴"的唯一通道，因此必须留下可被 review 的痕迹。
# 例：{"docs/web-reverse/captured/x.html": "上游抓包快照，第三方原文，不该被本项目口径覆盖"}
NOT_A_CARRIER: dict = {}

# 发现数与命中数的下界：防"发现逻辑坏了 / 正则与措辞脱节"导致的**空转假绿** ——
# 断言集合为空时，"没有问题"与"什么都没扫到"的输出一模一样。
_MIN_CARRIERS = 5
_MIN_ANCHOR_HITS = 12

# (正则, 期望值函数, 人话描述)
# 措辞取自仓库现状；新增一处口径表述时把它的正则加进来，否则这条门禁守不住它。
ANCHORS = (
    (r"free up to (\d+)s", lambda free: free, "免费上界（billing_note 原文）"),
    (r"duration\s*≤\s*(\d+)\s*s", lambda free: free, "免费区间上界"),
    (r"主动拉回 (\d+)s", lambda free: free, "prefer_free 拉回的目标秒数"),
    (r"超过 (\d+)s 的", lambda free: free, "prefer_free 的生效阈值"),
    # `duration` 前缀**可选**：正文里那种省略写法（只写 `turbo` 且 `≥ Ns` 就计费）同样要抓。
    # 实测漏过一处（session-runbook.md，8 秒时代残留）—— 原锚点强求 `duration` 前缀，
    # 于是它一直没被扫出来。放宽后对现有载体**零误报**（逐条人工读过才采纳）。
    (r"(?:duration\s*)?≥\s*(\d+)s", lambda free: free + 1, "计费起始秒数（上界 + 1）"),
)


def _scan_files():
    """遍历扫描面。

    按目录**剪枝**再递归：`avm-proxy/` 有上万文件，走进去只为跳过它们是纯浪费。
    """
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = pathlib.Path(dirpath) / name
            if p.suffix in SCAN_SUFFIXES:
                yield p


def discover_carriers() -> dict:
    """机械发现：全仓库里出现"免费窗口秒数"声明的文件 → 命中次数（仓库相对路径）。"""
    found = {}
    for path in _scan_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # 失效符号链接、权限不足之类：跳过，而不是让门禁崩掉
            continue
        hits = sum(len(re.findall(p, text)) for p, _, _ in ANCHORS)
        if hits:
            found[str(path.relative_to(ROOT))] = hits
    return found


class TestDocsBillingSync(unittest.TestCase):
    def _doc_text(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_docs_quote_the_same_free_window_as_code(self):
        problems = []
        total = 0
        for rel in DOCS:
            text = self._doc_text(rel)
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern, expected, desc in ANCHORS:
                    for m in re.finditer(pattern, line):
                        total += 1
                        got = int(m.group(1))
                        want = expected(FREE_MAX_DURATION)
                        if got != want:
                            problems.append(
                                f"{rel}:{lineno} [{desc}] 文档写 {got}s，"
                                f"代码常量 FREE_MAX_DURATION={FREE_MAX_DURATION} "
                                f"⇒ 应为 {want}s\n      {line.strip()}"
                            )
        self.assertEqual(problems, [], "文档与代码的计费口径已分叉：\n  " + "\n  ".join(problems))
        # 防**空转**：正则跟不上文档措辞（例如有人改了表述方式）时，上面会 0 命中
        # 而"没有问题"照样通过 —— 那是最隐蔽的虚假绿灯。钉一个下界。
        self.assertGreaterEqual(
            total, _MIN_ANCHOR_HITS,
            f"只匹配到 {total} 处口径声明，明显偏少 ⇒ 锚点正则已与文档措辞脱节，"
            "这条门禁正在空转（不是文档没问题）",
        )

    def test_no_unlisted_carrier(self):
        """凡声明了免费窗口的文件都必须**点名**（或显式豁免）—— 这是"范围"的兜底。

        为什么必须有这条：`DOCS` 是人写的清单，而人会漏 —— 两轮都栽在这上面
        （先漏 `app.py`，再漏 `src/web-adapter/README.md` 与 `TESTCASES.md`）。
        这里反过来做：**先机械发现，再要求逐一点名**。新增载体而忘了登记，门禁直接红
        并报出文件名；要让它通过只有两条路 —— 加进 `DOCS`（会被逐处比对数字），
        或写进 `NOT_A_CARRIER` 并给出理由。两条都是**显式决定**，不会静默漏扫。
        """
        found = discover_carriers()
        self.assertGreaterEqual(
            len(found), _MIN_CARRIERS,
            f"机械发现只命中 {len(found)} 个载体，明显偏少 ⇒ 发现逻辑坏了（不是仓库干净）",
        )
        unlisted = sorted(set(found) - set(DOCS) - set(NOT_A_CARRIER))
        self.assertEqual(
            unlisted, [],
            "以下文件声明了免费窗口秒数，却既不在 DOCS 里点名、也没被显式豁免 —— "
            "门禁守不住它们（本文件两轮盲区都是这么来的）：\n  " + "\n  ".join(unlisted)
            + "\n  修法：① 加进 DOCS（会被逐处比对数字）；② 确实不该比对就写进 "
            "NOT_A_CARRIER 并说明理由。",
        )
        # 清单里的载体必须真的存在 —— 否则真因是"清单漂移"，而报错会指向文件读取
        for rel in DOCS:
            with self.subTest(doc=rel):
                self.assertTrue((ROOT / rel).is_file(), f"{rel} 在 DOCS 里，但文件不存在")

    def test_every_carrier_mentions_the_window_somewhere(self):
        """每个载体都至少要有一处声明 —— 否则"没有漂移"只是因为压根没写。"""
        for rel in DOCS:
            text = self._doc_text(rel)
            hits = sum(len(re.findall(p, text)) for p, _, _ in ANCHORS)
            with self.subTest(doc=rel):
                self.assertGreater(hits, 0, f"{rel} 里找不到任何免费窗口的口径声明")


if __name__ == "__main__":
    unittest.main()
