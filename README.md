# videos — aivideomaker.ai 双线对接

围绕 [aivideomaker.ai](https://aivideomaker.ai) 视频生成能力的客户端工具、接口文档与实测产物。

该站对外提供**两套独立的能力体系**，本项目按两条线分别维护：

| 线 | 入口 | 认证方式 | 状态 |
|---|---|---|---|
| **官方 API** | `https://aivideomaker.ai/api/v1/*` | `key` 请求头（[创建入口](https://aivideomaker.ai/zh/app/settings/account/api-keys)） | ✅ 已打通 |
| **web 逆向** | 网页端内部接口（tRPC over `/api/*`） | 登录会话 Cookie（`auth_session`，TTL 400 天） | ✅ 已打通，见 `src/web-adapter/` |

**积分池是同一个**（2026-09-13 实测）：`credits.getCredits` 的 `totalRemaining` 与官方 API
`GET /api/v1/account` 的 `currentBalance` 数值一致（均为 796）。官方 `llms.txt` 说的
"API credits 与网站订阅是两回事"针对**订阅权益**成立，但**消耗的是同一份积分**。
详见 [`docs/web-reverse/README.md`](docs/web-reverse/README.md)。

## 目录结构

```
videos/
├── README.md                       本文件
├── .env.example                    环境变量模板
├── .gitignore
├── src/
│   ├── avm.py                      官方 API 客户端（零依赖，仅标准库）
│   ├── extract_web_models.py       web 侧情报提取工具（从页面/JS 挖模型清单与接口路径）
│   ├── web_session.py              web 侧会话工具（tRPC over /api，验证会话 / 调 procedure）
│   ├── ark_server.py               ★ 火山方舟兼容服务入口（FastAPI + uvicorn）
│   ├── ark_compat/                 ★ 火山方舟兼容层：两条上游 → Ark Seedance 协议
│   │   ├── README.md               实现文档（选线、字段映射、计费口径、未验证项）
│   │   ├── translate.py            纯函数翻译层（Ark ↔ 两条上游）+ 计费口径渲染
│   │   ├── upstreams.py            两条上游统一接口 + 媒体转存
│   │   ├── client.py               官方 API 客户端（httpx）
│   │   ├── web_client.py           网页端客户端（tRPC / 上传 / SSE）
│   │   ├── web_queue.py            web 线并发闸门（槽位占到底）
│   │   ├── sniff.py                magic bytes 媒体嗅探
│   │   ├── settings.py             环境变量配置
│   │   ├── observability.py        loguru + logfire 装配（可失败降级）
│   │   ├── app.py                  FastAPI 路由与 Ark 错误信封
│   │   └── errors.py               ParamError / OfficialApiError / WebApiError
│   └── web-adapter/                web 逆向版：把网页端协议适配成标准 API
│       ├── README.md               实现文档（接口清单、会话有效期、适配映射）
│       ├── adapter.mjs             适配层：OpenAI Responses + MiniMax + 火山方舟 Ark/Seedance 三形状
│       ├── client.mjs              AvmClient（基于 session cookie，无浏览器、无验证码）
│       ├── submit-queue.mjs        提交队列（并发闸门 + 延迟执行 + 退避重试）
│       ├── package.json            零依赖，仅需 Node ≥18
│       ├── cookies.example.json    Cookie 模板
│       ├── tests/                  端到端测试（含 fixtures/ 内素材、archive/ 一次性探针）
│       └── tools/                  check-session / credit-report / collect-tasks / upload 等运维工具
├── tests/
│   ├── test_ark_compat.py          翻译层 / official 上游 / HTTP 层（零消耗、零外发）
│   └── test_web_upstream.py        web 上游（全部离线，MockTransport）
├── requirements.txt                仅兼容层需要（fastapi/uvicorn/httpx/loguru/logfire）
├── docs/
│   ├── official/
│   │   ├── api-reference.md        官方 API 完整文档：8 个模型参数差异、价格、坑位
│   │   ├── aivideo-openapi.json    官方 OpenAPI 3.1 描述
│   │   ├── llms.txt                官方 LLM 发现索引
│   │   └── robots.txt              站点抓取规则（含公开路径线索）
│   └── web-reverse/
│       ├── README.md               web 逆向调研笔记（接口形态、数据模型、待办）
│       ├── model-inventory.md      ★ 模型对照表：web 端 11 个 vs API 端 8 个
│       ├── cookies.md              Cookie 与会话过期机制（含实测 TTL 400 天）
│       ├── TESTCASES.md            ★ 适配层测试用例报告（77/83 项、双向 request/response）
│       ├── test-run*.log           测试输出存档
│       └── captured/               抓取资产（页面 HTML、72 个 JS chunk、sitemap）
└── assets/
    ├── cyberpunk_sunset.mp4                官方 API t2v 产物（1248x704，15 积分）
    ├── seedance20_sunset_720p.mp4          官方 API seedance20 产物（1280x720，110 积分）
    └── web-reverse/                        web 逆向版实测产物（15 个，全部为免费组合）
```

## 快速开始

### web 逆向线（Node，零依赖，需 Node ≥18）

```bash
cd src/web-adapter
export AVM_COOKIE='auth_session=<40位随机串>'   # 浏览器导出，TTL 400 天
node tools/check-session.mjs                    # 先确认会话可用
node adapter.mjs                                # 起适配层 :8788
```

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

npm test          # 77 项，全程零消耗
npm run test:live # 追加真实提交（2 条免费任务）
npm run tasks     # 汇总任务与成片链接
```

完整说明见 [`src/web-adapter/README.md`](src/web-adapter/README.md)，
测试用例与双向 request/response 见 [`docs/web-reverse/TESTCASES.md`](docs/web-reverse/TESTCASES.md)。

> ⚠️ **计费红线**：`tier=base` 一律计费；`turbo` 时 `duration ≤ 8s` 免费、`≥ 9s` 计费。
> 免费组合请用 **480p / 5s / turbo**。判断是否花钱只看任务记录的 `paid` 字段。

### 官方 API 线（Python，零依赖）

```bash
cd /Users/betterme/PycharmProjects/AI/videos
cp .env.example .env          # 填入真实 AVM_KEY
export AVM_KEY="$(grep -o 'ak_[a-f0-9]*' .env)"
```

`avm.py` 只依赖 Python 标准库，无需安装任何依赖。

```bash
# 支持的模型列表与账户余额（免费）
python3 src/avm.py models

# 报价：提交前必做，不扣费，同时校验参数格式
python3 src/avm.py quote seedance20 \
  --param prompt="A cinematic sunset over a futuristic city skyline" \
  --param ratio=16:9 --param duration=5 --param resolution=720

# 探测某模型的必填字段（发送空请求体，免费）
python3 src/avm.py probe wan27

# 正式提交（带支出上限保护）
python3 src/avm.py submit t2v \
  --param prompt="..." --param aspectRatio=16:9 --param duration=5 \
  --raw-type duration --max-credits 20 --idempotency-key sunset-001

# 轮询至完成并自动下载
python3 src/avm.py wait <taskId> --download ./out.mp4

# 取消任务（积分全额退回）
python3 src/avm.py cancel <taskId>

# 历史任务与扣费明细
python3 src/avm.py list
```

完整命令：`python3 src/avm.py --help`

### 火山方舟兼容层（Python，FastAPI）★

把 aivideomaker 包装成火山方舟 Seedance 协议，用火山官方 SDK 直接调本服务。
**两条上游并存**：官方 API 线与网页端逆向线，对外协议完全一致。

```bash
pip install -r requirements.txt
export AVM_KEY="ak_xxx"                # 官方线
export AVM_COOKIE="auth_session=…"     # web 线（可选，两条都配就都能用）
export AVM_OFFICIAL_MAX_CREDITS=60
export AVM_UPSTREAM=official           # 默认线
python3 src/ark_server.py --port 8808  # base_url: http://127.0.0.1:8808/api/v3
```

```python
from arkruntime import Ark

client = Ark(base_url="http://127.0.0.1:8808/api/v3", api_key="sk-local")
r = client.content_generation.tasks.create(
    model="doubao-seedance-2-5-260628",
    content=[{"type": "text", "text": "a red balloon"}],
    ratio="16:9", resolution="480p", duration=5,
    extra_body={"aivideomaker_max_credits": 60, "aivideomaker_dry_run": True},
)
```

按请求切上游：`X-Avm-Upstream: web|official`（或 `?upstream=`）。选线口诀：
**要省钱走 `web`（turbo ≤8s 免费），要能取消 / 要幂等走 `official`（真取消 + 全额退积分）。**

路由、字段映射与官方《创建视频生成任务》
（[82379/1520757](https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1520757)）逐一对应。

> ⚠️ **两条线的计费口径不同，不能混。** 官方线**提交即计费**、没有免费窗口，所以
> 拿不到显式支出上限（`extra_body.aivideomaker_max_credits` 或 `AVM_OFFICIAL_MAX_CREDITS`）
> 就**拒绝提交**，不发上游请求；响应里的 `effective.billed` 会按当次实际使用的线预告。
> 调试一律先 dry-run。

测试：`python3 -m unittest discover -s tests`（141 项，零消耗、零外发）。
完整说明见 [`src/ark_compat/README.md`](src/ark_compat/README.md)。

## 关键结论

### 一、官方 API 侧

**可用模型 8 个**：`t2v` `i2v` `minimax` `t2v_v3` `i2v_v3` `seedance20` `wan27` `happyhorse`
**5 秒 16:9 价格区间**：15 ~ 250 积分（$0.15 ~ $2.50）

三条最重要的实操结论：

1. **先报价再提交。** `POST /api/v1/quote/{model}` 免费，参数校验逻辑与生成接口完全一致，
   可用来验证参数格式并预知成本（参数写错时返回详细校验提示）。
2. **各模型参数类型不统一。** `resolution` 在 `seedance20` 是数字 `720`，在 `wan27`/`happyhorse`
   是大写字符串 `"720P"`，在 `minimax` 是小写字符串 `"720p"`；比例字段名有 `aspectRatio` 和
   `ratio` 两种；`minimax` 用 `content` 而非 `prompt`。跨模型复用请求体会直接失败。
3. **价格取决于入口，与输出分辨率无关。** `t2v`（底层 minimax_h3）输出 1248×704、码率约
   1.3 Mbps、15 积分；`seedance20` 720p 输出 1280×720、码率约 6.2 Mbps、110 积分。
   同一分辨率下存在明显不同的质量档位。

### 二、web 逆向侧

**网页端有 11 个模型，其中 7 个官方 API 未开放**：

| 仅网页端可用 | 官方 API 已开放 |
|---|---|
| `seedance2` `seedance25` `kling2_5` `kling3` `ltx23` `veo3Fast` `veo31Fast` | `t2v` `i2v` `minimax` `t2v_v3` `i2v_v3` `seedance20` `wan27` `happyhorse` |

网页端独家包括 **Google Veo 3.1（支持 4K）**、**Kling 3**、**Seedance 2.5** —— 这是做 web 逆向的主要动机。
完整对照与待验证事项见 `docs/web-reverse/model-inventory.md`。

## 资产说明

| 文件 | 入口 | 分辨率 | 大小 | 积分 |
|---|---|---|---|---|
| `assets/cyberpunk_sunset.mp4` | `t2v` | 1248×704 | 0.81 MB | 15 |
| `assets/seedance20_sunset_720p.mp4` | `seedance20` `resolution=720` | 1280×720 | 3.86 MB | 110 |

两条使用相同提示词（`A cinematic sunset over a futuristic city skyline`），可用于直观对比
两个入口的码率与画质差异。

## 安全约定

- **API Key 一律通过 `AVM_KEY` 环境变量或 `--key` 参数传入，禁止写入任何受版本控制的文件。**
- **网页端会话 Cookie（`auth_session`）等同账号凭据，同样禁止入库**：`.env`、
  `cookies.json`、`src/web-adapter/.session-state.json` 均已在 `.gitignore` 中排除。
- `.env` 已在 `.gitignore` 中排除。
- 批量或自动化调用必须带 `X-Max-Credits`，避免参数误配导致超额扣费。
- `docs/web-reverse/captured/` 中为公开页面的抓取快照，仅用于离线分析。
