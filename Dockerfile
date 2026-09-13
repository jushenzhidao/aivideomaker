# aivideomaker → 火山方舟 Seedance 协议兼容服务（src/ark_compat）
#
# 为什么必须显式把 ARK_HOST 改成 0.0.0.0：
#   项目默认 ARK_HOST=127.0.0.1（对本机自测友好）。容器里绑回环 ⇒ 端口映射形同虚设，
#   表现为"容器 running、启动日志正常、但外部连不上"，最容易被误判成镜像有毛病。
#
# 配置一律走环境变量：项目不加载 dotenv（requirements 无 python-dotenv、全仓库无
# load_dotenv），所以 .env 既不需要也不应该进镜像。compose 会在启动时把 .env 里的
# 值通过 environment 注入。
#
#   docker run --rm -p 8808:8808 -e AVM_COOKIE="auth_session=…" -v avm-tasks:/data \
#     ghcr.io/yuanjie-ai/aivideomaker:latest
#
# 注：这里刻意**不用** `# syntax=docker/dockerfile:1` —— 该指令会强制去 Docker Hub
# 拉一个 BuildKit 前端镜像，凭空多一个外部依赖（实测在受限网络下这一步就会直接失败）。
# 本文件没有用到任何 BuildKit 专属语法，不需要它。

FROM python:3.12-slim

# 只作构建溯源用（项目代码不读它）。放 ENV 是为了能从镜像 config blob 的
# Env 里读回版本号 —— build-arg 本身不会留在 config 里。
ARG APP_VERSION=dev

LABEL org.opencontainers.image.title="aivideomaker ark-compat" \
      org.opencontainers.image.description="aivideomaker.ai → 火山方舟 Seedance 协议兼容层（上游：网页端内部接口）" \
      org.opencontainers.image.source="https://github.com/yuanjie-ai/aivideomaker" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ARK_HOST=0.0.0.0 \
    ARK_PORT=8808 \
    AVM_TASK_DB=/data/.ark-tasks.db \
    PYTHONPATH=/app/src \
    APP_VERSION="${APP_VERSION}"

WORKDIR /app

# 依赖单独成层：requirements.txt 未变则命中缓存，改源码不会触发重装
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt

COPY src/ ./src/

# 任务表落 SQLite。默认路径在 cwd；容器里显式指到 /data，挂卷后跨容器重启仍能查到任务。
# 顺带建非 root 用户并交出 /app、/data 属主。
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8808

# 存活探针只打本地 /healthz。deep 默认 0 ⇒ 不打上游，因此没有凭据也能过，
# 不会把"上游没配"误报成"容器不健康"。
# ⚠️ 该状态本身**不会**触发容器重启（Docker 只标记 unhealthy）。需要按健康状态
#    自动重建，用 compose 里的看门狗开关（AVM_GUNICORN_WATCHDOG=1）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8808/healthz', timeout=4).status == 200 else 1)"

# 默认走 gunicorn + uvicorn worker（生产/高可用形态，配置见 src/gunicorn_conf.py）。
# 用绝对路径 + PYTHONPATH，避免依赖 `--chdir` 与 `-c` 的加载先后顺序。
# 要回退到脚本式入口（开发/排障时日志更直观）：
#   docker run ... --entrypoint python <image> src/ark_server.py
CMD ["gunicorn", "-c", "/app/src/gunicorn_conf.py", "asgi_app:app"]
