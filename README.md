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
│   ├── compose_wiring_check.py        编排接线校验（key 必须同源、回环绑定×桥接会被拦；`--resolve` 比对真实值）
│   ├── env_sync_check.py              `.env` ⇄ `.env.example` 同步核验（**本机工具**：CI 里没有 `.env`）
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
export AVM_AUTH=key:sk-local           # 鉴权：一个变量三选一（留空 = 不校验）

python3 src/ark_server.py --port 8808  # base_url: http://127.0.0.1:8808/api/v3
```

> **鉴权是**一个变量** `AVM_AUTH`，三选一**（详解见 `src/ark_compat/README.md`）：
> 留空（不校验，本机自用）｜ `passthrough`（调用方的 `Authorization: Bearer` 直接携带
> **账号的**网页会话 cookie，本进程不再需要 `AVM_COOKIE`）｜ `key:<密钥>`（闸门）。
> ⚠️ 闸门与透传**互斥**（同一个 Bearer 不可能既是闸门密钥又是上游凭据）—— 收成单变量
> 之后，那个"必然 401"的组合**根本无法表达**；旧的 `AVM_GATE_KEY` /
> `AVM_PASSTHROUGH_COOKIE` 已废弃：**只要它们还有行为，启动就会被拒绝并打印迁移映射**
> （不静默放过 —— 忽略一个有行为的旧变量，要么闸门无声消失、要么透传无声失效）。

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

接口文档：服务起来后 `/docs`（Swagger UI）、`/redoc`（ReDoc）、`/openapi.json`（OpenAPI 3.1
schema）。⚠️ 三者**不受** `AVM_AUTH` 保护（闸门模式下也无需凭据），且页面资源由**浏览器**
从 CDN 取 —— 服务端返回 200 也可能白屏。详见 [`src/ark_compat/README.md`](src/ark_compat/README.md)。

测试：`python3 -m unittest discover -s tests`（750+ 项，零消耗、零外发）。
零外发可复验：`python3 tools/egress_audit.py`（有非回环出站、**或有用例被跳过**，都以非 0 退出
—— 进 CI 的门禁**被跳过 ≠ 通过**，实测有一条编排门禁因此长期没跑过）。
编排接线可复验：`python3 tools/compose_wiring_check.py`（部署前加 `--resolve` 用**解析后的真实值**
再核一遍，见下方「部署」）。
env 同步可复验：`python3 tools/env_sync_check.py`（`.env` ⇄ `.env.example` 的结构 / 取值 /
消费方三件事；**只在有 `.env` 的机器上跑** —— 该文件不入库，故它进不了 CI）。
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
> 也可以单实例透传（`AVM_AUTH=passthrough`，每凭据仍是独立账号上下文、零串扰）。

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
> 🔴 **minter 的 `TZ` 必须在场**（E2E-AVM-006 单变量实验定案）：容器默认 UTC 时 CF 判定
> 「时区/会话不一致」⇒ 下发**交互式挑战** ⇒ **铸造恒失败**（现象：`interactive` 分类、
> 约 12s 快速失败、`minted=0`；冷 profile 与热 profile 都一样，加超时预算也没用）。
> compose 与镜像都已默认 `Asia/Shanghai`（compose 专属变量 `AVM_MINTER_TZ`），**别删那一行**。
> 它属于"配了才可能对、不配也照样起"的项：服务照常 `ready`、`/healthz` 一片正常，
> 失败只体现在"铸造不出 token"这一个远端行为上 —— 本轮为此白跑了三轮实验。
> 部署前用 `python3 tools/compose_wiring_check.py --resolve` 会拦住它丢失或留空。
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
> 顺手还会拦下 `ARK_HOST` 被绑回环（端口映射会失效）、`AVM_MINTER_URL` 丢掉 `:-` 默认值
> （铸造能力被**静默**关掉）、minter 离开 host 网络（NAT 改写 TCP 指纹 ⇒ 铸造整体失效），
> 以及 minter 的 `AVM_MINTER_BIND_HOST` 被收窄到 **回环**而 ark-compat 仍是桥接
> （两者不在同一网络栈 ⇒ **适配层永远取不到 token**；回环绑定只在 ark-compat 也走
> host 网络时才成立，而默认 compose 里它是桥接的）—— 这几类"配了不生效"。
> 后一类尤其像"更安全的写法"：minter 照常铸造、`/healthz` 全绿、池子满着，只有
> `served` 恒为 0，闸门一翻才是 429。要收窄就绑**本项目 compose 网络的网关地址**
> （`docker network inspect aivideomaker_default` 确认），或用宿主防火墙把该端口
> 只放给容器网段。
>
> ✅ **健康检查跟随 `PORT`（不再有假 `unhealthy`）**：0.0.18 及以前，镜像的 `HEALTHCHECK`
> 把地址写死成 `curl http://127.0.0.1:8899/healthz` —— 只要部署把端口挪走（node064 的
> override 用的是 8895），服务好端端的、铸造照常，`docker ps` 却恒 `unhealthy`
> （E2E-AVM-010 定位、E2E-AVM-012 仍未修）。现在它调服务自己的 `--selfcheck`，
> **地址由服务进程从 `PORT` / `BIND_HOST` 推导**（exec 形态、不过 shell），判据与 `/healthz`
> 同一处 ⇒ 挪端口、改绑定地址之后健康状态都仍然可信（状态语义没变：`ready=false`
> 表示 Chrome 还在预热，**仍算健康**）。
> 判活三条命令（host 网络 ⇒ 宿主端口就是容器端口）：
>
> ```bash
> curl -s http://127.0.0.1:8899/healthz | python3 -m json.tool     # 挪过端口就把数字换掉
> docker compose exec minter python3 /app/tools/turnstile_service.py --selfcheck
> docker inspect --format '{{json .State.Health}}' avm-minter      # 健康检查的历史与原因
> ```
>
> ⚠️ **挪端口要同改三处**：minter 的 `PORT`、适配层的 `AVM_MINTER_URL`（宿主侧地址）、
> 宿主防火墙规则 —— 它们是**同一个数字**（compose 的插值不支持嵌套，只能各写一遍）。
> `--resolve` 会比对前两者（指向本机时才比对），只改一边会被当场拦下。
>
> 🔴 **宿主防火墙：bridge 容器 → host 网络的 minter 是「出网到宿主」**（E2E-AVM-008 定案）：
> minter 走 host 网络后"compose 内部访问"并不存在 —— ark-compat（桥接）访问 8899 受宿主
> INPUT 链管辖。node064 的 `iptables -P INPUT DROP` 丢掉这条路 ⇒ 闸门一翻、3 条提交全部
> 429（minter `served` 恒 1 —— 配置全对，包就是过不去）。放行**仅容器私网**：
>
> ```bash
> ufw allow proto tcp from 172.16.0.0/12 to any port 8899          # docker 默认网段都在 172.16/12
> ufw delete allow proto tcp from 172.16.0.0/12 to any port 8899   # 回滚
> ```
>
> **宿主能连 ≠ 容器能连** —— 历轮接线验证都只验到"名字能解析"，没验到"包能到"。
> 部署前加 `--probe` 在**容器内**实测一次：
>
> ```bash
> python3 tools/compose_wiring_check.py --probe   # = --resolve + 与 ark-compat 同网络的容器内真实打 /healthz
> ```
>
> 上面那条手工放行现在有自动化版本 —— **幂等、默认只读、失败自动回滚**：
>
> ```bash
> python3 tools/host_preflight.py               # 只读：打印将要做的事（要求服务已在跑）
> sudo python3 tools/host_preflight.py --apply  # 起服务 + 放行 + 容器内实测；探测不过 ⇒ 回滚本次新增的规则
> ```
>
> 🔴 **放行"谁"是按 docker 桥接接口，不按写死的网段**：`-i br-+`（用户自定义网络 `br-xxxxxxxx`）
> 加 `-i docker0` ⇒ **与子网无关**，Docker 换地址池（`default-address-pools` 配成 10.x /
> 192.168.x）、网络重建拿到新网段，规则都照样命中。ufw 主机不支持接口通配符，改为
> **枚举 docker 真实桥接网段**逐条放行（`docker network ls --filter driver=bridge`）。
> 端口从 `docker inspect` 读，并与 `AVM_MINTER_URL` 的端口**交叉核对**（分叉即拒绝上线）。
> ⚠️ 判定链是 **INPUT** 而不是 `DOCKER-USER`（后者只看转发流量）；任何情况下都不会写成
> `from any`（8899 = 谁能连上谁就能领过闸凭证）。
>
> 🔧 **冷启动那一次铸造**：常规 45s 预算下，冷 profile 的首次铸造曾在新部署上从未成功过
> （现象：`mint` 返回 503 + TIMEOUT，而 `/healthz` 早已报 ready）。三轮复盘的定性演变
> （别再倒回去）：E2E-AVM-004 证伪了旧的「冷启动 ≈46s 慢成功」解释（120s 冷预算下仍
> 精确压线失败 ⇒ 是**卡死**不是**慢**）；E2E-AVM-005 把预算一路加到 600s 仍不出
> （⇒ 不是"差一点"）；**E2E-AVM-006 定案：根因是环境缺 `TZ`**（见上面那条 🔴）——
> 补上后热卷首铸 2.96s、全新冷卷首铸 5.20s，各 4/4 成功。
> 超时预算与 INTERACTIVE 快速失败现在的价值是**归因**：12s 就给出失败分类与页面内状态
> （`/healthz` 的 `last_failure`），而不是让人对着 121s 猜。两条预算可调：
> `AVM_MINTER_RENDER_TIMEOUT_MS`（默认 45000）与 `AVM_MINTER_COLD_RENDER_TIMEOUT_MS`
> （默认 120000）。`warmed`（**真的铸出过** token）与 `ready`（Chrome 就绪）分开报 ——
> 只看 `ready` 会误判。⚠️ 旧结论"**根治要靠热 profile**（播种 /data 卷）"已被实验否掉
> （AVM16-SEED-HOT：整卷播种后 3 连败全 interactive）—— 要的是 `TZ` 在场，不是热卷。

**多 worker 是这里唯一的坑。** 上游闸门是进程内 `threading.Semaphore`，「全局只跑 2 个」依赖
单进程：开 N 个 worker 会让实际并发变成 N×2，第 3 个起上游直接返回 `The queue is full`
（并更易触发验证码闸门）。安全做法是让 worker 数不超过上游额度，两条路都已在
`src/gunicorn_conf.py` 收口：`AVM_MAX_CONCURRENT` 取**数字**时要求
`AVM_WORKERS ≤ AVM_MAX_CONCURRENT`，按 worker 数**向下分摊**并打印实际全局值
（`2 ÷ 2` ⇒ 每 worker 1、全局仍是 2）；取 **0（自动，模板与 compose 的默认）**时
master 下发除数为 worker 数，由 worker 内探测到的账号额度按 ÷N 分摊
（每账号全局仍恰好等于它的 `maxQueueLength`，pro 的 4 不会被缩成 2）。

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

探活端点与公开文档端点（`/healthz`、`/`、`/docs`、`/redoc`、`/openapi.json`）**不进 Logfire
上报**：容器 HEALTHCHECK 的轮询与扫描器否则会把真实请求淹掉 —— span 与日志两样都摘，本地
stderr 日志不受影响。名单可用 `AVM_LOGFIRE_EXCLUDED_PATHS` 覆盖：**只写路径，不写正则**
（普通路径精确匹配，以 `/` 结尾按子树匹配；留空 = 内置默认，写 `-` = 不排除任何路径），
正则由代码机械生成 —— 手写 `"/"` 会命中每一个 URL，等于把全站追踪静默关掉。

**任务查询端点（轮询）同样不进上报**（闸门 1：产生层，2026-09-15）：`GET /tasks/{id}` 会被
调用方按秒级反复查询，一个任务几十上百次请求里绝大多数 span 逐字段相同 —— 那是重复计价，
不是可观测性。现在的口径是**状态跃迁才留痕**：首次观测 / 状态跃迁 / 失败才发
`ark.task.fetch`，其余只累加指标（`avm.task.poll_requests`）；盯梢线程对上游的轮询 GET 也
被压制，全程只留一条 `ark.task.watch` span，跃迁记成它上面的 `status_change` 事件。
实测：同一个任务被查 60 次由 **123 条 span 降到 6 条**。名单可用 `AVM_LOGFIRE_POLL_PATHS`
覆盖，语义与探活表同构。⚠️ 只摘**成功**的轮询日志 —— 4xx/5xx 照报，否则"调用方到底看到了
什么"在 Logfire 里就不存在了。

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
  ⚠️ 其中 `AVM_GATE_KEY` 连同 `AVM_PASSTHROUGH_COOKIE` 现在**已废弃**（鉴权收成单变量
  `AVM_AUTH`）：它们仍被代码读取，但只为了在启动期**拒绝**并在报错里给出迁移映射 ——
  故刻意不登记进模板（见该测试里的 `NOT_IN_TEMPLATE`）。
- 批量或自动化调用一律先 dry-run 预检，并留意上方「验证码闸门」——连续提交会把它翻起来。
- **成片地址默认原样透传上游直链**，而那条直链里同时带着**上游域名**与**上游实际执行的
  模型名**（形如 `static2.img2video.ai/…_0_minimax_h3_….mp4`），它的响应头
  `content-disposition` 里还有一份模型名。要对外交付时配 `AVM_PUBLIC_BASE`，成片地址会换成
  `{AVM_PUBLIC_BASE}/v/{ark_id}.mp4` 并改由本服务流式回源（响应头白名单化）。
  这是**对外破坏性变更**（主机变了），留空则不启用。详见
  [`src/ark_compat/README.md`](src/ark_compat/README.md) 的「成片对外出口」一节。
- `docs/web-reverse/captured/` 中为公开页面的抓取快照，仅用于离线分析。
