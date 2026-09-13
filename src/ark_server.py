#!/usr/bin/env python3
"""Ark 兼容服务的启动入口。

    export AVM_COOKIE="auth_session=…"
    python3 src/ark_server.py --port 8808

随后把火山官方 SDK 的 base_url 指向 http://127.0.0.1:8808/api/v3 即可：

    from arkruntime import Ark
    client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="sk-local")
    r = client.content_generation.tasks.create(
        model="doubao-seedance-2-5-260628",
        content=[{"type": "text", "text": "a red balloon"}],
        resolution="480p", ratio="16:9", duration=5,
        extra_body={"aivideomaker_dry_run": True},   # ← 调试期先 dry-run，零成本
    )

上游是 aivideomaker 的**网页端内部接口**（tRPC over /api，会话 cookie）。
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
    ap = argparse.ArgumentParser(
        description="aivideomaker 网页端内部接口 → 火山方舟 Seedance 协议兼容服务"
    )
    # 用 ARK_* 而不是 AVM_PORT/PORT，避免和 web-adapter（默认 8788）撞变量
    ap.add_argument("--host", default=os.environ.get("ARK_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ARK_PORT", "8808")))
    args = ap.parse_args(argv)

    # 启动自述必须**立刻**可见：stdout 被重定向到文件/管道时是块缓冲，
    # 容器里 `docker logs` 的头几秒会是空的 —— 看起来像"服务没起来"或"没读到配置"。
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, OSError):  # 非 TextIOWrapper（测试捕获等）时忽略
        pass

    try:
        import uvicorn
    except ImportError:
        sys.exit("需要 uvicorn：pip install -r requirements.txt")

    # 配置错误（例如缺 AVM_COOKIE）在起服务之前就暴露，而不是等第一个请求打进来
    settings = Settings.from_env()
    try:
        settings.validate()
    except ValueError as e:
        sys.exit(str(e))

    app = create_app(settings)
    pool = sorted(app.state.upstreams)
    # 透传线的客户端要等凭据才存在，但**能力**要如实上报 —— 否则启动自述会打
    # "(none)"，把操作者引向"没配凭据"这个错误结论。
    kinds = sorted(
        set(pool) | (set(settings.available_upstreams) if settings.passthrough_cookie else set())
    )

    print(f"[ark-compat] Ark SDK base_url : http://{args.host}:{args.port}/api/v3")
    print(f"[ark-compat] available lines  : {', '.join(kinds) or '(none)'}")
    for kind in kinds:
        print(f"[ark-compat]   {kind:8} — {billing_note()}")
    print(f"[ark-compat] upstream host    : {settings.base_url}")
    if settings.passthrough_cookie:
        print(
            "[ark-compat] credentials      : 调用方自带（web ← 调用方会话 cookie）"
            " —— 本进程不持有该侧凭据"
        )
    print(f"[ark-compat] gate             : {settings.gate_key or 'open (建议设 AVM_GATE_KEY)'}")

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
