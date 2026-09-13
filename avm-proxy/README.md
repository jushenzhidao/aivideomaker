# aivideomaker.ai 反代

**纯 HTTP，不需要浏览器、不需要过验证码。**

关键发现：站点把 Turnstile 挂在 `model.needsCaptcha` 后面。订阅账号**在额度内**返回 `false`，此时 `ai.minimaxH3` 接受 `token: null`——所以整条链路就是几个 REST 调用加一个 session cookie。

> **⚠️ 修正（2026-09-10 实测）**：`needsCaptcha` 是**动态风控开关，不是账号属性**。同一个付费账号在 1 小时内连续生成约 7 条后翻转为 `true` 并持续 true；翻转后 `token: null` 与假 token 一样被**静默拒绝**（返回空串，不报错）。详见下方「动态验证码闸门」。

## 完整 API 表面

| 用途 | 端点 |
| --- | --- |
| 是否需要验证码 | `GET /api/model.needsCaptcha?input={"0":{"json":{"userId":"..."}}}` |
| 创建任务 | `POST /api/ai.minimaxH3?batch=1` body `{"0":{"json":{...,"token":null}}}` |
| 任务列表（分页） | `GET /api/model.listModel?input={"0":{"json":{"createdAt":null,"offset":0,"limit":40,"createAtSort":"desc","userId":"..."},"meta":{...}}}` |
| 队列位置 | `GET /api/model.queryQueueByModel?input={"0":{"json":{"id":"..."}}}` |
| 换状态令牌 | `GET /api/model-status/token?id=<id>&visitorId=<fp>` → `{token, expiresInSec:300}` |
| 任务状态（SSE） | `GET /api/model-status?id=<id>&token=<>&visitorId=<fp>` → `data: {...}` |
| **上传图片** | `POST /api/uploads.getPresignedUrl` `{fileName,contentType,fileSize,permanent}` → `{uploadUrl,headers,publicUrl,maxBytes}`；再 `PUT uploadUrl` |

状态令牌是 `base64url(JSON) + 32字节HMAC`：`{v:1, mid:<taskId>, inc:0, at:"u", aid:<accountId>, exp:+300s}`。

**`visitorId` 服务端不校验**——随便编个 32 位十六进制都行。

## 启动

```bash
cd avm-proxy
AVM_COOKIE='auth_session=xxx; NEXT_LOCALE=zh; ...' node proxy.mjs
# 或把 cookie 存到 cookies.json（参考 cookies.example.json）
```

无第三方依赖（只用 Node 内置 `http` / `fetch`）。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AVM_COOKIE` | 读 `cookies.json` | 原始 Cookie 头，必需 |
| `AVM_USER_ID` | 自动从 `auth.user` 解析 | 列表接口要用 |
| `COOKIES_FILE` | `./cookies.json` | cookie 文件备选来源 |
| `PORT` | `8787` | 监听端口 |

## 路由

```bash
# 健康检查（含账号 id + 是否需要验证码）
curl -s http://localhost:8787/healthz

# 生成（异步，立刻返回 taskId）
curl -s -X POST http://localhost:8787/generate \
  -H 'content-type: application/json' \
  -d '{"content":"a cat","aspectRatio":"16:9","duration":5,"resolution":"480p","tier":"turbo"}'
# -> {"mode":"web","taskId":"qaul40pcx9emuc8","ms":8421}

# 生成并阻塞等到出片 + 落盘
curl -s -X POST 'http://localhost:8787/generate?wait=1&download=/tmp/out.mp4' \
  -H 'content-type: application/json' \
  -d '{"content":"a ginger cat on a windowsill"}'

# 任务列表（分页）
curl -s 'http://localhost:8787/tasks?offset=0&limit=40&sort=desc'

# 单条状态
curl -s http://localhost:8787/task/qaul40pcx9emuc8

# 轮询等完成
curl -s 'http://localhost:8787/task/qaul40pcx9emuc8/wait?timeoutMs=600000&intervalMs=10000'

# 队列位置
curl -s http://localhost:8787/task/qaul40pcx9emuc8/queue
# -> {"queue":{"queue":0,"etaSeconds":0}}
```

任务记录字段：`taskStatus` / `url`（成片）/ `cover`（封面）/ `content`（提示词）/ `aiModel` / `duration` / `credits` / `createdAt` / `completedAt` / `aspectRatio` / **`kelingKeyId`（真实分辨率，如 `"1080"`、`"480"`）**。

## 协议适配层（OpenAI / MiniMax）

`adapter.mjs` 把上面的客户端包装成两套标准协议，**挂在同一端口下按路径分流**（两者路径不冲突）：

```bash
AVM_COOKIE='...' AVM_GATE_KEY=sk-avm-demo node adapter.mjs   # 默认 :8788
```

### OpenAI Responses API

```bash
# 异步提交（推荐）
curl -X POST http://localhost:8788/v1/responses \
  -H 'Authorization: Bearer sk-avm-demo' -H 'content-type: application/json' \
  -d '{"model":"minimax-h3","background":true,
       "input":[{"type":"input_text","text":"a paper boat in a rain gutter"}],
       "size":"1920x1080","seconds":6}'

# 阻塞到出片（不设 background）
# SSE 流式：加 "stream":true，事件序列 response.created → in_progress → output_item.done → completed

curl -H 'Authorization: Bearer sk-avm-demo' http://localhost:8788/v1/responses/resp_xxx
curl -H 'Authorization: Bearer sk-avm-demo' http://localhost:8788/v1/models
```

`output[0]` 是 `video_generation_call`（`result.url` 即成片地址），`output[1]` 是 `message`，其 `output_text` 直接就是 URL——这样只读 `output_text` 的朴素客户端也能用。

> 一处**故意偏离** OpenAI 规范：`image_generation_call` 的 `result` 是 base64 字符串，视频没有官方形态，这里用对象。

绑定已有任务（网页端发起的任务也能跟踪，不消耗额度）：`{"task_id":"e6o3376382zzwsy"}`。

### MiniMax 官方接口

```bash
curl -X POST http://localhost:8788/v1/video_generation \
  -H 'Authorization: Bearer sk-avm-demo' -H 'content-type: application/json' \
  -d '{"model":"MiniMax-Hailuo-02","prompt":"a red panda eating bamboo",
       "duration":6,"resolution":"1080P","prompt_optimizer":true}'
# -> {"task_id":"e6o3376382zzwsy","base_resp":{"status_code":0,"status_msg":"success"}}

curl -H 'Authorization: Bearer sk-avm-demo' \
  'http://localhost:8788/v1/query/video_generation?task_id=e6o3376382zzwsy'
# -> {"task_id":"...","status":"Success","file_id":"...","video_width":1920,"video_height":1080,"base_resp":{...}}

curl -H 'Authorization: Bearer sk-avm-demo' \
  'http://localhost:8788/v1/files/retrieve?file_id=e6o3376382zzwsy'
# -> {"file":{"download_url":"https://static2.img2video.ai/....mp4",...},"base_resp":{...}}
```

状态枚举 `Preparing / Queueing / Processing / Success / Fail`；`base_resp.status_code`：`0` 成功、`1002` 限流或验证码、`1004` 鉴权、`1008` 余额、`1026` 内容违规、`2013` 参数错误。

### 火山方舟 / Doubao Seedance 原生接口

对照官方文档实现（[创建](https://docs.volcengine.com/docs/82379/1520757) /
[查询](https://docs.volcengine.com/docs/82379/1521309) /
[取消删除](https://docs.volcengine.com/docs/82379/1521720)）。把 Ark SDK 的 base_url 指向
`http://<host>:8788/api/v3` 即可。

```bash
# 创建（文生视频）
curl -X POST http://localhost:8788/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer sk-avm-demo' -H 'content-type: application/json' \
  -d '{"model":"doubao-seedance-2-5-260628",
       "content":[{"type":"text","text":"a paper lantern drifting up a stairwell"}],
       "ratio":"16:9","resolution":"480p","duration":5}'
# -> {"id":"cgt-20260910170022-aa019ea1"}

# 查询
curl -H 'Authorization: Bearer sk-avm-demo' \
  http://localhost:8788/api/v3/contents/generations/tasks/cgt-20260910170022-aa019ea1

# 取消/删除（返回 {}）
curl -X DELETE -H 'Authorization: Bearer sk-avm-demo' \
  http://localhost:8788/api/v3/contents/generations/tasks/cgt-20260910170022-aa019ea1
```

状态枚举：`queued` / `running` / `succeeded` / `failed`（+ `cancelled`）。

**content 角色映射**（依据 Seedance 2.5 文档的任务类型判定规则）：

| Ark `content[]` | 上游字段 | 备注 |
| --- | --- | --- |
| `{"type":"text"}` | `content` | 多条用 `\n` 连接 |
| `image_url` + `role:"first_frame"` | `imageUrl` | 文档要求此时 `ratio` 必须 `adaptive` |
| `image_url` + `role:"last_frame"` | `lastFrameUrl` | |
| `image_url` + `role:"reference_image"` 或无 role | `referenceImageUrls` | |
| `video_url` + `role:"reference_video"` | `referenceVideoUrl` | |
| `audio_url` + `role:"reference_audio"` | `referenceAudioUrls` | |

`image_url.url` 支持 **base64 data URI** —— 适配层会解码后转存到站点 CDN（站点只认自己 CDN 的图）。

**参数映射与降级**：

| Ark 参数 | 处理 |
| --- | --- |
| `resolution` 480p/720p/1080p | 同名透传 |
| `ratio`（含 `adaptive`） | → `aspectRatio`；`adaptive` 表示跟随源素材，站点本来就这样，故不设值让它自推 |
| `duration` 4–30 或 `-1` | `-1` 视为 5s；超出站点档位会就近吸附并**在 `warnings` 里说明** |
| `frames` | 按 24fps 换算成秒（Ark 允许用 frames 代替 duration） |
| `model` | `doubao-seedance-2-5-*`、`2-0-*`、`1-0-pro*` → tier `base`；`1-0-lite*` → `turbo`；可用 `extra_body.aivideomaker_tier` 覆盖 |

**上游不支持的参数**不会静默丢弃，而是列在响应的 `unsupported[]` 里：
`watermark`、`generate_audio`、`seed`、`camera_fixed`、`return_last_frame`、`draft`、
`service_tier`、`priority`、`callback_url`、`safety_identifier`、`tools`、
`omni_reference_task_type`、`execution_expires_after`、`output_format`（共 14 个）。

`reference_video` / `reference_audio` 会转成 `referenceVideoUrl` / `referenceAudioUrls` 透传，
但**上游是否真的支持未经验证**，响应里会给出对应 warning。

**两个 `extra_body` 扩展**：

| 键 | 作用 |
| --- | --- |
| `aivideomaker_dry_run: true` | **只校验不提交**。走完完整翻译 + 图片转存后直接返回 `{effective, warnings, unsupported, upstream_payload}`，零额度消耗。用于调试请求体。 |
| `aivideomaker_prefer_free: true` | 时长吸附时优先落在**免费区**（≤8s）而不是数值最近的档位 |
| `aivideomaker_tier: "turbo"\|"base"` | 显式指定档位，覆盖 model 名默认值 |

```bash
# 零成本校验一个请求体
curl -X POST http://localhost:8788/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer sk-avm-demo' -H 'content-type: application/json' \
  -d '{"model":"doubao-seedance-2-5-260628","content":[{"type":"text","text":"..."}],
       "resolution":"480p","duration":8,
       "extra_body":{"aivideomaker_dry_run":true}}'
# -> {"dry_run":true,"ok":true,"effective":{...,"billed":false},"warnings":[],...}
```

> **注意时长吸附可能跨进计费区**：480p 只接受 5/10/15/20，所以 `duration:8` 会就近吸到 **10s**
> （计费），而 `duration:6` 吸到 5s（免费）。加 `prefer_free:true` 可以让 8/9/12s 都落到 5s。
> 响应里 `effective.billed` 会预告是否计费，`warnings` 也会明确提示这个"跨档"。

响应里另有三个扩展字段便于排障：`requested`（调用方原始请求）、`effective`（实际生效值）、
`warnings`（降级说明）、`aivideomaker`（上游原始任务记录）。

> `GET /api/v3/contents/generations/tasks`（列表）官方文档页是 JS 渲染的，**返回信封未能核实**，
> 目前返回 `{items, total, page_num, page_size}` 的尽力实现，只覆盖本进程内创建过的任务。

### 请求体全字段测试

`node ark-body-test.mjs` —— 64 个用例覆盖全部字段（必填校验、7 种 ratio、3 种分辨率、
content 的 9 种角色组合、duration/frames 吸附、14 个不支持参数、tier 映射、base64 图片），
**全部走 dry_run，零额度消耗**。

### 参数映射

| 客户端字段 | 上游字段 |
| --- | --- |
| `prompt` / `input` | `content` |
| `first_frame_image` / `image` / `imageUrl` | `imageUrl` |
| `last_frame_image` / `lastFrameUrl` | `lastFrameUrl` |
| `subject_reference[].image[]` / `reference_image_urls` | `referenceImageUrls` |
| `duration` / `seconds`（按分辨率吸附，见下） | `duration` |
| `resolution`（`1080P`→`1080p`，也支持 `size:"1920x1080"`） | `resolution` |
| `aspect_ratio` / `size` | `aspectRatio` |
| `prompt_optimizer` | `promptEnrichment` |
| model → `minimax-h3*` = `turbo`，`*-pro|base|hailuo-02` = `base` | `tier`（**上游 zod 只收 `turbo`\|`base`**） |

`captcha_token` / `turnstile_token` / `token` 透传，或用 `AVM_TURNSTILE_TOKEN`。

### 时长规则（两层校验，别一刀切）

zod 层放行了 5–20 的任意数字，但服务端还会**按分辨率再查一次**：

```
480p  duration=6  ->  INTERNAL_SERVER_ERROR "480p supports 5s, 10s, 15s, or 20s duration."
720p  duration=6  ->  接受（实测生成成功）
```

| 分辨率 | 允许的时长 | 依据 |
| --- | --- | --- |
| 480p | 5 / 10 / 15 / 20 | 实测 6、7 被拒 |
| 720p | 5–20 任意整数 | 实测 6 通过 |
| 1080p | 5 / 10 | 仅这两个实测通过，15/20 未验证 |

适配层按上表就近吸附。1080p 的 15/20 属保守处理——未验证前不主动发。

## 计费规则（重要，30+ 条实测）

任务记录里的 `paid` 字段表示**这次生成是否计费**。规则只有两条：

| 条件 | 结果 |
| --- | --- |
| `tier = "base"` | **一律计费**（与分辨率、时长无关） |
| `tier = "turbo"` 且 `duration ≤ 8s` | **免费** |
| `tier = "turbo"` 且 `duration ≥ 9s` | 计费 |

按时长统计（免费 / 计费）：5s 13/3、6s 3/0、7s 1/0、8s 5/0、9s 0/1、10s 0/3、11s 0/1、14s 0/2。
那 3 条"计费的 5s"全部来自 `tier=base` 调用。

> `credits` 字段与 `paid` 不是一回事：`paid=false` 记 `credits=1`，`paid=true` 记 `credits=0`。
> **看 `paid` 判断是否花钱，别看 `credits`。**

**因此适配层所有 model 名默认映射到 `turbo`**，`base` 必须显式要求：

```bash
# 显式要求 base（会计费）
-d '{"model":"minimax-h3","tier":"base", ...}'
# Ark 协议用 extra_body
-d '{"model":"doubao-seedance-2-5-260628","extra_body":{"aivideomaker_tier":"base"}, ...}'
```

Ark 响应里 `effective.billed` 会预告本次是否计费，`warnings` 也会提示。

## 删除任务记录

`model.deleteModel`，入参是 **`{ids: string[]}`**（不是 `id`），成功返回 `null`：

```bash
AVM_COOKIE='...' node -e "import('./client.mjs').then(async m => {
  const c = new m.AvmClient({cookie: process.env.AVM_COOKIE});
  console.log(await c.deleteTasks(['zkavkvfj13ak957']));
})"
```

**只是删记录，不能取消运行中的任务**——站点仍无取消端点，跑着的任务照跑照扣。

## session cookie 的有效期

**结论先说：`auth_session` 的过期时间从外部读不到，而且站点不会刷新它。**

实测证据：

| 检查项 | 结果 |
| --- | --- |
| token 形态 | 44 字符不透明随机串（`n557e2xduz5zxjtwlqzoh77qqkhe4uu4hlee3bir`），**不是 JWT，不含时间戳** |
| 普通 API 请求（`/api/auth.user`） | 响应**无** `Set-Cookie` —— 不刷新 |
| 页面请求（`/zh/generations`） | 只下发 `NEXT_LOCALE=zh; Max-Age=31536000`（1 年），**不下发 `auth_session`** —— 不刷新 |
| `/api/auth/session`、`/api/auth/csrf`、`/api/auth/providers` | 全部 **404**（非标准 Auth.js 部署） |
| `auth.session` / `auth.getSession` / `auth.me` 等 9 个 tRPC 过程 | 全部 **NOT_FOUND** |

因此：

1. **无法从 token 解析过期时间**（不透明随机串）。
2. **无法从接口查询**（无 session 端点）。
3. **站点不做滚动续期** —— 过期时间在登录那一刻就固定了，用多久都不会延长。
4. 只能**实测**：本会话已确认连续可用 ≥14.6 小时（2026-09-10T04:00Z → 18:39Z），上限未知。

**推断（未证实）**：token 形态与 `auth_session` 这个命名符合 next-auth 的数据库会话模式，
其默认 `session.maxAge` 是 **30 天**。若登录发生在订阅开始时（2026-09-10T03:50Z），
则预计约 **2026-10-10** 失效。**这是推断，不是实测。**

**想拿到确切数字**：下次登录时用 DevTools → Network → 找设置 `auth_session` 的那个响应 →
Response Headers → `Set-Cookie`，里面的 `Expires=` / `Max-Age=` 就是精确 TTL。

**监控**：

```bash
AVM_COOKIE='...' node check-session.mjs
```

校验会话并把 `first_seen` / `last_ok` 记到 `.session-state.json`，失效时退出码非 0，
可直接挂到定时任务里做告警。换新 cookie 时指纹变化会自动重置计时。

> 给自动化的建议：**不要假设它能活多久**。用 `check-session.mjs` 定期探活，
> 失效即告警；并预留"人工重新导出 cookie"的流程。

## 上游并发闸门与提交队列（submit-queue.mjs）

**上游同一时间只跑 1–2 个任务**（premium = 2）：

```
INTERNAL_SERVER_ERROR  The queue is full. The premium plan can only run 2 task at a time.
```

关键点：**槽位是从创建一直占用到任务进入终态的**，不是"创建请求返回就释放"。所以直连转发在第三个请求就会开始报错。

`submit-queue.mjs` 做了三件事：

1. **并发闸门**：同时最多 `maxConcurrent` 个任务在上游（默认 2）。
2. **延迟执行**：超出的请求**排队等待**，而不是直接失败；有空槽立刻放行。
3. **回退重试**：若上游仍报 `queue is full`（比如浏览器里也在生成），**明确打印错误**并按线性退避重试，超过上限才拒绝。

日志示例：

```
[adapter] [queue] upstream concurrency is capped at 2; 3 request(s) delayed until a slot frees
[adapter] [queue] UPSTREAM FULL (attempt 1/8) — delaying 20s then retrying. upstream said: ...queue is full...
[adapter] [queue] slot released (1a2b3c -> succeed) running=1 queued=2
[adapter] [queue] giving up after 8 attempts — upstream still full: ...
```

实时观测：`GET /queue`（也内嵌在 `/healthz` 的 `submit_queue` 字段）

```json
{ "max_concurrent": 2, "running": 1, "queued": 3, "running_tasks": ["1a2b3c"],
  "served_total": 12, "delayed_total": 4, "rejected_total": 0, "retry_captcha": false }
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AVM_MAX_CONCURRENT` | `2` | 上游并发上限（premium 是 2） |
| `AVM_QUEUE_MAX_ATTEMPTS` | `8` | 队列满时最多重试几次 |
| `AVM_QUEUE_BASE_DELAY_MS` | `20000` | 退避基数（线性递增，上限 120s） |
| `AVM_RETRY_CAPTCHA` | 关 | 是否也把验证码闸门当作"稍后重试"。默认**关闭**——等待可能长达数分钟且之后会真的计费，应当由调用方决定 |

单测（零消耗，用 mock 上游）：`node queue-test.mjs`

---

## 媒体上传（图片 / 视频 / 音频）

`参考素材` 模式需要视频和音频，不只是图片。上传限额按类型分档（实测 `maxBytes`）：

| 类型 | 上限 |
| --- | --- |
| 图片 | 10 MB |
| **视频** | **50 MB** |
| **音频** | **15 MB** |

```bash
AVM_COOKIE='...' node upload.mjs <本地路径或URL> [--permanent]
```

`AvmClient.uploadFile()` 按 magic bytes 识别 PNG/JPEG/WebP/MP4/MOV/WEBM/MP3/AAC/WAV/OGG，
不信扩展名（踩过 `.jpg` 实为 PNG、Content-Type 写 `image/jpg` 的坑）。

适配层会自动转存：`imageUrl` / `lastFrameUrl` → 图片通道；
`referenceVideoUrl` / `referenceAudioUrls` → 视频/音频通道。

---

## 通用零成本校验（dry_run）

加请求头 `X-Avm-Dry-Run: 1`，**三个协议都支持**：跑完完整翻译 + 媒体转存后直接返回 payload，
不提交、不消耗额度。

```bash
curl -X POST http://localhost:8788/v1/video_generation \
  -H 'Authorization: Bearer sk-avm-demo' -H 'content-type: application/json' \
  -H 'X-Avm-Dry-Run: 1' \
  -d '{"content":"a car","imageUrl":"https://static.img2video.ai/...jpg","duration":5,"resolution":"480p"}'
# -> {"dry_run":true,"ok":true,"upstream_payload":{...}}
```

Ark 协议也可用 `extra_body.aivideomaker_dry_run: true`。

> **调适配层一律先 dry_run。** 这是把"验证翻译层"和"花钱生成"解耦的唯一手段。

---

## 图生视频（i2v）

两个必须知道的坑，都是实测踩出来的：

### 1. 外链图片会被拒：`Unsupported upload content type`

站点**只收自己 CDN 的图**。直接传外链 URL 时，服务端会去拉并校验 `Content-Type`，
不在白名单里就报 `BAD_REQUEST: Unsupported upload content type`。

真实案例：`https://s3ai.cn/.../xxx.jpg` 返回 `Content-Type: image/jpg`
——**这不是标准 MIME**（正确的是 `image/jpeg`），于是被拒。更坑的是这个文件
**实际是 PNG**（扩展名和 Content-Type 都是错的）。

解法：先转存到站点 CDN。上传接口在站点自己的 JS bundle 里（注意路由名是复数 `uploads`）：

```js
const { uploadUrl, headers, publicUrl, maxBytes } =
  await trpc.uploads.getPresignedUrl.mutate({ fileName, contentType, fileSize, permanent });
await fetch(uploadUrl, { method: 'PUT', headers, body });
// publicUrl 形如 https://static.img2video.ai/<ts>-<uuid>-<name>.png
```

- `maxBytes` = **10 MB**（10485760）
- `contentType` 必须按**真实字节**填（用 magic bytes 嗅探，别信扩展名）
- 预签名 URL 只有 **60 秒**有效期，拿到就传

命令行：`AVM_COOKIE='...' node upload.mjs <本地路径或URL> [--permanent]`

**适配层已内置自动转存**：`imageUrl` / `first_frame_image` 只要不是 `static*.img2video.ai`
就会自动转存。加 `"no_rehost": true` 可关闭。

### 2. 出片比例跟随原图，不跟请求字段

| 原图 | 请求 aspectRatio | 实际成片 |
| --- | --- | --- |
| 800×1200（2:3） | `16:9` | **480×704**（≈2:3） |
| 2560×1440（16:9） | 自动推导 `16:9` | 864×480 / 1248×704（≈16:9） |

所以别手工填比例。**适配层在未显式传 `aspect_ratio`/`size` 时会读图自动推导。**

实测（`2560×1440` PNG，提示词「镜头缓慢向前推进」）：

- 480p / 5s → 864×480，1.07MB，**126 秒**，1 credit
- 768P（站点映射到 704）/ 5s → 1248×704，1.74MB，**167 秒**
- `duration:6` 在 480p 下就近吸附为 `5`（720p 则保留 6）

## 动态验证码闸门

| 提交的 token | `needsCaptcha=false` | `needsCaptcha=true` |
| --- | --- | --- |
| `null` | ✅ 返回 taskId | ❌ 返回 `""`（静默拒绝，不报错） |
| 假字符串 | — | ❌ 返回 `""` |
| 真实 Turnstile token | — | ✅ 通过 |

- **每次提交都重新问一次 `needsCaptcha`，不要缓存。**
- 闸门开启时：OpenAI 侧返回 HTTP 403 `permission_error`，MiniMax 侧返回 `base_resp.status_code: 1002`，两者 message 一致且可诊断。
- 无头破解走不通（自起 Chrome 注入真 cookie 后 Turnstile 挂死）。可用路径：BYO token、等闸门衰减、或走官方 API。

## 读取任务状态的两种姿势

| 方式 | 请求数 | 坑 |
| --- | --- | --- |
| `model.listModel` | 1 | 只含本账号任务，新任务可能晚几秒出现 |
| `model-status` SSE | 2（先换令牌再流） | **上游未接单时不发帧也不断开 → 会挂死**；`/token` 端点高频调用会 429 |

`client.mjs` 默认走 `listModel`，拿不到才回落 SSE，并对 SSE 加 12s 超时。

## 实测结果

- 创建 → 出片：**约 60–140 秒**（5s 480p）；1080p/10s 约 6 分钟
- 端到端（含下载）：142 秒，产出 566KB 有效 MP4
- `needsCaptcha` 初始 `false`，约 7 次生成后翻转为 `true`
- 样例成品：`sample-output.mp4`

## 已知限制

- **取消任务：站点没有暴露端点。** 探测过 `model-status/cancel|delete|abort`、`ai.cancelTask|cancel|deleteTask|delete|remove`、`task.cancel`、`video.delete` 等 13 个候选，全部 404。（`POST /v1/responses/:id/cancel` 因此返回 400。）
  注意：**删记录**是有接口的（`model.deleteModel`，见上），但删记录 ≠ 取消生成。
- **账号要验证码时此路不通**（且这是动态的，见上）。可用 BYO token，或等闸门衰减。
- **`model-status/token` 会 429。** 高频轮询别走 SSE 路径；`/api/model-status` 对未接单任务还会挂死，已加超时。
- **响应记录registry在内存里**，重启后 `resp_xxx` 失效（MiniMax 侧无状态，不受影响）。
- **session 会过期**，cookie 失效后重新导一份。
- 别把反代暴露公网，`cookies.json` 里有真实 `auth_session`；建议设 `AVM_GATE_KEY`。

## 文件

```
avm-proxy/
├── adapter.mjs            # 协议适配层：OpenAI /v1/responses + MiniMax 官方接口
├── proxy.mjs              # 简单 REST 入口
├── client.mjs             # 完整 API 客户端（tRPC + 上传 + SSE + 轮询 + 下载）
├── upload.mjs             # 图片转存到站点 CDN（修 i2v 的 content-type 拒绝）
├── cookies.json           # 你的真实会话（别外传）
├── cookies.example.json   # 样例
├── sample-output.mp4      # 文生视频样例
├── i2v-jump.mp4           # 图生样例：800×1200 竖图 → 480×704
├── i2v-newimage.mp4       # 图生样例：2560×1440 → 864×480
├── i2v-newimage-768p.mp4  # 图生样例：2560×1440 → 1248×704
└── README.md
```
