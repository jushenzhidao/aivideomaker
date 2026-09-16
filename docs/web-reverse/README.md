# web 逆向侧

本项目只对接 aivideomaker.ai 的**网页端内部接口**：

| 入口 | 认证 | 状态 |
|---|---|---|
| 网页端内部接口（tRPC over `/api/*`） | 登录会话 Cookie（`auth_session`） | ✅ 已实现，代码见 `src/web-adapter/` |

> 本目录是**调研笔记**（接口形态、数据模型、模型清单、Cookie 机制）；
> **实现与对接文档**在 [`src/web-adapter/README.md`](../../src/web-adapter/README.md)，两者互补。
> 会话检测工具：`src/web-adapter/tools/check-session.mjs`（Node）与 `src/web_session.py`（Python）。

---

## 当前进展

### 已完成：公开信息面侦查（无需登录）

抓取并归档了公开页面与前端 JS，从中提取出关键情报：

1. **网页端模型清单（11 个）**，其中 **7 个是网页端独家**：
   `seedance2`、`seedance25`、`kling2_5`、`kling3`、`ltx23`、`veo3Fast`、`veo31Fast`。
   完整对照表见 [`model-inventory.md`](./model-inventory.md)。

2. **网页端计费口径（实测修正）**：以任务记录的 `paid` 布尔字段为准 ——
   `tier=base` **一律计费**；`tier=turbo` 时 `duration ≤ 10s` 免费、`≥ 11s` 计费。
   注意 `credits` 字段与 `paid` **反相**（`paid=false` 记 1、`paid=true` 记 0），
   判断是否花钱只看 `paid`。34 条任务样本与 480p 时长吸附断层的完整数据见
   [`TESTCASES.md` §1](./TESTCASES.md)。

3. **官方公开资源路径**（从 `robots.txt` 发现）：
   `llms.txt`、`docs/aivideo-openapi.json`、`docs/api/{quickstart,minimax-h3,task-lifecycle}`。

### 已完成：登录态接口已打通（2026-09-13）

携带 `auth_session` Cookie 后，`/zh/app/*` 页面由 `307` 变为 `200`，app 侧 JS chunk 可获取，
内部接口已实测可用。

**关键技术事实：网页端不是 REST，而是 tRPC over `/api`。**

```
GET /api/{procedure}?batch=1&input={"0":{"json":null,"meta":{"values":["undefined"]}}}
```

- **路径没有 `/trpc` 前缀**，procedure 直接拼在 `/api` 之后（如 `/api/auth.user`）。
- 前端由 `createClient({links:[httpBatchLink({url: location.origin + "/api"})]})` 构造，
  因此路径是运行时拼接的，静态搜索 `"/api/xxx"` 找不到任何结果。
- 用 `POST` 调 query 类型 procedure 会返回
  `No "mutation"-procedure on path "..."`，可据此判断 procedure 类型。

**已实测可用的 procedure：**

| procedure | 类型 | 返回 |
|---|---|---|
| `auth.user` | query | 当前用户（id / email / role / avatarUrl / onboardingComplete / impersonatedBy） |
| `credits.getCredits` | query | 积分总额与明细（`totalAmount` / `totalRemaining` / `credits[]`） |
| `billing.subscription` | query | Stripe 订阅对象 |
| `billing.plans` | query | 套餐列表 |
| `ai.queryUserPermission` | query | 需传 input，否则 400 |
| `auth.loginWithEmail` / `auth.loginWithPassword` / `auth.signup` / `auth.logout` | mutation | 登录注册登出 |
| `admin.unimpersonate` | mutation | 退出模拟身份 |

工具：`python3 src/web_session.py verify` / `call <procedure>` / `watch`

### 从前端 bundle 泄露的服务端数据模型

`2417-742cdfe3addf9964.js` 中被错误打包进前端的 Prisma schema 片段：

```
UserSession         { id, userId, expiresAt, impersonatorId }
User                { id, email, emailVerified, role, name, avatarUrl, createdAt,
                      hashedPassword, onboardingComplete, apiIntroCreditsGranted,
                      firstGenerate, deleted, anonymousUserIp, question, visitorId }
ApiKey              { id, createdAt, userId, name, key, status, dayLimit, rpm,
                      creditsWarning, source, medium, campaign, platform,
                      skillVersion, deleted }          ← 注意：无 expiresAt
Credits             { id, userId, createdAt, type, amount, remaining,
                      effectiveAt, expirationAt, remark }
CreditsType         { PACK, CHECKIN, SHARE, SUBSCRIPTION, GIFT }
SubscriptionStatus  { TRIALING, ACTIVE, PAUSED, CANCELED, PAST_DUE, UNPAID,
                      INCOMPLETE, EXPIRED }
```

两条结论：

1. **API Key 没有过期字段**，不会自动失效，只能手动删除或置为禁用。
2. **积分池与 API 侧共享**：`credits.getCredits` 返回的 `totalRemaining` 与
   官方 API `GET /api/v1/account` 的 `currentBalance` 数值一致（实测均为 796）。
   网页订阅权益与 API 权益不同，但消耗的是同一份积分。

### 剩余待办

- [x] 建立登录态并抓取 app 侧 JS chunk。
- [x] 定位内部接口（tRPC over `/api`）。
- [ ] 从 app 侧 chunk 中提取 tRPC router 的**完整** procedure 清单（当前靠 grep 关键字，可能不全）。
- [ ] **补记（2026-09-16 核查）**：上一条**没有现成资产可用** —— `captured/app-js/` 只是
      **路由级**的 chunk 集合（`layout-*` / `main-app-*` / `page-*`），
      `grep -r` 在里面搜 `minimaxH3` / `needsCaptcha` / `getModel` / `listModel`
      **全部为空** ⇒ 它不含 `/zh/ai-video-generator` 生成页的懒加载 route chunk。
      要拿清单必须按**登录态重抓该 route** 的 chunk（现有 34 个 app-js 里没有）。
- [ ] **模型映射的前置**：站点 11 个模型各自对应哪条 procedure、哪种 web_params 形态
      （键名见 [`model-inventory.md`](./model-inventory.md)）。
      **2026-09-17 更新**：落点已定且**实现已落地**（`X-Channel-Options.model` / `.model_map`，
      语义见 `docs/channel-options-model.md`，代码见 `src/ark_compat/channel_options.py`）。
      **这条清单仍是待办，但它已不再是"能不能做"的阻塞** —— 现在缺它只影响两件事：
      ① 把 `ai.<槽位>` 这个**推断**换成实测表（当前映射到非 `minimaxH3` 槽位时证据里标
      `model_verified=false` 并告警）；② 补 per-model 的 `duration` / `resolution` 合法域
      （各模型上限不同，见 `src/ark_compat/README.md`「模型映射」）。
- [ ] 验证网页端未开放模型（`kling3`、`veo31Fast`、`seedance25` 等）的生成调用链。
- [x] ~~实测 `auth_session` 的实际 TTL~~ —— **已解出，无需 watch**：
      浏览器导出的 cookie jar 里带 `expirationDate` 字段，`auth_session` 为
      **`2027-10-15T03:41:53Z`（本地 2027-10-15 11:41:53）**。
      对照订阅开始时间 `2026-09-10T03:50:58Z`，**TTL = 400 天**。
      该值在登录时一次性固定，站点**不做滚动续期**（普通 API 请求与页面请求均不重下发
      `auth_session`）。同批 cookie 的其余过期时间见 [`cookies.md`](./cookies.md)。

## 抓取资产

`captured/` 目录：

| 文件 | 说明 |
|---|---|
| `docs-zh.html` / `docs-en.html` | 官方文档页（中/英），**模型清单的主要来源** |
| `home.html` | 首页 |
| `settings-api-keys.html` | API 密钥设置页（实际为 307 重定向响应体，含登录引导文案） |
| `js/*.js` | 首页加载的 38 个 Next.js chunk（未登录态） |
| `chunk-list.txt` | chunk 路径清单 |
| `sitemap.xml` | 站点地图（228 条 URL，含多语言 docs 路由） |

## 复现抓取

```bash
cd docs/web-reverse/captured

# 公开页面（无需登录）
curl -sS "https://aivideomaker.ai/zh/docs" -H "User-Agent: Mozilla/5.0" -o docs-zh.html
curl -sS "https://aivideomaker.ai/docs"    -H "User-Agent: Mozilla/5.0" -o docs-en.html
curl -sS "https://aivideomaker.ai/"        -H "User-Agent: Mozilla/5.0" -o home.html
curl -sS "https://aivideomaker.ai/robots.txt"  -o robots.txt
curl -sS "https://aivideomaker.ai/sitemap.xml" -o sitemap.xml

# 提取模型清单
python3 ../../../src/extract_web_models.py docs-zh.html docs-en.html --format md
```

## 待办

- [ ] 建立登录态（浏览器 Cookie 或账号密码换取 token），抓取 `/zh/app/*` 的页面 JS chunk。
- [ ] 从 app 侧 chunk 中定位内部生成接口的路径与请求体结构（参考 `src/extract_web_models.py --api-paths`）。
- [ ] 验证网页端未开放模型（`kling3`、`veo31Fast`、`seedance25` 等）的内部接口是否可直调。
- [ ] 验证 `default` 模型与 API 侧 `t2v`/`i2v` 的对应关系。
- [ ] 若内部接口可复用，参考 `src/avm.py` 的结构补齐 web 侧客户端（注意：需处理会话过期与风控）。

## 注意

网页端接口属于未公开的内部实现，**不保证稳定性，且可能触发风控**。
调用前应确认账号安全边界；`X-Max-Credits` 之类的保护措施在 web 侧不存在，误操作会直接消耗账号积分。
