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

    # 配置错误（例如缺 AVM_COOKIE、旧的鉴权变量还留着）在起服务之前就暴露，
    # 而不是等第一个请求打进来。⚠️ `from_env()` 也包在里面：`AVM_AUTH` 的取值校验与
    # 「旧变量还有行为 ⇒ 拒绝启动」都在那一层（见 settings.parse_auth / legacy_auth_usage），
    # 否则那两处会以 traceback 而不是一句可读的 exit 信息呈现。
    try:
        settings = Settings.from_env()
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
    # 鉴权模式**如实打印**（一个变量三选一，见 settings 模块头）：排障时不必再猜
    # "到底是不是闸门模式"。旧的鉴权变量若只剩"关"值，也在这里点名提示删除。
    print(f"[ark-compat] auth mode        : {settings.auth}")
    print(f"[ark-compat] gate             : "
          f"{settings.gate_key or f'open (建议设 AVM_AUTH=key:<密钥>)'}")
    if settings.auth_deprecated:
        print(f"[ark-compat] [warn] 已废弃的鉴权变量仍在环境里（当前取'关'值、无行为差异）："
              f"{', '.join(settings.auth_deprecated)} —— 请删除，鉴权现由 AVM_AUTH 一个变量表达")

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
