#!/usr/bin/env python3
"""Turnstile token 铸造器（Linux + **Xvfb 有头** Chrome + 原生 CDP）

2026-09-14 实测（node-064 / Ubuntu 22.04 / Chrome 153 / 出口 64.81.112.31）：

| 模式                        | 结果                                 |
|-----------------------------|--------------------------------------|
| `--headless=new`            | **0/4**，每个 46 秒 TIMEOUT（挂死）   |
| 有头（xvfb-run）            | **8/8**，平均 **3.7 秒/个**           |

⇒ **headless 不可行，Xvfb 有头可行**。别再为省资源去试无头。

⚠️ **"冷启动 ≈46s" 已证伪**（E2E-AVM-004）：46s 是 45s 预算的**超时签名**
（45s 预算 + ~1s 探针），不是一次成功测量 —— 全新 profile 的首铸在 120s 冷预算下
同样不出 token（两次精确压线失败），是**卡死**不是**慢**（疑似 CF 对低信誉 profile
下发交互式挑战等人操作）。本轮起 render 带**状态采样**（iframe / getResponse），
挑战升级交互式时按宽限期**快速失败**（before-interactive-callback），分类与最后
状态进 /healthz —— 不再让"等人点复选框"伪装成一行 TIMEOUT。

**性能对照（同机同 Chrome，量"秒/个"）：**

| 方案                                            | 秒/个 | 说明                                  |
|-------------------------------------------------|-------|---------------------------------------|
| 每个 token 新开标签页 + 加载应用页              | 3.38  | 最笨的做法                            |
| 一个热页面反复 render                           | 2.39  | 省掉标签页与页面加载（**-29%**）      |
| **同 origin 的轻量页 + 注入 CF api.js**（默认） | **1.72** | 省掉整个 Next.js bundle（**-49%**） |

⚠️ **但铸造不是瓶颈**：pro 账号 4 并发 × 任务 ~2 分钟 ⇒ 只需 **1 token / 30 秒**，
而本工具 **1.7 秒/个（≈17 倍富余）**。要提吞吐该去加账号，别在这里抠速度。

用法:
    N=3 /usr/bin/python3 /tmp/minter.py               # 有头（Xvfb）
    N=3 HEADLESS=1 /usr/bin/python3 /tmp/minter.py     # --headless=new
    KEEP=1 ...                                          # 复用已运行的 Chrome

环境变量：`CDP_PORT` / `CHROME_PROFILE` / `CHROME_BIN` / `SITEKEY` / `ORIGIN` / `LIGHT_URL`
          / `N` / `HEADLESS` / `KEEP`
          / `MINT_TIMEOUT_MS`（常规 render 预算，默认 45000）/ `COLD_MINT_TIMEOUT_MS`
            （**冷启动**首轮预算，默认 120000）

产出 /tmp/tokens.txt（一行一个 token），并打印每个 token 的铸造耗时。

要点:
  * 必须在**真实 origin** 的页面里 turnstile.render()（sitekey 与页面同源，否则 110200）
  * token 单次有效 ⇒ 每提交一次要重新铸一个
  * 铸造与提交应走**同一个出口 IP**（绑定强度待实测）
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

PORT = int(os.environ.get("CDP_PORT", "9222"))
# profile 目录必须**按平台**给：macOS 上给 Linux 路径（/root/...）Chrome 会静默回落到默认
# profile，于是"看到已有实例 ⇒ 在现有会话中打开"，--remote-debugging-port 被忽略、CDP 起不来。
PROFILE_DIR = os.environ.get("CHROME_PROFILE") or ("/root/avm-chrome" if sys.platform.startswith("linux") else "/tmp/avm-chrome")   # CDP 端口；被占用时用 CDP_PORT 换一个
SITEKEY = os.environ.get("SITEKEY", "0x4AAAAAABrddy3Hsje8mwB_")
ORIGIN = os.environ.get("ORIGIN", "https://aivideomaker.ai/zh/ai-video-generator")
# 同 origin 的轻量页（sitekey 按域名生效，任何同源页面都行）—— 比加载整个应用页快一倍
LIGHT_URL = os.environ.get("LIGHT_URL", "https://aivideomaker.ai/robots.txt")
CF_API = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
INJECT_API = ("(()=>{if(window.__cfInjected)return;window.__cfInjected=true;"
             "const s=document.createElement('script');s.src=%s;s.async=true;"
             "(document.head||document.documentElement).appendChild(s);})()" % json.dumps(CF_API))
N = int(os.environ.get("N", "1"))
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
KEEP = os.environ.get("KEEP", "0") == "1"


def http_json(url, timeout=15):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def cdp_ok(port: int | None = None) -> bool:
    """CDP 就绪探测：IPv4 与 IPv6 回环**都试**。

    Chrome 的 DevTools 在端口被占时可能只绑到 [::1]（2026-09-14 宿主机实测：
    新实例 bind IPv4 失败后落 IPv6），只探 127.0.0.1 会把活着的实例
    误报成"CDP 起不来"。
    """
    p = PORT if port is None else port
    for host in ("127.0.0.1", "[::1]"):
        try:
            http_json(f"http://{host}:{p}/json/version")
            return True
        except Exception:
            continue
    return False


def chrome_bin() -> str:
    """Chrome 可执行文件：优先 CHROME_BIN；其次按平台猜（Linux 用 google-chrome，macOS 用 app 内二进制）。"""
    env = os.environ.get("CHROME_BIN")
    if env:
        return env
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "darwin" and os.path.exists(mac):
        return mac
    # arm64 容器里装的是 Debian 原生 chromium（Google 不出 Linux/arm64 的 Chrome）
    for cand in ("/usr/bin/google-chrome", "/usr/bin/chromium"):
        if os.path.exists(cand):
            return cand
    return "/usr/bin/google-chrome"


def clear_profile_locks() -> list:
    """清掉上一次运行留下的 profile 锁。

    ⚠️ 容器里**必须做**：profile 落在卷上，容器重建后 hostname 变了，Chrome 会认定
    "profile 被另一台机器上的进程占用"并**直接拒绝启动**（日志只有一句 SingletonLock 的
    ERROR，CDP 永不就绪 ⇒ 服务一直 ready=false，看不出是为什么）。
    """
    cleared = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.remove(path)
                cleared.append(name)
        except OSError:
            pass
    return cleared


def kill_all_chrome(force: bool = True) -> None:
    """杀掉**本项目**的残留 Chrome 实例，并等 CDP 端口真正释放。

    只按**自己的特征**杀 —— profile 路径 + 调试端口，**绝不按进程名通杀**：
    宿主机上用户自己的浏览器（macOS 进程名 "Google Chrome"）绝不能被误伤。
    2026-09-14 宿主机实测：9222 上叠着两个旧调试实例（IPv4+IPv6 各一），
    按端口 pkill 没杀干净 ⇒ 新实例落到 [::1] 而 cdp_ok 只探 127.0.0.1
    ⇒ 误报"CDP 起不来"。按 profile 补杀是第二层覆盖；若端口仍被外来实例
    占着，这里宁可报错也别带旧实例跑 —— 换 CDP_PORT 即可绕开。
    """
    for pat in (f"user-data-dir={PROFILE_DIR}", f"remote-debugging-port={PORT}"):
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    # 端口真正释放才算清干净（老的 /json/version 探活会连到将死的实例，必须轮询到空）
    for _ in range(30):
        if not cdp_ok():
            return
        time.sleep(0.2)
    raise SystemExit(
        f"Chrome 清理不干净（CDP 端口 {PORT} 一直被占）—— 换 CDP_PORT 可绕开外来实例"
    )


def start_chrome():
    if cdp_ok() and KEEP:
        return
    kill_all_chrome()
    locks = clear_profile_locks()
    if locks:
        print(f"[info] 已清掉陈旧的 profile 锁：{', '.join(locks)}", flush=True)
    args = [chrome_bin(), f"--remote-debugging-port={PORT}",
            "--remote-debugging-address=127.0.0.1", f"--user-data-dir={PROFILE_DIR}",
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check", "--window-size=1280,900",
            "--remote-allow-origins=*",
            # 容器/无 GPU 环境：显式走软件渲染。CF 会查 WebGL 渲染器，
            # 缺 GPU 又不给 SwiftShader 时报"无 WebGL" ⇒ 强负信号（宿主机有真 GPU，
            # 所以这条在 Mac 上不影响）。
            "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
    if HEADLESS:
        args.append("--headless=new")
    # setsid / xvfb-run 都是 Linux 专有：macOS 上要按 which 判断（少了 setsid 会直接报
    # FileNotFoundError，而服务只会在 /healthz 的 last_error 里露出，很难查）
    head = ["nohup"]
    if shutil.which("setsid"):
        head = ["setsid"] + head
    # Xvfb 只在"没有显示器"时必要；macOS 直接有头跑
    if shutil.which("xvfb-run"):
        head += ["xvfb-run", "-a", "--server-args=-screen 0 1280x900x24"]
    cmd = head + args + ["about:blank"]
    subprocess.Popen(cmd, stdout=open("/tmp/chrome-run.log", "ab"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(90):
        if cdp_ok():
            return
        time.sleep(1)
    raise SystemExit("CDP 起不来")


def val_of(resp):
    return (resp.get("result") or {}).get("result", {}).get("value")


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url,
            timeout=150,
            suppress_origin=True,
            # ⚠️ 与 http_json 同理：websocket-client 默认也读 proxy 环境，
            # 不排除回环就会被代理接管（表现为连不上刚起的 Chrome）。
            http_no_proxy=("127.0.0.1", "localhost"),
        )
        self.i = 0

    def call(self, method, session=None, **params):
        self.i += 1
        msg = {"id": self.i, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                return m


# ---- render 超时预算 --------------------------------------------------------
# 两条预算分开：常规 `MINT_TIMEOUT_MS`；**首轮**（页面/画像还是冷的）用
# `COLD_MINT_TIMEOUT_MS`。CLI 路径在常规预算超时后用冷预算重试一次；服务路径首轮
# 直接就是冷预算（见 tools/turnstile_service.py 的 MintCore.mint）。
#
# 🔴 历史教训（别再改回去）：2026-09-14 曾把首铸失败归因为「冷启动 ≈46s、预算不够」并加码
#    到 120s —— E2E-AVM-004 证伪：两次都精确 121.01s（探针 ~1s + 满预算 120s），render
#    根本不会成功，只是**卡住**。"46s" 其实是 45s 预算的超时签名被当成了慢成功（循环引用：
#    报告引 Dockerfile 注释，注释又源自 headless 实验的超时表）。加预算只是把失败推迟。
#    真正的对策是**状态可观测**（MINT_JS_TMPL 的采样与 before-interactive-callback），
#    以及失败后保留浏览器只换页面（服务层）。
MINT_TIMEOUT_MS = int(os.environ.get("MINT_TIMEOUT_MS", "45000"))
COLD_MINT_TIMEOUT_MS = int(os.environ.get("COLD_MINT_TIMEOUT_MS", "120000"))
# 交互式宽限（毫秒）：挑战升级成"等人操作"后，给 CF 这么长时间自动收尾；过点即快速失败。
# 刻意**不做成环境变量**：.env.example 与 compose 的透传有 tests/test_env_template.py 的
# 双向门禁把关，新增 env 读入必须同步三处 —— 常量足够用，不值得为此动编排。
INTERACTIVE_GRACE_MS = 10000

# 求值结果里的页面内状态（超时/失败时随 val["state"] 返回，进 /healthz）：
#   iframe      —— 挑战 iframe（challenges.cloudflare.com）有没有真正挂上来
#   response    —— turnstile.getResponse() 是否已出 token
#   interactive —— 是否进入过交互模式（要人点复选框）
#   samples     —— 采样次数（>1 说明轮询真的在跑）
MINT_JS_TMPL = """
(async () => {
  if (typeof window.turnstile === 'undefined') return {ok:false, why:'no turnstile api'};
  return await new Promise((resolve) => {
    // 上一次失败留下的半成品 widget 必须清掉：失败不再重启浏览器 ⇒ 宿主 div 会累积。
    document.querySelectorAll('[data-avm-ts-host]').forEach((n) => n.remove());
    const state = {iframe: false, response: false, interactive: false, samples: 0};
    let wid = null;
    const snap = () => {
      try {
        state.iframe = !!document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        try {
          state.response = !!(wid !== null && window.turnstile.getResponse(wid));
        } catch (e) { state.response = false; }
      } catch (e) {}
      state.samples++;
    };
    const done = (ok, payload) => {
      clearInterval(iv); clearTimeout(t); if (g) clearTimeout(g);
      resolve(Object.assign({ok: ok, state: state}, payload));
    };
    const fail = (why) => { snap(); done(false, {why: why}); };
    const t = setTimeout(() => fail('TIMEOUT'), %d);
    const iv = setInterval(snap, 3000);
    let g = null;
    snap();
    try {
      const host = document.createElement('div');
      host.setAttribute('data-avm-ts-host', '1');
      host.style.cssText = 'position:fixed;left:10px;top:10px;width:300px;height:65px;z-index:99999;background:#fff';
      document.body.appendChild(host);
      wid = window.turnstile.render(host, {
        sitekey: %s,
        callback: (tok) => done(true, {token: String(tok)}),
        'error-callback': (e) => fail('ERR ' + String(e)),
        // 挑战升级成交互式（要人点）：无人值守环境里等满预算没有意义。低风险场景 CF
        // 会自己勾掉，所以给一小段宽限；过点仍未完成就按 INTERACTIVE **快速失败**，
        // 别让"等人点复选框"伪装成 TIMEOUT —— E2E-AVM-004 的 120s 黑等就是这么来的。
        'before-interactive-callback': () => {
          state.interactive = true;
          if (!g) g = setTimeout(() => fail('INTERACTIVE'), %d);
        },
      });
    } catch (e) { fail('EX ' + String(e)); }
  });
})()
"""


def mint_js(timeout_ms: int, grace_ms: int | None = None) -> str:
    """按给定预算生成 render 脚本（页面内的 `setTimeout` 是**唯一**的总超时闸门）。

    `grace_ms` 是交互式宽限：before-interactive-callback 触发后再等这么久，仍没出
    token 就按 INTERACTIVE 快速失败（低风险场景 CF 会自己勾掉，所以留宽限而非立刻弃）。
    """
    if grace_ms is None:
        grace_ms = INTERACTIVE_GRACE_MS
    return MINT_JS_TMPL % (int(timeout_ms), json.dumps(SITEKEY), int(grace_ms))


# 兼容旧用法（也供不方便传预算的调用点使用）
MINT_JS = mint_js(MINT_TIMEOUT_MS)

PROBE = "typeof window.turnstile + '|' + document.readyState + '|' + location.href"


def failure_reason(val) -> str:
    """把 render 的求值结果归类成稳定短标签（进日志 / /healthz / 调用方错误信息）。

    分类意在对症下药：
      ok          —— 成功（不该出现在失败路径）
      timeout     —— 预算打满仍无回调（配合 val["state"] 判断停在哪个阶段）
      interactive —— 挑战升级成交互式且宽限内没自动完成 ⇒ 等人点，重试无意义
      error       —— CF 主动报错（error-callback），带原始错误码
      exception   —— 页面内抛异常（多半是 api.js 行为变化）
      no-result   —— CDP 求值没拿到值（连接/页面层面的问题）
    """
    if not isinstance(val, dict):
        return "no-result"
    if val.get("ok"):
        return "ok"
    why = str(val.get("why", ""))
    if why == "TIMEOUT":
        return "timeout"
    if why.startswith("INTERACTIVE"):
        return "interactive"
    if why.startswith("ERR"):
        return "error"
    if why.startswith("EX"):
        return "exception"
    return "unknown:" + why


def open_warm_page(bc):
    """打开一个**轻量页**并注入 CF api.js，返回 (targetId, sessionId)。"""
    created = bc.call("Target.createTarget", url="about:blank")
    tid = created["result"]["targetId"]
    sess = bc.call("Target.attachToTarget", targetId=tid, flatten=True)["result"]["sessionId"]
    bc.call("Page.enable", session=sess)
    bc.call("Page.navigate", session=sess, url=LIGHT_URL)
    time.sleep(1.5)
    bc.call("Runtime.evaluate", session=sess, returnByValue=True, expression=INJECT_API)
    return tid, sess


def mint_on(bc, sess, i, timeout_ms: int | None = None, grace_ms: int | None = None):
    """在**已预热**的页面上 render 一次（热页面 ≈1.7s）。

    `timeout_ms` 显式给预算；不给则用常规预算。返回 `(token, 耗时s, 原始求值结果)` ——
    第三个值带着 `why` 分类与页面内 `state` 采样，调用方按需落账（服务层进 /healthz）。

    **超时不算终局（仅常规预算路径）**：TIMEOUT 时用冷预算再试一次；
    INTERACTIVE 不重试 —— 等人点的挑战，换预算没有意义。
    """
    budget = MINT_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    t0 = time.time()
    ready = False
    for _ in range(90):
        v = val_of(bc.call("Runtime.evaluate", session=sess, returnByValue=True, expression=PROBE)) or ""
        if v.startswith("object"):
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        v = val_of(bc.call("Runtime.evaluate", session=sess, returnByValue=True, expression=PROBE)) or ""
        print(f"  #{i} 页面未就绪：{str(v)[:120]}")
        return None, time.time() - t0, None

    val = _render_once(bc, sess, budget, grace_ms)
    if isinstance(val, dict) and not val.get("ok") and val.get("why") == "TIMEOUT" \
            and budget < COLD_MINT_TIMEOUT_MS:
        # ★ 冷预算重试（仅常规预算路径）：服务路径首轮已是冷预算，不会再进这里。
        print(f"  #{i} 首次 {budget}ms 超时 ⇒ 用冷启动预算 {COLD_MINT_TIMEOUT_MS}ms 重试一次")
        val = _render_once(bc, sess, COLD_MINT_TIMEOUT_MS, grace_ms)

    dt = time.time() - t0
    if isinstance(val, dict) and val.get("ok"):
        print(f"  #{i} token len={len(val['token'])} 铸造耗时={dt:.2f}s head={val['token'][:24]}...")
        return val["token"], dt, val
    print(f"  #{i} 失败[{failure_reason(val)}]：{val}（耗时 {dt:.2f}s）")
    return None, dt, val


def _render_once(bc, sess, timeout_ms: int, grace_ms: int | None = None):
    """单次 render（不重试）。返回 JS 的求值结果。"""
    return val_of(bc.call("Runtime.evaluate", session=sess, expression=mint_js(timeout_ms, grace_ms),
                          awaitPromise=True, returnByValue=True))


def mint(bc, i):
    """兼容旧用法：单次铸造（内部开一个轻量页）。"""
    tid, sess = open_warm_page(bc)
    try:
        tok, dt, _val = mint_on(bc, sess, i)
        return tok, dt
    finally:
        try:
            bc.call("Target.closeTarget", targetId=tid)
        except Exception:
            pass


def mint_legacy_app_page(bc, i):
    """旧路径（加载整个应用页）：轻量页万一失效时回退用。"""
    t0 = time.time()
    created = bc.call("Target.createTarget", url="about:blank")
    tid = created["result"]["targetId"]
    att = bc.call("Target.attachToTarget", targetId=tid, flatten=True)
    sess = att["result"]["sessionId"]
    try:
        bc.call("Page.enable", session=sess)
        bc.call("Page.navigate", session=sess, url=ORIGIN)
        ready = False
        for _ in range(60):
            v = val_of(bc.call("Runtime.evaluate", session=sess, returnByValue=True, expression=PROBE)) or ""
            if v.startswith("object"):
                ready = True
                break
            time.sleep(1)
        if not ready:
            v = val_of(bc.call("Runtime.evaluate", session=sess, returnByValue=True, expression=PROBE)) or ""
            print(f"  #{i} 页面未就绪：{str(v)[:150]}")
            return None, time.time() - t0
        val = val_of(bc.call("Runtime.evaluate", session=sess,
                             expression=mint_js(COLD_MINT_TIMEOUT_MS),
                             awaitPromise=True, returnByValue=True))
        dt = time.time() - t0
        if isinstance(val, dict) and val.get("ok"):
            print(f"  #{i} token len={len(val['token'])} 铸造耗时={dt:.1f}s head={val['token'][:24]}...")
            return val["token"], dt
        print(f"  #{i} 失败[{failure_reason(val)}]：{val}（耗时 {dt:.1f}s）")
        return None, dt
    finally:
        try:
            bc.call("Target.closeTarget", targetId=tid)
        except Exception:
            pass


def main():
    start_chrome()
    ver = http_json(f"http://127.0.0.1:{PORT}/json/version")
    print("浏览器：", ver.get("Browser"), "| headless =", HEADLESS)
    bc = CDP(ver["webSocketDebuggerUrl"])
    tokens, times = [], []
    if os.environ.get("LEGACY_APP_PAGE", "0") == "1":
        for i in range(1, N + 1):
            tok, dt = mint_legacy_app_page(bc, i)
            if tok:
                tokens.append(tok)
            times.append(dt)
    else:
        tid, sess = open_warm_page(bc)          # ★ 一个热页面反复用（默认轻量页）
        try:
            for i in range(1, N + 1):
                tok, dt, _val = mint_on(bc, sess, i)
                if tok:
                    tokens.append(tok)
                times.append(dt)
        finally:
            try:
                bc.call("Target.closeTarget", targetId=tid)
            except Exception:
                pass
    with open("/tmp/tokens.txt", "w") as f:
        f.write("\n".join(tokens))
    if times:
        print(f"\n成功 {len(tokens)}/{N}，平均铸造耗时 {sum(times)/len(times):.1f}s（min {min(times):.1f} / max {max(times):.1f}）")
    print("token 已写入 /tmp/tokens.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
