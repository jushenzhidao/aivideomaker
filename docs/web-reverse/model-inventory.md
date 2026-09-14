# 模型清单对照：web 端 vs 官方 API

> 数据来源：`captured/docs-zh.html` 与 `captured/docs-en.html` 中的 Next.js i18n 文案
> （提取命令：`python3 src/extract_web_models.py docs/web-reverse/captured/docs-*.html`）
> API 侧数据来源：`GET /api/v1/account` 的 `supportedModels`（官方权威）。
>
> 采集时间：2026-09-13

## 核心结论

**网页端有 11 个模型，官方 API 只开放 8 个。** 其中 7 个模型（Veo 3.1、Kling 3、Seedance 2.5 等）
**仅存在于网页端**，官方 API 的生成接口不提供 —— 这是做 web 逆向的主要动机。

## 对照表

| 网页端模型 | 定位 | 能力说明（文案原文摘要） | 官方 API 是否开放 |
|---|---|---|---|
| `default` | 480P Free | 480p 免费生成，最长 8 秒。订阅用户 720p 5/8 秒免费、15/20 秒 3 积分/秒；1080p 4 积分/秒 | 疑似对应 `t2v` / `i2v`（待验证） |
| `HappyHorse` | 同步写实 | 专业电影级视觉还原度，实时音画对齐 | ✅ `happyhorse` |
| `seedance20` | 电影质感 | 文生视频与图生视频，480P/720P，4–15 秒，灵活宽高比 | ✅ `seedance20` |
| `wan27` | 电影级 | 文生视频与图生视频，720P/1080P，5–15 秒，支持提示词扩写 | ✅ `wan27` |
| `seedance2` | 专业版 | 更智能的动态表现与高分辨率输出 | ❌ **仅网页端** |
| `seedance25` | 专业电影质感 | 480P/720P，4–20 秒，更多宽高比 | ❌ **仅网页端** |
| `kling2_5` | 极速 | 专业视觉特效、电影短片、逼真物理效果 | ❌ **仅网页端** |
| `kling3` | 下一代 | 电影级画面、先进物理真实感、更高创作控制力 | ❌ **仅网页端** |
| `ltx23` | 原生音频 | 480p/5 秒免费，原生音频，最长 20 秒，处理快 | ❌ **仅网页端** |
| `veo3Fast` | 高质量 | Google 先进模型，电影级、高质量、更快 | ❌ **仅网页端** |
| `veo31Fast` | 高级版 | Google Veo 3.1，支持 **4K** 分辨率与灵活宽高比 | ❌ **仅网页端** |

**官方 API 侧另有 4 个入口在网页文案中没有直接对应项：**

| API 模型 | 说明 |
|---|---|
| `t2v` | 文生视频，底层为 minimax_h3 720p turbo 档（码率约 1.3 Mbps） |
| `i2v` | 图生视频，入参 `image` + `duration` |
| `t2v_v3` / `i2v_v3` | V3 版本。网页文案中 "V3 来了" 提到「自动生成配音、更清晰的 1080P 画质」，**疑为同一模型**（待验证） |
| `minimax` | MiniMax H3，官方主推，参数 schema 独立（用 `content` 而非 `prompt`） |

## 网页端订阅计费口径（与 API 积分互相独立）

`default` 模型的文案暴露了网页端的计费规则：

> ⚠️ **2026-09-14 更正两处**（下列条目保留原文以便与文案对照，但**不要照此实现**）：
> ① 免费窗口是 **≤10s，不是 8s** —— 依据站点终态任务记录 `480p/10s/turbo`
>    ⇒ `taskStatus=succeed` + **`paid=False`**；
> ② **「480p 最长 8 秒」不成立** —— 这条由页面文案推出的限制**已被同一份实测证伪**：
>    480p 的合法时长同为 **5–20 连续秒数**（与 720p / 1080p 一致，见
>    `translate.DURATION_ALLOWED`），免费档覆盖 **5–10s 的任意整数秒**。
> 详见 `docs/pricing-enum-480p-720p.md` 顶部的更正横幅。

- 480p：免费，最长 8 秒
- 720p：订阅用户 5/8 秒免费；15/20 秒按 3 积分/秒
- 1080p：4 积分/秒

官方 `llms.txt` 明确提示：**「API credits and consumer website subscriptions are distinct.
A website subscription does not imply unlimited API generation.」** —— 网页订阅与 API 积分是两套体系，
不能互相推断权益。

## 待验证事项

1. `default` 模型是否即 `t2v`/`i2v` 的网页端别名。
2. 网页端的 `seedance2`、`kling3` 等未开放模型，其内部生成接口是否可被直接调用（需登录态）。
3. 网页端 `t2v_v3`/`i2v_v3` 与文案中的 "V3" 是否为同一模型。
4. 网页端是否存在 `minimax` 之外的隐藏入口。

> 验证上述事项需要登录态，属于 `web-reverse` 待办（见 `README.md`）。
