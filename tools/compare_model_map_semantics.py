#!/usr/bin/env python3
"""把**两套** `X-Channel-Options.model_map` 实现跑在同一批输入上，逐例对比。

    aivideomaker   src/ark_compat/channel_options.py            resolve_model()
    video-adapter  script_store/aivideomaker/video@v1.py        resolve_upstream_model()

**为什么需要它**：两个项目共用同一套键名，但边界规则并不相同。只读文档会对不上 ——
文档说的是意图，这里比的是**行为**（每条用例都真跑两边的实现）。

⚠️ **2026-09-17 起 aivideomaker 侧已把通配降级为"唯一一条 `*` 兜底"**（多模式机制整体拆掉，
见 `docs/channel-options-model.md` §3）。所以本工具里的"非 `*` 通配"用例会显示为分歧：
这边**在解析配置时就拒绝**，那边则接受任意单条模式、只在**请求命中多条**时才报错。
"多条通配命中"那一栏的用例已随之删除 —— 这边现在**不可能**出现重叠。

用法（本机诊断，需 sibling 仓库；**不进 CI**、零网络、零副作用）：

    python tools/compare_model_map_semantics.py
    python tools/compare_model_map_semantics.py --repo /path/to/video-adapter

⚠️ 读结果的纪律：**不要把"词表不同"读成"规则不同"**。两边合法的槽位值几乎不重叠
（交集只有 `seedance20` / `wan27`），所以脚本默认只用交集值 —— 否则一多半"分歧"只是
"这个值在对面不合法"。词表那条单独打印，它是**对接事实**，不是缺陷。

退出码：0 = 无规则分歧；1 = 有分歧（便于挂进本地巡检）。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AIVID = HERE.parent
DEFAULT_OTHER = AIVID.parent / "video-adapter"


class _Ctx:
    """video-adapter 脚本 ctx 的桩：只实现它用到的那一个方法。"""

    class Fail(Exception):
        def __init__(self, message: str, code: str = ""):
            super().__init__(message)
            self.code = code or "channel_config_error"

    def fail(self, message, code="", param="", status=0):  # noqa: ANN001
        raise _Ctx.Fail(message, code)


def _load_other(repo: Path):
    path = repo / "script_store" / "aivideomaker" / "video@v1.py"
    if not path.exists():
        raise SystemExit(f"对面仓库里找不到脚本：{path}（用 --repo 指定）")
    spec = importlib.util.spec_from_file_location("vad_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 用例：只用两边**都合法**的槽位值（交集），差异才只能来自规则
A, B = "seedance20", "wan27"
CASES = [
    ("无配置 · 名字本身是槽位名", {}, A),
    ("无配置 · 表外名字", {}, "doubao-seedance-2-5-260628"),
    ("兜底通配 · 表外名字", {"model_map": {"*": B}}, "doubao-seedance-2-5-260628"),
    ("兜底通配 · 调用方指名真槽位", {"model_map": {"*": B}}, A),
    ("兜底通配 · 带分辨率后缀的真槽位名", {"model_map": {"*": B}}, f"{A}_1080p"),
    ("精确键压过兜底", {"model_map": {"doubao-x": A, "*": B}}, "doubao-x"),
    ("非 `*` 的通配模式（我已拒绝）", {"model_map": {"doubao-*": B}}, "doubao-x"),
    ("`?` 是字面量而非元字符", {"model_map": {"a?": B}}, "ab"),
    ("大小写敏感（大写真槽位名）", {"model_map": {"*": B}}, A.upper()),
]


def _mine(options, name):
    from ark_compat.channel_options import resolve_model
    from ark_compat.errors import ParamError

    try:
        r = resolve_model(name, options)
        return f"{r.slot}  [{r.source}]", r.slot
    except ParamError as e:
        kind = "渠道配置错误" if e.param == "X-Channel-Options" else "调用方错误(model)"
        return f"❌ {kind}", None


def _theirs(mod, options, name):
    try:
        slot, mapped, pattern = mod.resolve_upstream_model(name, options, _Ctx())
        src = ("model_map" + (f"/*{pattern}*" if pattern else "")) if mapped else "passthrough"
        return f"{slot}  [{src}]", slot
    except _Ctx.Fail as e:
        kind = "渠道配置错误" if e.code == "channel_config_error" else f"调用方错误({e.code})"
        return f"❌ {kind}", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=DEFAULT_OTHER, help="video-adapter 仓库路径")
    args = ap.parse_args()

    sys.path.insert(0, str(AIVID / "src"))
    from ark_compat.channel_options import KNOWN_SLOTS  # noqa: E402

    other = _load_other(args.repo)
    official = list(other.OFFICIAL_MODELS)
    inter = sorted(set(KNOWN_SLOTS) & set(official))

    print(f"值域 · aivideomaker（{len(KNOWN_SLOTS)}）: {', '.join(KNOWN_SLOTS)}")
    print(f"值域 · video-adapter（{len(official)}）: {', '.join(official)}")
    print(f"交集（{len(inter)}）: {', '.join(inter)}  ← 只用它取值，隔离「词表差异」")
    print()

    diff = 0
    for label, options, name in CASES:
        a, aslot = _mine(options, name)
        b, bslot = _theirs(other, options, name)
        agree = aslot == bslot
        diff += not agree
        print(f"{'一致  ' if agree else '❌分歧'} | {label}")
        print(f"         配置 {options}   请求 {name!r}")
        print(f"           aivideomaker  : {a}")
        print(f"           video-adapter : {b}")
    print(f"\n一致 {len(CASES) - diff} / 分歧 {diff}")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
