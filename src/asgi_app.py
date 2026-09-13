#!/usr/bin/env python3
"""可导入的 ASGI 应用入口 —— 供 gunicorn 这类「导入字符串」启动器使用。

`src/ark_server.py` 是**脚本式**入口（自己建 app 再调 uvicorn.run），gunicorn 复用它
需要一个 `模块:对象` 的导入路径，所以单开这个模块。

    PYTHONPATH=src gunicorn -c src/gunicorn_conf.py asgi_app:app

两个入口的语义刻意保持一致：配置错误（AVM_COOKIE 缺失、AVM_TASK_STORE 拼错…）都在
**启动期**抛错，而不是等第一个请求打进来才 500。

关于 worker 数：`app` 是**模块级对象**，由每个 worker 进程各自导入 ⇒ 每个 worker 拿到
自己的 app 实例，也就有自己的上游并发闸门与 SQLite 连接。这不是疏漏，是刻意为之；
代价与换算规则见 `src/gunicorn_conf.py` 顶部说明。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python -c "import asgi_app"` 这类从 src/ 直接使用；gunicorn 场景靠
# PYTHONPATH=src（见 Dockerfile），这里只是冗余保险。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ark_compat.app import create_app as _build_app  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402


def create_app():
    """按环境变量建 app；配置非法直接抛错（启动期暴露，与 ark_server.py 一致）。"""
    settings = Settings.from_env()
    settings.validate()
    return _build_app(settings)


# gunicorn 用 `asgi_app:app` 直接取这个；也支持 `--factory asgi_app:create_app`。
app = create_app()
