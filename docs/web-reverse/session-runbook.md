# 会话凭据 runbook（web 线）

> **定位**：把「cookie 顶多久 / 要不要自动刷新 / 能不能绕过去」这三个问题固化成**可复现流程**。
> 事实来源：本篇为流程，TTL 与实测证据见 [`cookies.md`](./cookies.md)，
> 适配层行为见 [`src/web-adapter/README.md`](../../src/web-adapter/README.md)。
> 最后核验：2026-09-13。

---

## 1. 凭据落位（二选一，均已在 `.gitignore` 内）

| 方式 | 用法 | 适用 |
|---|---|---|
| 环境变量（**推荐**） | `export AVM_COOKIE='auth_session=…; NEXT_LOCALE=zh'` | 临时跑、CI、不落盘 |
| cookie 文件 | `src/web-adapter/cookies.json`（Cookie-Editor 导出的**数组**格式），或用 `COOKIES_FILE` 指向别处 | 长期本地开发 |

加载优先级与 `adapter.mjs` 一致：**`AVM_COOKIE` 先于 `COOKIES_FILE`**（后者默认 `./cookies.json`，需为
`[{name,value,…}]` 数组，会被拼成 Cookie 头）。

### ⚠️ `AVM_COOKIE` 是**整条 `Cookie` 请求头**，不是 token

这个值会被**逐字**发出，所以格式不对**不会报错**——服务端只是把你当成未登录，
而这与「会话已过期」**表现完全一致**，结果会让人去追一个根本没到期的 TTL。

**能接受的粘贴形态**（工具会自动剥掉脏壳，并在**修过**的时候告警）：

| 你粘贴的 | 结果 |
|---|---|
| `<40位>` 裸 token | 补成 `auth_session=<40位>` + 告警 |
| `auth_session=<40位>` | ✅ 直接可用（**推荐写法**） |
| `auth_session=<40位>;NEXT_LOCALE=zh` | ✅ 直接可用（`;` 后有无空格都行） |
| `Cookie: auth_session=…` | ✅ 剥掉 `Cookie:` 标签 |
| `-H 'Cookie: auth_session=…'`（cURL 片段） | ✅ 剥掉 cURL 包装 |
| `Set-Cookie: auth_session=…; Path=/; HttpOnly` | ✅ 丢掉 `Path`/`HttpOnly` 等**属性** |
| `[{"name":"auth_session","value":"…"}]`（导出 jar JSON） | ✅ 拼成 Cookie 头 |
| 多行粘帖、每行以 `;` 结尾 | ✅ 正常解析 |
| 认不出来的（太短、含空格、换行但缺 `;`） | ⛔ **原样下发**，让 HTTP 层明确报错——**绝不猜** |

`NEXT_LOCALE=zh` **非必需**（那是界面语言 cookie，与鉴权无关）。

> **一个刻意的取舍**：修过的形态一律**告警**、不静默修正（否则用户永远学不会正确写法）；
> 而"换行但缺 `;`"这种畸形输入**不做容错**——折叠它会把两对粘成一对、发出一个
> "看似合法"的错误 cookie，属于**沉默发错**，比直接报错危险得多。

**两侧同规则**（三处实现、两份单测，用例表逐条对应）：

| 侧 | 实现 | 单测 |
|---|---|---|
| JS | `src/web-adapter/client.mjs` → `normalizeCookieHeader()` | `npm --prefix src/web-adapter run test:unit` |
| Python | `src/ark_compat/cookie.py` → `normalize_cookie_header()` | `python3 -m unittest tests.test_cookie_normalize` |
| Python（零依赖工具） | `src/web_session.py` 内联同一规则（刻意不 import 本包） | 同上，含**两实现一致性**用例 |

改任一处，必须同步改另两处并跑两份单测。接线也一并覆盖：`Settings.from_env` 与
`WebClient.__init__` 都已接入——**只测实现不测接线，等于没接**。

`.gitignore` 已覆盖：`cookies.json`、`src/web-adapter/cookies.json`、`src/web-adapter/.session-state.json`。
**任何情况下不要把 Cookie 写进 `.env` 后提交** —— `.env` 本身被忽略，但复制到别处即泄露。

> 该 Cookie 等同账号凭据：能读账号信息、能消耗积分。工具脚本只会打印短指纹（如 `n557e2…3bir`），
> **从不打印 Cookie 值本身**。

---

## 2. 三条命令

```bash
cd src/web-adapter

# ① 有凭据：身份 + 验证码闸门 + 余额（纯 query，零积分、不创建任务）
AVM_COOKIE='auth_session=…' node tools/session-diagnose.mjs

# ② 无凭据：匿名对照（同样零成本），用于判定"免登录这条路通不通"
node tools/session-diagnose.mjs

# ③ 会话探活 + 存活计时（退出码非 0 = 已失效，可直接挂 cron / 告警）
AVM_COOKIE='auth_session=…' node tools/check-session.mjs
```

**离线自检**（完全不联网）：

```bash
node tools/session-diagnose.mjs --dry-run          # 构造请求但不发送
node tools/session-diagnose.mjs --dry-run --submit # 连提交也一并预演
```

> `--dry-run` 下脚本**不输出任何结论**（只打印 `(not sent)`）。
> 这是刻意的：一个在没问过服务端的情况下断言"闸门已关闭"的体检脚本，比没有更糟。

---

## 3. 到期与续期：**不存在"刷新"，只能排期**

| 项 | 值 |
|---|---|
| 绝对到期 | `2027-10-15T03:41:53Z`（本地 `2027-10-15 11:41:53`） |
| TTL | **400 天**（登录 `2026-09-10T03:50:58Z` 起算） |
| 截至 2026-09-13 剩余 | **396.6 天** |

"自动刷新"在技术上不成立，三条独立证据：

| 检查项 | 结果 |
|---|---|
| `GET /api/auth.user`（带有效 cookie） | 响应**无 `Set-Cookie`** |
| 页面请求 `/zh/generations` | 只下发 `NEXT_LOCALE`（1 年），**不下发 `auth_session`** |
| `/api/auth/session`、`/api/auth/csrf`、`/api/auth/providers` | 全部 **404**（非标准 Auth.js 路由） |
| token 形态 | `^[a-z0-9]{40}$` 不透明随机串，**非 JWT**，客户端不可自解析 |

`session-diagnose.mjs` 会顺带复核这一条：若任一探针回执里出现 `re-issued:`，说明站点行为变了，
**必须回头修订 `cookies.md`**。

**✅ 已实测（2026-09-13，带有效凭据，零成本只读）** —— 三条探针**全部 `200` 且返回实体**，
且**响应里没有任何 `Set-Cookie`** ⇒ **有凭据路径下确认不滚动续期**：

| 探针 | 实测值 |
|---|---|
| `auth.user` | `userId=cmtuzdsvw000bsq5zsfbgefnt`（会话有效） |
| `model.needsCaptcha` | `false`（闸门当前关闭，可读） |
| `credits.getCredits` | `totalRemaining=796` |
| `Set-Cookie` | **无**（无滚动续期） |

`796` 与官方 API 侧 `currentBalance` 同池口径一致 ⇒ 两份快照无漂移。

**对照**：同一组探针**不带凭据**时**全部被拒**（三种形态见 §5）⇒ 匿名与登录态的差异
是**授权**，不是输入问题。这也说明 §5 里 `needsCaptcha` 的 `400` 确实只是"匿名态拿不到
字符串 `userId`"，而非权限拒绝。

**正确策略**：定时探活（②）→ 失效告警 → **到期前人工重导一份**。到期日固定，所以这是**可排期事件**。

### 3.1 重导流程（"刷新"的唯一实现方式）

**没有"刷新"这个动作，只有"重新登录 + 重新导出"。** 服务端只在**登录事件**上下发一次
`Set-Cookie`；请求续期、refresh 端点、延长接口三样都不存在（证据见上表）⇒ 想延长有效期，
只能**制造一次新的登录**。

先按征兆区分是哪种情形 —— 处置相同，但**记录要求不同**：

| 情形 | 征兆 | 附加动作 |
|---|---|---|
| 自然到期 | `2027-10-15` 之后 `check-session.mjs` 退出码 1 | 无 |
| **服务端提前失效** | 日历未到就失效 | ⚠️ **必须记录 `first_seen` → `last_ok` 的实测存活时长，回来修订 `cookies.md`** —— 这是判定"服务端 DB 的 `expiresAt` 是否比 cookie 更短"的唯一证据 |
| 换账号 / 被风控登出 | 需要另一个账号的余额 | 无 |

**步骤**：

1. 浏览器打开 `aivideomaker.ai`，**确认已登录**（未登录直接导出等于导出一份匿名 cookie）。
2. 用 **Cookie-Editor / EditThisCookie 扩展**导出为 **JSON 数组**格式。
   ⚠️ **这一步不可替代**：必须是扩展导出，因为只有它带 `expirationDate`；
   `document.cookie` 读不到属性，手抄的 `Cookie` 头同样没有。丢了它，TTL 就只能靠探活猜。
3. 覆盖写入 `avm-proxy/cookies.json`（`web_session.py` 按
   `avm-proxy/cookies.json` → `src/web-adapter/cookies.json` → `cookies.json` 顺序自动查找）。
4. **核验指纹必须变化**：

```bash
python3 src/web_session.py probe --rounds 1     # 打印指纹（n557e2…3bir）与剩余天数
```

5. 探活：

```bash
AVM_COOKIE="auth_session=$(python3 -c "import json;print([c['value'] for c in json.load(open('avm-proxy/cookies.json')) if c['name']=='auth_session'][0])")" \
  node src/web-adapter/tools/check-session.mjs
```

**指纹未变的两种可能**，且症状相同（都能读到账号、都不会报错）⇒ 必须靠指纹判定，别靠"跑通了"：

- 你根本没重新登录，导出的还是旧 cookie；
- 你把 jar 写到了别处，工具读的还是旧文件。

**状态文件不需要手工清理**：`check-session.mjs` 检测到指纹变化会**自动重置** `first_seen`
（见其 `if (state.fingerprint !== fingerprint)` 分支）。想强行归零才需要删。

> ⚠️ **状态文件按脚本目录落位，不是 cwd。** 历史上它曾写在 `process.cwd()`，
> 结果在 `avm-proxy/` 与 `src/web-adapter/` 各留一份、`first_seen` 相差三天，
> **"已确认存活"这个数字变得不可信** —— 而这正是判定"服务端是否提前失效"的唯一依据。
> 现已锚定到脚本所在目录（`src/web-adapter/.session-state.json`）。若见到
> `avm-proxy/.session-state.json` 这类旧副本，属修复前残留，可删。

---

## 4. "绕过去"有三种含义，判定各不相同

| 含义 | 结论 | 依据 |
|---|---|---|
| 绕开 cookie → 改用官方 API `key` | ✅ **可行（已验证）** | `ApiKey` 模型**无 `expiresAt` 字段**，不会过期；无 Turnstile 闸门 |
| 绕开 cookie → 只带 `visitorId` 匿名调用 | ⚠️ **未坐实**（见 §5） | 项目内无归档证据；`visitorId` 服务端不校验，属弱标识 |
| 绕开 `needsCaptcha` 动态闸门 | ❌ **不可行**（除非带 token） | 闸门开着时 `token: null` 会被静默拒；**带上真 token 即可连投**（2026-09-14 实测：自动化 Chrome 也能产出 token，条件见 skill `browser-cdp-anti-bot-proxy` 的 09-14 更正）；或等衰减 |

补充：**你自己贴的那条 curl 没有 `Cookie` 头**，只有 `visitorId` + `token: null`。
其中 `token: null` 仅在 `needsCaptcha=false` 时有效；闸门开启后**同一个 `token: null` 会被静默拒绝
—— 返回空字符串 `""`，不报错**。这种"看起来没失败其实没提交"的形态最容易误判为成功。

---

## 5. 匿名路径：读接口被拒（**已实测**），提交通路仍待坐实

**已实测（2026-09-13，`node tools/session-diagnose.mjs` 不带凭据；只读 query、零成本）：
匿名态读不到任何身份信息。** 关键在于——**拒绝形态有三种，只看状态码必然判错**：

| procedure | HTTP | 返回形态 |
|---|---|---|
| `auth.user` | **200** | **`json: null`（空实体）** —— 这里的 "200" **不是成功** |
| `model.needsCaptcha` | **400** | `BAD_REQUEST`，根因是 `userId` 必须为 **string**（传 `null` 被 zod 挡下） |
| `credits.getCredits` | **401** | `UNAUTHORIZED` |

三条推论：

1. **判据必须改成「状态码 + 返回体里有实体」**；只认状态码会把 200 的空实体读成通过。
2. `model.needsCaptcha` 需要**字符串 `userId`** ⇒ 匿名态**读不到闸门**。任何"没有 cookie
   却显示 `needsCaptcha=false`"的输出都是**假信号**（不是"闸门关闭"）。
3. 同一个"未登录"在三种 procedure 上是三种形态 ⇒ **按状态码分支的代码一定会错**。

**仍未坐实的部分**：以上只证明**只读接口**拒绝匿名。`ai.minimaxH3` 是 **mutation**，
权限模型可能不同（有的站点只拦读、放行试用生成）。所以：

- **用户报告**：此前有过成功案例（口头，未归档）。
- **项目内证据**：无。适配层 `client.mjs` **始终携带 cookie**；Prisma `User` 表带
  `visitorId` / `anonymousUserIp` 字段，只能说明站点为匿名访客建过记录，**推不出
  `ai.minimaxH3` 可免登录提交**。
- **坐实方式**（会创建一个任务，需显式确认）：

```bash
node tools/session-diagnose.mjs --submit
```

**判定标准很关键**：

| 返回 | 含义 |
|---|---|
| `taskId=<非空串>` | ✅ 匿名提交真的成功 |
| `data=""` | ❌ **静默拒绝** —— 不是成功。多半是闸门开启或匿名不被受理 |
| HTTP 4xx / `error=…` | ❌ 明确拒绝（如未授权） |
| `200` + 空实体 | ❌ **未授权**（本站 `auth.user` 的实际形态） |

坐实后请回来**更新本节**，并给出时间、返回体与是否计费。

> 本节结论已由**离线桩测试**固化：`npm run test:diagnose` 重放上表三种拒绝形态，
> 并断言工具不会把 200 空实体读成通过、且 `--dry-run` 不输出任何结论。

---

## 6. 计费护栏（最高优先级，先读再跑）

- web 线的免费窗口只有：**`tier=turbo` 且 `duration ≤ 8s`**（如 480p / 5s）。
  `tier=base` **一律计费**；`turbo` 且 `≥ 9s` 也计费。
- `--submit` 的请求体**硬编码**为该免费组合，**故意不可参数化** —— 它是安全带，不是便利项。
- 判断一次生成是否花钱，看任务记录的 **`paid` 字段**；**别看 `credits`**（两者反相）。
- 批量/自动化前先读 [`src/web-adapter/README.md`](../../src/web-adapter/README.md) 的提交队列与
  `X-Max-Credits` 章节。web 线**没有** `X-Max-Credits` 这类保护。
- **提交前必须重新探测闸门。** `needsCaptcha` 是**动态**的、会翻转 —— 文档里记的
  `false` 只是某次快照：2026-09-13 当天先测到 `false`，几小时后再测就是 `true`。
  沿用旧快照的代价是：闸门已开启时，`token: null` 的请求会被**静默拒绝**（返回空字符串
  `""`，不报错），看起来像提交成功。**一律以当次 `check-session.mjs` / `probe` 的输出为准。**

---

## 7. 常见失败判读

| 现象 | 含义 | 处置 |
|---|---|---|
| `auth.user` 不回 `id` | 会话已过期，或凭据串不完整 | 重导 cookie；核对是否为完整 `auth_session=<40 位>` |
| `model.needsCaptcha` 报 4xx | 该 procedure 带 input（`{userId}`），无身份时可能被拒 | 先跑 ① 有凭据版拿到 `userId` 再问 |
| `--submit` 返回 `""` | **静默拒绝，不是成功** | 见 §5 判定标准，别当成通过 |
| `--submit` 返回 4xx / `error=…` | 明确拒绝（未授权，或闸门已开启） | 看 `code` 分支处理，**不要盲目重试到计费** |
| `check-session.mjs` 退出码非 0 | 会话已死 | 重新导出；指纹变化会自动重置计时 |
| `model-status/token` 返回 429 | 该端点有频控 | 高频轮询别走 SSE 路径，改用 `model.listModel` / `model.getModel` |

> 排查补充：网络层若被代理劫持，拿到的是**别的网关的错误体**而不是 `connection refused`
> ——这个坑在本项目其它线出现过（见 `src/ark_compat/README.md` 的 `trust_env` 条目）。
> 网页线本身是外网调用、通常不涉及回环，但归因失败时先排除代理变量。

---

## 8. 风控与封号：**不要伪造指纹**

结论：**不伪造。** 它解决不了封号问题，而且在 TLS 层**反而提高**可疑度。

### 8.1 先分清两个"指纹"——混淆它们会直接导致错误决策

| | 凭据短指纹（`n557e2…3bir`） | 浏览器 / 设备指纹 |
|---|---|---|
| 是什么 | 本工具打印凭据时的**脱敏显示值** | UA、TLS 握手、canvas/WebGL、`visitorId` 等 |
| 去哪了 | **从不进入任何请求** | 部分进入请求头 |
| 伪造它 | **无意义** —— 服务端看不到这个值 | 无收益，见 §8.3 |

### 8.2 三条证据表明站点不做指纹风控

1. **数据模型里没有任何封禁 / 风险字段。** 泄露的 Prisma 片段中 `User` 为
   `{ …, firstGenerate, deleted, anonymousUserIp, question, visitorId }` ——
   **没有** `banned` / `riskScore` / `deviceFingerprint` 一类字段。
2. **风控的设计理念是「按凭据限流」。** `ApiKey { …, dayLimit, rpm, … }` ——
   日额度 + 每分钟请求数。这是站点自己给出的节流维度，与浏览器指纹无关。
3. **实测反证（最有力）**：当前客户端"指纹"其实相当不自洽 ——
   TLS 握手是 Python `urllib` / Node `fetch`（与 Chrome 相差极远）、
   不发 `sec-ch-ua` / `sec-fetch-*` 系列、不执行任何 JS —— **却从未被拦**。
   若站点做这层风控，**第一次调用就该被拒**；实测多轮全部 `200`。

### 8.3 伪造为什么反而更危险

在做 TLS 指纹（JA3/JA4）的风控里，**「UA 声称是 Chrome、TLS 指纹却是 Python」是最高优先级的
可疑信号** —— 它比一个诚实的 Python 客户端**更**可疑，因为它表达了**伪装意图**。
真要做到形似需 curl-impersonate 级别的 JA3 伪装，成本高，且一旦被识破后果更重。

> 一句话：**裸奔被判的是「是个脚本」，伪装被判的是「是个想藏起来的脚本」。**

### 8.4 真风险在行为侧，不在指纹

| 维度 | 现状 | 依据 |
|---|---|---|
| 频率 | `model-status/token` 实测返回过 **429** | §7 |
| 并发 | 上游同时只跑 1–2 个任务 | `src/web-adapter/README.md` |
| `visitorId` **硬编码** | `client.mjs` 全项目共用同一个固定值 | 代码 |

最后一条值得单列：`User.visitorId` 字段存在，说明站点**会记录**该值。服务端不校验它
（故不影响鉴权），但**全项目共用一个值**意味着跨账号、跨会话的行为，在服务端看来来自
同一个"访客"。
**证据等级**：字段存在为**实测**；"会被用于行为关联"为**推演**。

**建议**：① 控频（别高频轮询 `model-status/token`）；② **按账号设置 `AVM_VISITOR_ID`**，
不要沿用内置的全局默认值（见下）；③ 长跑优先走官方 API 线 —— `ApiKey` 自带
`dayLimit` / `rpm`，是站点自己认可的节流方式。

> **`AVM_VISITOR_ID` 一直可配，只是模板漏收录。** 代码侧 `client.mjs`、
> `session-diagnose.mjs` 与 `ark_compat/web_client.py` 都读该环境变量，回退到同一个
> 内置默认值 `f29ee26…`。`.env.example` 此前未列出它，"能不能配"因此不明确 —— 现已补上，
> 三处默认值的一致性由 `tests/test_ua_consistency.py` 锁死。

> 附：UA 在 **4 个**源码里各写一份（`client.mjs` / `session-diagnose.mjs` /
> `web_session.py` / `ark_compat/web_client.py`），此前是 3 处 `Chrome/152` +
> 1 处 `Chrome/140` 的分叉。**已统一为 `Chrome/152`**，并由
> `tests/test_ua_consistency.py` 锁死（含**覆盖清单**断言 —— 某处定义被删也会红）。
> 注意：统一 UA 只为消除自相矛盾，**不是"伪装"**，§8.3 的结论不变。

---

## 9. 与其余文档的关系

| 想知道 | 去哪 |
|---|---|
| TTL 怎么测出来的、同批 cookie 各自何时过期 | [`cookies.md`](./cookies.md) |
| tRPC 内部接口清单、Prisma 数据模型、积分池口径 | [`README.md`](./README.md) |
| 适配层如何对外暴露 OpenAI / MiniMax / Ark 协议 | [`src/web-adapter/README.md`](../../src/web-adapter/README.md) |
