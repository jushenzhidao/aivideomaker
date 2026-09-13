# aivideomaker.ai 视频生成 API 参考

> Base URL：`https://aivideomaker.ai`
> 认证：所有请求在请求头带 `key: <API Key>`；带 JSON 体的 POST 还需 `Content-Type: application/json`。
> 计费单位：`1 credit = $0.01`。

本文档基于 2026-09-10 的实测结果整理（含官方未公开的端点与参数差异），并交叉验证了官方
OpenAPI 描述（见 `docs/official/aivideo-openapi.json`）。

---

## 1. 接口清单

| 用途 | 方法与路径 | 是否计费 |
|---|---|---|
| 体检 / 模型列表 / 余额 | `GET /api/v1/account` | 否 |
| 报价（权威，用于试参数） | `POST /api/v1/quote/{model}` | 否 |
| 创建任务 | `POST /api/v1/generate/{model}` | **是** |
| 任务列表 | `GET /api/v1/tasks` | 否 |
| 查详情（信息最全） | `GET /api/v1/tasks/{id}` | 否 |
| 查状态 | `GET /api/v1/tasks/{id}/status` | 否 |
| 取消任务（退回积分） | `PUT /api/v1/tasks/{id}/cancel` | 否 |

官方 `supportedModels` 共 8 个：

```
t2v  i2v  minimax  t2v_v3  i2v_v3  seedance20  wan27  happyhorse
```

---

## 2. 推荐调用流程

**核心纪律：submit 之前先 quote。**

报价接口 `/api/v1/quote/{model}` 使用与生成接口**完全相同**的参数校验和计费逻辑，
但不会创建任务、不扣费（返回 `"billable": false`）。这带来两个好处：

1. 参数写错时返回详细的 zod 校验提示（例如 `expected: '720P' | '1080P'`），可免费拿到正确格式；
2. 参数合法时返回权威报价，可在提交前确认成本与余额是否充足。

标准流程：

```
GET  /api/v1/account            确认模型存在 + 看余额
  ↓
POST /api/v1/quote/{model}      试参数 + 拿报价（免费）
  ↓
POST /api/v1/generate/{model}   正式提交（带保护头）
  ↓
GET  /api/v1/tasks/{id}         轮询至 COMPLETED，取 output.url
  ↓
立即下载（成品只保留 24 小时）
```

---

## 3. 各模型参数与实测价格

以下为 5 秒、16:9 的实测报价。**参数类型差异是最容易踩的坑**。

| model | 必填参数（注意类型） | 积分（5s） | 美元 |
|---|---|---|---|
| `t2v` | `prompt`(str), `aspectRatio`(str), `duration`(**str** `"5"`) | 15 | $0.15 |
| `t2v_v3` | 同 `t2v` | 20 | $0.20 |
| `i2v` | `image`(url), `duration`(**str**) | 15 | $0.15 |
| `i2v_v3` | 同 `i2v` | 20 | $0.20 |
| `minimax` | 见下方专节 | 15 / 20 / 20 / 25 | $0.15~$0.25 |
| `seedance20` | `prompt`, `ratio`(str), `duration`(**number**), `resolution`(**number** `480`/`720`) | 480=51, 720=110 | $0.51 / $1.10 |
| `wan27` | `prompt`, `ratio`, `duration`(**str** `"5"`/`"10"`/`"15"`), `resolution`(**str** `"720P"`/`"1080P"`) | 720P=50, 1080P=75 | $0.50 / $0.75 |
| `happyhorse` | `prompt`, `duration`, `resolution`(**str** `"720P"`/`"1080P"`)，**无 ratio** | 720P=125, 1080P=250 | $1.25 / $2.50 |

### 关键差异警告

**`resolution` 在三个模型里有三种类型：**

| 模型 | 类型 | 合法值 |
|---|---|---|
| `seedance20` | number | `480` / `720` |
| `wan27` / `happyhorse` | string（大写 P） | `"720P"` / `"1080P"` |
| `minimax` | string（小写 p） | `"720p"` / `"1080p"` |

**比例字段名不一致：** `t2v`/`t2v_v3` 用 `aspectRatio`，`seedance20`/`wan27` 用 `ratio`，
`happyhorse` **没有**比例字段，`minimax` 用 `aspectRatio`。

**`minimax` 用 `content` 而非 `prompt`**，是唯一一个参数 schema 与其余模型完全不同的入口。

### minimax 专节（官方主推模型）

```json
{
  "content": "提示词",
  "duration": 5,
  "resolution": "720p",
  "tier": "turbo",
  "aspectRatio": "16:9",
  "imageUrl": null,
  "lastFrameUrl": null,
  "referenceImageUrls": [],
  "referenceVideoUrl": null,
  "referenceAudioUrls": []
}
```

| 字段 | 类型 / 取值 | 默认 |
|---|---|---|
| `content` | string，**必填** | — |
| `duration` | integer 5–20 | 5 |
| `resolution` | `"720p"` / `"1080p"` | `"720p"` |
| `tier` | `"turbo"` / `"base"` | `"turbo"` |
| `aspectRatio` | `auto` / `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` | `16:9` |
| `referenceImageUrls` | string[]，最多 4 个 | — |
| `referenceAudioUrls` | string[]，最多 2 个 | — |

限制：**帧输入（`imageUrl`/`lastFrameUrl`）与参考素材（`referenceImageUrls` 等）互斥**，
不能同时使用。图片字段接受公开 HTTP(S) URL 或 data URI，视频/音频只接受公开 HTTP(S) URL。

官方定价 $0.03~$0.05 / 生成秒，对应上表 15 / 20 / 20 / 25 积分（720p turbo → 1080p base）。

---

## 4. 两个必备的保护头

| 请求头 | 作用 |
|---|---|
| `Idempotency-Key` | 8–128 字符。相同 key + 相同请求体重放时返回**原任务**且不二次扣费；key 相同但请求体不同 → `409 IDEMPOTENCY_CONFLICT` |
| `X-Max-Credits` | 调用方批准的支出上限（整数）。权威报价高于该值时，**在计费前**直接拒绝（`422 BUDGET_EXCEEDED`） |

**批量任务或交由 agent 自动调用时，`X-Max-Credits` 必须带。** 本项目实测中就曾因为
"用一个又一个候选值去试探合法参数"而误建任务，白扣 51 积分 —— 若有该头则会直接被拦下。

---

## 5. 计费机制：按入口定价，不按输出分辨率

同一提示词、同为 5 秒 16:9，两个入口的实际产出与价格差异显著：

| 入口 | 底层模型 | 实际输出 | 文件大小 | 码率 | 积分 |
|---|---|---|---|---|---|
| `t2v` | minimax_h3（从成品 URL 可辨识） | 1248×704 | 0.81 MB | ≈1.3 Mbps | **15** |
| `seedance20` `resolution=720` | seedance | 1280×720 | 3.86 MB | ≈6.2 Mbps | **110** |

**结论：两者分辨率接近，但码率相差约 4.8 倍，价格相差 7 倍多。**

平台按 `model` 入口定价，与实际输出分辨率无关。`t2v` 走固定公式 `duration × 3`，
因此即使底层命中 minimax_h3 720p turbo 档，也只收 15 积分 —— 这与 `minimax` 入口
720p turbo 的报价完全一致，可确认 `t2v` 就是 minimax_h3 的低码率经济档。

**不要凭输出分辨率推算价格，一律以报价接口或提交返回的 `creditsCharged` 为准。**

---

## 6. 状态流转与计费

```
SUBMITTED ──> PROGRESS ──> COMPLETED
                       └─> FAILED
SUBMITTED ──> CANCEL（PUT 取消后）
```

- 提交即扣积分，字段名为 `creditsCharged`。
- 取消成功后积分**全额退回**：`creditsRefunded == creditsCharged`，`listValueCents` 归 0。
- 实测耗时：`t2v` 5s 约 6.5 分钟；`seedance20` 720p 5s 约 3.5 分钟。
- `GET /api/v1/tasks/{id}` 返回信息比 `/status` 完整（含 `input` / `output` / 计费明细）。
- 生产环境建议创建时带 `webhookUrl` 请求头做回调通知，避免长轮询。

---

## 7. 已实证的四个坑

1. **试探参数会真实扣积分。** 一旦所有字段合法，接口立即创建任务并计费。
   不要用"穷举候选值"的方式试探合法参数。正确做法：先发空 body `{}` 拿到必填字段清单，
   再用报价接口验证参数，最后才正式提交。

2. **取消任务必须用 `PUT`。** POST 和 DELETE 均返回 `405 Method Not Allowed`。
   接口文档只给出了 `cancelUrl`，未标注 HTTP 方法。

3. **成品链接只保留 24 小时。** `output.msg` 会明确提示
   "The file will only be saved for 24 hours, please download it promptly."
   拿到 URL 后应立刻下载转存。

4. **各模型参数类型不统一。** 见第 3 节的差异警告。跨模型复制请求体是最常见的失败原因。

---

## 8. 免费发现模型与价格的三种方式

### 方式一：官方体检端点（推荐）

```bash
curl -sS https://aivideomaker.ai/api/v1/account -H "key: $AVM_KEY"
```

返回 `supportedModels`、`currentBalance`、密钥限额（`dailyCreditLimit`、
`generationRequestsPerMinute`、`creditWarningAt`，`-1` 表示无限制）。
官方对其的描述是 "non-billable doctor endpoint for agents"。

### 方式二：报价端点

见第 2 节。除报价外还能免费验证参数格式。

### 方式三：模型名穷举探测

向不存在的模型发空 body，会返回：

```json
{"status":"FAILED","errorCode":"INVALID_MODEL","message":"Unsupported model: xxx"}
```

而存在的模型返回 `INVALID_PAYLOAD` + 字段要求。据此可批量试探模型名，**全程不扣费**。
本项目曾用此法验证 47 个候选名（kling / hailuo / veo / sora / runway / luma / pixverse 等），
结果与官方 `supportedModels` 完全一致。

### 附：官方公开资源

| 资源 | 地址 |
|---|---|
| LLM 发现索引 | `https://aivideomaker.ai/llms.txt` |
| OpenAPI 3.1 描述 | `https://aivideomaker.ai/docs/aivideo-openapi.json` |
| API 快速开始 | `https://aivideomaker.ai/docs/api/quickstart` |
| MiniMax H3 契约 | `https://aivideomaker.ai/docs/api/minimax-h3` |
| 任务生命周期 | `https://aivideomaker.ai/docs/api/task-lifecycle` |

---

## 9. 错误码

| errorCode | 含义 |
|---|---|
| `AUTH_FAILED` | 密钥缺失、无效、被禁用或已删除 |
| `INVALID_MODEL` | 模型名不受支持 |
| `INVALID_PAYLOAD` | 请求体字段校验失败（会附带字段级提示） |
| `INSUFFICIENT_CREDITS` | 余额不足（HTTP 402） |
| `BUDGET_EXCEEDED` | 超出 `X-Max-Credits` 上限（HTTP 422） |
| `IDEMPOTENCY_CONFLICT` | 同一 `Idempotency-Key` 配不同请求体（HTTP 409） |
| `RATE_LIMITED` | 超过密钥生成频率限制（HTTP 429，遵循 `Retry-After`） |
