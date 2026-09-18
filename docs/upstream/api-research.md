# AIVideoMaker API 上游契约调研

调研日期：2026-09-10
调研人：齐活林（Qi）· 交付总监（前置技术准备）
目标：把 aivideomaker.ai 的视频生成能力反向代理为 new-api 兼容的上游

---

## 0. 关键结论

| 维度 | 结论 |
|---|---|
| 上游形态 | **官方提供 REST API**（非纯黑盒），不需要抓包逆向 |
| 鉴权方式 | 单一 `key` header（API Key），无 OAuth/会话概念 |
| 接口基数 | 6 个 REST 端点（见 §1） |
| 支持模型 | `minimax`(H3) / `t2v` / `i2v` / `t2v_v3` / `i2v_v3` / `seedance20` / `wan27` / `happyhorse` / `lv`（OpenAPI JSON 只详细定义了 `minimax`，其他模型要看官方文档） |
| 状态机 | `SUBMITTED → PROGRESS → COMPLETED / FAILED / CANCEL`（5 态，少见的有取消态） |
| 幂等机制 | `Idempotency-Key` header，min 8 / max 128；同 key 不同 payload → 409 |
| 消费上限 | `X-Max-Credits` header；超出报价则预扣前拒绝 |
| 计费单位 | 积分（credits）；1 credit = $0.01；按 `credits_per_sec × duration` 计算 |
| 限速 | 查询端点 60 req/min/IP；可能 429 + `Retry-After` |
| 退款 | 终端 FAILED 自动退；视频保留 24 小时 |
| SLA 含义 | "完成文件保留 24 小时" — 拿到 URL 后必须立刻落到自己的存储 |

## 1. 端点总览

| # | Method | 端点 | 用途 | 计费 |
|---|---|---|---|---|
| 1 | `GET` | `/api/v1/account` | Doctor，验证 key + 余额 + 限额 | 否 |
| 2 | `POST` | `/api/v1/quote/{model}` | 报价（验证字段 + 算积分） | 否 |
| 3 | `POST` | `/api/v1/generate/{model}` | 创建任务（最常用） | **是** |
| 4 | `GET` | `/api/v1/tasks` | 列出当前 key 的所有任务 | 否 |
| 5 | `GET` | `/api/v1/tasks/{taskId}` | 任务详情（含 output） | 否 |
| 6 | `GET` | `/api/v1/tasks/{taskId}/status` | 任务状态（轻量） | 否 |
| 7 | `PUT` | `/api/v1/tasks/{taskId}/cancel` | 取消（仅 SUBMITTED 状态有效） | 退 |

OpenAPI JSON 已落盘到 `docs/upstream/aivideo-openapi.json`（11.4KB, 313 行, openapi 3.1.0）。

## 2. 核心请求 schema：`MiniMaxH3Request`

```json
{
  "content": "A cinematic candle on a wooden table",       // required
  "duration": 5,                                           // 5-20, default 5
  "resolution": "720p",                                    // 720p|1080p, default 720p (480p 不支持)
  "tier": "turbo",                                         // turbo|base, default turbo
  "aspectRatio": "16:9",                                   // auto|21:9|16:9|4:3|1:1|3:4|9:16, default 16:9
  "imageUrl": "https://...",                               // 首帧图 URL 或 data:image/...;base64,...
  "lastFrameUrl": "https://...",                           // 尾帧图
  "referenceImageUrls": [...],                             // 最多 4 个，参考图
  "referenceVideoUrl": "https://...",                      // 1 个，参考视频
  "referenceAudioUrls": [...]                              // 最多 2 个，参考音频
}
```

约束：
- **首尾帧（imageUrl/lastFrameUrl）与参考素材（referenceImageUrls/referenceVideoUrl/referenceAudioUrls）互斥**——一次请求只能选一种输入模式。
- 图片 field 接受公网 HTTP(S) URL 或 `data:image/...;base64,...`。
- 视频/音频 field 只接受公网 HTTP(S) URL，不接受 data URI。

## 3. 计费表（USD，每秒积分 × 时长）

| 配置 | 积分/秒 | USD/秒 |
|---|---|---|
| 720p Turbo | 3 | $0.03 |
| 720p Base | 4 | $0.04 |
| 1080p Turbo | 4 | $0.04 |
| 1080p Base | 5 | $0.05 |

**默认配置**（720p + turbo + 5s） = 5 × 3 = **15 credits = $0.15**。

> 报价接口（`/api/v1/quote/{model}`）是权威价格，**不要在客户端重算积分**。

## 4. 状态机

```
       ┌──────────────────┐
       │                  ▼
   ┌─► SUBMITTED ───► PROGRESS ───► COMPLETED
   │     │
   │     └────────────────────────► FAILED（自动退积分）
   │     │
   └─────(PUT /cancel)─────────────► CANCEL（仅 SUBMITTED 状态可取消，状态进入 PROGRESS 即不可取消）
```

| 状态 | 含义 | 代理应做什么 |
|---|---|---|
| SUBMITTED | 已接受，等待开始 | 继续轮询；可调用 cancel |
| PROGRESS | 生成中 | 继续轮询；不能 cancel |
| COMPLETED | 输出就绪 | **立刻下载**（24 小时保留），别再轮询 |
| FAILED | 终止失败 | 反射错误给上游；不重试 |
| CANCEL | 用户取消 | 反射给上游；不重试 |

## 5. 响应 schema

### `Quote`（POST `/api/v1/quote/{model}` → 200）
```json
{
  "model": "minimax",
  "credits": 15,
  "listPriceUsd": 0.15,
  "usdPerCredit": 0.01,
  "currentBalance": 50,
  "affordable": true,
  "billable": false
}
```

### `SubmittedTask`（POST `/api/v1/generate/{model}` → 200）
```json
{
  "status": "SUBMITTED",
  "taskId": "tv_abc123",
  "creditsCharged": 15,
  "idempotentReplay": false,
  "responseUrl": "https://aivideomaker.ai/api/v1/tasks/tv_abc123",
  "statusUrl":   "https://aivideomaker.ai/api/v1/tasks/tv_abc123/status",
  "cancelUrl":   "https://aivideomaker.ai/api/v1/tasks/tv_abc123/cancel"
}
```
> `idempotentReplay = true` 表示这次是幂等重放（没再扣费），代理应识别这个标志，禁止重复创建。

### `Task`（GET `/api/v1/tasks/{taskId}` → 200）
```json
{
  "id": "tv_abc123",
  "createdAt": "2026-09-10T12:30:00Z",
  "model": "minimax",
  "input": { "content": "..." },
  "output": { "url": "https://cdn.../video.mp4", "msg": "...", "error": null },
  "status": "COMPLETED",
  "creditsCharged": 15,
  "creditsRefunded": 0,
  "listValueCents": 15,
  "idempotencyKey": "my-video-001",
  "completedAt": "2026-09-10T12:33:42Z"
}
```

### `Error`（通用失败响应）
```json
{
  "status": "FAILED",
  "errorCode": "INSUFFICIENT_CREDITS",
  "message": "Current balance 5 is below required 15",
  "error": "INSUFFICIENT_CREDITS"
}
```

**已知 `errorCode` 枚举**（来自 OpenAPI examples）：
- `AUTH_FAILED` — key 缺失 / 无效 / 禁用 / 删除
- `BUDGET_EXCEEDED` — 超过 `X-Max-Credits` 上限
- `IDEMPOTENCY_CONFLICT` — 同 key 不同 payload
- `INSUFFICIENT_CREDITS` — 余额不足
- `RATE_LIMITED` — 限流

**HTTP 状态码**（推荐分级映射）：
| 上游 | 含义 | 推荐代理处理 |
|---|---|---|
| 200 / 201 | 成功（含部分业务错误） | 看 errorCode 再判 |
| 400 | 模型/payload 非法 | 4xx 给终端用户 |
| 401 | key 无效 | 配置错，上抛 + 报警 |
| 402 | 余额不足 | 充值后再试，不要无限重试 |
| 409 | 幂等冲突 | 罕见，记录 + 重试用新 key |
| 422 | 超预算/内容审核拒绝 | 4xx 给终端用户 |
| 429 | 限速 | 退避 + 读 `Retry-After` |

## 6. 关键技术约束

### 6.1 幂等键怎么用
- **稳定**：同一个逻辑生成（同提示词 + 同参数）必须用同一个 key。
- **隔离**：不同逻辑生成必须换 key，绝不复用。
- 生成一个至少 16 字节的随机串就够用，绝不要用时间戳等会变的值。
- 代理层应在内部把 `user_request_id`（即 new-api 的 `request_id`）作为稳定 key。

### 6.2 消费上限怎么用
- 调用方传 `X-Max-Credits: <N>` 表示"这次最多扣 N credits"。
- 上游拿到这个 header，会在**预扣之前**判断报价是否超过；超了直接 422 不创建任务。
- 代理层应**先 `quote` 拿真实价格**，再把价格（或用户授权的上限）透传给 `generate`，避免凭空扣费。

### 6.3 数据 URI 限制
- 图片：允许 `data:image/...;base64,...`
- 视频/音频：**禁止** data URI，只能公网 HTTP(S) URL

> 这意味着如果终端用户上传本地图片给代理，代理要么自己图床上传拿到公网 URL，要么转 data URI 给 `imageUrl` —— 两种路径都要支持。
> 但 `referenceVideoUrl` / `referenceAudioUrls` 不能传 data URI，所以本地视频/音频必须**先传到公网**，否则只能放弃这个输入维度。

### 6.4 24 小时保留
- 一旦 `output.url` 出现，必须在 24 小时内主动下载到自己的存储（OSS/S3/本地）。
- 代理层应该有"下载器"：拿到 URL 后立刻落盘，并把内网/外网镜像 URL 返给终端用户。

### 6.5 限速
- 查询端点共享 60 req/min/IP —— 同一进程多任务并发轮询时必须做**令牌桶**或队列。
- 退避策略：指数退避 + jitter；尊重 `Retry-After`。

### 6.6 多账号 / 池化
- 上游没有会话上限概念（不同于 new-api），但每个 key 有余额上限（默认 50 credits 起）。
- 多 key 池化是有价值的（突破单 key 余额/限速）。
- 但要避免 cheap spam（不要无限创建免费试用 key）。

## 7. 已知风险与未覆盖点

| # | 风险 | 备注 |
|---|---|---|
| R-1 | OpenAPI JSON 只详细定义了 `minimax` 模型 | 其他 7 个模型仍需调研；页面文档分散在中/英/西/德子站 |
| R-2 | 视频 24h 过期 | 代理必须主动落盘；落盘介质（OSS / 本地）待架构决定 |
| R-3 | 多 key 余额池化 | 上游没有原生概念，需要自建 |
| R-4 | 取消语义窄 | 只能取消 SUBMITTED；进了 PROGRESS 就放弃退积分（除非上游失败） |
| R-5 | 内容审核 | 上游会拒绝 NSFW，但消息可能模糊；记录 `errorCode` 供审计 |
| R-6 | 货币换算 | 上游 1 credit = $0.01；new-api 用"美分" vs "积分"单位需统一 |
| R-7 | Webhook | 文档提到 `webhookUrl` header，但未在 OpenAPI 路径里出现；可选，后续增强 |

## 8. 给后续团队的输入要点

- **产品经理（许清楚）**：用户故事应该围绕"终端用户用 sk-xxx 在 new-api 兼容接口下调出视频"，状态枚举要映射到 new-api 的 TaskStatus（含 NOT_START/SUBMITTED/QUEUED/IN_PROGRESS/FAILURE/SUCCESS/UNKNOWN）。
- **架构师（高见远）**：要决定「独立中间层 Go service」还是「fork new-api 加 channel adapter」二选一；要包含下载器、轮询器、限速器、错误码映射。
- **工程师（寇豆码）**：基于 OpenAPI JSON 生成 Go 类型；HTTP client 用 `net/http` + `time.Ticker` 轮询，记住 24h 过期窗口。
- **QA（严过关）**：用 fake server 测每个端点的 200/4xx/5xx 映射；幂等重放；24h 过期边缘情况。

## 9. 原始资料索引

| 资料 | 路径 / 链接 |
|---|---|
| OpenAPI JSON（推荐用） | `docs/upstream/aivideo-openapi.json` |
| MiniMax H3 协议页 | https://aivideomaker.ai/zh/docs/api/minimax-h3 |
| 任务生命周期页 | https://aivideomaker.ai/zh/docs/api/task-lifecycle |
| 快速开始（中文） | https://aivideomaker.ai/zh/docs/api/quickstart |
| 完整 skill 页 | https://aivideomaker.ai/zh/skill |
| 英文 skill 页 | https://aivideomaker.ai/skill |
