#!/usr/bin/env python3
"""文档里的**免费窗口秒数**必须跟着代码常量走。

为什么需要这条门禁（2026-09-14 审计的直接产物）：
  免费窗口先是被写成 8s，用户按站点终态记录（`480p/10s/turbo` ⇒ `paid=False`）
  更正为 **10s**。更正时**代码与测试都改了，5 处文档漏了** —— 根 README 的分界表、
  实现文档的启动自述样例、口径表、`prefer_free` 说明、响应示例，全都还写着 8s。

  代码侧本来就有门禁（`tests/test_free_window.py` 钉住常量与 `billing_note()`），
  但**没有任何门禁守文档** —— 于是"凡是被更正的数值，文档必然漂移"。

  这条漂移不是纯文字问题：文档是调用方**决定发什么请求**的依据。按 8s 理解，
  要么白白放弃 9~10s 的免费档，要么误以为 15s 免费而直接产生计费请求
  （判据是任务记录的 `paid`，花了就回不来）。

做法：把文档里所有"声明免费窗口秒数"的措辞当成契约，逐个抽出数字与
`FREE_MAX_DURATION` 比对。数字对不上就判失败，并指出是哪个文件哪一行。

运行：python3 tests/test_docs_billing_sync.py
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ark_compat.translate import FREE_MAX_DURATION  # noqa: E402

# 会被当作"口径声明"来校验的文档
DOCS = ("README.md", "src/ark_compat/README.md", ".env.example")

# (正则, 期望值函数, 人话描述)
# 措辞取自仓库现状；新增一处口径表述时把它的正则加进来，否则这条门禁守不住它。
ANCHORS = (
    (r"free up to (\d+)s", lambda free: free, "免费上界（billing_note 原文）"),
    (r"duration\s*≤\s*(\d+)\s*s", lambda free: free, "免费区间上界"),
    (r"主动拉回 (\d+)s", lambda free: free, "prefer_free 拉回的目标秒数"),
    (r"超过 (\d+)s 的", lambda free: free, "prefer_free 的生效阈值"),
    (r"duration\s*≥\s*(\d+)\s*s", lambda free: free + 1, "计费起始秒数（上界 + 1）"),
)


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
            total, 5,
            f"只匹配到 {total} 处口径声明，明显偏少 ⇒ 锚点正则已与文档措辞脱节，"
            "这条门禁正在空转（不是文档没问题）",
        )

    def test_every_doc_mentions_the_window_somewhere(self):
        """每份文档都至少要有一处声明 —— 否则"没有漂移"只是因为压根没写。"""
        for rel in DOCS:
            text = self._doc_text(rel)
            hits = sum(len(re.findall(p, text)) for p, _, _ in ANCHORS)
            with self.subTest(doc=rel):
                self.assertGreater(hits, 0, f"{rel} 里找不到任何免费窗口的口径声明")


if __name__ == "__main__":
    unittest.main()
