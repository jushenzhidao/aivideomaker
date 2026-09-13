# aivideomaker → 火山方舟 Seedance 协议兼容层

把 aivideomaker 的**网页端内部接口**包装成火山方舟的
`POST /api/v3/contents/generations/tasks` 形状。

上游只有一条：**tRPC over `/api`，session cookie 认证**（差异由 `upstreams.py` 吸收）。
技术栈：FastAPI + uvicorn + httpx + loguru + logfire。

> 接口形状对齐火山方舟《创建视频生成任务》
> <https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1520757>
> Seedance 2.5「全能参考」（多素材 + `omni_reference_task_type` / `output_format` /
> `generate_audio`）的处理见下方「请求字段映射」。

## 这条上游的特性

|   | `web` 网页端内部接口 |
|---|---|
| 凭据 | `AVM_COOKIE`（`auth_session`），或开透传由调用方自带 |
| **计费** | `tier=base` 计费；`turbo` 且 ≤8s **免费** |
| 预算保护 | 无（靠免费窗口兜底） |
| **取消任务** | ❌ **没有端点**（跑着的照跑照扣，`DELETE` 只删本地记录） |
| 验证码 | 动态闸门（按速率翻转，**不是账号属性**） |
| 并发 | **上游只跑 2 个**，槽位占到底 |
| 媒体输入 | **必须转存到站点 CDN**（适配层已自动做） |
| 幂等 | 无 |

## 快速开始

```bash
pip install -r requirements.txt

export AVM_COOKIE="auth_session=…" # 浏览器导出，TTL 约 400 天（或多租户改用透传，见下）
export AVM_GATE_KEY=sk-local       # 本机闸门（不设则对任何调用方开放）

python3 src/ark_server.py --port 8808
```

启动时会自述可用性与计费口径：

```
[ark-compat] Ark SDK base_url : http://127.0.0.1:8808/api/v3
[ark-compat] available lines  : web
[ark-compat] billing          : web upstream: tier=base is always billed; tier=turbo is free up to 8s
[ark-compat] upstream host    : https://aivideomaker.ai
[ark-compat] gate             : sk-local
```

用火山官方 SDK（`arkruntime`）：

```python
from arkruntime import Ark

client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="sk-local")
r = client.content_generation.tasks.create(
    model="doubao-seedance-2-5-260628",
    content=[{"type": "text", "text": "明亮多彩的广告片风格，一只红色气球升起"}],
    ratio="16:9", resolution="480p", duration=5,
    extra_body={"aivideomaker_dry_run": True},   # 调试期先 dry-run，零成本
)
```

## 鉴权：进程持凭据，还是调用方自带凭据（透传）

**开关一开，调用方的 `Authorization: Bearer` 就是上游凭据本身**
—— 本进程不再持有凭据，也就没有"服务自己的账号"这回事。

| 线 | 进程持凭据（默认） | 调用方自带（透传） |
|---|---|---|
| `web` | `AVM_COOKIE` | `AVM_PASSTHROUGH_COOKIE=1`，Bearer 放**网页会话 cookie** |

```bash
# 每个调用方带自己的会话，各自享自己账号的免费窗口
export AVM_PASSTHROUGH_COOKIE=1
python3 src/ark_server.py --port 8808

curl -X POST http://127.0.0.1:8808/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <40 位 auth_session 值>' \
  -H 'content-type: application/json' -H 'X-Avm-Dry-Run: 1' -d '…'
```

凭据形态很宽（裸 token / `auth_session=…` / 完整 Cookie 串 / cookie jar JSON 都认），
规范化规则见 `cookie.py`，与 JS 侧由同一份用例锁定。

三条**必须知道**的约束：

1. **透传与闸门 `AVM_GATE_KEY` 互斥。** 同一个 Bearer 不可能既是闸门密钥又是上游凭据，
   同设时启动直接失败 —— 否则每个请求都 401，而现象看起来像"调用方凭据错了"。
2. **透传即多租户。** 任务表多了 `owner` 维度（凭据的 sha256 前 16 位，**不落凭据原文**），
   `GET /tasks`、`GET/DELETE /tasks/{id}` 全按归属过滤，别人的任务一律 404
   （连"存在"都不透露）。切换开关前建的旧记录没有归属，在透传模式下查不到 ——
   保留窗口只有 7 天，很快自然淘汰。
3. **每凭据一个上游客户端 + 一把并发闸门**，缓存上限 64 条，淘汰**只关空闲的**
   （关掉正在被轮询的客户端＝那次查询直接失败，现象是"任务查不到"）。
   上游"同时只跑 2 个"是**按账号**的限制 ⇒ 不同 cookie 各 2 槽是正确的；
   同一 cookie 的槽位仍是进程内状态（多 worker 会翻倍，理由见部署章节）。

`/healthz` 会把这件事说清楚：`credentials_from_caller`、`passthrough_cookie`，
以及 `available_upstreams` —— 透传线的客户端要等凭据才存在，但**能力**照样如实上报
（否则会显示成"没配任何上游"）。透传模式下 `?deep=1` 没带凭据**不会** 401，
而是在 `upstream_probe` 里写明原因。

`AVM_PASSTHROUGH_COOKIE=1` 时，调用方的 `Authorization: Bearer` 就是**网页会话
cookie 本身**（裸 token 或完整 Cookie 串都行），本进程不再需要 `AVM_COOKIE`。
任务表按**凭据指纹**隔离，A 看不到 B 的任务。

⚠️ 它与 `AVM_GATE_KEY` **互斥**：同一个 Bearer 不可能既是闸门密钥又是上游凭据，
两者同设会让每个请求都在闸门处 401 —— 所以 `Settings.validate()` 直接拒绝启动。

## 上游选线（已移除）

早期这里同时支持一条 `official` 上游（aivideomaker 官方 `/api/v1/*`，`key` 头），
可按 `AVM_UPSTREAM` / `X-Avm-Upstream` / `?upstream=` 切换。**该上游与选线机制已整体
移除**，本项目只对接 web 线，且**不保留任何兼容层**：

- `AVM_UPSTREAM` 不再被读取（`Settings` 里没有这个字段，设了也无效）；
- `X-Avm-Upstream` / `?upstream=` 不再被解析 —— 发了也一律走 web 线，不报错。

## 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v3/contents/generations/tasks` | 创建任务 |
| GET | `/api/v3/contents/generations/tasks` | 列表（**仅本凭据创建过的**；非透传时=本进程创建过的） |
| GET | `/api/v3/contents/generations/tasks/{id}` | 查询（实时回上游取） |
| DELETE | `/api/v3/contents/generations/tasks/{id}` | **只删本地记录**并在响应里说明（站点没有取消端点） |
| GET | `/healthz` | 存活 + 上游可用性与计费口径；`?deep=1` 额外查上游 |

## 任务持久化

任务表**默认落 SQLite**（`.ark-tasks.db`，WAL 模式），不是进程内 dict：

```bash
AVM_TASK_STORE=sqlite      # 默认；单文件 + WAL，跨重启可读
AVM_TASK_DB=.ark-tasks.db  # 路径（已 gitignore：.ark-tasks.db*）
AVM_TASK_RETENTION_DAYS=7  # 保留窗口，启动时清理过期记录
AVM_TASK_STORE=memory      # 显式开发/测试开关：重启即丢
```

为什么不省事用 dict：契约要求 `GET /tasks/{id}` 在保留窗口内始终可查。进程内 dict
一重启就让调用方手里正在轮询的 `cgt-*` 凭空 404 —— 实测踩到过（为加载一处修正重启服务，
正在轮询的任务立刻查不到）。这类缺陷**只在重启时现形**，平时完全看不出来。

`memory` 是**显式开关**：写错配置直接报错，**不会**静默退回内存（那会让持久化在没人
注意时悄悄失效）。`/healthz` 的 `task_store` 字段可一眼确认后端与是否 `durable`。

> **为什么不是 Redis**：本服务的瓶颈是**上游出片 2–9 分钟**，而任务表每小时只有几条操作，
> 单次 I/O 从 ~1ms 降到 ~0.2ms 端到端毫无可感知差异。反观代价：多一个要装/起/配/监控的
> 外部服务，且 Redis 默认 RDB 严格说**会丢最后几秒**（要严格得上 AOF `always`，性能优势也就没了）。
> 存储层已接口化（`put` / `get` / `delete` / `list_recent` / `count` / `prune`），
> 将来要多实例共享状态时加一个 `RedisTaskStore` 即可，调用方零改动。

## 请求字段映射

### 能承接的

| Ark 字段 | 处理 |
|---|---|
| `model` | → 站点模型（**任何** Ark 模型名都映射到站点侧的默认入口） |
| `content[].type=text` | 多条用 `\n` 连接 → `content` |
| `content[].type=image_url` | `role=first_frame`/`last_frame` 走帧通道；`reference_image` 或无 role 进 `referenceImageUrls` |
| `content[].type=video_url` | → `referenceVideoUrl`（单槽位） |
| `content[].type=audio_url` | → `referenceAudioUrls` |
| `ratio` | → `aspectRatio`（站点字段名）；`adaptive` 表示跟随源图，不设值 |
| `resolution` | 480p/720p/1080p 原样透传 |
| `duration` | 站点约束是「**连续秒数 + 上限**」，因此**原样透传**（越界才就近钳制）；`-1` 目前在翻译层落 5s |
| `frames` | 按 24fps 换算成秒 |
| `omni_reference_task_type` | `reference`/`auto` 放行；`edit`/`extend` 上游无此能力 → **真实提交 400**（dry-run 仍可校验） |
| `output_format` | `mp4` 放行；`mov` 上游只出 mp4，带显式告警 |
| `generate_audio` | 站点**没有这个开关** —— 记录并回显，同时说明音画由上游决定 |

### 参考素材：按上游上限截断，且截断必留痕

上游站点 UI 文案（中英双份一致，取自 `docs/web-reverse/captured/docs-{zh,en}.html`）
标称的上限，适配层**照此截断**：

| 类型 | 上限 |
|---|---|
| 参考图片 `reference_image` | **4** 张 |
| 参考视频 `reference_video` | **1** 个 |
| 参考音频 `reference_audio` | **2** 条 |

⚠️ **截断不会静默发生**。每一次截断都会写进 `warnings[]`：

1. 点名**被丢弃的每一项 URL**与数量（`reference_video: 6 supplied, upstream accepts
   at most 1 — 5 dropped: https://…`）；
2. 指出提示词里因此**悬空的占位符**（`prompt references @视频2, @视频3… but only 1
   reference_video asset(s) are forwarded — those placeholders cannot resolve`）。

第 2 条针对的是本项目最难归因的一类缺陷：提示词逐条写着 `参考@视频1…@视频6`，
素材却被截断到 1 段 —— 模型会去参考**不存在的素材**，且从产出上几乎看不出原因。

另有一条上游硬约束：**参考素材与首尾帧互斥**（`Cannot mix reference assets with
first/last frame`），混用时适配层直接 400，不再让上游拒绝。

> Ark SDK 会把"未建模字段"放进 `extra_body`，真正发出时合并到顶层 ——
> 所以 `extra_body` 里的这些键**同样**会被识别。

### 承接不了、会列进 `unsupported[]` 的 11 个（不静默丢弃）

`watermark` `seed` `camera_fixed` `return_last_frame` `draft` `service_tier`
`priority` `callback_url` `safety_identifier` `tools` `execution_expires_after`

## ⚠️ 计费：只有一条口径

| 规则 |
|---|
| `tier=base` **一律计费**；`tier=turbo` 且 `duration ≤ 8s` **免费** |

判据只有一个：站点任务记录里的 **`paid`** 字段（`credits` 与它**反相** ——
免费任务也记 `credits`，不要拿它判断）。

响应里的 `effective.billed` 会预告是否计费，并附一句 `billing_note`。
`extra_body.aivideomaker_prefer_free=true` 是省钱开关：把超过 8s 的**合法**时长
主动拉回 8s（不只是越界时才生效）。

> 多素材请求要留意累加效应：参考素材本身可能带时长，
> `duration:15` 这类请求**必然越过免费窗口**。

## 零成本校验（dry-run）

调试请求体**一律先 dry-run**。`X-Avm-Dry-Run: 1` 或 `extra_body.aivideomaker_dry_run: true`：

```bash
curl -X POST http://127.0.0.1:8808/api/v3/contents/generations/tasks \
  -H 'content-type: application/json' \
  -d '{"model":"doubao-seedance-2-5-260628",
       "content":[{"type":"text","text":"a red balloon"}],
       "resolution":"480p","duration":5,
       "extra_body":{"aivideomaker_dry_run":true}}'
```

返回里能看到真正会发出去的 payload（`web_params`）、本轮的
`warnings` / `unsupported` / `incompatible`，并带上计费判定：

```json
{
  "dry_run": true, "upstream": "web",
  "effective": {"resolution": "480p", "duration": 5, "output_format": "mp4",
                "billed": false, "tier": "turbo",
                "billing_note": "web upstream: tier=base is always billed; tier=turbo is free up to 8s"}
}
```

`incompatible` 里的条目**只在真实提交时**变成 400 —— 这样"这个请求上游做不了"
这件事可以零成本查出来。

**dry-run 不产生任何副作用** —— 包括不做媒体转存（已测）。

## 四个必知点

1. **没有取消端点。** `DELETE` 只删记录，响应会明确给出
   `{"cancelled": false, "reason": "…no cancel endpoint…"}`，绝不谎报已取消。
2. **动态验证码闸门。** `needsCaptcha` 是风控开关不是账号属性；闸门开着时站点会把
   `token: null` **静默拒掉（返回空串）**，适配层把它转成显式
   `429 RateLimitExceeded`。**这不是会话失效**。绕过方式：自带真 token
   （`extra_body.aivideomaker_captcha_token`），或等它衰减。
3. **并发上限 2，且槽位占到底**（从创建一直到任务进入终态）。适配层有信号量闸门
   （`AVM_MAX_CONCURRENT`），超出会**排队等待**而不是直接失败。
4. **媒体必须转存到站点 CDN。** 适配层自动做：外链图片 → 先下载再走预签名上传；
   data URI → 解码成字节上传；已在 `static*.img2video.ai` 上的原样放过。
   站点只收自己 CDN 的地址，外链会因 Content-Type 白名单被拒。

## 可观测性

- **loguru** 是唯一日志出口，每条带 `request_id`（同时回显在响应头 `x-request-id`）。
- 经 `logfire.loguru_handler()` 桥接进 Logfire；`instrument_fastapi` 让每个请求成一条
  span，`instrument_httpx` 让每次上游调用成子 span。
- 具名 span（属性即契约，用内存 exporter 断言：`tests/test_trace_contract.py`）：
  - `ark.create.submit` —— `ark_id` / `upstream` / `ark_model` /
    `resolution` / `duration` / `billed` / `warning_count` / `warnings` /
    **`upstream_task_id`** / `request`（调用方发来的 Ark 请求原文）/ **`upstream_calls`**
  - `ark.create.dry_run` —— 同上（除 taskId）：零成本校验路径也要能在 trace 里复盘
  - `ark.task.fetch` —— `ark_id` / `upstream` / **`upstream_task_id`** / **`status`** /
    **`paid`**（出片后对账的两项关键；`paid` 才是"这次花没花钱"的判据，不是 `credits`）/
    `upstream_response`（归一化后的任务对象）/ `upstream_calls`
  - `ark.task.cancel` —— `ark_id` / `upstream_task_id` / `cancelled` / `upstream_response` /
    `upstream_calls`（站点**没有**取消端点，`cancelled=false` 是实话）
  - 失败路径额外带 `error`（含错误码，如 `WebApiError[NOT_FOUND] http=404`）+
    `upstream_calls` —— "上游拒绝了"与"我们没发出去"必须能区分
- **`upstream_calls` 是什么**：本次调用期间每一条上游 HTTP 往返的
  `{call, request, response, status, error, duration_ms, task_id}`（web 线是 tRPC 的
  `procedure` + 原始信封）。采集点在 `WebClient.trpc|upload_file`，靠 contextvar 与
  app 层的 span 对接（`asyncio.to_thread` 会复制上下文）；**没有活跃采集时立即返回，零开销**。
- **不做脱敏，但控体积**：`scrubbing=False` 是默认（`AVM_LOGFIRE_SCRUBBING=1` 可打开），
  请求/响应原文直出；data URI 只留 `<data-uri image/png · 1234 chars>` 摘要、
  超长串按 `AVM_LOGFIRE_MAX_CHARS`（默认 20000）截断 —— 这是体积控制，不是脱敏。
- 🔴 **关了脱敏，"凭证不入 trace"就只剩一条防线**，所以它必须是硬的：
  `capture_headers=False`（`instrument_fastapi` / `instrument_httpx` 都显式传）+
  `observability.safe_headers()` 过滤请求头（`key` / `cookie` / `authorization` / …）。
  测试用真 app 跑一遍全量 span，断言 cookie 与 Bearer 一次都不出现。
  `AVM_LOGFIRE_CAPTURE_HEADERS=1` 可显式打开抓头 —— 那时凭证会明文进 trace。
- **默认不外发**：`send_to_logfire="if-token-present"`，没 `LOGFIRE_TOKEN` 就只在本地收集。
- **函数参数不自动记录**：`inspect_arguments=False`（参数里可能有 token）。
- 装配失败只降级、不致命（`/healthz` 的 `logfire` 字段会显示 `false`）。

### 号池指标（余额 / 闸门 / 可达性）

透传之后，"这个进程手里有哪些账号、各剩多少积分、闸门开没开"必须看得见。做法是
**周期性的只读采样 → Logfire 指标**（`AVM_ACCOUNT_REPORT_SECONDS`，默认 300s，`0`=关）：

| 指标 | 含义 |
|---|---|
| `avm.account.credits` | 剩余积分。**两线是同一个积分池**，所以 official 与 web 采到的是同一个数 |
| `avm.account.captcha_required` | 当前是否需要 Turnstile（`1`=需要）。它**按速率动态翻转**，只有时间序列才看得出"开多久会衰减" |
| `avm.account.reachable` | 凭据是否可用（`0`=会话过期 / API Key 失效） |

- 标签只有：`upstream` / `account`（**凭据 sha256 前 16 位**）/ `source`
  （`process`=本进程持凭据，`passthrough`=调用方自带）/ `pid`。
  🔴 **凭据原文绝不进遥测** —— `tests/test_pool_metrics.py` 专门守这条，并做了变异测试。
- **只读**：`credits.getCredits` / `GET /api/v1/account` / `model.needsCaptcha` ——
  不创建任务、不计费（实测：连续采样期间余额不变）。
- **采集与出口解耦**：Logfire 未装配或出口不通时**照样采样**，只是指标不可用
  （启动日志会写明"仅本地日志"，`/healthz.accounts_tracked` 仍给号池规模）。
  这里踩过坑：曾把两者绑在一起，结果恰恰是"出口坏掉的那一刻"什么都看不到。
  另实测：服务进程内经沙箱代理导出 logfire 偶尔撞 10s 读超时（`logfire-us.pydantic.dev`
  单发 curl 是 200 / 4.4s ⇒ 是"慢"不是"不通"），给足时间不打断即可正常导出。
- **采样失败不影响服务**：某个账号不可达 ⇒ `reachable=0`，且**不报一个假的余额 0**；
  一轮失败不会让上报停摆（`test_report_survives_repeated_cycles`）。
- ⚠️ 多 worker 会各报一份（指标带 `pid`，看板过滤一个即可）。
- `/healthz` **只给数量**（`accounts_tracked`），不列明细 —— 它不鉴权，而透传模式下
  明细就是别人的账号。

## 未验证项（诚实清单）

| 项 | 状态 |
|---|---|
| 上游「参考视频最多 1 个」这一上限 | 依据是站点 UI 文案（中英双份）+ 后端单数字段名 `referenceVideoUrl`，**未做 API 层实测证伪** |
| 站点各分辨率的**时长上限** | 站点约束是「连续秒数 + 不得超过 N 秒」（UI 文案 `videoDurationMaxWarnTip`），当前统一按 `[5, 20]` 处理；**上限未逐分辨率实测** |
| `480p` / `1080p` 接受连续秒数 | 由站点 UI 文案 + `720p` 实测推断（720p 的 5/6/7/8/9/11/14s 实测通过）；**480p/1080p 未单独实测** |
| `web` 线的 SSE 帧结构 | 已实现（`model-status`），但生产路径默认走 `model.getModel`，SSE 只作回落 |

## 测试

```bash
python3 -m unittest discover -s tests      # 220 项，零消耗、零外发
```

三条纪律：

1. **零额度消耗** —— 所有提交路径走 dry-run；唯一测的"真实提交"是"上游不具备的能力被拒"，
   它在发出上游请求**之前**返回。
2. **零外发** —— 上游 `base_url` 指向没人监听的本地端口，且客户端 `trust_env=False`
   （httpx 默认会把回环地址也交给系统代理，那会让"死端口"验证失效）；web 线的测试
   全部用 `httpx.MockTransport` 做站点替身；Logfire 用 `send_to_logfire=False`。
   （已用 `socket.socket.connect` 审计全量运行：非回环 TCP 出站为 **0**。）
3. **门禁要能被证伪** —— 关键门禁（截断、截断留痕、悬空占位符点名、`incompatible`
   拦截、枚举校验、trace 属性契约与凭证红线）都做过**变异证明**：逐条回退后测试确实变红。

## 文件

```
src/
├── ark_server.py            启动入口（uvicorn）
└── ark_compat/
    ├── __init__.py          包说明、版本、服务名（改名只改这里）
    ├── translate.py         纯函数翻译层（Ark ↔ 站点接口）+ 计费口径渲染
    ├── upstreams.py         上游统一接口 + 媒体转存 + 上游工厂
    ├── web_client.py        网页端客户端（tRPC / 上传 / SSE）
    ├── web_queue.py         并发闸门（信号量 + 终态释放）
    ├── sniff.py             magic bytes 媒体嗅探 + 图片尺寸
    ├── store.py             任务持久化（SQLite 默认 / 显式内存开关）
    ├── settings.py          环境变量配置
    ├── observability.py     loguru + logfire 装配（可失败降级）
    ├── app.py               FastAPI 路由与 Ark 错误信封
    └── errors.py            ParamError / WebApiError / CaptchaRequiredError
tests/
├── test_ark_compat.py       翻译层（时长吸附、计费口径、unsupported）、HTTP 层
├── test_web_upstream.py     web 上游（全部离线，MockTransport）
├── test_cookie_normalize.py Cookie 头规范化（含 JS↔Python 一致性）
├── test_seedance25_omni.py  Seedance 2.5 全能参考（截断、专属字段、前置拒绝）
├── test_trace_contract.py   trace 属性契约（内存 exporter 捞 span 断言 + 凭证红线）
├── test_passthrough_cookie.py 透传（多租户隔离、缓存淘汰、闸门互斥）
├── test_task_store.py       任务持久化（跨实例可读 = 重启不丢）
├── test_env_template.py     .env 模板门禁（声明了却不被代码读取 → 判失败）
└── test_ua_consistency.py   UA / 服务名一致性
```

## 错误码映射

| 上游 | 原始 | Ark 返回 | HTTP |
|---|---|---|---|
| web | `CAPTCHA_REQUIRED` | `RateLimitExceeded` | 429 |
| web | `QUEUE_TIMEOUT`（等不到空槽） | `TaskQueueFull` | 429 |
| web | `NOT_FOUND` | `TaskNotFound` | 404 |
| 本地 | 上游未配置 | `UpstreamUnavailable` | 503 |
| 本地 | 参数不合法 / 上游不具备该能力 | `InvalidParameter` | 400 |
| 任一 | 网络 / 上游 5xx | `NETWORK_ERROR` / `UpstreamError` | 502 |
