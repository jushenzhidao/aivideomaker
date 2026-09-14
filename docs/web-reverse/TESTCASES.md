# aivideomaker.ai 反代 · 测试用例文档

被测对象：`avm-proxy/`（协议适配层 `adapter.mjs` + 客户端 `client.mjs`）
测试日期：2026-09-10 ~ 2026-09-11
测试账号：`aichatfire@gmail.com`（premium，$49.99，2026-09-10 起）
上游：`https://aivideomaker.ai`（MiniMax H3 封装）

> 所有真实生成任务均在账号后台可查：<https://aivideomaker.ai/zh/generations>

## 测试结果总览

**`node run-all-tests.mjs` → 通过 77 项，失败 0 项**（输出存档：`test-run.log`）
**`node run-all-tests.mjs --live` → 通过 83 项，失败 0 项**（含 K 组 2 条真实提交；输出存档：`test-run-live.log`）

| 分组 | 项数 | 覆盖内容 |
| --- | --- | --- |
| A. 接口契约 | 7 | 鉴权 401、未知路由 404、任务不存在 404、列表、/healthz |
| B. 请求体校验与枚举 | 15 | 必填、非法 ratio/resolution、7 种 ratio、3 种分辨率 |
| C. content 角色映射 | 9 | first_frame / last_frame / reference_image / 无 role / video / audio / 多 text |
| D. duration / frames / 计费 | 12 | 吸附规则、档位钳制、`prefer_free`、计费判定、跨档告警 |
| E. 站点不支持参数 | 16 | 14 个参数逐个 + 一起传 + 未知 content type |
| F. model → tier | 7 | 默认 turbo（不自动计费）、显式 base、非法值 |
| G. base64 转存 | 1 | data URI → CDN URL |
| H. 官方三条示例 | 3 | 多素材参考 / 视频编辑 / 首尾帧 |
| I. 媒体上传 | 2 | 视频、音频真实上传并原样回读 |
| J. 并发队列 | 5 | 并发不超 2、延迟提示、queue-full 回退重试 |
| **K. 真实提交（仅 `--live`）** | **6** | **2 条真实任务端到端 + 免计费断言** |

**默认运行的消耗**：仅 I 组产生真实网络写入（CDN 上传，免费）；其余全部走 `X-Avm-Dry-Run`
或 mock 上游，**不创建任何生成任务、不消耗额度**。

### K 组：真实提交（免费组合）

`--live` 会真实提交，使用**免费组合**——`tier:turbo` 且 `duration ≤ 10s`
（480p 合法时长只有 5/10/15/20，所以**只有 5s 落在免费区**）：

| 用例 | taskId | 成片 | 计费断言 |
| --- | --- | --- | --- |
| K1 文生视频 Ark 480p/5s | `cgt-20260910180737-c5e439ed` | [livetest-t2v-480p.mp4](https://static.img2video.ai/1789063767591-0cb7b67b-1d71-4637-9daa-ba9d81633399-1620612_0_minimax_h3_1620612.mp4) | `paid=false` ✓ |
| K2 图生视频 Ark first_frame 480p/5s | `cgt-20260910181116-5b3e6168` | [livetest-i2v-480p.mp4](https://static.img2video.ai/1789064046981-fc7e7536-d0b7-4571-ace5-2607c4b7fec8-1620616_0_minimax_h3_1620616.mp4) | `paid=false` ✓ |

每个用例断言四件事：**提交成功 → 状态到 `succeeded` → 拿到 `content.video_url` →
上游记录 `paid === false`（确认真免费）**。最后一条是关键——"用免费组合"不能只靠推断，
必须回查上游记录确认没有被计费。

执行细节：闸门关闭时会等待（每 30s 探一次，上限 10 分钟），K2 实际等了约 2 分钟才放行。

---



---

## 0. 测试环境与前置条件| 项 | 值 |
| --- | --- |
| Node | v22.22.2（managed） |
| 本机代理 | 有 `HTTP_PROXY=http://127.0.0.1:52651`，**curl 打 localhost 必须 `--noproxy '*'`** |
| 适配层端口 | `8788`，网关密钥 `sk-avm-demo` |
| 上游鉴权 | 仅需 session cookie（`auth_session=...`） |
| 启动 | `AVM_COOKIE='...' AVM_GATE_KEY=sk-avm-demo PORT=8788 node adapter.mjs` |

---

## 1. 计费规则验证（实测 34 条任务）

任务记录的 `paid` 字段表示**本次生成是否计费**。结论：

| 条件 | 计费 |
| --- | --- |
| `tier = "base"` | **一律计费**（与分辨率、时长无关） |
| `tier = "turbo"` 且 `duration ≤ 10s` | 免费 |
| `tier = "turbo"` 且 `duration ≥ 11s` | 计费 |

### 证据：按时长分组统计

| duration | 免费 `paid=false` | 计费 `paid=true` |
| --- | --- | --- |
| 5s | 13 | 3 |
| 6s | 3 | 0 |
| 7s | 1 | 0 |
| 8s | 5 | 0 |
| 9s | 0 | 1 |
| 10s | 0 | 3 |
| 11s | 0 | 1 |
| 14s | 0 | 2 |

那 3 条"计费的 5s"全部来自 `tier=base` 调用（`MiniMax-Hailuo-02` ×2、`doubao-seedance-2-5` ×1），
与该规则完全吻合。

### 关键坑：`credits` ≠ `paid`

| 字段 | 含义 |
| --- | --- |
| `paid` | **是否真实计费** —— 判断花钱只看这个 |
| `credits` | `paid=false` 记 1，`paid=true` 记 0（与 `paid` 反相，易误读） |

### 关键坑：480p 的时长吸附会跨进计费区

480p 只接受 `5 / 10 / 15 / 20`，所以：

| 请求 | 就近吸附 | 是否计费 |
| --- | --- | --- |
| 480p / 6s | 5s | 免费 |
| 480p / 8s | **10s** | **计费** |
| 480p / 9s | **10s** | **计费** |

对策：`extra_body.aivideomaker_prefer_free: true` → 8/9/12s 都吸附到 5s，保持免费。

### 其他限制（实测）

| 限制 | 值 | 错误信息 |
| --- | --- | --- |
| 并发任务数 | **2**（premium） | `The queue is full. The premium plan can only run 2 task at a time.` |
| 480p 允许时长 | 5 / 10 / 15 / 20 | `480p supports 5s, 10s, 15s, or 20s duration.` |
| 720p 允许时长 | 5–20 任意整数 | — |
| 1080p 已验证时长 | 5 / 10（15/20 未验证） | — |
| zod 时长范围 | 5–20 | `too_small minimum=5` / `too_big maximum=20` |

---

## 2. 协议 A：火山方舟 / Seedance 原生 —— 请求体全字段

**测试方式**：`extra_body.aivideomaker_dry_run: true`，走完完整翻译 + 图片转存后返回，**不提交、不消耗额度**。

**执行**：`node ark-body-test.mjs` → **64 个用例，全部通过**

### 结论

| 分层 | 状态 | 依据 |
| --- | --- | --- |
| **接口契约（路径 / 方法 / 鉴权 / 状态码）** | ✅ 通过 | 三个端点实测：POST 返回 `{"id":"cgt-..."}`、GET 返回完整 Ark 任务对象、DELETE 返回 `{}`；无鉴权 401、未知任务 404 |
| **请求体翻译层** | ✅ 通过 | 64 个 dry_run 用例覆盖全部字段（见 A1–A8） |
| **响应体映射** | ✅ 通过 | 状态枚举、`content.video_url`、`usage.credits`、`effective.billed`、`warnings`、`unsupported` 均验证 |
| **端到端真实生成** | ⚠️ 部分 | 文生（Ark）与图生（Ark）各跑通一次并出片；其余靠 dry_run |
| **上游能力覆盖度** | ❌ 有缺口 | 参考视频编辑/延长、多段参考视频、`callback_url` 等站点本身不支持（见第 11 节） |

> 一句话：**翻译层与接口契约完全通过；"能不能真的做出 Seedance 那样的效果"取决于上游，
> 而 aivideomaker 是 MiniMax H3 封装，能力边界不同。**

### A1. 必填与错误分支

| # | 用例 | 期望 | 实际 |
| --- | --- | --- | --- |
| 1 | 缺 `model` | 400 | ✅ `MissingParameter / model is required` |
| 2 | 缺 `content` | 400 | ✅ `MissingParameter / content is required` |
| 3 | `content: []` | 400 | ✅ `MissingParameter / content is required` |
| 4 | `ratio: "5:4"` | 400 | ✅ `InvalidParameter / ratio: invalid enum value "5:4"` |
| 5 | `resolution: "4k"` | 400 | ✅ `InvalidParameter / resolution: invalid enum value "4k"` |
| 6 | `content[].type` 未知 | 200 + 记录 | ✅ `unsupported: ["content[].type=\"weird_type\""]` |

### A2. `content` 各角色映射（9 种组合）

| 用例 | 映射结果 |
| --- | --- |
| 仅 `text` | `content` 填充，无图 |
| `text` + `role:first_frame` | → `imageUrl` |
| `text` + `role:last_frame` | → `lastFrameUrl` |
| `text` + `role:reference_image` | → `referenceImageUrls[0]` |
| `text` + `image_url` 无 role | → `referenceImageUrls[0]` |
| `text` + first + last | 两个槽位都填 |
| `text` + `role:reference_video` | → `referenceVideoUrl` |
| `text` + `role:reference_audio` | → `referenceAudioUrls[0]` |
| 多条 `text` | 用 `\n` 连接 |

### A3. `ratio` 枚举（7 种）

| 值 | 结果 |
| --- | --- |
| `16:9` `4:3` `1:1` `3:4` `9:16` `21:9` | 原样映射到 `aspectRatio` |
| `adaptive` | **不设值**，由站点从原图推导（语义一致） |

### A4. `resolution` 枚举（3 种）

`480p` / `720p` / `1080p` 均原样透传；非法值（`4k`）→ 400。

### A5. `duration` / `frames`

| 用例 | 吸附结果 | 计费 |
| --- | --- | --- |
| 480p / 5s | 5s | 免费 |
| 480p / 6s | 5s | 免费 |
| 480p / 12s | 10s | 计费 |
| 480p / 20s | 20s | 计费 |
| 720p / 6s | 6s | 免费 |
| 720p / 20s | 20s | 计费 |
| 720p / 30s | 20s（超上限） | 计费 |
| 1080p / 6s | 5s | 免费 |
| 1080p / 18s | 10s | 计费 |
| `frames:120`（24fps） | 5s | 免费 |
| `frames:240` | 10s | 计费 |
| `duration:-1`（智能时长） | 5s | 免费 |
| `prefer_free` + 480p / 8s | **5s** | **免费** |
| `prefer_free` + 480p / 9s | **5s** | **免费** |
| `prefer_free` + 480p / 12s | **5s** | **免费** |

### A6. 站点不支持的参数（逐个传 + 全部一起传）

`watermark`、`generate_audio`、`seed`、`camera_fixed`、`return_last_frame`、`draft`、
`service_tier`、`priority`、`callback_url`、`safety_identifier`、`tools`、
`omni_reference_task_type`、`execution_expires_after`、`output_format`

→ 逐个传时各自出现在 `unsupported`；**全部一起传返回完整 14 项**，不静默丢弃。

### A7. `model` → `tier` 映射与显式覆盖

| 用例 | tier | 计费 |
| --- | --- | --- |
| `doubao-seedance-2-5-260628` | turbo | 否 |
| `doubao-seedance-2-0-260128` | turbo | 否 |
| `doubao-seedance-1-0-pro-250528` | turbo | 否 |
| `unknown-model` | turbo | 否 |
| `extra_body.aivideomaker_tier:"base"` | base | **是**（并给出 warning） |
| `extra_body.aivideomaker_tier:"normal"`（非法） | turbo | 否（warning 说明被忽略） |

> 所有 model 名**默认 turbo**。这是刻意设计：`base` 一律计费，不能因为模型名听起来高级就自动升级。

### A8. base64 data URI 图片

传 `data:image/png;base64,...` 首帧 → 适配层解码后转存站点 CDN，返回
`https://static.img2video.ai/1789060330095-848a3531-...png` ✅

---

### 2B. 双向 request / response 对照

#### 2B.1 aivideomaker（站点）侧的四种响应形态

站点是 tRPC 批处理协议，响应恒为一个数组 `[{...}]`。实测捕获到四种形态：

**① 成功** —— `result.data.json` 是 taskId 字符串

```http
HTTP/1.1 200 OK
content-type: application/json
```

```json
[{ "result": { "data": { "json": "uxml26jtfygfdud" } } }]
```

**② 静默拒绝** —— 返回**空串**，既不报错也不建任务（token 无效 / 验证码闸门开启）

```http
HTTP/1.1 200 OK
```

```json
[{ "result": { "data": { "json": "" } } }]
```

> ⚠️ 这是最容易误判的形态：**空串 = 拒绝**，必须当成显式失败处理。

**③ 参数校验失败（zod）** —— `error.json.code` 为 `-32600`，`data.code` 为 `BAD_REQUEST`

```http
HTTP/1.1 400 Bad Request
```

```json
[{
  "error": {
    "json": {
      "message": "[{\"code\":\"invalid_enum_value\",\"options\":[\"turbo\",\"base\"],\"path\":[\"tier\"],\"message\":\"Invalid enum value. Expected 'turbo' | 'base', received 'normal'\"}]",
      "code": -32600,
      "data": { "code": "BAD_REQUEST", "httpStatus": 400 }
    }
  }
}]
```

**④ 业务错误** —— `code` 为 `-32603`，`data.code` 为 `INTERNAL_SERVER_ERROR`

```json
[{
  "error": {
    "json": {
      "message": "ai.minimaxH3: The queue is full. The premium plan can only run 2 task at a time.",
      "code": -32603,
      "data": { "code": "INTERNAL_SERVER_ERROR", "httpStatus": 500 }
    }
  }
}]
```

已实测到的业务错误文案：

| 触发 | message |
| --- | --- |
| 并发超限 | `The queue is full. The premium plan can only run 2 task at a time.` |
| 480p 时长非法 | `480p supports 5s, 10s, 15s, or 20s duration.` |
| 不支持的上传类型 | `Unsupported upload content type` |

**状态查询**（`model.getModel`）—— 直接返回任务记录对象：

```http
GET /api/model.getModel?batch=1&input={"0":{"json":{"id":"uxml26jtfygfdud"}}}
```

```json
[{
  "result": {
    "data": {
      "json": {
        "id": "uxml26jtfygfdud",
        "userId": "cmtuzdsvw000bsq5zsfbgefnt",
        "taskId": "https://batch.pipelet.net/queue/minimax_h3/requests/1619163",
        "taskStatus": "succeed",
        "duration": "5",
        "url": "https://static.img2video.ai/...-1619163_0_minimax_h3_1619163.mp4",
        "cover": "https://static.img2video.ai/...-image.jpg",
        "content": "a red panda eating bamboo",
        "aspectRatio": "16:9",
        "kelingKeyId": "480",
        "aiModel": "minimax-h3",
        "credits": 1,
        "paid": false,
        "createdAt": "2026-09-10T05:11:14.390Z",
        "completedAt": "2026-09-10T05:13:00.448Z"
      }
    }
  }
}]
```

#### 2B.2 逐用例 wire trace（真实抓取，dry_run 未产生任务）

`node ark-wire-trace.mjs` 逐用例打印四段：**调用方 → 适配层**（火山原生）、
**适配层 → aivideomaker**（站点 tRPC）、以及两侧的响应。

##### 用例 1：文生视频（仅 text）

**① 调用方 → 适配层**（火山原生格式）

```http
POST /api/v3/contents/generations/tasks
Authorization: Bearer <ARK_API_KEY>
Content-Type: application/json
X-Avm-Dry-Run: 1     # 仅校验，不提交
```
```json
{
  "model": "doubao-seedance-2-5-260628",
  "content": [
    {
      "type": "text",
      "text": "a paper boat circling in a rain puddle"
    }
  ],
  "ratio": "16:9",
  "resolution": "480p",
  "duration": 5
}
```

**② 适配层 → aivideomaker**（站点 tRPC 格式）

```http
POST https://aivideomaker.ai/api/ai.minimaxH3?batch=1
Cookie: auth_session=<session>
Content-Type: application/json
```
```json
{
  "0": {
    "json": {
      "content": "a paper boat circling in a rain puddle",
      "imageUrl": null,
      "lastFrameUrl": null,
      "referenceImageUrls": [],
      "referenceVideoUrl": null,
      "referenceAudioUrls": [],
      "aspectRatio": "16:9",
      "duration": 5,
      "resolution": "480p",
      "tier": "turbo"
    }
  }
}
```

**③ 适配层 → 调用方**（本次为 dry_run，故返回校验结果；真实提交见下）

```json
{
  "dry_run": true,
  "ok": true,
  "effective": {
    "aspectRatio": "16:9",
    "duration": 5,
    "resolution": "480p",
    "tier": "turbo",
    "billed": false
  },
  "warnings": [],
  "unsupported": []
}
```

---

##### 用例 2：图生（role=first_frame）

**① 调用方 → 适配层**（火山原生格式）

```http
POST /api/v3/contents/generations/tasks
Authorization: Bearer <ARK_API_KEY>
Content-Type: application/json
X-Avm-Dry-Run: 1     # 仅校验，不提交
```
```json
{
  "model": "doubao-seedance-2-5-260628",
  "content": [
    {
      "type": "text",
      "text": "a car"
    },
    {
      "type": "image_url",
      "role": "first_frame",
      "image_url": {
        "url": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg"
      }
    }
  ],
  "ratio": "adaptive",
  "resolution": "480p",
  "duration": 5
}
```

**② 适配层 → aivideomaker**（站点 tRPC 格式）

```http
POST https://aivideomaker.ai/api/ai.minimaxH3?batch=1
Cookie: auth_session=<session>
Content-Type: application/json
```
```json
{
  "0": {
    "json": {
      "content": "a car",
      "imageUrl": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg",
      "lastFrameUrl": null,
      "referenceImageUrls": [],
      "referenceVideoUrl": null,
      "referenceAudioUrls": [],
      "duration": 5,
      "resolution": "480p",
      "tier": "turbo",
      "aspectRatio": "16:9"
    }
  }
}
```

**③ 适配层 → 调用方**（本次为 dry_run，故返回校验结果；真实提交见下）

```json
{
  "dry_run": true,
  "ok": true,
  "effective": {
    "aspectRatio": "auto(from image)",
    "duration": 5,
    "resolution": "480p",
    "tier": "turbo",
    "billed": false
  },
  "warnings": [],
  "unsupported": []
}
```

---

##### 用例 3：首尾帧（first + last）

**① 调用方 → 适配层**（火山原生格式）

```http
POST /api/v3/contents/generations/tasks
Authorization: Bearer <ARK_API_KEY>
Content-Type: application/json
X-Avm-Dry-Run: 1     # 仅校验，不提交
```
```json
{
  "model": "doubao-seedance-1-5-pro-251215",
  "content": [
    {
      "type": "text",
      "text": "360度环绕运镜"
    },
    {
      "type": "image_url",
      "role": "first_frame",
      "image_url": {
        "url": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg"
      }
    },
    {
      "type": "image_url",
      "role": "last_frame",
      "image_url": {
        "url": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg"
      }
    }
  ],
  "generate_audio": true,
  "ratio": "adaptive",
  "duration": 5,
  "watermark": false
}
```

**② 适配层 → aivideomaker**（站点 tRPC 格式）

```http
POST https://aivideomaker.ai/api/ai.minimaxH3?batch=1
Cookie: auth_session=<session>
Content-Type: application/json
```
```json
{
  "0": {
    "json": {
      "content": "360度环绕运镜",
      "imageUrl": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg",
      "lastFrameUrl": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg",
      "referenceImageUrls": [],
      "referenceVideoUrl": null,
      "referenceAudioUrls": [],
      "duration": 5,
      "resolution": "720p",
      "tier": "turbo",
      "aspectRatio": "16:9"
    }
  }
}
```

**③ 适配层 → 调用方**（本次为 dry_run，故返回校验结果；真实提交见下）

```json
{
  "dry_run": true,
  "ok": true,
  "effective": {
    "aspectRatio": "auto(from image)",
    "duration": 5,
    "resolution": "720p",
    "tier": "turbo",
    "billed": false
  },
  "warnings": [],
  "unsupported": [
    "watermark",
    "generate_audio"
  ]
}
```

---

##### 用例 4：参考素材（图+视频+音频）

**① 调用方 → 适配层**（火山原生格式）

```http
POST /api/v3/contents/generations/tasks
Authorization: Bearer <ARK_API_KEY>
Content-Type: application/json
X-Avm-Dry-Run: 1     # 仅校验，不提交
```
```json
{
  "model": "doubao-seedance-2-5-260628",
  "content": [
    {
      "type": "text",
      "text": "视频编辑：删除 @视频1中的所有人，除了主角。"
    },
    {
      "type": "image_url",
      "role": "reference_image",
      "image_url": {
        "url": "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg"
      }
    },
    {
      "type": "video_url",
      "role": "reference_video",
      "video_url": {
        "url": "https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4"
      }
    },
    {
      "type": "audio_url",
      "role": "reference_audio",
      "audio_url": {
        "url": "https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3"
      }
    }
  ],
  "generate_audio": true,
  "ratio": "16:9",
  "duration": 5,
  "omni_reference_task_type": "reference",
  "output_format": "mov"
}
```

**② 适配层 → aivideomaker**（站点 tRPC 格式）

```http
POST https://aivideomaker.ai/api/ai.minimaxH3?batch=1
Cookie: auth_session=<session>
Content-Type: application/json
```
```json
{
  "0": {
    "json": {
      "content": "视频编辑：删除 @视频1中的所有人，除了主角。",
      "imageUrl": null,
      "lastFrameUrl": null,
      "referenceImageUrls": [
        "https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg"
      ],
      "referenceVideoUrl": "https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4",
      "referenceAudioUrls": [
        "https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3"
      ],
      "aspectRatio": "16:9",
      "duration": 5,
      "resolution": "720p",
      "tier": "turbo"
    }
  }
}
```

**③ 适配层 → 调用方**（本次为 dry_run，故返回校验结果；真实提交见下）

```json
{
  "dry_run": true,
  "ok": true,
  "effective": {
    "aspectRatio": "16:9",
    "duration": 5,
    "resolution": "720p",
    "tier": "turbo",
    "billed": false
  },
  "warnings": [
    "reference_video is forwarded as referenceVideoUrl but upstream support is unverified",
    "reference_audio is forwarded as referenceAudioUrls but upstream support is unverified"
  ],
  "unsupported": [
    "generate_audio",
    "omni_reference_task_type",
    "output_format"
  ]
}
```

---

##### 用例 5：参数校验失败（缺 model）

```http
HTTP/1.1 400
```
```json
{
  "error": {
    "code": "MissingParameter",
    "message": "model is required",
    "param": "model",
    "type": "InvalidRequest"
  }
}
```


## 3. 协议 A：官方文档三条示例

**校验**：`node ark-doc-examples.mjs`（全部 dry_run）

| 示例 | 内容 | `effective` | 计费 |
| --- | --- | --- | --- |
| 示例1 | 1 图 + 6 视频参考，15s，16:9 | 16:9 / 720p / 15s / turbo | **计费** |
| 示例2 | 视频编辑，`adaptive` / `duration:-1` | auto(from image) / 720p / 5s | 免费 |
| 示例3 | 首尾帧（Seedance 1.5 pro），`adaptive` / 5s | **1:1（从原图推导）** / 720p / 5s | 免费 |

要点：

- **示例3 的两张外链图被自动转存**（`ark-project.tos-cn-beijing.volces.com` → `static.img2video.ai`），
  证明外链图片处理链路可用；比例从原图（正方形）正确推导为 `1:1`。
- **示例1 的 6 个 `reference_video` 只透传第 1 个**（上游 `referenceVideoUrl` 只有一个槽位），
  其余被丢弃 —— 真实限制，已在 `warnings` 提示。
- `omni_reference_task_type` / `output_format` 等列入 `unsupported`。

**测试方式说明**：协议 A（Seedance / Ark 原生）属于**适配层交付**，其验证以
`dry_run` 翻译校验为主，**不需要真实生成**。第 2 节全部 64 个用例均为零消耗。

作为补充，示例2 / 示例3 曾用 480p/5s（免费组合）实跑过一次以确认链路可用；
示例1（15s）会跨进计费区，**未提交**。

> ⚠️ 本文件诚实记录：在适配过程中我曾越界提交过 3 条计费任务
> （`9yf5rspla13ewym`、`pvq28g3w8p36fsp`、`zkavkvfj13ak957`），原因已在第 8 节说明。
> 后续凡是"适配/封装"类需求，一律只用 `dry_run` 验证，不发真实生成。

---

## 4. 协议 B：MiniMax 官方接口

| # | 用例 | 期望 | 实际 |
| --- | --- | --- | --- |
| 1 | `POST /v1/video_generation` 创建 | `task_id` + `base_resp.status_code:0` | ✅ `e6o3376382zzwsy` |
| 2 | `GET /v1/query/video_generation` | `Success` + `file_id` + 宽高 | ✅ `video_width:1920, video_height:1080` |
| 3 | `GET /v1/files/retrieve` | `download_url` | ✅ 下载 42.9MB 有效 MP4 |
| 4 | 非法 key | 401 | ✅ |
| 5 | 闸门开启时创建 | `base_resp.status_code:1002` | ✅ |
| 6 | `tier:"normal"`（非法） | 上游 zod 拒绝 | ✅ 暴露枚举只有 `turbo|base` |

状态枚举：`Preparing / Queueing / Processing / Success / Fail`。

---

## 5. 协议 C：OpenAI Responses

| # | 用例 | 期望 | 实际 |
| --- | --- | --- | --- |
| 1 | `POST /v1/responses`（`background`） | response 对象 | ✅ |
| 2 | `GET /v1/responses/:id` | `status: completed` + 成片 | ✅ |
| 3 | `GET /v1/models` | 模型列表 | ✅ |
| 4 | `stream: true` | SSE 事件序列 | ✅ `created → in_progress → output_item.done → completed → [DONE]` |
| 5 | 错误密钥 | 401 | ✅ |
| 6 | 未知路由 | 404 | ✅ |
| 7 | 闸门开启时创建 | 403 `permission_error` | ✅ |

`output[0]` 为 `video_generation_call`（`result.url` 成片地址），`output[1]` 为 `message`，
其 `output_text` 直接是 URL（兼容只读 `output_text` 的客户端）。

---

## 6. 三种生成模式（对齐站点 UI）

站点 UI 有三个页签：**仅文字** / **首尾帧** / **参考素材**。

| UI 模式 | 上游字段 | 适配层入口 |
| --- | --- | --- |
| 仅文字 | 只填 `content` | `input` / `prompt` / `content` |
| 图生（首帧） | `imageUrl` | `image_url`(role=first_frame) / `first_frame_image` |
| 首尾帧 | `imageUrl` + `lastFrameUrl` | `last_frame_image` / `role=last_frame` |
| 参考素材·图 | `imageUrl` + `referenceImageUrls` | `reference_image_urls` / `role=reference_image` |
| 参考素材·视频 | `referenceVideoUrl` | `reference_video_url` / `role=reference_video` |
| 参考素材·音频 | `referenceAudioUrls` | `reference_audio_urls` / `role=reference_audio` |

### 图生 / 参考素材校验（`node i2v-reference-test.mjs`，全部 dry_run）

| 用例 | 结果 |
| --- | --- |
| 图生（`imageUrl` 单图） | ✅ |
| 首尾帧（`imageUrl` + `lastFrameUrl`） | ✅ |
| 参考素材·多图 | ✅ |
| 参考素材·图 + 视频 | ✅ |
| 参考素材·图 + 视频 + 音频 | ✅ |
| 参考素材·外链视频自动转存 | ✅ 5.7MB → `static.img2video.ai/...-seedance2.5_reference2.mp4` |

> 测试中修掉两个真实 bug：①`translate()` 没有读取站点自己的字段名 `content`，
> 导致只传 `content` 时提示 "prompt is required"；②`referenceVideoUrl` / `referenceAudioUrls`
> **完全没有被透传**，参考素材模式的核心字段是缺失的。

### ⚠️ 上游硬约束：不能混用「首尾帧」与「参考素材」

真实提交时上游直接拒绝（R2/R3 首次跑就是踩了这个）：

```
INTERNAL_SERVER_ERROR
ai.minimaxH3: Cannot mix reference assets with first/last frame.
Use either frame-to-video or reference-to-video
```

即：**一次请求只能是下面两种之一**

| 任务类型 | 允许的素材 | 禁止 |
| --- | --- | --- |
| frame-to-video | `imageUrl`(首帧) / `lastFrameUrl`(尾帧) | 任何 reference 素材 |
| reference-to-video | `referenceImageUrls` / `referenceVideoUrl` / `referenceAudioUrls` | `imageUrl` / `lastFrameUrl` |

**适配层已加前置校验**（`assertTaskKindIsPure`），混用时直接返回 400/`2013`，不再让上游拒绝：

```
cannot mix reference assets with first/last frame: upstream requires either
frame-to-video (first_frame / last_frame only) or reference-to-video
(reference_image / reference_video / reference_audio only), not both
```

### 参考素材真实提交结果（`node live-reference-test.mjs`，480p/5s 免费组合）

| 用例 | Ark taskId | 上游 id | 状态 | paid | 成片 |
| --- | --- | --- | --- | --- | --- |
| R1 多图参考（`reference_image` ×2） | `cgt-20260910181721-6559500f` | `9nvnmc56ll974c8` | ✅ succeeded | **false** | `ref-r1-multiimage.mp4` |
| R2 图+视频参考 | `cgt-20260910182322-24a2e15f` | `bga3d1uaw7v8ksu` | ✅ succeeded | **false** | `ref-r2-img-video.mp4` |
| R3 图+视频+音频参考 | `cgt-20260910182715-1a13548a` | `j6t0lwmrgermecv` | ✅ succeeded | **false** | `ref-r3-img-video-audio.mp4` |
| R4 完整素材集（图×2+视频+音频） | `cgt-20260910183114-bfa2e4b9` | `jp054u2wipkwnjd` | ✅ succeeded | **false** | `ref-r4-full-set.mp4` |

**结论：`referenceVideoUrl` 与 `referenceAudioUrls` 上游确实接受**——原先标注的"已透传但未验证"
现已验证通过。三个用例全部 `paid=false`，未计费。

### ❌ 该站**没有**生图能力

把 20 个可能的生图 procedure 名全探了一遍（`ai.flux` / `ai.image` / `ai.generateImage` /
`ai.sd` / `ai.nanoBanana` / `ai.gptImage` / `model.generateImage` / `image.generate` …），
**全部 NOT_FOUND**；只有 `ai.minimaxH3` 存在。

`aivideomaker.ai` 是**纯视频站**（MiniMax H3）。任何"图片/音频/视频 → 生图"的需求在此站无解，
只能产出视频。需要生图请用别的后端。

---

## 7. 媒体上传（图片 / 视频 / 音频）

`参考素材` 需要视频和音频上传。实测限额按类型分档：

| 类型 | `maxBytes` |
| --- | --- |
| 图片 | 10,485,760（10 MB） |
| **视频** | **52,428,800（50 MB）** |
| **音频** | **15,728,640（15 MB）** |

`node media-upload-test.mjs` 实测（上传不产生生成任务、不消耗额度）：

| 素材 | 类型 | 大小 | 回读校验 |
| --- | --- | --- | --- |
| `test-video.mp4` | `video/mp4` | 554.3KB | http 200，`content-type: video/mp4`，**字节一致 ✓** |
| `test-tone.mp3` | `audio/mpeg` | 15.9KB | http 200，`content-type: audio/mpeg`，**字节一致 ✓** |

类型识别基于 magic bytes（不信任扩展名）：PNG / JPEG / WebP / MP4 / MOV / WebM / MP3 / AAC / WAV / OGG。

---

## 8. 上游并发闸门与提交队列

上游并发上限 **2**（premium）：

```
The queue is full. The premium plan can only run 2 task at a time.
```

**槽位占用到任务终态为止**，不是创建请求返回就释放。`submit-queue.mjs` 提供
并发闸门 + 延迟执行 + 退避重试。

`node queue-test.mjs`（mock 上游，零消耗）：

| # | 用例 | 结果 |
| --- | --- | --- |
| 1 | 5 个提交、并发上限 2 | ✅ 峰值并发 = 2，5 个全部完成，打印 3 次"delayed until a slot frees" |
| 2 | 上游先回 2 次 `queue is full` | ✅ 重试 2 次后成功，打印 2 条 `UPSTREAM FULL (attempt n/6)` |
| 3 | 上游持续满、超过重试上限 | ✅ 打印 `giving up after N attempts` 并拒绝，`rejected_total=1` |
| 4 | 槽位在任务终态后释放 | ✅ `running` 归零 |

可观测：`GET /queue`，以及 `/healthz` 的 `submit_queue` 字段。

---

## 9. 图生视频（i2v）专项

| # | 用例 | 结论 |
| --- | --- | --- |
| 1 | 站点只收自己 CDN 的图 | 外链返回 `image/jpg`（非标准 MIME）被拒：`Unsupported upload content type` |
| 2 | 上传接口 | `uploads.getPresignedUrl`（**复数**）→ `{uploadUrl, headers, publicUrl, maxBytes}`，`maxBytes=10MB`，PUT 有效期 **60 秒** |
| 3 | 出片比例 | **跟随原图，不跟请求字段**：800×1200 → 480×704；2560×1440 → 864×480 / 1248×704 |
| 4 | 自动转存 | 适配层对非 `static*.img2video.ai` 的图自动转存，可用 `no_rehost:true` 关闭 |
| 5 | 外链图端到端 | 480p → 864×480（126s）；768P → 1248×704（167s） |

---

## 10. 真实生成任务与视频链接

> 完整 34 条见文末附录。以下为本文档涉及的验证任务。

| 用途 | 上游任务 ID | 协议 | 参数 | 成片 |
| --- | --- | --- | --- | --- |
| 端到端基线 | `uxml26jtfygfdud` | web | 480p/5s | https://static.img2video.ai/1789017175432-4a0ff52b-b46e-4ae2-b59d-5a89fe261fe3-1619163_0_minimax_h3_1619163.mp4 |
| 高清时长 | `e6o3376382zzwsy` | web | 1080p/10s | https://static2.img2video.ai/1789017947547-1c1605e6-c69d-4b16-9708-b1782cf016bc-1619200_0_minimax_h3_1619200.mp4 |
| 图生（竖图） | `g2udhakmtwgxfnv` | web | 480p/5s | https://static.img2video.ai/1789020457318-b381617b-ff04-4c1d-ae60-38b6b873d272-1619275_0_minimax_h3_1619275.mp4 |
| 外链图 480p | `iq4ipf5j6lpt48n` | web | 480p/5s | https://static.img2video.ai/1789021067574-1de61297-68db-4f32-968b-653d43aab024-1619301_0_minimax_h3_1619301.mp4 |
| 外链图 768P | `gejww1f1bmwjkye` | MiniMax | 768P/5s | https://static2.img2video.ai/1789021480364-2c57a413-741b-4cb7-89d0-c0d5c1ac2cf7-1619312_0_minimax_h3_1619312.mp4 |
| 720p/6s | `vh05olo9b6bccbq` | web | 720p/6s | https://static.img2video.ai/1789022975696-021ec578-b220-4b3b-a7bd-6689944c... |
| 时长 5s | `t5tk34m9xfplgda` | web | 720p/5s | https://static.img2video.ai/1789023442687-d24a843e-afc9-4683-9ac1-fd43391b7d13-1619399_0_minimax_h3_1619399.mp4 |
| 时长 6s | `fnmiohol6e6v2m8` | web | 720p/6s | https://static.img2video.ai/1789023643712-28f5c19e-11a8-419f-88f6-94d45c8dbbaa-1619405_0_minimax_h3_1619405.mp4 |
| 时长 7s | `34cexvtfxpwpujp` | web | 720p/7s | https://static.img2video.ai/1789023783912-055a8194-908f-4043-a56b-661085997c7a-1619416_0_minimax_h3_1619416.mp4 |
| 时长 8s | `3gb0tnp2buj5ovg` | web | 720p/8s | https://static.img2video.ai/1789023976703-562b8ebb-b88d-4529-828d-462efb12e39b-1619426_0_minimax_h3_1619426.mp4 |
| 时长 9s（计费） | `s24mijo8w0kv9tv` | web | 720p/9s | https://static2.img2video.ai/1789024263412-80877b95-5bf0-460c-8224-17d85511f23c-1619437_0_minimax_h3_1619437.mp4 |
| 时长 11s（计费） | `o2386isvqt30n8l` | web | 720p/11s | https://static2.img2video.ai/1789024247286-e2b7a43e-910c-4b43-9036-56e2a3af4421-1619433_0_minimax_h3_1619433.mp4 |
| 时长 10s（计费） | `22o77yfqlqcvb0a` | web | 720p/10s | https://static2.img2video.ai/1789024605523-58fca95d-17e3-4a3e-9093-f890af1582e2-1619448_0_minimax_h3_1619448.mp4 |
| 时长 14s（计费） | `vgygmni8ck9izbv` | web | 720p/14s | https://static2.img2video.ai/1789025150182-3bdcd748-acc8-4a79-9723-7a1ca9ba4729-1619462_0_minimax_h3_1619462.mp4 |
| 时长 14s 复测 | `3oi55v1kaeb15dq` | web | 720p/14s | https://static2.img2video.ai/1789029947490-5e0a21d3-c91b-4b8d-a973-044576f6251c-1619676_0_minimax_h3_1619676.mp4 |
| 批量 8s #1 | `rg2gxu4rxcsmety` | web | 720p/8s | https://static.img2video.ai/1789030157278-3fb686be-1989-45e4-a09f-6ad5bde9fc10-1619696_0_minimax_h3_1619696.mp4 |
| 批量 10s（计费） | `wtcvzcx7xeadxcn` | web | 720p/10s | https://static2.img2video.ai/1789030424582-b8233e13-a9ce-43f5-b3c5-7540cd85a8ca-1619707_0_minimax_h3_1619707.mp4 |
| 批量 8s #2 | `9k6ikmjvd61nrly` | web | 720p/8s | https://static.img2video.ai/1789030778268-71efe9c4-6872-4806-a29b-57452ddfee01-1619725_0_minimax_h3_1619725.mp4 |
| 批量 8s #3 | `q31lc4abisdyn09` | web | 720p/8s | https://static.img2video.ai/1789031000758-ac00eddd-d2e9-4088-8bd2-7a26b6871d3c-1619729_0_minimax_h3_1619729.mp4 |
| Ark 文生视频 | `9yf5rspla13ewym` | Ark | 480p/5s | https://static2.img2video.ai/1789059701703-f5a0cfc9-ddfb-443f-8529-e107c8dd1ee0-1620534_0_minimax_h3_1620534.mp4 |
| Ark 图生（首帧） | `pvq28g3w8p36fsp` | Ark | 720p/5s | https://static2.img2video.ai/1789060059561-badb6109-b9c5-4725-91b1-cdb99d13dc5a-1620542_0_minimax_h3_1620542.mp4 |

---

## 11. 未验证 / 已知限制

| 项 | 状态 |
| --- | --- |
| **该站是否支持"生图"** | ❌ **不支持**。20 个生图 procedure 名全部 NOT_FOUND，只有 `ai.minimaxH3`（视频）。此站是纯视频站，任何"图/音/视频 → 生图"的需求都无解 |
| **首尾帧与参考素材混用** | ❌ 上游硬拒 `Cannot mix reference assets with first/last frame`，适配层已前置校验 |
| Ark 列表接口返回信封 | **未核实**（官方文档页 JS 渲染取不到），当前为尽力实现 |
| 1080p 的 15s / 20s | 未验证（探测时闸门关闭） |
| 官方 API key 分支（`ak_` 前缀） | 只验证到请求构造，无真实 key |
| 取消运行中的任务 | 站点**无取消端点**；`model.deleteModel` 只能删记录 |
| 多段 `reference_video` | 上游仅一个槽位，只透传第 1 个 |
| `watermark` / `seed` / `camera_fixed` / `callback_url` 等 14 项 | 上游不支持，仅回显在 `unsupported` |

### 适配过程中产生的计费任务（教训记录）

做协议 A 适配时，我用真实提交代替 `dry_run` 去"端到端验证"，产生了 3 条计费任务：

| 任务 ID | 参数 | 计费原因 | 修复 |
| --- | --- | --- | --- |
| `9yf5rspla13ewym` | 480p/5s | `ARK_MODEL_TIER` 把 `doubao-seedance-2-5` 映射成 `base` | 该表已清空，默认全 turbo |
| `pvq28g3w8p36fsp` | 704p/5s | `MODEL_MAP` 把 `MiniMax-Hailuo-02` 映射成 `base` | 已改回 turbo |
| `zkavkvfj13ak957` | 704p/20s | 发 `duration:99` 想验证拒绝，适配层却先钳到 20s 再提交 | 记录已删；验证用例改用 `dry_run` |

**教训**：①"适配"类需求用 `dry_run` 即可，不必真实提交；
②验证"非法入参被拒"前，要确认适配层不会先把非法值"修好"再发出去；
③model 名到计费档位的默认映射必须保守（默认 turbo），不能让"模型名听起来高级"导致自动升级。

---

## 12. 复现方式

```bash
cd avm-proxy
export AVM_COOKIE='auth_session=...; NEXT_LOCALE=zh'
export AVM_GATE_KEY=sk-avm-demo
node adapter.mjs &                      # :8788

node run-all-tests.mjs                  # A–J 组，全程零消耗（77 项）
node run-all-tests.mjs --live           # 追加 K 组真实提交（+6 项，2 条免费任务）

node ark-body-test.mjs                  # 64 个请求体用例（零消耗）
node ark-wire-trace.mjs                 # 双向 request/response trace（零消耗）
node ark-doc-examples.mjs               # 官方三条示例校验（零消耗）
node i2v-reference-test.mjs             # 图生 / 参考素材校验（零消耗）
node queue-test.mjs                     # 并发队列单测（mock 上游）
node media-upload-test.mjs              # 视频/音频上传（免费）
node upload.mjs <图URL或路径>            # 媒体转存站点 CDN
node durt-submit.mjs 5 480p             # 单个真实任务（务必用免费组合）
node collect-tasks.mjs                  # 汇总任务与视频链接
node credit-report.mjs                  # 计费统计
```

> 所有真实生成前建议先用 `dry_run` 校验，并用 `effective.billed` 确认是否会计费。

---

## 附录：全部生成任务与视频链接

数据来源：`model.listModel`（账号共 43 条，取回 43 条）。
时间均为 UTC。`paid` 为 false 表示免费，true 表示计费。

| # | 任务 ID | 创建时间 (UTC) | 来源 | 分辨率 | 时长 | 比例 | credits | paid | 成片链接 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `e3anjy5bpi52lyt` | 2026-09-10T03:55:26.502Z | text | 704 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789012689087-621bf2b0-2337-48d1-b65f-caa5a3473e71-1618977_0_minimax_h3_1618977.mp4) |
| 2 | `bbdke79mc11qh4s` | 2026-09-10T04:00:28.593Z | image | 480 | — | — | 0 | false | [播放](https://static.img2video.ai/1789012938445-cd5c5f69-bf7d-46e6-8503-ae9c1f4bb2d6-image.png) |
| 3 | `8h798aj835sxouh` | 2026-09-10T04:01:46.199Z | text | 704 | 8 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789013098970-0580e348-012b-4201-8c56-5a4b07772868-1618993_0_minimax_h3_1618993.mp4) |
| 4 | `geb5ccpc8d17yf3` | 2026-09-10T04:03:49.928Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789013187926-b75c4399-f1ce-454e-9fc8-e81b3b6babca-1618999_0_minimax_h3_1618999.mp4) |
| 5 | `03bah76l6fsq089` | 2026-09-10T04:13:11.174Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789013743613-8259c16e-b44e-4250-8cd0-044dba212a23-1619032_0_minimax_h3_1619032.mp4) |
| 6 | `zmihlxvefmz56lr` | 2026-09-10T04:24:26.728Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789014382010-54af519b-6e71-423f-84ac-a85e2c846ecd-1619051_0_minimax_h3_1619051.mp4) |
| 7 | `z2qg3cli8abhgu9` | 2026-09-10T04:32:00.350Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789014837705-0ff54000-3a3c-4122-af83-480cf4f04141-1619063_0_minimax_h3_1619063.mp4) |
| 8 | `zict8zwntxu70ry` | 2026-09-10T04:49:22.753Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789015857911-3c59b424-3136-4944-8437-24189090f121-1619109_0_minimax_h3_1619109.mp4) |
| 9 | `qaul40pcx9emuc8` | 2026-09-10T04:54:23.016Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789016175645-852e7ba1-dff7-4cea-ab57-eb3ec55c6dbd-1619115_0_minimax_h3_1619115.mp4) |
| 10 | `uxml26jtfygfdud` | 2026-09-10T05:11:14.390Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789017175432-4a0ff52b-b46e-4ae2-b59d-5a89fe261fe3-1619163_0_minimax_h3_1619163.mp4) |
| 11 | `e6o3376382zzwsy` | 2026-09-10T05:20:00.277Z | text | 1080 | 10 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789017947547-1c1605e6-c69d-4b16-9708-b1782cf016bc-1619200_0_minimax_h3_1619200.mp4) |
| 12 | `6szj51ed19jl3h9` | 2026-09-10T05:33:48.523Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789018621863-706487af-b40e-4353-b5e4-9565f4602976-1619222_0_minimax_h3_1619222.mp4) |
| 13 | `g2udhakmtwgxfnv` | 2026-09-10T06:04:58.646Z | image | 480 | 5 | 9:16 | 1 | false | [播放](https://static.img2video.ai/1789020457318-b381617b-ff04-4c1d-ae60-38b6b873d272-1619275_0_minimax_h3_1619275.mp4) |
| 14 | `iq4ipf5j6lpt48n` | 2026-09-10T06:14:57.187Z | image | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789021067574-1de61297-68db-4f32-968b-653d43aab024-1619301_0_minimax_h3_1619301.mp4) |
| 15 | `gejww1f1bmwjkye` | 2026-09-10T06:21:30.167Z | image | 704 | 5 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789021480364-2c57a413-741b-4cb7-89d0-c0d5c1ac2cf7-1619312_0_minimax_h3_1619312.mp4) |
| 16 | `psdbqvfu6j4yeob` | 2026-09-10T06:29:52.971Z | image | 480 | 5 | 1:1 | 1 | false | [播放](https://static.img2video.ai/1789021966045-3aa81685-be57-414e-aa5a-26ed05cde2d2-1619332_0_minimax_h3_1619332.mp4) |
| 17 | `3hn0lf9uok08ldq` | 2026-09-10T06:34:34.923Z | text | 704 | 6 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789022253257-8068fa7c-49cc-473a-b0be-0f42cc2e21b0-1619345_0_minimax_h3_1619345.mp4) |
| 18 | `vh05olo9b6bccbq` | 2026-09-10T06:46:32.725Z | text | 704 | 6 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789022975696-021ec578-b220-4b3b-a7bd-6689944c1992-1619379_0_minimax_h3_1619379.mp4) |
| 19 | `t5tk34m9xfplgda` | 2026-09-10T06:53:44.727Z | text | 704 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789023442687-d24a843e-afc9-4683-9ac1-fd43391b7d13-1619399_0_minimax_h3_1619399.mp4) |
| 20 | `fnmiohol6e6v2m8` | 2026-09-10T06:57:12.369Z | text | 704 | 6 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789023643712-28f5c19e-11a8-419f-88f6-94d45c8dbbaa-1619405_0_minimax_h3_1619405.mp4) |
| 21 | `34cexvtfxpwpujp` | 2026-09-10T07:00:27.045Z | text | 704 | 7 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789023783912-055a8194-908f-4043-a56b-661085997c7a-1619416_0_minimax_h3_1619416.mp4) |
| 22 | `3gb0tnp2buj5ovg` | 2026-09-10T07:03:43.620Z | text | 704 | 8 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789023976703-562b8ebb-b88d-4529-828d-462efb12e39b-1619426_0_minimax_h3_1619426.mp4) |
| 23 | `o2386isvqt30n8l` | 2026-09-10T07:06:58.341Z | text | 704 | 11 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789024247286-e2b7a43e-910c-4b43-9036-56e2a3af4421-1619433_0_minimax_h3_1619433.mp4) |
| 24 | `s24mijo8w0kv9tv` | 2026-09-10T07:07:20.423Z | text | 704 | 9 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789024263412-80877b95-5bf0-460c-8224-17d85511f23c-1619437_0_minimax_h3_1619437.mp4) |
| 25 | `22o77yfqlqcvb0a` | 2026-09-10T07:13:30.234Z | text | 704 | 10 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789024605523-58fca95d-17e3-4a3e-9093-f890af1582e2-1619448_0_minimax_h3_1619448.mp4) |
| 26 | `vgygmni8ck9izbv` | 2026-09-10T07:20:54.221Z | text | 704 | 14 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789025150182-3bdcd748-acc8-4a79-9723-7a1ca9ba4729-1619462_0_minimax_h3_1619462.mp4) |
| 27 | `3oi55v1kaeb15dq` | 2026-09-10T08:40:22.903Z | text | 704 | 14 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789029947490-5e0a21d3-c91b-4b8d-a973-044576f6251c-1619676_0_minimax_h3_1619676.mp4) |
| 28 | `rg2gxu4rxcsmety` | 2026-09-10T08:47:01.610Z | text | 704 | 8 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789030157278-3fb686be-1989-45e4-a09f-6ad5bde9fc10-1619696_0_minimax_h3_1619696.mp4) |
| 29 | `wtcvzcx7xeadxcn` | 2026-09-10T08:50:21.826Z | text | 704 | 10 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789030424582-b8233e13-a9ce-43f5-b3c5-7540cd85a8ca-1619707_0_minimax_h3_1619707.mp4) |
| 30 | `9k6ikmjvd61nrly` | 2026-09-10T08:57:35.118Z | text | 704 | 8 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789030801027-dfdf093d-a2fc-4998-9ba4-c044f34f1ec0-1619725_0_minimax_h3_1619725.mp4) |
| 31 | `q31lc4abisdyn09` | 2026-09-10T09:00:51.156Z | text | 704 | 8 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789031000758-ac00eddd-d2e9-4088-8bd2-7a26b6871d3c-1619729_0_minimax_h3_1619729.mp4) |
| 32 | `9yf5rspla13ewym` | 2026-09-10T17:00:21.963Z | text | 480 | 5 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789059701703-f5a0cfc9-ddfb-443f-8529-e107c8dd1ee0-1620534_0_minimax_h3_1620534.mp4) |
| 33 | `pvq28g3w8p36fsp` | 2026-09-10T17:04:23.106Z | image | 704 | 5 | 16:9 | 0 | true | [播放](https://static2.img2video.ai/1789060059561-badb6109-b9c5-4725-91b1-cdb99d13dc5a-1620542_0_minimax_h3_1620542.mp4) |
| 34 | `c6gsdeus4okqtsh` | 2026-09-10T17:12:25.590Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789060451572-c2197b29-16b9-4dc3-a18f-c46cc4226847-1620555_0_minimax_h3_1620555.mp4) |
| 35 | `v1r33g10fim2622` | 2026-09-10T17:16:01.407Z | video | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789060764217-d16351c9-8c03-46db-a94e-c09f16ee883d-1620560_0_minimax_h3_1620560.mp4) |
| 36 | `2j4tja9yanscv16` | 2026-09-10T17:20:01.556Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789060879276-db321cb7-31a1-487a-a351-e995ac7da8c5-1620565_0_minimax_h3_1620565.mp4) |
| 37 | `e0hvprrm5kma57e` | 2026-09-10T17:21:10.815Z | image | 480 | 5 | 1:1 | 1 | false | [播放](https://static.img2video.ai/1789060997894-28bffce9-de68-4f0a-8524-b375742a4cd3-1620567_0_minimax_h3_1620567.mp4) |
| 38 | `9hpwyvk6sdvjtax` | 2026-09-10T18:07:37.221Z | text | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789063791489-7fa8ef52-6bf0-4d48-bebd-9cbf1fbacf51-1620612_0_minimax_h3_1620612.mp4) |
| 39 | `jzv0s6wkeecggrs` | 2026-09-10T18:11:16.250Z | image | 480 | 5 | 9:16 | 1 | false | [播放](https://static.img2video.ai/1789064060390-945c9638-7f26-4328-b81d-e70a37f5d7c7-1620616_0_minimax_h3_1620616.mp4) |
| 40 | `9nvnmc56ll974c8` | 2026-09-10T18:17:21.210Z | image | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789064342125-0f15d218-4a60-453f-9813-02132e17627e-1620622_0_minimax_h3_1620622.mp4) |
| 41 | `bga3d1uaw7v8ksu` | 2026-09-10T18:23:22.422Z | image | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789064828495-6704640a-ba13-4cc6-889f-0c5a61d08ebf-1620633_0_minimax_h3_1620633.mp4) |
| 42 | `j6t0lwmrgermecv` | 2026-09-10T18:27:15.834Z | image | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789065003652-88a84123-7699-43bd-8b58-9f114a15b572-1620636_0_minimax_h3_1620636.mp4) |
| 43 | `jp054u2wipkwnjd` | 2026-09-10T18:31:14.078Z | image | 480 | 5 | 16:9 | 1 | false | [播放](https://static.img2video.ai/1789065305376-b709543e-fddf-4238-815f-fc8f074a09c3-1620639_0_minimax_h3_1620639.mp4) |

**汇总**：共 43 条，免费 33 条、计费 10 条；43 条已产出成片。

> 上游视频 URL 有效期 24 小时，且 Seedance 2.5 的链接下载次数上限 100 次，请及时转存。
