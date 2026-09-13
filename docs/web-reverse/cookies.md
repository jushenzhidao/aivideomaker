# Cookie 与会话过期机制

> 实测时间：2026-09-13
> 站点：`aivideomaker.ai`（Next.js App Router + tRPC + Prisma + Stripe）

## Cookie 清单与过期时间

| Cookie | 用途 | 过期时间 | 依据 |
|---|---|---|---|
| `auth_session` | **登录会话**（服务端 `UserSession.id`） | **2027-10-15T03:41:53Z**（TTL 400 天） | ✅ 实测，见下节 |
| `NEXT_LOCALE` | 界面语言 | **1 年** | ✅ 实测 `Max-Age=31536000` |
| `_ga` | Google Analytics 客户端 ID | 2 年 | GA4 默认 |
| `_ga_01629CFM2X` | GA4 流级别会话 | 2 年 | GA4 默认 |
| `_gcl_au` | Google Ads 转化链接器 | 90 天 | Google 规范 |
| `_clck` | Microsoft Clarity 用户标识 | 1 年 | Clarity 规范 |
| `_clsk` | Microsoft Clarity 会话 | 1 天 | Clarity 规范 |
| `googleRedirectTo` | 注册后回跳路径 | 会话级（关浏览器失效） | 无 Max-Age |

`NEXT_LOCALE` 的实测响应头：

```
set-cookie: NEXT_LOCALE=zh; Path=/; Expires=Mon, 13 Sep 2027 11:42:04 GMT;
            Max-Age=31536000; SameSite=lax
```

其余第三方 cookie 由各自的 JS SDK 在浏览器端写入，curl 侧无法观测，其时长取自各厂商的固定规范。

## `auth_session` 的过期机制

这是唯一真正影响自动化脚本可用性的 cookie，实测结论：

1. **它不是 JWT，无法客户端自解析。** 值为 40 字符的随机串（`^[a-z0-9]{40}$`），
   服务端通过查表校验。

2. **服务端记录了精确过期时间。** 前端打包的服务端 Prisma schema 中可见：

   ```
   UserSession { id, userId, expiresAt, impersonatorId }
   ```

   `expiresAt` 是数据库字段，**未通过任何接口暴露**，响应体里也没有。

3. **服务端不滚动刷新。** 携带有效 `auth_session` 请求受保护页面与 tRPC 接口时，
   **响应中不含任何 `Set-Cookie`** —— 意味着会话不会因持续使用而延长，
   属于「固定 TTL、到期即失效」。

4. **前端代码里没有 TTL 配置。** app 侧全部 JS chunk 中只有 cookie 库的
   `deleteCookie`（`maxAge:-1`）等工具函数，无会话时长常量。

**推断：站点使用 Auth.js（NextAuth v5）的数据库会话策略。**

`src/web-adapter/cookies.example.json` 中出现 `__Secure-authjs.session-token` —— 这是 Auth.js
的会话 cookie 命名；服务端存在 `UserSession` 表也符合 Auth.js 的 **database session** 策略
（而非 JWT 策略）。**Auth.js 的默认 `session.maxAge` 是 30 天（2592000 秒）。**

但要注意：实际生效的 cookie 名是 `auth_session` 而非 `__Secure-authjs.session-token`，
说明站点至少做过命名定制，**默认值只能作为量级参考，不能当作结论**。

## ✅ 已解出：`auth_session` 的实际过期时间

**不必再 watch，也不必去 DevTools 翻** —— 浏览器导出的 **cookie jar**（`cookies.json`，
Chrome 的 EditThisCookie / Cookie-Editor 等扩展导出的格式）里就带 `expirationDate` 字段：

```json
{ "name": "auth_session", "value": "n557e2…3bir", "domain": "aivideomaker.ai",
  "path": "/", "expirationDate": 1823571713, "httpOnly": true, "secure": true, "sameSite": "lax" }
```

换算结果：

| 项 | 值 |
|---|---|
| `expirationDate` | `1823571713` (epoch 秒) |
| **绝对过期时刻** | **`2027-10-15T03:41:53Z`**（本地 `2027-10-15 11:41:53`，UTC+8） |
| 登录时刻（≈订阅开始） | `2026-09-10T03:50:58Z` |
| **TTL** | **400.0 天** |

两个需要澄清的细节：

- **`document.cookie` 读不出，cookie jar 可以。** 之前判断「用户给的 Cookie 字符串不含属性」
  是对的，但导出文件里**包含**属性 —— 这是两条不同的数据通路。
- **TTL 不是 30 天。** 按 Auth.js 默认 `session.maxAge` 推断出的 30 天**不成立**，
  实测为 400 天。默认值只适合作为量级参考，此处站点显然做了定制。

同批 cookie 的过期时间（同一 jar，同一实测来源）：

| Cookie | 过期时刻 (UTC) |
|---|---|
| `auth_session` | 2027-10-15T03:41:53Z |
| `NEXT_LOCALE` | 2027-09-10T04:04:05Z |
| `_ga` | 2027-10-15T04:01:13Z |
| `_ga_01629CFM2X` | 2027-10-15T04:23:50Z |
| `_gcl_au` | 2026-12-09T03:40:25Z |
| `_clck` | 2027-09-10T03:40:25Z |
| `googleRedirectTo` | 2027-01-26T21:46:40Z |

> 注意 `_clsk`（2026-09-11）与 `google_oauth_state`（2026-09-10）在导出时**已过期**，
> 属正常残留，不影响会话。

**对自动化的含义 —— 分两层，别混：**

| 层 | 结论 | 证据等级 |
|---|---|---|
| **浏览器侧** | 浏览器会一直携带这个 cookie 直到 `2027-10-15T03:41:53Z` | ✅ **确定** —— jar 的 `expirationDate` |
| **服务端侧** | 服务端是否也认到那一天 | ⚠️ **未坐实** |

已经坐实的：**不因使用而延长**（4 轮 `auth.user` + 页面请求，响应均无 `Set-Cookie`）。

**未坐实的：「不会提前失效」。** 服务端另有 `UserSession.expiresAt`（数据库字段，
未通过任何接口暴露），它与 cookie 的 `expirationDate` 是否同源，**无法由外部直接验证**。

> ⚠️ **一个值得警惕的不符**：400 天与 Auth.js database-session 的常见配置
> （默认 `session.maxAge` 30 天）相差甚远。若站点只把 **cookie 的 maxAge** 设成 400 天，
> 而数据库 session 另有更短的 `expiresAt`，就会出现**「cookie 还在、服务端已拒」**。
>
> **实证方法**：定期探活。若在 `2027-10-15` 之前失效，就是服务端侧短 TTL 的证据。
> **不要按日历信任这个日期**，把探活当作唯一权威。

**一条支撑「jar 属性 = Set-Cookie 投影」的算术交叉验证**（这决定了 400 天可信到什么程度）：

`NEXT_LOCALE` 有**原始报文**佐证（`Max-Age=31536000` = 365 天），拿它当已知正确答案标定：

```
jar 过期 2027-09-10T04:04:05Z  −  Max-Age 365 天  =  下发时刻 2026-09-10T04:04:05Z
```

与「页面请求发生在 2026-09-10 04:04 前后」完全自洽 ⇒ **jar 的 `expirationDate`
确实是 Set-Cookie 的投影**（不是扩展自己编的）。

同法反推 `auth_session`（它没有报文，只能这样推）：

| 假设 Max-Age | 反推出的下发时刻 | 与登录时刻（≈2026-09-10T03:50:58Z）是否吻合 |
|---|---|---|
| **400 天** | `2026-09-10T03:41:53Z` | ✅ **最贴合** |
| 365 天 | `2026-10-15T03:41:53Z` | ❌ 比登录晚一个多月 |
| 30 天 | `2027-09-15T03:41:53Z` | ❌ 比登录晚一年 |

⇒ **cookie 自己的 maxAge 极可能就是 400 天**（这一步是可信的）。
⇒ 但**服务端 DB 的 `expiresAt` 是另一个数**，从这里推不出来 —— 那才是真正的未知量。

兜底手段仍然保留（换账号、被风控登出等情况）：

```bash
# Node：会话探活 + 记录观察到的存活时长
AVM_COOKIE='...' node src/web-adapter/tools/check-session.mjs
# Python
python3 src/web_session.py verify
```

## 相关：API Key 不会过期

服务端 `ApiKey` 模型为：

```
ApiKey { id, createdAt, userId, name, key, status, dayLimit, rpm,
         creditsWarning, source, medium, campaign, platform, skillVersion, deleted }
```

**没有 `expiresAt` 字段。** 官方 API 用的 `key` 不会自然过期，
只能通过 `status` 置为禁用或标记 `deleted`。这与 `auth_session` 形成对比：
API Key 适合长期自动化，网页会话需要定期续期。

## 结论速查

| 场景 | 建议 |
|---|---|
| 自动化调用官方 API | 用 `key`，**不会过期**，无需维护 |
| 抓取网页端接口 | 需要 `auth_session`，**固定 TTL 400 天且不滚动刷新**；到期日 `2027-10-15`，可提前排期重新导出 |
| 判断会话是否仍可用 | `node src/web-adapter/tools/check-session.mjs` 或 `python3 src/web_session.py verify` |

---

## 复验记录（2026-09-13，只读诊断脚本）

```bash
python3 src/web_session.py probe --rounds 4      # 全只读：身份 / 闸门 / 余额
```

一次跑完五项检查：

| 检查 | 结果 | 判读 |
|---|---|---|
| 带凭据连发 4 次 `auth.user` | 4/4 HTTP 200，**`Set-Cookie: 无`** | 会话有效，且**服务端不重下发会话 cookie** |
| 匿名对照（同请求去掉 cookie） | HTTP 200 但**无用户实体** | 必须有 cookie；`auth.user` 不给匿名者身份 |
| 页面请求 `GET /zh/ai-video-generator` | HTTP 200，**`Set-Cookie: 无`** | 连 `NEXT_LOCALE` 都没重发（请求里已带） |
| `/api/auth/session` · `/csrf` · `/providers` | 三个全 **404** | 非标准 Auth.js 实现，别照框架默认推断 |
| `needsCaptcha` / `totalRemaining` | `False` / `796` | 闸门当前关闭；余额与官方 API 同池 |

### "TTL 30 天" 的说法不成立

`auth_session` 的绝对过期时间**写在导出 jar 的 `expirationDate` 里**：

```bash
python3 -c "import json;print([c['expirationDate'] for c in json.load(open('avm-proxy/cookies.json')) if c['name']=='auth_session'])"
# → 1823571713
date -u -r 1823571713 "+%Y-%m-%dT%H:%M:%SZ"     # → 2027-10-15T03:41:53Z
```

- **绝对过期**：`2027-10-15T03:41:53Z`
- **TTL**：自登录时刻（`2026-09-10T03:50:58Z`）起算 **≈400 天**
- 那个 `30 天` 是 Auth.js 的**框架默认值**（`session.maxAge`），本站做了定制。
  默认值只能当量级参考，**不能当结论**。

为什么只能从 jar 读 —— 四条路都堵死：

1. token 是 40 位不透明随机串（**不是 JWT**，客户端无法自解析）；
2. `auth.user` 响应不含过期时间；
3. 普通 API 请求与页面请求**都不重下发** `auth_session`（本次 4 轮 + 页面请求均验证）；
4. 服务端 `UserSession.expiresAt` 未通过任何接口暴露。

### 探活接法

`probe` 的退出码语义：会话有效 `0`、无效/异常 `1`；**网络故障直接非零退出**
（不会把"网络不通"误判成"会话失效"）。

```bash
# 每天 09:00 探活，失效即告警
0 9 * * * cd /path/to/videos && python3 src/web_session.py verify \
  || echo "aivideomaker 会话失效，需重新导出 cookie"
```

把到期日当**可排期事件**：`2027-10-15` 前用浏览器重新导出 cookie 覆盖
`avm-proxy/cookies.json` 即可（`probe` 会打印指纹，换 cookie 后指纹变化肉眼可辨）。

> 报告默认落 `/tmp/session-probe-<ts>.json`（按约定不入库）。
> 脚本只打印凭据**指纹**（`n557e2…3bir`），**从不打印凭据值**。
