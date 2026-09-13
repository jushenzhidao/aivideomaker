#!/usr/bin/env python3
"""Ark 兼容服务的启动入口。

    export AVM_KEY="ak_xxx"
    python3 src/ark_server.py --port 8808

随后把火山官方 SDK 的 base_url 指向 http://127.0.0.1:8808/api/v3 即可：

    from arkruntime import Ark
    client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="ak_xxx")
    r = client.content_generation.tasks.create(
        model="doubao-seedance-2-5-260628",
        content=[{"type": "text", "text": "a red balloon"}],
        resolution="480p", ratio="16:9", duration=5,
        extra_body={"aivideomaker_max_credits": 60},   # ← 官方线提交即计费，必须先给上限
    )

依赖：fastapi / uvicorn / loguru / logfire / httpx（见 requirements.txt）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ark_compat.app import create_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.translate import billing_note  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="aivideomaker 官方 API → 火山方舟 Seedance 协议兼容服务")
    # 用 ARK_* 而不是 AVM_PORT/PORT，避免和 web-adapter（默认 8788）撞变量
    ap.add_argument("--host", default=os.environ.get("ARK_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ARK_PORT", "8808")))
    args = ap.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        sys.exit("需要 uvicorn：pip install -r requirements.txt")

    # 配置错误（例如缺 AVM_KEY）在起服务之前就暴露，而不是等第一个请求打进来
    settings = Settings.from_env()
    try:
        settings.validate()
    except ValueError as e:
        sys.exit(str(e))

    app = create_app(settings)
    pool = sorted(app.state.upstreams)
    default_kind = settings.upstream if settings.upstream in pool else (pool[0] if pool else "-")

    print(f"[ark-compat] Ark SDK base_url : http://{args.host}:{args.port}/api/v3")
    print(f"[ark-compat] available lines  : {', '.join(pool) or '(none)'}")
    print(f"[ark-compat] default line     : {default_kind}")
    print("[ark-compat]   switch per request:  X-Avm-Upstream: web|official   (或 ?upstream=)")
    for kind in pool:
        print(f"[ark-compat]   {kind:8} — {billing_note(kind)}")
    print(f"[ark-compat] upstream host    : {settings.base_url}")
    print(f"[ark-compat] gate             : {settings.gate_key or 'open (建议设 AVM_GATE_KEY)'}")
    if settings.official_ready:
        cap = settings.max_credits
        print(
            f"[ark-compat] official spend cap: "
            f"{cap if cap is not None else '(unset — 每个 official 提交必须自带 max_credits)'}"
        )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=None,   # 不覆盖 loguru 的装配
        access_log=False,  # 访问日志已由 request_context 中间件统一输出
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
