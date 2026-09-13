# aivideomaker → 火山方舟 Seedance 协议兼容层

把 aivideomaker 包装成火山方舟的 `POST /api/v3/contents/generations/tasks` 形状。
**两条上游并存**（不是二选一）：

- **`official`** —— 官方 API（`key:` 头，`/api/v1/*`）
- **`web`** —— 网页端内部接口（tRPC over `/api`，session cookie）

对外协议完全一致，差异被 `upstreams.py` 吸收。技术栈：FastAPI + uvicorn + httpx +
loguru + logfire。

> 接口形状对齐火山方舟《创建视频生成任务》
> <https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1520757>
> Seedance 2.5「全能参考」（多素材 + `omni_reference_task_type` / `output_format` /
> `generate_audio`）的处理见下方「请求字段映射」。

## 为什么两条都留

|   | `official` 官方 API | `web` 网页端内部接口 |
|---|---|---|
| 凭据 | `AVM_KEY` | `AVM_COOKIE`（`auth_session`） |
| **计费** | **一律计费**，无免费窗口 | `tier=base` 计费；`turbo` 且 ≤8s **免费** |
| 预算保护 | `X-Max-Credits`，超限**在计费前**拒绝 | 无（靠免费窗口兜底） |
| **取消任务** | ✅ 真取消，**全额退积分** | ❌ **没有端点**（跑着的照跑照扣） |
| 验证码 | 无 | 动态闸门（约 7 条后翻转） |
| 并发 | 官方限流（429） | **上游只跑 2 个**，槽位占到底 |
| 媒体输入 | 外链 / data URI 直接可用 | **必须转存到站点 CDN**（适配层已自动做） |
| 幂等 | `Idempotency-Key` | 无 |

一句话选线：**要省钱走 `web`，要能取消 / 要幂等走 `official`。**

## 快速开始

```bash
pip install -r requirements.txt

# 至少配一条线；两条都配就两条都能用
export AVM_KEY="ak_xxx"            # official 线
export AVM_COOKIE="auth_session=…" # web 线（浏览器导出，TTL 约 400 天）

export AVM_UPSTREAM=official       # 默认走哪条
export AVM_OFFICIAL_MAX_CREDITS=60 # official 线的全局支出上限（强烈建议）
export AVM_GATE_KEY=sk-local       # 本机闸门（不设则对任何调用方开放）

python3 src/ark_server.py --port 8808
```

启动时会自述两条线的可用性与计费口径：

```
[ark-compat] available lines  : official, web
[ark-compat] default line     : official
[ark-compat]   switch per request:  X-Avm-Upstream: web|official   (或 ?upstream=)
[ark-compat]   official — official upstream: every submit is billed — X-Max-Credits is the only guard
[ark-compat]   web      — web upstream: tier=base is always billed; tier=turbo is free up to 8s
```

用火山官方 SDK（`arkruntime`）：

```python
from arkruntime import Ark

client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="sk-local")
r = client.content_generation.tasks.create(
    model="doubao-seedance-2-5-260628",
    content=[{"type": "text", "text": "明亮多彩的广告片风格，一只红色气球升起"}],
    ratio="16:9", resolution="480p", duration=5,
    extra_body={
        "aivideomaker_max_credits": 60,   # official 线必须；web 线忽略
        "aivideomaker_dry_run": True,     # 调试期先 dry-run，零成本
    },
)
```

## 按请求切换上游

```bash
# 头部（推荐）
curl -X POST http://127.0.0.1:8808/api/v3/contents/generations/tasks \
  -H 'X-Avm-Upstream: web' ...

# 或查询参数
curl -X POST 'http://127.0.0.1:8808/api/v3/contents/generations/tasks?upstream=official' ...
```

切到一条**没配凭据**的线会明确告诉你缺什么（`503 UpstreamUnavailable`），
不会静默回退到另一条 —— 那会悄悄改变计费语义。

## 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v3/contents/generations/tasks` | 创建任务 |
| GET | `/api/v3/contents/generations/tasks` | 列表（**仅本进程创建过的**） |
| GET | `/api/v3/contents/generations/tasks/{id}` | 查询（实时回上游取） |
| DELETE | `/api/v3/contents/generations/tasks/{id}` | `official` 真取消并退款；`web` 只删记录并在响应里说明 |
| GET | `/healthz` | 存活 + 两条线的可用性与计费口径；`?deep=1` 额外查上游 |

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
| `model` | → 官方模型（`official`）／站点模型（`web`） |
| `content[].type=text` | 多条用 `\n` 连接 → `prompt`（官方）／`content`（站点） |
| `content[].type=image_url` | `role=first_frame`/`last_frame` 走帧通道；`reference_image` 或无 role 进 `referenceImageUrls` |
| `content[].type=video_url` | → `referenceVideoUrl`（单槽位） |
| `content[].type=audio_url` | → `referenceAudioUrls` |
| `ratio` | → `ratio` / `aspectRatio`（按模型）；`adaptive` 表示跟随源图，不设值 |
| `resolution` | 480p/720p/1080p → 各上游要求的类型 |
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

### 官方线的模型与类型差异

`doubao-seedance-*` → `seedance20`；`hailuo|minimax` → `minimax`；`wan*` → `wan27`。
可用 `extra_body.aivideomaker_official_model` 或 `AVM_OFFICIAL_MODEL` 覆盖。

三个官方模型对同一字段的类型要求不一致（`INVALID_PAYLOAD` 的首要原因）：

| 官方模型 | `duration` | `resolution` | 比例字段 |
|---|---|---|---|
| `seedance20` | number | `480` / `720`（**number**） | `ratio` |
| `minimax` | integer | `"720p"` / `"1080p"` | `aspectRatio` |
| `t2v` / `i2v` | **string** | — | `aspectRatio` |
| `wan27` | string | `"720P"` / `"1080P"` | `ratio` |
| `happyhorse` | string | `"720P"` / `"1080P"` | （无） |

## ⚠️ 计费：两条线口径不同，不要混

| 上游 | 规则 |
|---|---|
| `official` | **提交即计费**。拿不到显式支出上限就 **400 拒绝提交**，且在发出上游请求之前 |
| `web` | `tier=base` 一律计费；`tier=turbo` 且 ≤8s **免费** |

`official` 的上限来源（请求级优先）：`extra_body.aivideomaker_max_credits` →
`AVM_OFFICIAL_MAX_CREDITS`，作为 `X-Max-Credits` 传给上游，超限在计费前被拒。
`web` 线不需要上限。

响应里的 `effective.billed` 会按**当次实际使用的上游**预告是否计费，
并附一句 `billing_note` 说明该线的规则。把 `web` 的"免费窗口"提示端给 `official`
的调用方，是本项目最贵的一类 bug —— 所以两线的提示是分开渲染的。

> 多素材请求要留意累加效应：参考素材本身可能带时长（官方 Seedance 2.5 的口径是
> "输入视频总时长 + 输出时长"计费），`duration:15` 这类请求**必然越过免费窗口**。

## 零成本校验（dry-run）

调试请求体**一律先 dry-run**。`X-Avm-Dry-Run: 1` 或 `extra_body.aivideomaker_dry_run: true`：

```bash
curl -X POST http://127.0.0.1:8808/api/v3/contents/generations/tasks \
  -H 'content-type: application/json' -H 'X-Avm-Upstream: web' \
  -d '{"model":"doubao-seedance-2-5-260628",
       "content":[{"type":"text","text":"a red balloon"}],
       "resolution":"480p","duration":5,
       "extra_body":{"aivideomaker_dry_run":true}}'
```

返回里能看到真正会发出去的 payload（`official_payload` 与 `web_params` 都在）、
本轮的 `warnings` / `unsupported` / `incompatible`，并带上该线的计费判定：

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

## `web` 线的四个必知点

1. **没有取消端点。** `DELETE` 只删记录，响应会明确给出
   `{"cancelled": false, "reason": "…no cancel endpoint…"}`，绝不谎报已取消。
2. **动态验证码闸门。** `needsCaptcha` 是风控开关不是账号属性；闸门开着时站点会把
   `token: null` **静默拒掉（返回空串）**，适配层把它转成显式
   `429 RateLimitExceeded`。绕过方式：自带真 token
   （`extra_body.aivideomaker_captcha_token`）、等衰减、或切到 `official`。
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
  - `ark.create.submit` —— `ark_id` / `upstream` / `ark_model` / `official_model` /
    `resolution` / `duration` / `billed` / `max_credits` / `warning_count` / `warnings` /
    **`upstream_task_id`** / `request`（调用方发来的 Ark 请求原文）/ **`upstream_calls`**
  - `ark.create.dry_run` —— 同上（除 taskId）：零成本校验路径也要能在 trace 里复盘
  - `ark.task.fetch` —— `ark_id` / `upstream` / **`upstream_task_id`** / **`status`** /
    **`paid`**（出片后对账的两项关键；`paid` 才是"这次花没花钱"的判据，不是 `credits`）/
    `upstream_response`（归一化后的任务对象）/ `upstream_calls`
  - `ark.task.cancel` —— `ark_id` / `upstream_task_id` / `cancelled` / `upstream_response` /
    `upstream_calls`（web 线**没有**取消端点，`cancelled=false` 是实话）
  - 失败路径额外带 `error`（含错误码，如 `WebApiError[NOT_FOUND] http=404`）+
    `upstream_calls` —— "上游拒绝了"与"我们没发出去"必须能区分
- **`upstream_calls` 是什么**：本次调用期间每一条上游 HTTP 往返的
  `{call, request, response, status, error, duration_ms, task_id}`（web 线是 tRPC 的
  `procedure` + 原始信封；官方线是 `METHOD path` + body）。采集点是
  `OfficialClient._req` / `WebClient.trpc|upload_file`，靠 contextvar 与 app 层的 span
  对接（`asyncio.to_thread` 会复制上下文）；**没有活跃采集时立即返回，零开销**。
- **不做脱敏，但控体积**：`scrubbing=False` 是默认（`AVM_LOGFIRE_SCRUBBING=1` 可打开），
  请求/响应原文直出；data URI 只留 `<data-uri image/png · 1234 chars>` 摘要、
  超长串按 `AVM_LOGFIRE_MAX_CHARS`（默认 20000）截断 —— 这是体积控制，不是脱敏。
- 🔴 **关了脱敏，"凭证不入 trace"就只剩一条防线**，所以它必须是硬的：
  `capture_headers=False`（`instrument_fastapi` / `instrument_httpx` 都显式传）+
  `observability.safe_headers()` 过滤请求头（`key` / `cookie` / `authorization` / …）。
  测试用真 app 跑一遍全量 span，断言 key / cookie / Bearer 一次都不出现。
  `AVM_LOGFIRE_CAPTURE_HEADERS=1` 可显式打开抓头 —— 那时凭证会明文进 trace。
- **默认不外发**：`send_to_logfire="if-token-present"`，没 `LOGFIRE_TOKEN` 就只在本地收集。
- **函数参数不自动记录**：`inspect_arguments=False`（参数里可能有 key/token）。
- 装配失败只降级、不致命（`/healthz` 的 `logfire` 字段会显示 `false`）。

## 未验证项（诚实清单）

| 项 | 状态 |
|---|---|
| `seedance20` 的图像字段名（`image` / `lastFrameImage` / `referenceImages`） | **未实证**。官方 OpenAPI 只详述了 `minimax` 的 schema；当前按 `i2v` 命名转发并带 `unverified` warning |
| `seedance20` 的参考视频字段（`referenceVideoUrl`） | **未实证**。此前**完全没有转发**（6 段参考视频全丢），现已按单槽位字段转发并带 warning |
| 上游「参考视频最多 1 个」这一上限 | 依据是站点 UI 文案（中英双份）+ 后端单数字段名 `referenceVideoUrl`，**未做 API 层实测证伪** |
| 站点各分辨率的**时长上限** | 站点约束是「连续秒数 + 不得超过 N 秒」（UI 文案 `videoDurationMaxWarnTip`），当前统一按 `[5, 20]` 处理；**上限未逐分辨率实测** |
| `480p` / `1080p` 接受连续秒数 | 由站点 UI 文案 + `720p` 实测推断（720p 的 5/6/7/8/9/11/14s 实测通过）；**480p/1080p 未单独实测** |
| `web` 线的 SSE 帧结构 | 已实现（`model-status`），但生产路径默认走 `model.getModel`，SSE 只作回落 |

零成本探测模型必填字段：`python3 src/avm.py probe seedance20`（发空 body，不创建任务）。

## 测试

```bash
python3 -m unittest discover -s tests      # 241 项，零消耗、零外发
```

三条纪律：

1. **零额度消耗** —— 所有提交路径走 dry-run；唯一测的"真实提交"是"缺支出上限被拒绝"
   与"上游不具备的能力被拒"，两者都在发出上游请求**之前**返回。
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
    ├── translate.py         纯函数翻译层（Ark ↔ 两条上游）+ 计费口径渲染
    ├── upstreams.py         两条上游的统一接口 + 媒体转存 + 上游工厂
    ├── client.py            官方 API 客户端（httpx）
    ├── web_client.py        网页端客户端（tRPC / 上传 / SSE）
    ├── web_queue.py         web 线并发闸门（信号量 + 终态释放）
    ├── sniff.py             magic bytes 媒体嗅探 + 图片尺寸
    ├── store.py             任务持久化（SQLite 默认 / 显式内存开关）
    ├── settings.py          环境变量配置
    ├── observability.py     loguru + logfire 装配（可失败降级）
    ├── app.py               FastAPI 路由与 Ark 错误信封
    └── errors.py            ParamError / OfficialApiError / WebApiError
tests/
├── test_ark_compat.py       翻译层、official 上游、HTTP 层
├── test_web_upstream.py     web 上游（全部离线，MockTransport）
├── test_cookie_normalize.py Cookie 头规范化（含 JS↔Python 一致性）
├── test_seedance25_omni.py  Seedance 2.5 全能参考（截断、专属字段、前置拒绝）
├── test_trace_contract.py   trace 属性契约（内存 exporter 捞 span 断言 + 凭证红线）
└── test_task_store.py       任务持久化（跨实例可读 = 重启不丢）
```

## 错误码映射

| 上游 | 原始 | Ark 返回 | HTTP |
|---|---|---|---|
| official | `AUTH_FAILED` | `AuthenticationError` | 401 |
| official | `INSUFFICIENT_CREDITS` | `InsufficientCredits` | 402 |
| official | `BUDGET_EXCEEDED` | `BudgetExceeded` | 422 |
| official | `IDEMPOTENCY_CONFLICT` | `IdempotencyConflict` | 409 |
| official | `INVALID_PAYLOAD` / `INVALID_MODEL` | `InvalidParameter` | 400 |
| official | `RATE_LIMITED` | `RateLimitExceeded` | 429 |
| official | 本地：无支出上限 | `BudgetGuardRequired` | 400 |
| web | `CAPTCHA_REQUIRED` | `RateLimitExceeded` | 429 |
| web | `QUEUE_TIMEOUT`（等不到空槽） | `TaskQueueFull` | 429 |
| web | `NOT_FOUND` | `TaskNotFound` | 404 |
| 本地 | 上游未配置 | `UpstreamUnavailable` | 503 |
| 本地 | 参数不合法 / 上游不具备该能力 | `InvalidParameter` | 400 |
| 任一 | 网络 / 上游 5xx | `NETWORK_ERROR` / `UpstreamError` | 502 |
