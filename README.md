# aivideomaker.ai — web 线对接

围绕 [aivideomaker.ai](https://aivideomaker.ai) **网页端内部接口**的视频生成工具链：
把网页端协议翻译成第三方标准协议，附录里给出火山方舟 Seedance 形状的兼容层，
外加逆向调研笔记与实测产物。

本项目**只走 web 线**：认证只用登录会话 Cookie（`auth_session`），走 tRPC over `/api/*`。
不涉及官方 API 线。

选择 web 线的三个理由：

1. **有免费窗口** —— `tier=turbo` 且 `duration ≤ 10s` 不计费（判据见下方「计费红线」）。
2. **有独家模型** —— 网页端 11 个模型中有 7 个不在别处开放，含 **Google Veo 3.1（支持 4K）**、
   **Kling 3**、**Seedance 2.5**。
3. **零依赖可跑** —— 适配层不需要浏览器、正常额度内不需要过验证码，仅一个 HTTP 客户端加一个 Cookie。

> ⚠️ **两条必须提前知道的硬约束**
> 1. **web 线没有取消端点。** 任务一旦提交，只能删本地记录，上游照跑照扣。提交前想清楚。
> 2. **上游并发只跑 2 个。** 闸门是**进程内**信号量，「全局只跑 2 个」这个保证依赖单进程运行 ——
>    多 worker 会让实际并发变成 N×2。详见[「部署」](#部署)。

## 计费红线

| 情形 | 是否计费 |
|---|---|
| `tier=turbo` 且 `duration ≤ 10s` | ❌ **免费** |
| `tier=turbo` 且 `duration ≥ 11s` | ✅ 计费 |
| `tier=base`（任何时长） | ✅ 计费 |

判据只有一个：任务记录里的 **`paid`** 字段。
⚠️ `credits` 与它**反相**（免费任务也记 credits），不要拿 `credits` 判断是否花钱。

- 免费组合：**480p / 5s / turbo**。
- **时长是连续秒数，不是档位**：`range(5, 21)` 原样透传，仅越界时才就近钳制。
- 调试请求先带 `X-Avm-Dry-Run: 1`，构建完整请求体但不提交、不计费。

## 目录结构

```
aivideomaker/
├── README.md                          本文件
├── .env.example                       环境变量模板
├── requirements.txt                   兼容层依赖（fastapi/uvicorn/httpx/loguru/logfire）
├── Dockerfile                         非 root 镜像
├── docker-compose.yml                 容器编排（web 线）
├── .github/workflows/release.yml      打 tag 发版：Release + GHCR 多架构镜像
├── src/
│   ├── ark_compat/                    ★ 火山方舟 Seedance 协议兼容层（FastAPI）
│   │   └── README.md                  实现文档（字段映射、计费口径、闸门、未验证项）
│   ├── ark_server.py                  兼容层入口（uvicorn，默认 :8808）
│   ├── asgi_app.py                    ASGI 入口（gunicorn 使用）
│   ├── gunicorn_conf.py               worker 配置（按 worker 数向下分摊上游额度）
│   ├── web_session.py                 会话工具（验证会话 / 调用 tRPC procedure）
│   ├── extract_web_models.py          情报提取（从页面与 JS 挖模型清单、接口路径）
│   └── web-adapter/                   ★ Node 适配层（零依赖，Node ≥18，默认 :8788）
│       ├── README.md                  实现文档（接口清单、会话有效期、适配映射）
│       ├── adapter.mjs                适配层：OpenAI Responses + MiniMax + 火山方舟 Ark 三形状
│       ├── client.mjs                 AvmClient（session cookie，无浏览器、无验证码）
│       ├── submit-queue.mjs           提交队列（并发闸门 + 延迟执行 + 退避重试）
│       ├── tests/                     端到端测试（fixtures/ 内素材、archive/ 一次性探针）
│       └── tools/                     check-session / session-diagnose / credit-report / collect-tasks / upload
├── tests/                             Python 单测（380+ 项，零消耗、零外发）
├── tools/                             运维与验证工具（零第三方依赖）
│   ├── egress_audit.py                全量单测 + 零外发审计 + **零跳过**（CI 的测试门禁）
│   ├── compose_wiring_check.py        编排接线校验（两侧 key 必须同源；`--resolve` 比对真实值）
│   ├── turnstile_service.py           Turnstile 铸造**服务**（常驻，镜像是 Dockerfile.minter）
│   └── turnstile_minter.py            铸造核心（Xvfb 有头 Chrome + 原生 CDP）
├── docs/
│   ├── web-reverse/
│   │   ├── README.md                  调研笔记（接口形态、数据模型、待办）
│   │   ├── model-inventory.md         ★ 模型对照表（网页端 11 个）
│   │   ├── cookies.md                 Cookie 与会话过期机制（含实测 TTL 400 天）
│   │   ├── session-runbook.md         会话凭据 runbook（导出 / 续期 / 自检）
│   │   ├── TESTCASES.md               ★ 适配层测试用例报告（双向 request/response）
│   │   ├── test-run*.log              测试输出存档
│   │   └── captured/                  抓取资产（页面 HTML、JS chunk、sitemap）
│   └── pricing-enum-480p-720p.md      480p / 720p 定价枚举实测
├── avm-proxy/                         web 逆向早期工作目录（平铺版脚本，保留供对照）
└── assets/
    └── web-reverse/                   web 线实测产物（17 个，全部为免费组合）
```

## 快速开始

### 一、Node 适配层（零依赖，需 Node ≥18）

```bash
cd src/web-adapter
export AVM_COOKIE='auth_session=<40位随机串>'   # 浏览器导出，TTL 约 400 天
node tools/check-session.mjs                    # 先确认会话可用
node adapter.mjs                                # 起适配层 :8788
```

对外暴露三种协议形状，同一套上游、同一个端口：

| 协议 | base_url | 端点 |
|---|---|---|
| OpenAI Responses | `http://localhost:8788` | `POST /v1/responses` |
| MiniMax 官方 | `http://localhost:8788` | `POST /v1/video_generation` |
| 火山方舟 Ark / Seedance | `http://localhost:8788/api/v3` | `POST /contents/generations/tasks` |

```bash
# 零成本校验请求体（不提交、不扣费）—— 调适配层一律先做这个
curl -X POST localhost:8788/v1/video_generation \
  -H 'Authorization: Bearer sk-avm-demo' -H 'X-Avm-Dry-Run: 1' \
  -H 'content-type: application/json' \
  -d '{"content":"a car","imageUrl":"https://static.img2video.ai/...jpg","duration":5,"resolution":"480p"}'

npm test          # 77 项契约与参数校验，零计费
npm run test:live # 追加真实提交（2 条免费任务）
npm run tasks     # 汇总任务与成片链接
npm run credits   # 积分消耗对账
```

完整说明见 [`src/web-adapter/README.md`](src/web-adapter/README.md)，
测试用例与双向 request/response 见 [`docs/web-reverse/TESTCASES.md`](docs/web-reverse/TESTCASES.md)。

### 二、火山方舟兼容层（Python，FastAPI）

把 web 线包装成火山方舟 Seedance 协议，用火山官方 SDK 直接调本服务。

```bash
pip install -r requirements.txt

export AVM_COOKIE="auth_session=…"     # 唯一需要的凭据
export AVM_GATE_KEY=sk-local           # 本机闸门（不设则对任何调用方开放）

python3 src/ark_server.py --port 8808  # base_url: http://127.0.0.1:8808/api/v3
```

> **调用方自带凭据（透传）**：`export AVM_PASSTHROUGH_COOKIE=1`，调用方的
> `Authorization: Bearer` 直接携带**账号的**网页会话 cookie，本进程不再需要 `AVM_COOKIE`；
> 它与 `AVM_GATE_KEY` 互斥（同一个 Bearer 不可能既是闸门密钥又是上游凭据）。

```python
from arkruntime import Ark

client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="sk-local")
r = client.content_generation.tasks.create(
    model="doubao-seedance-2-5-260628",
    content=[{"type": "text", "text": "a red balloon"}],
    ratio="16:9", resolution="480p", duration=5,
    extra_body={"aivideomaker_dry_run": True},   # 调试期先 dry-run，零成本
)
```

路由、字段映射与官方《创建视频生成任务》
（[82379/1520757](https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1520757)）逐一对应。
Seedance 2.5「全能参考」上限为**图 4 / 视频 1 / 音频 2**，超限条目会被截断并在响应的
`unsupported` 中留痕。

测试：`python3 -m unittest discover -s tests`（380+ 项，零消耗、零外发）。
零外发可复验：`python3 tools/egress_audit.py`（有非回环出站、**或有用例被跳过**，都以非 0 退出
—— 进 CI 的门禁**被跳过 ≠ 通过**，实测有一条编排门禁因此长期没跑过）。
编排接线可复验：`python3 tools/compose_wiring_check.py`（部署前加 `--resolve` 用**解析后的真实值**
再核一遍，见下方「部署」）。
完整说明见 [`src/ark_compat/README.md`](src/ark_compat/README.md)。

## 部署

> **生产口径（2026-09-14 定）：Linux 服务器是一等公民**，下面两步开箱即用——
> Release 流水线会把主镜像与 minter（Turnstile 铸造服务）镜像一起推 GHCR
> （`ghcr.io/jushenzhidao/aivideomaker[-minter]`），compose 直接拉取。
> minter 必须跑在 **Linux 原生 Docker 的 host 网络**下（bridge + NAT 的 TCP MSS/指纹
> 改写会被 CF 判机器人，`render()` 一律 46s 超时；host 网络实测 2.3~3.5s/个）。
> macOS 只是开发机：Docker Desktop 的 host 网络仍经 VPNkit NAT，minter 请宿主直跑
> 并把 `AVM_MINTER_URL` 指过去。
>
> **项目定位：账号级语义 —— 一个实例服务一个账号。** 凭据、并发额度、免费窗口、
> 任务归属全部按账号隔离。多账号 = **部署多实例**（每实例一份 `AVM_COOKIE`，或调用方
> 固定带同一账号的 cookie）+ new-api 轮询（每渠道填一个账号的 cookie）；账号很少时
> 也可以单实例透传（`AVM_PASSTHROUGH_COOKIE=1`，每凭据仍是独立账号上下文、零串扰）。

```bash
cp .env.example .env      # 只需填 AVM_COOKIE（.env 已被 gitignore）—— 铸造服务不需要凭据
docker compose up -d      # 起 ark-compat + minter（宿主端口见 AVM_HOST_PORT，默认 8808）
```

> 🔴 **铸造能力是默认开启的**（2026-09-14 定）：闸门开启时没 token 的提交吞吐约 20 条/小时，
> 有铸造约 120 条/小时 —— 生产默认需要它，所以 `minter` 随 `up -d` 一起起。为此
> ark-compat 侧的 `AVM_MINTER_URL` 用 `:-` 插值：.env 里**留空与未设同义**，回退到
> `http://host.docker.internal:8899`（minter 走 host 网络 ⇒ 就监听在宿主 8899；Mac 上
> 宿主直跑 minter 也是这个地址）。**别把留空理解成"关掉能力"**。
>
> 🔓 **铸造服务默认开放：不需要 `AVM_MINTER_KEY`**（2026-09-14 定）—— **默认参数直接能跑**，
> 不新增任何必填项。代价必须知道：minter 走 host 网络 ⇒ 8899 就是**宿主端口**，无凭据即
> "谁连得上谁就能领 token"（等于把过闸能力分发出去，也会让账号更快撞风控）。三条应对：
> - **有公网 IP 就别敞开**：`AVM_MINTER_BIND_HOST=127.0.0.1`（需 ark-compat 也走 host 网络）
>   或内网地址；或在外层防火墙只放行 8808；
> - **要恢复 fail-closed**：`AVM_MINTER_KEY=<随机串>` 且 `AVM_MINTER_ALLOW_INSECURE=0`
>   —— 于是缺 key 会让 `docker compose up` 直接失败并打印两条出路（机制仍在，默认不启用）；
> - 开放模式启动时，minter 与前置校验都会打一条**显式告警**（仅非回环绑定时）。
>
> **只要适配层、不要铸造**时：`docker compose up -d ark-compat`（少一份常驻 Chrome）。
>
> **两侧必须成对相等**：minter 的 `MINTER_KEY` 与 ark-compat 的 `AVM_MINTER_KEY` 是同一个
> 值（compose 里两者同源于 `AVM_MINTER_KEY`；**手工接线**时最容易只改一边 ⇒ 取 token 全 401）。
>
> compose 仍带**前置校验**（`minter-preflight` 一次性容器，直接复用 minter 自己的守卫函数）：
> 默认（开放）模式下它只告警不拦；一旦按上面的方式恢复 fail-closed，缺 key 会让
> `docker compose up` **直接失败并打印两条出路** —— 不会再出现"命令退出 0、minter 却在
> restart 循环里"的静默失败（那正是 0.0.7 上线时的部署陷阱）。
>
> ⚠️ **但那个容器有盲区，必须知道**（2026-09-14 实测）：它比的是**自己那份 env**
> （`MINTER_KEY` 与 `ARK_MINTER_KEY` 都插值自 `AVM_MINTER_KEY` ⇒ 同源、永远相等），
> 因此它拦得住"改 preflight 自己的 env"，**拦不住最常见的那种改法** —— 把 **minter 服务**
> 那一行写死成字面量（容器里既没有 docker CLI，也看不见别的服务的 env）。
> **真正的接线校验在宿主机侧**，部署前跑一次：
>
> ```bash
> python3 tools/compose_wiring_check.py             # 静态：两侧 key 必须同源（CI 里也跑这条）
> python3 tools/compose_wiring_check.py --resolve   # 更强：比对 docker compose config 解析后的**真实值**
> ```
>
> 顺手还会拦下 `ARK_HOST` 被绑回环（端口映射会失效）与 `AVM_MINTER_URL` 丢掉 `:-` 默认值
> （铸造能力被**静默**关掉）这两类"配了不生效"。
>
> 🔧 **冷启动那一次铸造**：全新 profile 的**首次** render 实测 ≈46s，而常规预算是 45s
> ⇒ 不加宽就会"新部署的第一次铸造必然失败一次"（现象：`mint` 返回 503 + TIMEOUT，
> 而 `/healthz` 早已报 ready）。现在首轮走冷启动预算并在超时后用冷预算重试一次；
> 两条预算可调：`AVM_MINTER_RENDER_TIMEOUT_MS`（默认 45000）与
> `AVM_MINTER_COLD_RENDER_TIMEOUT_MS`（默认 120000）。`/healthz` 会把
> `warmed`（**真的铸出过** token）与 `ready`（Chrome 就绪）分开报 —— 只看 `ready` 会误判。

**多 worker 是这里唯一的坑。** 上游闸门是进程内 `threading.Semaphore`，「全局只跑 2 个」依赖
单进程：开 N 个 worker 会让实际并发变成 N×2，第 3 个起上游直接返回 `The queue is full`
（并更易触发验证码闸门）。安全做法是让 `AVM_WORKERS ≤ AVM_MAX_CONCURRENT`，
`src/gunicorn_conf.py` 会把全局额度按 worker 数**向下分摊**并打印实际全局值
（`2 ÷ 2` ⇒ 每 worker 1、全局仍是 2）。

其余两个必须成对看的参数：

- `AVM_GUNICORN_TIMEOUT`（默认 660s）必须**远大于**闸门等待上限 600s，否则 worker 会在
  等待空槽时被当卡死杀掉（表现为随机 502）。
- `AVM_GUNICORN_GRACEFUL_TIMEOUT` 要与容器 `stop_grace_period` 对齐，否则容器先 SIGKILL、
  gunicorn 来不及优雅退出。

任务表默认持久化到 SQLite（WAL，已 gitignore）；`AVM_TASK_STORE=memory` 是**显式**测试开关，
写错会直接抛错而非静默退化。`/healthz` 的 `task_store` 字段是判断当前后端的唯一依据。

`/healthz` 把 **`logfire`（SDK 装上了）与 `logfire_exporting`（数据真的会外发）分开报** ——
`send_to_logfire="if-token-present"` 且没有 `LOGFIRE_TOKEN` 时，前者是 `true`、后者是 `false`；
只看前者会以为 trace 在云端，**实际一条都没出去**。另外镜像依赖必须写成
`logfire[fastapi]`：少了这个 extra，`instrument_fastapi` 每次启动都会静默失败（只留一条
警告），每个请求的自动 span 随之消失。

## 关键结论

**网页端有 11 个模型**，其中 7 个不在别处开放：

| 仅网页端可用 | 其余 4 个 |
|---|---|
| `seedance2` `seedance25` `kling2_5` `kling3` `ltx23` `veo3Fast` `veo31Fast` | `t2v` `i2v` `minimax` `happyhorse` |

网页端独家包括 **Google Veo 3.1（支持 4K）**、**Kling 3**、**Seedance 2.5**。
完整对照与待验证事项见 [`docs/web-reverse/model-inventory.md`](docs/web-reverse/model-inventory.md)。

三条最重要的实操结论：

1. **先 dry-run 再提交。** `X-Avm-Dry-Run: 1` 会走完整的翻译与校验、返回 `effective` 预览
   （实际时长、分辨率、是否计费、`unsupported` 清单），但不提交、不计费。
2. **分辨率会被实际改写。** 请求 `720p` 时站点实际返回 **704p**，适配层刻意回填**实际值**而非请求值，
   所以不要拿请求参数当对账依据。
3. **验证码闸门是动态风控，不是账号属性。** `model.needsCaptcha` 按速率翻转，**第 1 条提交成功后
   即可能翻为 true**；开启时 `token: null` 被**静默拒绝**（返回空串、不报错），适配层会把它转成
   `429 RateLimitExceeded`。别误判为「会话失效」或「会话过期」——那是完全不同的故障。
   端到端测试**必须串行发**。

## 安全约定

- **网页端会话 Cookie（`auth_session`）等同账号凭据，禁止入库**：`.env`、`cookies.json`、
  `src/web-adapter/.session-state.json` 均已在 `.gitignore` 中排除。
- 变量名以**代码实际读取的**为准。历史上有三个名字写错（`AVM_COOKIE_FILE` / `AVM_PORT` /
  重复定义的 `AVM_GATE_KEY`），共同后果都是**不报错、静默不生效**。
  `tests/test_env_template.py` 是一道门禁：声明了却不被代码读取的变量会判失败。
- 批量或自动化调用一律先 dry-run 预检，并留意上方「验证码闸门」——连续提交会把它翻起来。
- `docs/web-reverse/captured/` 中为公开页面的抓取快照，仅用于离线分析。
