"""aivideomaker 官方 API → 火山方舟 Seedance 协议兼容层。

把 aivideomaker 的**官方 API**（`key` 头，`/api/v1/*`）包装成火山方舟
`POST /api/v3/contents/generations/tasks` 的形状；把火山官方 SDK 的
`base_url` 指向本服务，即可用 Ark 代码调用 aivideomaker：

    from arkruntime import Ark
    client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="ak_xxx")

模块划分：

    translate.py      纯函数翻译层（Ark ↔ 官方），零依赖、可单测
    client.py         官方 API 客户端（httpx）
    settings.py       环境变量配置
    observability.py  loguru + logfire 装配
    app.py            FastAPI 路由与错误信封

⚠️ 计费语义：官方线**提交即计费**，没有 web 线（`src/web-adapter/`）的
turbo/≤8s 免费窗口。因此没有显式支出上限的提交会被直接拒绝。
调试一律先走 dry-run。
"""

__version__ = "1.0.0"

# ========================= 服务名（确定后只改这里） =========================
# 名字待定，先用工作名。这两行是**唯一来源** —— /healthz、日志、logfire 的
# service_name、FastAPI 的 title 全部引用它们；不确定时可临时用环境变量
# AVM_SERVICE_NAME 覆盖。
SERVICE_NAME = "avm-ark-compat"
SERVICE_TITLE = "aivideomaker → Volcengine Ark compat"

__all__ = ["__version__", "SERVICE_NAME", "SERVICE_TITLE"]
