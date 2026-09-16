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
| **计费** | `tier=base` 计费；`turbo` 且 ≤10s **免费** |
| 预算保护 | 无（靠免费窗口兜底） |
| **取消任务** | ❌ **没有端点**（跑着的照跑照扣，`DELETE` 只删本地记录） |
| 验证码 | 动态闸门（按速率翻转，**不是账号属性**） |
| 并发 | **按账号额度**：premium 2 / pro 4（`ai.queryUserPermission.maxQueueLength`）；`AVM_MAX_CONCURRENT=0` 即自动、读不到回落 2。槽位占到底 |
| 媒体输入 | **必须转存到站点 CDN**（适配层已自动做） |
| 幂等 | 无 |

## 快速开始

```bash
pip install -r requirements.txt

export AVM_COOKIE="auth_session=…" # 浏览器导出，TTL 约 400 天（或改用透传由调用方自带，见下）
export AVM_AUTH=key:sk-local       # 鉴权：一个变量三选一（留空 = 不校验）

python3 src/ark_server.py --port 8808
```

启动时会自述可用性与计费口径：

```
[ark-compat] Ark SDK base_url : http://127.0.0.1:8808/api/v3
[ark-compat] available lines  : web
[ark-compat] billing          : web upstream: tier=base is always billed; tier=turbo is free up to 10s
[ark-compat] upstream host    : https://aivideomaker.ai
[ark-compat] auth mode        : gate
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

## 鉴权：**一个变量** `AVM_AUTH`，三选一

| `AVM_AUTH` | 含义 | 上游凭据来自 |
|---|---|---|
| 留空（或 `open`） | 不校验（本机自用） | 本进程的 `AVM_COOKIE` |
| `passthrough` | 调用方的 `Authorization: Bearer` **就是上游凭据本身**（网页会话 cookie） | 调用方（本进程不再需要 `AVM_COOKIE`） |
| `key:<密钥>` | 闸门：Bearer 必须等于 `<密钥>` | 本进程的 `AVM_COOKIE` |

`passthrough` 即"没有服务自己的账号"这回事：每个调用方带自己的会话，各自享自己账号的
免费窗口。取值写法**刻意不猜** —— 裸密钥不算闸门（必须写 `key:` 前缀），
`key:` 空密钥、`gate:x` 这类近似写法一律**拒绝启动**并列出可选写法。

```bash
# 每凭据一账号：调用方自带会话
export AVM_AUTH=passthrough
python3 src/ark_server.py --port 8808

curl -X POST http://127.0.0.1:8808/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <40 位 auth_session 值>' \
  -H 'content-type: application/json' -H 'X-Avm-Dry-Run: 1' -d '…'
```

凭据形态很宽（裸 token / `auth_session=…` / 完整 Cookie 串 / cookie jar JSON 都认），
规范化规则见 `cookie.py`，与 JS 侧由同一份用例锁定。

三条**必须知道**的约束：

1. **闸门与透传互斥，且单变量让那个组合无法表达。** 同一个 Bearer 不可能既是闸门密钥
   又是上游凭据；旧版用两个变量（`AVM_GATE_KEY` + `AVM_PASSTHROUGH_COOKIE`）表达同设时
   每个请求都 401，而现象看起来像"调用方凭据错了"。那两个变量**已废弃**：只要还有行为
   （闸门非空 / 透传不是显式的关值），启动就被拒绝并打印迁移映射 —— 不静默放过。
2. **透传按凭据隔离（owner 维度）。** 任务表多了 `owner` 维度（凭据的 sha256 前 16 位，
   **不落凭据原文**），`GET /tasks`、`GET /tasks/{id}` 全按归属过滤，别人的任务一律 404
   （连"存在"都不透露）。切换开关前建的旧记录没有归属，在透传模式下查不到 ——
   保留窗口只有 7 天，很快自然淘汰。
   ⚠️ 这是**防御性隔离**，不是"多租户网关"定位：本项目语义是**账号级**（一个实例 =
   一个账号）。多账号请部署多实例；单实例透传多凭据仅适合账号很少的临时场合。
3. **每凭据一个上游客户端 + 一把并发闸门**，缓存上限 64 条，淘汰**只关空闲的**
   （关掉正在被轮询的客户端＝那次查询直接失败，现象是"任务查不到"）。
   上游"同时只跑 2 个"是**按账号**的限制 ⇒ 不同 cookie 各 2 槽是正确的；
   同一 cookie 的槽位仍是进程内状态（多 worker 会翻倍，理由见部署章节）。

**任务查询的凭据绑定（task_id ↔ api-key，E2E-AVM-011）**：透传的鉴权前置在
newapi —— 适配层自己**不再要求轮询方重带凭据**。创建任务时把当时的凭据绑定到
这条任务上（sqlite `credential` 列），之后：

- `GET /tasks/{id}` **不带凭据也能查** —— 按绑定解析上游凭据（这正是 newapi
  轮询需要的形态；E2E-AVM-011 的阻塞项就是"没带 Bearer ⇒ 401"）；
- 带了凭据则必须与任务归属一致，否则 404（不变）；
- 列表 `GET /tasks` 是跨任务的租户视图，**仍然必须带凭据**（否则 401）——
  没法界定"这是谁的列表"时宁可拒绝，也不能把 B 的任务泄给 A；
- 没有绑定的旧记录（机制上线前创建）且未带凭据 ⇒ 明确的 401 提示，而不是
  被静默当成"任务不存在"。

⚠️ **取舍必须知情**：sqlite 任务库从此存有**会话凭据原文**（等价于 newapi 把渠道
key 落库这件事）。防线：文件权限自动收到 `0600`、凭据绝不进日志 / span / 响应体、
删除与保留窗口清理时随记录一起删除。`owner` 指纹照旧不变 —— owner 管**租户隔离**，
credential 管**上游解析**，两者用途不同、互不替代。

`/healthz` 会把这件事说清楚：`auth`（当前模式）、`credentials_from_caller`、
`passthrough_cookie`、`gate`，以及 `available_upstreams` —— 透传线的客户端要等凭据才
存在，但**能力**照样如实上报（否则会显示成"没配任何上游"）。透传模式下 `?deep=1`
没带凭据**不会** 401，而是在 `upstream_probe` 里写明原因。

`AVM_AUTH=passthrough` 时，调用方的 `Authorization: Bearer` 就是**网页会话 cookie
本身**（裸 token 或完整 Cookie 串都行），本进程不再需要 `AVM_COOKIE`；
任务表按**凭据指纹**隔离，A 看不到 B 的任务。

⚠️ 闸门与透传互斥（同一个 Bearer 不可能既是闸门密钥又是上游凭据）：旧版这是两个变量
（`AVM_GATE_KEY` + `AVM_PASSTHROUGH_COOKIE`），同设会让每个请求都在闸门处 401，只能靠
`Settings.validate()` 拦。现在收成**一个变量**（`AVM_AUTH` 三选一），那种组合
**根本无法表达**；旧变量只要还有行为，启动即被拒并打印迁移映射。

## 上游选线（已移除）

早期这里同时支持一条 `official` 上游（aivideomaker 官方 `/api/v1/*`，`key` 头），
可按 `AVM_UPSTREAM` / `X-Avm-Upstream` / `?upstream=` 切换。**该上游与选线机制已整体
移除**，本项目只对接 web 线，且**不保留任何兼容层**：

- `AVM_UPSTREAM` 不再被读取（`Settings` 里没有这个字段，设了也无效）；
- `X-Avm-Upstream` / `?upstream=` 不再被解析 —— 发了也一律走 web 线，不报错。

## 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v3/contents/generations/tasks` | 创建任务（透传下把凭据绑定到这条任务上） |
| GET | `/api/v3/contents/generations/tasks` | 列表（**仅本凭据创建过的**；透传下必须带凭据，否则 401） |
| GET | `/api/v3/contents/generations/tasks/{id}` | 查询（实时回上游取；透传下**可不带凭据**，按创建时绑定的凭据取；TTL 缓存节流见下） |
| POST | `/v1/videos` | 创建任务（**OpenAI v1/videos 形状**，见下节） |
| GET | `/v1/videos/{id}` | 查询（OpenAI v1/videos 形状） |
| GET | `{AVM_PUBLIC_PATH}/{name}` | **成片下载出口**（默认 `/v/{ark_id}.mp4`，见下节）。未配置 `AVM_PUBLIC_BASE` 时**不存在** |
| GET | `/healthz` | 存活 + 上游可用性与计费口径；`?deep=1` 额外查上游 |

### `GET /api/v3/contents/generations/tasks/{id}` 的响应体

**只含必要字段，且只含有真实值的字段**（2026-09-15 两轮口径：先对齐官方
[查询视频生成任务](https://www.volcengine.com/docs/82379/1521309) 的「响应参数」表，
再收窄到"不要 `model` / `resolution`，值不确定或取不到的一律不给"）：

```json
{"id": "cgt-…", "status": "succeeded", "error": null,
 "content": {"video_url": "https://…/x.mp4"},
 "duration": 5, "ratio": "16:9", "created_at": 1789477818, "updated_at": 1789477959}
```

- **白名单**：`id` · `status` · `error` · `content{video_url[,last_frame_url]}` ·
  `duration` · `ratio` · `created_at` · `updated_at`（`translate.ARK_TASK_FIELDS`）。
  官方 SDK（Java/Go）对 unknown field 是**报错**而非忽略 ⇒ 多一个键就是坏一个客户端。
- **没有值就不给键**，唯一例外是 `error`（官方规定成功时显式返回 `null`，那是**确定**的
  信息，不是"不知道"）。任务还在跑时，响应可能就只有 `{"status": "queued", "error": null}`
  —— 那是**正确**的，不是字段漏了。
- 🔴 `duration` 是 **integer**。站点任务记录里它是**字符串** `"5"` —— 原样透传会让官方
  Java/Go SDK 在反序列化时**抛类型错误**（这就是 2026-09-15 报告"跟原生接口对不上"的元凶）。
- **不给编造值**。`seed:-1` / `framespersecond:24` / `service_tier:"default"` /
  `execution_expires_after:172800` / `draft:false` / `usage.completion_tokens:0` 这些
  曾经让响应"看起来很完整"的常量，已从归一化层**移除** —— 编一个合理的值比不给更糟：
  调用方会把它当成事实。
- `status` 覆盖官方 6 态，含 **`expired`**（任务超时 —— 与"还在排队"是相反的两个结论，
  从前它会落到 `queued`）。

**证据去哪了**：上游实际执行的模型、原始站点记录、实际产出档位（`resolution`）、创建时的
`warnings[]` / `unsupported[]`、计费 `paid` —— 一律**不进响应体**，只走 logfire 的
`ark.task.fetch` span（属性见下）。**想零成本看校验结论请用 dry-run**，
不要指望在查询响应里读到它们。

门禁：[`tests/test_ark_task_schema.py`](../../tests/test_ark_task_schema.py) —— 两个字段集
（官方全集 / 我们的收窄白名单）都**硬编码**在测试里，不引用实现的常量；否则把白名单改宽时
"多出字段"那条断言会跟着放水（自引用门禁不可证伪）。

## 成片对外出口：不暴露上游域名与模型名

上游返回的成片是一个**公开直链**，实测形态（2026-09-16 取证）：

```
https://static2.img2video.ai/1789479777409-…-1635002_0_minimax_h3_1635002.mp4
         └─── 域名：上游服务商 ───┘                └─ 上游**实际执行**的模型名 ─┘
```

原样透传等于一次泄露**三**样东西，第三条最隐蔽：

| # | 泄露面 | 位置 | 备注 |
|---|---|---|---|
| ① | 上游域名 | URL | 直接指向上游服务商 |
| ② | 上游实际模型名 | URL 的文件名 | 站点对调用方请求的模型名只是**回显**（见 `translate.normalize_web_task`） |
| ③ | 同一个模型名 | 🔴 **响应头 `content-disposition`** | 上游回的是 `attachment; filename="…_0_minimax_h3_….mp4"` —— **只把 URL 换掉挡不住它**：只要还在转发上游响应头，每一次下载都会把模型名发出去 |

上游还回 `server: cloudflare` / `cf-ray: …` / `nel` / `report-to` —— 这些同样能指认上游。

配上 `AVM_PUBLIC_BASE` 之后：

- `GET /tasks/{id}` 的 `content.video_url` 变成
  `{AVM_PUBLIC_BASE}/v/cgt-20260916-a1b2c3d4.mp4` —— 路径里只有**本服务的任务 id**；
- 取用（`GET /v/{name}`）时**才**回源上游，流式转发、不落盘 —— 没被取用的成片不占存储；
- 响应头走**白名单**（`media_proxy._PASS_THROUGH`）：`content-disposition` 由我们重写，
  `server` / `cf-ray` / `nel` 一律不透传。白名单而非黑名单 —— 上游以后新增什么头都不会
  顺着这条管子漏出去（黑名单会静默失守）。

### 下载端点的语义

| 情形 | 状态码 | 说明 |
|---|---|---|
| 正常 | `200` / `206` | `Range` 原样透传（播放器会发它） |
| 任务不存在 / 不属于本凭据 | `404` | 与任务查询同一套归属校验；刻意**不用 403** —— 那会透露"这条任务存在" |
| 任务在、但还没出片 | `409` | 让调用方明白"不是没有，是还没好"，可以重试 |
| 回源失败 / 上游 4xx-5xx | `502` / `404` | 报文只有一句通用描述，**不带**上游原文与主机名 |

### 为什么不加密拼接 token

脱敏的要害是"URL 里不含上游信息"，不透明 id 已经做到。加密要额外付三笔成本 ——
**不可撤销**（除非轮换密钥，那会作废全部已发链接）、**无法与鉴权绑定**、**密钥即全局命门**
（泄漏即全量泄漏）。它唯一换来的"无状态"在本服务已有一张任务表（`store.py`，7 天保留窗口）
时没有价值。将来若真要抽成**不能持久化**的独立服务，才需要回到 token 方案。

🔴 **绝不提供"传任意 URL 给我换一个链接"的接口** —— 那等于开放代理 + SSRF，还白送别人
用你的带宽与出口 IP。下载地址只能由服务**自己**在观测到任务成功时给出（`app._apply_media_gate`），
调用方无法指定源地址。

### 对调用方的破坏性变更

一旦配置 `AVM_PUBLIC_BASE`，**对外返回的成片地址就换了主机**。已按上游域名写过逻辑
（域名白名单、签名校验、拼接下载）的调用方要跟着改。留空则不启用，行为与从前逐字节一致。

门禁：[`tests/test_media_proxy.py`](../../tests/test_media_proxy.py) —— 覆盖响应头白名单、
"响应体零上游痕迹"、归属校验、`404/409/502` 语义，并做了**五条变异自证**：把
`content-disposition` 改回透传、把 `cf-ray` 塞进白名单、取消扩展名白名单、出口不换址、
下载端点去掉归属校验 —— 每一条都会让测试变红（门禁可被证伪，否则等于没有）。

### 交互式文档

| 路径 | 说明 |
|---|---|
| `/docs` | Swagger UI（可点 Authorize 填 Bearer，直接试发请求） |
| `/redoc` | ReDoc（只读、按标签分组，适合通读字段域） |
| `/openapi.json` | OpenAPI 3.1 schema 本体 |

> 历史：`app.py` 里曾长期是 `docs_url=None, redoc_url=None`（从首次提交 `c9777e2` 起，
> 且没留理由），所以 `/docs` 一直是 **404** —— 而"服务没坏、是文档被关掉"这件事从外部
> 看不出来（`/openapi.json` 反而活着）。现已恢复，并由
> [`tests/test_api_docs_enabled.py`](../../tests/test_api_docs_enabled.py) 钉住。

🔴 这三条是**公开**端点：本服务的鉴权是**逐路由** `Depends(require_bearer)`，作用不到
FastAPI 自动注册的文档路由上 —— 即使在闸门模式（`AVM_AUTH=key:…`）下也**不需要**凭据。
要保护它们得额外加中间件，别误以为"挂了闸门就顺带挡住了文档"。

> **2026-09-16 决定：这三条保持公开，不在应用层加鉴权。** 理由很直接 —— 文档的价值就在
> "不带凭据也能打开"，在应用层加门会把最常用的调试路径一起挡住，而它挡不住真正想看的人
> （那类人本来就有凭据）。**但"公开"意味着可见范围必须由部署层决定**：实例要对外时，
> 在反向代理上处置 `/docs` `/redoc` `/openapi.json`（限来源网段 / 加 Basic Auth / 直接 404）。
> 这是**部署决策**，代码侧不替你做，也不会代你判断"这个实例算不算对外"。
> 该口径由 livetest 报告 `E2E-AVM-015` 提出（它建议"结合自身鉴权策略评估是否需要收"）。

🔴 页面里的 JS/CSS 由**浏览器**从 `cdn.jsdelivr.net` 取（ReDoc 还额外拉 Google Fonts 字体）。
服务端一切正常、`/docs` 明明返回 200，浏览器到不了该 CDN 时**照样白屏**，
而**日志里没有任何线索** —— 别据此判成"路由没生效"或"服务坏了"。要彻底消除这个外部
依赖只能自托管静态资源。

## OpenAI `/v1/videos` 兼容面

把同一套管线再包一层 OpenAI Videos 形状，对齐 Chatfire「OpenaiVideos格式 /
Seedance」的两份 OpenAPI（[创建](https://oneapis.apifox.cn/369966278e0) /
[查询](https://oneapis.apifox.cn/369966279e0)）—— **响应只含契约声明的字段**，
调用方按 Chatfire 文档写的解析代码原样可用：

| 调用 | 响应（恰好这些字段） |
|---|---|
| `POST /v1/videos` | `{"id", "object": "video", "status": "queued", "created_at"}` |
| `GET /v1/videos/{id}` | `{"id", "object": "video", "status", "progress", "video_url", "created_at"}` |

请求字段映射：`model` 原样保留（`_480p/_720p/_1080p` 后缀决定分辨率档位）；
`prompt` → 提示词；`seconds` → 时长；`size` → 宽高比（`keep_ratio`/`adaptive`
上游语义相同 = 跟随输入图，映射会留痕）；`first_frame_image` / `last_frame_image` → 首/尾帧
（**只收单值**：给成数组一律 400 —— 参考素材静默丢失是本项目的红线）；
`input_reference` → **参考图列表**（≤4 张，超限截断并**点名丢掉哪一张**），三种形态都收：

  - JSON body 给数组：`"input_reference": ["https://…", "https://…"]`
  - 表单**重复部件**：`--form input_reference=@a.png`（**文件流**）或
    `--form input_reference=<URL>`；**两者可混用**，按出现顺序排
  - 表单里把数组写成字符串：`--form 'input_reference=["u1","u2"]'` ⇒ 会**摊平 + 留痕**
    ⚠️ 2026-09-15 修：以前它会变成一个**垃圾 URL**（既不是 URL 也不是 data URI）且**完全
    静默**，一路撑到转存阶段才炸；看着像数组但不是合法 JSON ⇒ 直接 400
🔴 **`input_reference` 与帧字段互斥**（"首帧/首尾帧" vs "参考素材"是上游的两种模式）⇒ 混用
   由 `translate_create` 前置 **400**，报文写明原因。
🔴 调用方带的**渠道选择头**（如 `x-base-url: volc`）本服务无法满足 —— 本服务只有**一条**
   web 逆向线、不按 base_url 路由。**不路由，但必须留痕**（进 `warnings` → trace + 日志）：
   静默按 web 线服务等于**悄悄换掉上游**（免费窗口 vs 按量、排队、质量都不同）。
   OpenAI 面的响应体只有四个字段，所以这条提示只在 trace 里可见 —— 刻意不为一句提示破坏契约。
表单（multipart / urlencoded，Chatfire 的 curl 形态）与 JSON（OpenAI SDK 形态）
都收；表单文件部件按 magic bytes 定 MIME 转内联，真实提交时照常转存站点 CDN。
🔴 **表单上传的两道体积闸**（2026-09-15 补）：**单件 50MB**（与
`WebClient.MEDIA_HARD_MAX_BYTES` 同值，由门禁钉住）与**整个请求体 128MB**
（= 站点合法的最坏组合：图 4×10 + 视频 50 + 音频 2×15 = 120MB，加一点余量）。
超限一律 **400**，且报文里**点名**是"单件上限"还是"请求体"（两条闸各测各的，否则删一条
靠另一条兜住也会绿）。为什么必须有：`.form()` 会把 >1MB 的部件**落盘**、`read()` 再**整个**
读进内存、然后 base64（+33%）—— 没有闸时一个超大件就是"先写满磁盘 → 吃掉几倍内存 →
最后才被站点 `maxBytes` 拒掉"。
⚠️ 这是「**整包读入**」而不是「流式上传」：请求体先落进内存/临时文件、再包成 data URI，
  交给转存管线；真要流式得等上游支持分片（见 `OPEN-DECISIONS`）。所以入口体积闸是必需的。
status 词表：`queued` / `in_progress` / `completed` / `failed`；`progress` 只在
终态成功时是 100（上游没有可读百分比，不编造中间值）；`video_url` 仅 `completed`
且有产出时给值。鉴权、计费口径、dry-run、凭据绑定与方舟线**完全一致**
（`X-Avm-Dry-Run: 1` 照常可用）。

⚠️ 刻意没有 `DELETE /v1/videos/{id}`：站点没有取消端点，理由同上。

⚠️ **刻意没有 `DELETE /tasks/{id}`**（2026-09-15 定）：站点没有取消端点，"删本地记录"
只会制造"任务没了"的错觉（跑着的照跑照扣）。对外接口面只有**创建 + 查询**，
任务记录随保留窗口（7 天）自然淘汰。

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

**凭据绑定列（E2E-AVM-011）**：透传创建的任务会在这份库里存一份**会话凭据原文**
（`credential` 列，与 `owner` 指纹分开 —— owner 管租户隔离、credential 管上游解析），
供 `GET /tasks/{id}` 免凭据轮询时解析上游。文件权限自动收到 `0600`；
删除与保留窗口清理时凭据随记录一起删除；绝不进日志 / span / 响应体。

## 查询节流（避免把上游打到 429）

任务从提交到出片要 **~60s**，调用方（newapi 的任务轮询）却可能每秒来一发 ——
每次都实时回上游取，查询请求就成了上游负载的主要来源。适配层按 `AVM_TASK_CACHE_TTL`
（默认 **15s**，`0` = 关闭）缓存上游视图：

- **非终态**：TTL 内直接回缓存（60s 的任务，15s 粒度不丢信息）；
- **终态**（succeeded/failed/cancelled）：**永久**缓存到记录被删 —— 记录已定格，
  多查一次只是白挨限流。

上游请求量从"调用方想打多勤就多勤"变成"TTL 一拍 + 终态一次"。命中缓存的查询**不再**每次
留一条 span（2026-09-15 闸门 1：产生层）—— 只有**本进程第一次观测**这个任务时才发
`ark.task.fetch`（那次的 `cached=true`，status/paid 契约属性照常），此后同状态的重复查询
一律只累加指标。终态与 `paid` 的对账口径因此**不出现空洞**:终态那一次必定是一次跃迁
（或首次观测），照发。
注：上游 tRPC 本身支持把多个 `model.getModel` 合并进一次 HTTP（batch 信封），适合
"一次查多个任务"的列表场景；当前逐任务查询已被 TTL 缓存兜住，批量留作后续增强。

> **为什么不是 Redis**：本服务的瓶颈是**上游出片 2–9 分钟**，而任务表每小时只有几条操作，
> 单次 I/O 从 ~1ms 降到 ~0.2ms 端到端毫无可感知差异。反观代价：多一个要装/起/配/监控的
> 外部服务，且 Redis 默认 RDB 严格说**会丢最后几秒**（要严格得上 AOF `always`，性能优势也就没了）。
> 存储层已接口化（`put` / `get` / `delete` / `list_recent` / `count` / `prune`），
> 将来要多实例共享状态时加一个 `RedisTaskStore` 即可，调用方零改动。

## 请求字段映射

### 能承接的

| Ark 字段 | 处理 |
|---|---|
| `model` | **参与路由**（决定上游 procedure）。判定顺序：渠道映射表精确命中 → 名字本身是上游槽位（**默认透传**）→ 映射表的 **`*` 兜底** → **400**（`model` 钉住键已撤除，遗留即报错）。键与语义见 `docs/channel-options-model.md`；值**不进上游请求体**（站点把模型编在 procedure 路径上）—— 唯一另有副作用的是 `/v1/videos` 面的 `_480p`/`_720p`/`_1080p` 后缀（解析成 `resolution`，**先剥掉再比对槽位**）。**未命中不再静默落到 `ai.minimaxH3`**（迁移见下方「模型映射」） |
| `content[].type=text` | 多条用 `\n` 连接 → `content` |
| `content[].type=image_url` | `role=first_frame`/`last_frame` 走帧通道；`reference_image` 或无 role 进 `referenceImageUrls` |
| `content[].type=video_url` | → `referenceVideoUrl`（单槽位） |
| `content[].type=audio_url` | → `referenceAudioUrls` |
| `ratio` | → `aspectRatio`（站点字段名）；`adaptive` 表示跟随源图，不设值 |
| `resolution` | 严格枚举 `480p`/`720p`/`1080p`，**大小写敏感**（`480P` / `4k` / 数字 `720` 一律 400）；缺省或空串 → **`720p`**；校验通过后原样透传 |
| `duration` | 站点约束是「**连续秒数 + 上限**」，因此**原样透传**（越界才就近钳制到 `[5, 20]`）；缺省 → **5**；`-1` 落 5s（带告警）；小数**截断**（`7.5`→`7`，不是四舍五入）；`frames` 按 24fps 换算且**优先级低于** `duration` |
| `frames` | 按 24fps 换算成秒 |
| `omni_reference_task_type` | `reference`/`auto` 放行；`edit`/`extend` 上游无此能力 → **真实提交 400**（dry-run 仍可校验） |
| `output_format` | `mp4` 放行；`mov` 上游只出 mp4，带显式告警 |
| `generate_audio` | 站点**没有这个开关** —— 记录并回显，同时说明音画由上游决定 |

### 模型映射（**已落地**，2026-09-17）

`model` **是路由键**：它决定上游 procedure（`ai.<槽位>`）。键与语义的**唯一权威表述**在
[`docs/channel-options-model.md`](../../docs/channel-options-model.md)（与 video-adapter **共用一套**，
那边的原始出处是 `ADR-012`）。实现落在 `src/ark_compat/channel_options.py`。

```jsonc
// 渠道级头（逐渠道声明"哪个名字落哪个槽位"）
X-Channel-Options: {"model_map": {"doubao-seedance-2-0-260128": "seedance20",   // 精确键：逐条写
                                  "doubao-seedance-2-5-260628": "seedance25",
                                  "*": "minimaxH3"}}                            // 唯一兜底（可选）
```

判定顺序：① 映射**精确**命中 → ② 映射的 **`*` 兜底**（**覆盖一切**未被精确列出的名字，
含调用方写出的真槽位名）→ ③ 名字本身是上游槽位（**默认透传**）→ ④ **400**（报文给出已知槽位与
表里已声明的键）。每一步都进 `warnings`，并留下证据字段 `effective.model` / `.model_source` /
`.model_verified`、`web_params.procedure`、span `ark.create.submit` 的 `upstream_slot`。

🔴 **`model`（钉住）键已撤除**（2026-09-17，与 video-adapter 一致）：遗留它 ⇒ **渠道配置错误**，
不静默忽略。⇒ 要"这个渠道只跑某一档"，**用兜底**：`{"model_map": {"*": "minimaxH3"}}`
（改写会留痕，不静默）；想让某个名字走别的槽位，就把它写进**精确表**（精确优先于兜底）。

🔴 **通配已降级为"唯一一条 `*` 兜底"**（2026-09-17）：任何**非 `*`** 却含 `*` 的键
（`doubao-seedance-*`）⇒ **渠道配置错误**，报文给出两条出路（逐条写精确键，或保留唯一的 `*`）。
要覆盖一族名字就逐条写 —— 那些名字是有限的、已知的。**多模式机制拆掉之后，"多命中怎么排"
这一整类问题不可能发生**，两个项目在这点上曾各写一套的分歧也随之消失（`docs/channel-options-model.md` §3/§9）。

🔴 **这是对外契约变更**：改动前"任意模型名都通过（一律走 `ai.minimaxH3`）"，现在
**未命中即 400**（且发生在任何上游请求之前）。要恢复旧行为，给渠道配
`{"model_map": {"*": "minimaxH3"}}`（**不要**再用 `{"model": "minimaxH3"}` —— 该键已撤除）。

**仍未做完的前置（本实现的一处已知假设）：**

1. **站点 11 个模型的 procedure 清单还没拿到** ⇒ `procedure_for_slot()` 里的 `ai.<槽位>` 是
   **按命名形态推断**（全站只实测出 `ai.minimaxH3`）。映射到非 `minimaxH3` 槽位时若名字不对，
   表现为上游 NOT_FOUND（显式失败、不建任务、不计费）；证据字段会标 `model_verified=false`
   并在 `warnings` 里说明。清单到手后把推断换成**表**，并让表里没有的槽位直接被拒。
2. **每个模型的能力上限各不相同**，不能假定与 `ai.minimaxH3` 同形 —— 站点文案里
   `veo31Fast` 支持 **4K**、`seedance25` 上限 **20s**、`seedance20` 上限 **15s**、
   `wan27` 只给 **720P/1080P** ⇒ 拿到清单时**必须同时带上 per-model 的
   `duration` / `resolution` 合法域**，否则一次映射就会静默把越界值发给上游。
   现行 `duration`/`resolution` 校验仍是全局 `[5,20]` × `480p/720p/1080p`。

⚠️ **不要**凭站点回显的 `aiModel` 反推映射：站点对请求模型名只是回显，回显值 ≠ 实际执行模型
（`tests/test_upstream_model_visibility.py` 钉住这条事实）。

门禁：`tests/test_channel_model_map_wildcard.py`（纯函数层）+
`tests/test_channel_model_wiring.py`（接线层：真 `WebClient` + 站点替身，断言解析结果真的变成
上游 URL）。两条都做过变异自证。

### 已知缺陷：非法**类型**的 `duration` / `frames` 落 500（**按口径暂不修，仅登记**）

`translate_create` 里 `duration` 与 `frames` 直接 `float(...)`，抛出的 `TypeError` /
`ValueError` **没有 handler 接** —— app 只注册了 `ArkError` / `ParamError` /
`WebApiError` 三个异常处理器。⇒ 非法**类型**的入参走 **500 Internal Server Error**，
而不是 400 `InvalidParameter`：

| 入参 | 现状（实测） | 应为 |
|---|---|---|
| `duration="abc"` / `"1.2.3"` | **500**（ValueError） | 400 `InvalidParameter` |
| `duration=[]` / `{}` / `["5"]` | **500**（TypeError） | 400 `InvalidParameter` |
| `frames="abc"` / `[]` / `{}` | **500** | 400 `InvalidParameter` |
| **`frames=""`** | **500** | 400，或按 `duration` 的口径当"没传" |
| `duration=true` | 200，静默当 `1s` 再吸附成 5s | 保留现状 |
| `duration="8"` | 200，接受字符串数字（**无任何告警**） | 保留现状 |

**2026-09-16 用户口径：以上一律"先不修"，仅在此登记备查。** 所以本表是**现状说明**、
不是待办清单 —— 动这些行为需要重新拍板（修 500 会改对外错误码，属契约变更）。

三条附带证据，供将来动手时省一次排查：

1. **危害不是"少一个 400"，而是归因被颠倒** —— 调用方看到 5xx 会去查服务端故障、去重试、
   去告警，真相却是它自己传错了；同时监控里的 5xx 被污染，SLO 与告警阈值一起失真。
2. 🔴 **`frames=""` 还暴露出一条不对称**：`duration` 判 `not in (None, "")`（`""` = 没传），
   而 `frames` 只判 `is not None` ⇒ 同一个空值在两条**平行**解析路径上语义不同。
   「两条平行路径只有一处做了某件事」正是本文件反复踩的一类病根（另见 `resolution` 那行）。
3. 🔴 **当时 758 条测试全绿也照漏**：用例断言的是合法输入的正确输出，非法**类型**从来不在
   集合里。要修就得**同时**补门禁，且门禁必须断言**状态码 4xx** —— 纯函数层直调只看得到
   `ValueError` 冒出来，看不到它最终变成 500。⚠️ 修法**不要**改成在全局 handler 里
   catch-all `ValueError → 400`：那会把服务端自己的 bug（如 `int(None)`、上游响应结构变化）
   也伪装成客户端错误，是方向相反的同一类病。

对照：OpenAI 面的 `seconds` 走 `_seconds_to_duration`，**已正确**抛 `ParamError`
（`seconds="abc"` → 400）—— 同一个服务里两条时长路径**只有一条是对的**。

### 适配层的扩展开关（`extra_body.aivideomaker_*`）

Ark SDK 把未建模字段塞进 `extra_body`；这些是**本适配层自己的旋钮**（顶层同名字段也认）：

| 开关 | 默认 | 作用 |
|---|---|---|
| `aivideomaker_dry_run` | `false` | 零成本校验，不提交（见「零成本校验」） |
| `aivideomaker_tier` | `turbo` | `turbo`/`base`；决定计费口径（见「计费」） |
| `aivideomaker_prefer_free` | `false` | 越线时把时长拉回免费区（见「计费」） |
| `aivideomaker_captcha_token` | — | BYO 真 Turnstile token（闸门开着时用） |
| **`aivideomaker_prompt_enrichment`** | **`true`** | 站点侧的**提示词增强**。**默认开**（2026-09-16 用户口径），与站点自己前端发的请求一致；传 `false` 关闭 |

- `prompt_enrichment`：值**认不出就不猜** —— 保持默认开 + 写进 `warnings`（与 `aivideomaker_tier`
  同形：不静默）。接受 JSON 布尔与字符串 `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`（大小写不敏感）。
- ⚠️ **OpenAI 兼容面（`/v1/videos`）没有这个开关**：那条契约的字段集是固定的
  （`model`/`prompt`/`seconds`/`size`/`input_reference`/`first_frame_image`/`last_frame_image`，
  多一个字段会被当成"未知字段"忽略并告警）⇒ 该面上**只能**用默认值（开）。
- 门禁：`tests/test_ark_compat.py::TestTranslateCreate`（默认/关闭/字面量/非法值留痕 4 条）
  + `tests/test_web_upstream.py`（**真客户端发出去的 body** 里确实是那个值 —— 默认开的那条
  端到端用例证明了"单看 web_params 不够"）。

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
| `tier=base` **一律计费**；`tier=turbo` 且 `duration ≤ 10s` **免费** |

判据只有一个：站点任务记录里的 **`paid`** 字段（`credits` 与它**反相** ——
免费任务也记 `credits`，不要拿它判断）。

🔴 **但 `paid` 不在对外响应体里**（0.0.27 起）：`GET /tasks/{id}` 按官方 schema 收窄，
`usage` 整块不下发（见「路由」的响应体白名单）⇒ **别再找任务响应里的 `usage`**，
那里永远没有，而它的缺席**不是**"没计费"的证据（结论恰好相反的那种误读）。
想自查某条任务花没花钱，只有两个权威口径：

| 口径 | 做法 | 前提 |
|---|---|---|
| **余额前后差** | `GET /healthz?deep=1` 取 `balance`：提交前记一次、出片后再记一次，**差值即花费**（免费组合差 0） | 本进程在上游侧有可用凭据 |
| trace 属性 | `ark.task.fetch` span 的 `paid` | 挂了 Logfire |

`/healthz` 的 `billing_check` 字段会**自述这两条**（`usage_in_task_response: false` + `how`），
运维不必翻到这一节 —— 加它的直接原因是 livetest 报告 `E2E-AVM-015` 提的那条告警。

响应里的 `effective.billed` 会预告是否计费，并附一句 `billing_note`。
`extra_body.aivideomaker_prefer_free=true` 是省钱开关：把超过 10s 的**合法**时长
主动拉回 10s（不只是越界时才生效）。

**越线一定会有声音**（2026-09-14 起）：只要 `billed` 是**因为时长**而为 true，`warnings`
里就会多一条显式提醒。此前的行为是 `duration:15` **静默**进计费区 —— `billed: true`
而 `warnings` 为空，调用方除非自己去读 `billed` 字段，根本不知道这条请求要花钱
（而 `tier` 默认是 `turbo`，看起来就像免费档）：

```
duration 15s is outside the free window (10s) — this request WILL BE BILLED (tier=turbo);
pass extra_body.aivideomaker_prefer_free=true to snap it down to 10s instead
```

⚠️ `tier=base` **不会**触发这条：它是调用方显式点名的选择（`extra_body.aivideomaker_tier`），
不属于"悄悄变贵"，重复喊只会让人对告警脱敏。

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
                "billing_note": "web upstream: tier=base is always billed; tier=turbo is free up to 10s"}
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
3. **并发上限按账号额度（premium 2 / pro 4；`AVM_MAX_CONCURRENT=0` 自动探测），且槽位占到底**（从创建一直到任务进入终态）。适配层有信号量闸门
   （`AVM_MAX_CONCURRENT`），超出会**排队等待**而不是直接失败。
4. **媒体必须转存到站点 CDN。** 适配层自动做：外链图片 → 先下载再走预签名上传；
   data URI → 解码成字节上传；已在 `static*.img2video.ai` 上的原样放过。
   站点只收自己 CDN 的地址，外链会因 Content-Type 白名单被拒。

   🔴 **取"参考文件链接"有两道闸，缺一它会变成"调用方超时 + 我们照扣费"**（2026-09-15 修）：
   - **单项预算** `AVM_MEDIA_FETCH_TIMEOUT`（默认 **30s**，2026-09-16 由 20 上调）+ **硬字节上限 50MB**：链接是**流式**读的
     （块间受超时保护、整体受预算保护、总量受上限保护），超了给 **`504`** 并在报文里点名
     `download` 与**具体 URL**；声明超限/实际超限给 **`400`**。
   - **总闸** `AVM_MEDIA_REHOST_BUDGET`（默认 **120s** = 图 4 × 单项 30s，2026-09-16 由 90 上调）：一次创建最多 7 个媒体项
     （图 4 / 视频 1 / 音频 2）且**串行**转存，没有总闸时最坏耗时是"单项超时 × 项数" ——
     而这几分钟全部发生在调用方**同步等待**的那一个请求里。最坏的后果不是"调用方超时"，
     而是**它超时之后我们继续跑完并把任务提交出去（那一步计费）**。有了总闸：走不完就 504，
     且**绝不**继续提交（门禁 `tests/test_media_fetch_budget.py` 专门断言这一点）。
   - 预签名 PUT **只补预签名没给的请求头**，且不手写 `Content-Length`：站点自己的实现是
     `fetch(uploadUrl, {method:"PUT", headers, body})`（见 `docs/web-reverse/captured/js`），
     即**原样**用返回的 headers —— 对预签名 URL 多带未签名头是 403（签名失配）的风险。
   - 埋点（见下"可观测性"）：`download` / `uploads.getPresignedUrl` / `uploads.PUT` 三条记录
     都挂在 `ark.create.submit` 的 `upstream_calls` 上。

## 可观测性

- **loguru** 是唯一日志出口，每条带 `request_id`（同时回显在响应头 `x-request-id`）。
- 经 `logfire.loguru_handler()` 桥接进 Logfire；`instrument_fastapi` 让每个请求成一条
  span，`instrument_httpx` 让每次上游调用成子 span。
- 🔴 **探活与公开文档端点不上报**（`observability.DEFAULT_PROBE_PATHS` = `/healthz`、`/`
  与交互式文档三条）：容器 HEALTHCHECK 每 30s 打一次 `/healthz`，`/docs` 那一族不鉴权、
  会被扫描 —— 每条都成 span + 日志就是一天上千条噪声。**span** 在
  `instrument_fastapi(excluded_urls=…)` 里摘，**日志**在 logfire 那个 sink 的 filter
  （`keep_off_logfire`，按请求中间件绑的 `avm_probe` 判）里摘 —— 两个口都从
  `is_probe_path()` 派生，改一处不会漏另一处。本地 stderr 照打：探活坏掉时本机仍看得见。
  名单可用 `AVM_LOGFIRE_EXCLUDED_PATHS` 覆盖（逗号分隔；留空 = 用默认表，`-` = 不排除）；
  由 `_apply_excluded_paths()` 在启动时解析，条目不合规会**告警剔除**（不静默放过）。
  ⚠️ **只写路径，别写正则**：上游做的是 `re.search`（**子串**匹配），写 `"/"` 会命中
  **每一个** URL（任何 URL 都含 `/`）⇒ 全站追踪被静默关掉；未锚定的 `"/healthz"` 也会
  连带吃掉 `/healthz/extra`。故语义只定义一次（`_path_matches`：普通=精确，尾斜杠=子树），
  正则由 `_regex_for()` 同构生成。被匹配的串是 `scheme://host + scope["path"]`（**不含
  query**）⇒ `/healthz?deep=1` 命中同一条。
  门禁：`tests/test_logfire_probe_exclusion.py`（含变异自证：退化成朴素串 / 丢掉 env 读取
  都会红）。
- 🔴 **轮询端点不产生 span（闸门 1：产生层）**：`DEFAULT_POLL_PATHS` = 任务查询路径
  （方舟线 `/api/v3/contents/generations/tasks/` 与 OpenAI 兼容线 `/v1/videos/`）。
  与探活表**语义不同、机制同源**：探活"根本不是业务"；这里是业务端点、但被调用方按秒级
  反复查询 —— 同一个任务几十上百次请求里绝大多数 span 逐字段相同，那是重复计价，
  不是可观测性（Logfire 官方文档就把 "High-frequency polling" 列为应排除埋点的用例）。
  实测（同一个任务被查 60 次 / 盯梢 30 次轮询上游）：**123 → 6** 与 **31 → 1** 条 span。
  实现分三处，**缺一不可**（都是实测结论，不是照抄文档）：
  1. **入站 span** ⇒ `instrument_fastapi(excluded_urls=…)`。⚠️ handler 里的
     `suppress_instrumentation()` 对入站**无效** —— ASGI 中间件在 handler 之外就把 span
     建好了，所以排除粒度只能是 URL。
  2. **状态跃迁才留痕** ⇒ `app.PollReportGate`：首次观测 / 状态跃迁 / **失败**才发
     `ark.task.fetch`，其余只累加指标。⇒ 终态与 `paid` 的对账口径**不出现空洞**，
     而失败**一律留痕**（失败时上游状态未知，不能据此认定"没变化"）。
  3. **日志** ⇒ 请求中间件对轮询路径**只在 4xx/5xx** 时才把 access log 送上 logfire
     （成功的轮询静音）。把失败响应一起静音，等于把"调用方到底看到了什么"删掉。
  名单可用 `AVM_LOGFIRE_POLL_PATHS` 覆盖（与探活表同一套解析器与语义：留空 = 默认表，
  `-` = 不排除任何路径，非法条目告警剔除）；门禁 `tests/test_poll_suppression.py`
  （含 10 条变异自证全红）。
- **出站轮询也不产生 span**：盯梢线程 `WebSubmitQueue._watch` 全程只有**一条** span
  `ark.task.watch`，状态跃迁记成它上面的 `status_change` **事件**；轮询真正发出的上游 GET
  由 `WebClient.wait_for_task` 里的 `suppress_http()` 压掉。
  ⚠️ **顺序契约**：压制按 contextvar 在 **span 处理器层**生效，会连带压掉我们自己显式开的
  `span()` ⇒ 必须"先开 span、再用它只包 GET"。写反了，全程那条 span 会**静默消失**。
  ⚠️ 出站 httpx 埋点打在 `HTTPTransport.handle_request` 上 ⇒ 用 `MockTransport` 的测试
  **看不到** httpx span（这就是"httpx 自动子 span"长期只写在注释里、从没被断言的原因）。
  指标：`avm.task.poll_requests`（每次查询都计数，`reported` 区分留没留 span，`source`
  区分调用方轮询 / 盯梢）+ `avm.task.wait_seconds`（跃迁进终态时记一次）。
  🔴 指标标签**只放低基数枚举**：绝不放 `ark_id` / `task_id` —— 那会把时间序列打成一任务
  一条，比 span 还贵。
- 具名 span（属性即契约，用内存 exporter 断言：`tests/test_trace_contract.py`）：
  - `ark.create.submit` —— `ark_id` / `upstream` / `ark_model` /
    `resolution` / `duration` / `billed` / `warning_count` / `warnings` /
    **`upstream_task_id`** / `request`（调用方发来的 Ark 请求原文）/ **`upstream_calls`**

    ★ **上传链路也在 `upstream_calls` 里**（"文件上传有做埋点吗"的答案，2026-09-15）：
    外链/内联素材的每一步都留痕，**成功失败都有**（失败那条带 `status="error"` + 原始错误）：
      - `download` —— 拉取调用方给的链接：`request.url` + 字节数 + 耗时
      - `uploads.getPresignedUrl` —— tRPC 预签名：`fileName` / `contentType` / `fileSize`
      - `uploads.PUT` —— 转存到站点 CDN：`publicUrl` / 状态码 / 字节数 / 用了哪些请求头
    它们**不是独立 span**，共用 `ark.create.submit` 那一条 —— 一次创建的因果链在一个地方读完。
    ⚠️ 这条分支曾**零测试覆盖**（app 层用例把整个 `WebClient` 换成替身、真客户端用例只传
    bytes）⇒ 现在由 `tests/test_media_fetch_budget.py` 钉住（含 9 条变异自证）。
  - `ark.create.dry_run` —— 同上（除 taskId）：零成本校验路径也要能在 trace 里复盘
  - `ark.task.fetch` —— `ark_id` / `upstream` / **`upstream_task_id`** / **`status`** /
    **`paid`**（出片后对账的两项关键；`paid` 才是"这次花没花钱"的判据，不是 `credits`）/
    `upstream_response`（归一化后的**完整内部视图** —— 上游实际执行的模型
    `upstream_model`、站点原始记录 `upstream_record`、实际产出档位 `resolution` 都在里面，
    从 `4211e5b` 起一直如此）/ **`warnings`** / **`unsupported`** / `upstream_calls`

    2026-09-15 响应体两轮收窄时**只单独补了 `warnings` / `unsupported`** —— 它们来自本地
    任务记录（`entry`），**不在** `upstream_response` 里，不挂就真的丢了。
    其余证据**不重复上报**：同一份数据挂两遍只会让 trace 变大、口径还容易漂，
    也会让人误以为"是这次特意补的"（用户当场指出 `upstream_record` "之前在 logfire 就有"）。
    门禁：`tests/test_trace_contract.py::test_evidence_is_not_uploaded_twice`。

    ★ **只在状态跃迁时产生**（闸门 1，2026-09-15）：同状态的重复轮询**不发 span**（只累加
    `avm.task.poll_requests`）。判据与理由见上面「轮询端点不产生 span」那条。因此这条 span
    多带三项**跃迁信息**，读 trace 的人不必再靠"跟上一行对比"推断状态变没变：
      - `transition_reason` —— `first`（本进程首次观测）/ `transition`（跃迁）/ `error`（失败）
      - `transition_from` / `transition_to` —— 跃迁方向（`first` 时 `from` 为空串）
      - `poll_count` —— 本进程观测到的查询次数（含那些没留 span 的）
      - `cached` —— 这次是不是命中了节流缓存（命中就没有上游调用 ⇒ 无 `upstream_calls`）
      - `upstream_ms` —— 上游 calls 的耗时合计；⚠️ 因为"先取数据、后决定要不要留痕"，
        这条 span **不再包住上游调用**，它的自身时长近似 0，看耗时请用这个属性
      - 跃迁（且不是 `first`）时另挂 `status_change` **事件**（`{from, to}`）
      ⚠️ 跨请求的"全程唯一 span"在本服务落不了地（span 不能跨 HTTP 请求；多 worker 下同一
      条轮询会落到不同进程）⇒ 事件只能挂在**跃迁那一条** span 上。
  - `ark.task.watch` —— 盯梢线程的**全程唯一** span：`upstream` / `upstream_task_id` /
    `task.poll_count` / `task.last_status` / `task.final_status` / `task.done` /
    `task.watch_ms`，跃迁记成 `status_change` 事件；盯梢失败或超时另带 `error`。
    ⚠️ 槽位释放放在 `finally`、span 的创建在它里面 —— 埋点坏掉**绝不许**把闸门卡死。
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
| `avm.account.credits` | 剩余积分（web 线账号的积分池） |
| `avm.account.captcha_required` | 当前是否需要 Turnstile（`1`=需要）。它**按速率动态翻转**，只有时间序列才看得出"开多久会衰减" |
| `avm.account.reachable` | 凭据是否可用（`0`=会话过期 / 凭据失效） |

- 标签只有：`upstream` / `account`（**凭据 sha256 前 16 位**）/ `source`
  （`process`=本进程持凭据，`passthrough`=调用方自带）/ `pid`。
  🔴 **凭据原文绝不进遥测** —— `tests/test_pool_metrics.py` 专门守这条，并做了变异测试。
- **只读**：`credits.getCredits` / `model.needsCaptcha` ——
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
| minter 的 `TZ` 修复（E2E-AVM-006） | ✅ **已验收**：E2E-AVM-008（node064，0.0.15 实机）—— minter 持续 mint（池 4/4）、`served` 1→2、放通防火墙后两条真实提交成功；历史结论由单变量矩阵定案 |
| 429 的归因提示与 `TokenMinter.last_error`（E2E-AVM-008 后新增） | 仅单测覆盖（unreachable ⇒ 防火墙修法提示；HTTP 失败 ⇒ 指向 minter），**未在 node064 实机复验报文形态** |
| `compose_wiring_check --probe`（容器内连通性探测）与 host 网络静态契约 | 仅本地单测；探针在 node064 的下一次部署前实测（E2E-AVM-008 的矩阵用的是手工一次性容器） |
| `tzdata` 是否必须 | 决定性实验跑在**没有** tzdata 的镜像上（浏览器走自带 ICU 时区库）⇒ 它是"消除半生效"的加固，不是铸造成功的前提；这一判断**未做过对照实测** |

## 测试

```bash
python3 -m unittest discover -s tests      # 300+ 项，零消耗、零外发
```

三条纪律：

1. **零额度消耗** —— 所有提交路径走 dry-run；唯一测的"真实提交"是"上游不具备的能力被拒"，
   它在发出上游请求**之前**返回。
2. **零外发** —— 上游 `base_url` 指向没人监听的本地端口，且客户端 `trust_env=False`
   （httpx 默认会把回环地址也交给系统代理，那会让"死端口"验证失效）；web 线的测试
   全部用 `httpx.MockTransport` 做站点替身；Logfire 用 `send_to_logfire=False`。
   🔴 **"死端口"只覆盖 `base_url` 那条路径**：媒体转存走的是 `httpx.get(绝对 URL)`，
   **不受 `base_url` 约束**，会真的去公网下载。2026-09-14 复跑时抓到一次真实出站
   （`220.181.116.10x:443`，来自 `upstreams._rehost` → `web_client.upload_file`）：
   那个用例每次运行都向文档站 TOS 发请求 —— 既让这条纪律名不副实，也让用例依赖
   外部 CDN 的可用性与延迟（下载超时上限 60s）。已修（素材 URL 改指死端口）。
   并固化为**可执行的审计**：`python3 tools/egress_audit.py` —— 跑全量单测、拦下
   所有非回环 TCP 并打印调用栈，有出站即退出码 1。当前实测非回环出站 **0 次**。
3. **门禁要能被证伪** —— 关键门禁（截断、截断留痕、悬空占位符点名、`incompatible`
   拦截、枚举校验、trace 属性契约与凭证红线）都做过**变异证明**：逐条回退后测试确实变红。

## 文件

```
src/
├── ark_server.py            启动入口（uvicorn）
├── asgi_app.py / gunicorn_conf.py ASGI 入口与 gunicorn 装配（多 worker 额度分摊）
└── ark_compat/
    ├── __init__.py          包说明、版本、服务名（改名只改这里）
    ├── translate.py         纯函数翻译层（Ark ↔ 站点接口）+ 计费口径渲染
    ├── openai_videos.py     OpenAI /v1/videos 兼容面的纯翻译（OpenAI 形 ↔ Ark 形）
    ├── upstreams.py         上游统一接口 + 媒体转存 + 上游工厂
    ├── web_client.py        网页端客户端（tRPC / 上传 / SSE）
    ├── web_queue.py         并发闸门（信号量 + 终态释放）
    ├── minter.py            铸造服务客户端（取 Turnstile token；多地址轮询/故障转移）
    ├── sniff.py             magic bytes 媒体嗅探 + 图片尺寸
    ├── store.py             任务持久化（SQLite 默认 / 显式内存开关）
    ├── media_proxy.py       成片对外出口（换成自有地址 + 流式回源 + 响应头白名单脱敏）
    ├── settings.py          环境变量配置（唯一集中解析处）
    ├── observability.py     loguru + logfire 装配（可失败降级）
    ├── app.py               FastAPI 路由与 Ark 错误信封
    └── errors.py            ParamError / WebApiError / CaptchaRequiredError
tools/
├── turnstile_service.py    铸造服务（常驻；非回环绑定 + 无 MINTER_KEY ⇒ 拒绝启动）
├── turnstile_minter.py     铸造核心（Xvfb 有头 Chrome + CDP）
└── egress_audit.py         零外发审计（跑全量单测 + 拦非回环 TCP，非 0 即有出站）
tests/                       # 见下；`python3 -m unittest discover -s tests`
├── test_ark_compat.py       翻译层（时长吸附、计费口径、unsupported）、HTTP 层
├── test_web_upstream.py     web 上游（全部离线，MockTransport）
├── test_cookie_normalize.py Cookie 头规范化（含 JS↔Python 一致性）
├── test_free_window.py      免费窗口（常量 ↔ billing_note）
├── test_docs_billing_sync.py 文档里的免费秒数必须跟着 FREE_MAX_DURATION 走
├── test_seedance25_omni.py  Seedance 2.5 全能参考（截断、专属字段、前置拒绝；零外发）
├── test_openai_videos.py    OpenAI /v1/videos 兼容面（Chatfire 契约六字段；零外发）
├── test_media_proxy.py      成片对外出口（响应头白名单、零上游痕迹、归属；含 5 条变异自证）
├── test_trace_contract.py   trace 属性契约（内存 exporter 捞 span 断言 + 凭证红线）
├── test_passthrough_cookie.py 透传（多租户隔离、缓存淘汰、闸门互斥）
├── test_task_store.py       任务持久化（跨实例可读 = 重启不丢）
├── test_env_template.py     .env 模板门禁（**双向**：声明 ↔ 生产代码读取）
├── test_compose_env_injection.py 模板声明的键必须真的**注入容器**（声明 ↔ 编排注入）
├── test_auto_concurrency.py 并发槽位按账号额度自动定（含多 worker 分摊）
├── test_probe_retry.py      只读探测的超时与重试（写入绝不重试）
├── test_pool_metrics.py     号池指标（凭据原文绝不进遥测，含变异测试）
├── test_token_minter.py     铸造服务接线（取不到 token 就如实失败）
├── test_minter_chrome_guard.py 铸造器 Chrome 生命周期守卫（绝不叠实例）
├── test_minter_bind_guard.py 铸造服务绑定安全（非回环 + 无 key ⇒ 拒绝启动）
├── test_minter_timezone.py  铸造时区门禁（缺 TZ ⇒ 铸造恒 interactive；含变异自证）
├── test_minter_healthcheck_port.py 健康检查**跟随 PORT/BIND_HOST**（假 unhealthy 的反例自证）
├── test_auth_single_var.py  鉴权单变量（三态解析、旧变量拒绝、闸门×透传不可表达）
├── test_compose_wiring_check.py 宿主机侧接线校验（含"它自己能被证伪"的变异清单）
├── test_minter_preflight_attribution.py 前置校验的归因准确性（P-07/P-08）
├── test_minter_render_budget.py   render 预算（冷启动 / 常规两条）
├── test_turnstile_render_states.py 铸造失败分类与页面内状态采样
├── test_billing_advisory.py  计费提醒只在 duration 越线时出现
├── test_task_credential_binding.py 任务凭据绑定（免凭据查询 / 租户隔离 / 空表 401）
├── test_ark_task_schema.py  查询响应 = 官方 schema 的**真子集**（无 `model`/`resolution`、
│                           只给有真值的字段、`duration` 是 int、status 含 expired；
│                           两个字段集都硬编码 —— 含变异自证）
├── test_upstream_model_visibility.py 内部视图里 `model`(请求值) 与 `upstream_model`(实际值)
│                           并存；两者都**不进响应体**，实际值只在 trace（见 test_ark_task_schema）
├── test_logfire_export_signal.py  观测出口"真的在发"（而不是只装上了 SDK）
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
| 本地 | 参考文件链接超单项预算或总预算 | `UpstreamError` | **504** |
| 任一 | 网络 / 上游 5xx | `NETWORK_ERROR` / `UpstreamError` | 502 |

🔴 **`code` 是白名单，不是上游码的透传**：`trpc` 会把**站点自己的**错误码（上游信封里的
`data.code`）一起带进来，那是上游的内部词表 ⇒ 认不出一律落到 `UpstreamError`。
只有 `NOT_FOUND` 这类**语义已确认**的才映射（`_web_code_for`）。

### 上游错误的**对外脱敏**（2026-09-15 定；用户口径："客户端侧上游错误脱敏、参考火山
seedance 报错形态；logfire 侧全部上报上游"）

上游错误的原文里塞满了**实现细节**，而它以前会**原样进客户端报文**：

- 站点内部 procedure 名（`ai.minimaxH3` / `model.needsCaptcha`）
- 铸造服务主机与端口（`host.docker.internal:8899`）
- **运维口令**（`ufw allow proto tcp from 172.16.0.0/12 to any port 8899`）
- 内部 runbook 编号（`E2E-AVM-008`）与工具路径（`tools/compose_wiring_check.py`）
- 上游返回的原始报文

现在的形状**照官方「公共错误码」表**：`code` / `message` / `param` / `type`，且

- `message` 是**通用描述句** + 结尾的 **`Request ID: {id}`**（官方每条 message 都这么写）；
  正文里不出现任何内部标识。ID 与响应头 `x-request-id` 同源 —— **脱敏之后它是调用方
  唯一的抓手**：报障时报它，运维能在 trace 里捞到全量原文。
- `Request ID` 的拼接**只有一处**（`_with_request_id`）。两处各拼一遍时，其中一处会变成
  死代码、门禁也就证伪不了它（变异自证实测踩到）。
- 我们**自己**的报文（参数校验 / 能力不支持 / 任务不存在）**不脱敏** —— 它不含上游实现
  细节、对调用方有用（"哪个值错了"要说得出），只补 Request ID。
- 🔴 **按阶段区分**（2026-09-16 用户口径：**超时多发生在传图片 / 传 URL 这条路上**）：
  procedure 翻成**阶段说法** —— `fetching a reference file you supplied` /
  `preparing the reference upload` / `uploading a reference file to the upstream`。
  **"调用方自己的素材慢"与"上游自己慢"必须分得开**：一句笼统的"上游没响应"会把责任指错
  方向，调用方也拿不到"换更快/更小的直链"这个行动项。procedure 原名一律不出现。
  同理，传输层超时（`ReadTimeout`…）会被识别成"did not respond in time（可重试）"——
  这类词是通用 HTTP 客户端词汇、不含内部标识，属于**必须留给调用方的事实**。

**两个通道**（与既有纪律一致：证据换通道，不是消失）：

| | 客户端看到 | trace / 日志 |
|---|---|---|
| message | 通用描述句 + Request ID | `error` = **全量原文**（含 procedure 名、ufw 口令、runbook 编号） |
| 归因 | — | `captcha_gate` / `minter_unreachable` / `minter_last_error` |
| 上游原文 | — | `upstream_calls`（请求/响应逐字） |
| 调用方实际收到的那句 | — | `client_message`（出口回归只能靠它复盘） |

门禁：`tests/test_upstream_error_redaction.py` —— 出口**黑名单扫描**（任何内部标识出现即红）
+ "trace 仍全量"的**对偶断言** + 官方四键形状；含 6 条变异自证全红。
⚠️ 能过黑名单**不代表**过审：新增内部标识（新主机名、新 runbook 编号）要把它加进
`INTERNAL_TOKENS`，否则门禁不认识它。
