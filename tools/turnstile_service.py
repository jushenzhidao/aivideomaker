#!/usr/bin/env python3
"""Turnstile token 铸造**服务**（常驻）—— 把 tools/turnstile_minter.py 包成 HTTP 服务。

为什么需要"常驻 + 流水线"：
  * 冷 profile 的**首次** render 可能远超常规预算、甚至**永久卡死**（E2E-AVM-004：120s
    冷预算也不出 token，疑似交互式挑战等人操作）⇒ 按需拉起必死，必须常驻预热；
  * token **单次有效**，且每条提交都要一个 ⇒ 需要"取一个、补一个"的流水线；
  * 调用方（适配层/其他服务）只想要"给我一个能用的 token"，不想关心 Chrome。

API（默认 127.0.0.1:8899）：
  GET  /healthz                     服务与浏览器状态、池子水位、铸造统计、最近一次失败分类
  POST /v1/turnstile/mint           取一个 token（优先取池子；池空则现铸）
                                    → {"token": "...", "source": "pool|live", "age_ms": 1200}
  POST /v1/turnstile/mint?n=4       取 n 个 → {"tokens": [...], "sources": [...]}

鉴权（**fail-closed**）：设了 `MINTER_KEY` 就要求 `X-Minter-Key` 头；
**非回环绑定 + 未设 key ⇒ 启动即拒**（退出码 2），不再只告警后继续服务 ——
本服务产出的 token 能直接过掉上游风控闸门，暴露到公网等于免费分发过闸能力。
确需在私有网络内匿名运行时，显式设 `MINTER_ALLOW_INSECURE=1` 自担风险。

降级：铸造失败一律返回 503 + 明确错误（含失败分类与页面内状态采样），
调用方据此退回"等闸门衰减"的慢路径 —— **绝不能让上游把"铸造失败"当成"闸门放行"**。

环境变量：PORT / BIND_HOST / MINTER_KEY / MINTER_ALLOW_INSECURE /
          POOL_TARGET / TOKEN_TTL_S / SITEKEY / LIGHT_URL / HEADLESS /
          CDP_PORT / CHROME_PROFILE / MINT_TIMEOUT_MS / COLD_MINT_TIMEOUT_MS
          （后两条是 render 预算，见 tools/turnstile_minter.py 的「render 超时预算」段：
            首轮用 `COLD_*`，之后回落常规值）
"""
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turnstile_minter as M  # noqa: E402  （同目录，复用铸造核心）

PORT = int(os.environ.get("PORT", "8899"))
# ⚠️ 绑定地址：默认只绑回环（本机跑最安全）；**容器里必须绑 0.0.0.0**，
# 否则 docker-proxy 从容器 eth0 转发进来的连接无人接收 —— 宿主侧表现为
# `Connection reset by peer`，而容器内 curl 自己却是好的（最迷惑的一种）。
# 与主 Dockerfile 里 `ARK_HOST=0.0.0.0` 是同一条教训。
# 🔴 但 0.0.0.0 **不是**可以裸奔的：见下方 `bind_guard_error()`。
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
KEY = os.environ.get("MINTER_KEY", "")
POOL_TARGET = int(os.environ.get("POOL_TARGET", "4"))
TTL_S = float(os.environ.get("TOKEN_TTL_S", "120"))   # 保守：CF 侧约 300s，站点校验按更短算

# 只有这些绑定算"本机自用"，无 key 时放行
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def env_flag(raw: str) -> bool:
    """环境变量字面值 → 布尔。**只认开启值**（大小写不敏感、允许空白）。

    刻意采用白名单：`0` / `false` / `no` / `off` / 空串 / 任何拼错的值
    一律判为关闭。因为这里是安全开关，**判错的代价不对称** ——
    把关闭误判成开启会直接暴露铸造能力，反之只是让人多设一个 MINTER_KEY。
    """
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# 显式自担风险的开关（私有网络 / 已有外层 ACL 时用）。故意做得"必须显式"：
# 默认不设 ⇒ 无 key + 非回环一律拒启动。
ALLOW_INSECURE = env_flag(os.environ.get("MINTER_ALLOW_INSECURE", ""))

_STATE = {
    "pool": [],            # [(token, minted_at)]
    "stats": {"minted": 0, "failed": 0, "served": 0, "spawned_at": time.time()},
    "last_error": None,
    "last_failure": None,  # 最近一次铸造失败的分类证据 {"reason","why","state","at"}
    "ready": False,
    "browser": None,
}
# 两把锁**必须分开**：铸造一把（串行化 Chrome，可能持有数秒到两分钟冷预算），
# 状态一把（毫秒级）。曾经共用一把 ⇒ `/healthz` 被正在进行的铸造阻塞、探活直接超时，
# 那是最糟的组合：服务看着"挂了"，其实只是忙。
_MINT_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_WAKE = threading.Event()


def bind_guard_error(bind_host: str, key: str, allow_insecure: bool = False) -> str | None:
    """非回环绑定且无凭据 ⇒ 返回拒绝启动的原因；`None` 表示放行。

    为什么是**拒绝启动**而不是打条告警：本服务的产物（Turnstile token）能直接过掉
    上游的提交闸门，等同于"不限次数的免费提交额度"。容器形态下 `BIND_HOST=0.0.0.0`
    ＋ `network_mode: host` ⇒ 0.0.0.0 就是**宿主公网地址**，匿名即可领 token
    （一个 curl 就能把铸造池刷干，也把账号推到风控面前）。

    姿态一致性：本项目其余入口都是 fail-closed —— `Settings.validate()` 直接拒绝启动、
    `ARK_HOST` 必须显式设置、`AVM_TASK_STORE` 写错即抛错。这里曾经只 `print` 一行 warn
    就照常服务，是**全项目唯一的 fail-open**，故按同一口径收口。
    """
    if key.strip() or bind_host.strip() in LOOPBACK_HOSTS or allow_insecure:
        return None
    return (
        f"拒绝启动：BIND_HOST={bind_host!r} 是非回环地址，但 MINTER_KEY 未设置 ——\n"
        "  任何能访问该端口的人都能**匿名领取 Turnstile token**，"
        "等于把「过闸能力」分发出去（并让账号更快撞上风控）。\n"
        "  出路一（推荐）：设 MINTER_KEY=<随机串>，调用方带 X-Minter-Key 头。\n"
        "  出路二（仅私有网络 / 已有外层 ACL）：显式设 MINTER_ALLOW_INSECURE=1 自担风险。\n"
        "  出路三（仅本机自用）：BIND_HOST=127.0.0.1（也是默认值）。"
    )


def _prune():
    now = time.time()
    _STATE["pool"] = [p for p in _STATE["pool"] if now - p[1] < TTL_S]


def backoff_delay(fail_streak: int) -> float:
    """连续失败后的补货退避：8s 起步指数翻倍、封顶 300s，再叠 ±25% 抖动。

    曾经固定 8~20s：冷 profile 卡死时，补货线程以 ~2.5 分钟一周期无限杀/重启 Chrome
    （120s 卡死 + 退避 + 冷启动各来一遍），宿主侧持续制造进程抖动却永无产出
    （E2E-AVM-004 实测形态，宿主当时已背着上千个 chrome 进程）。
    指数退避让空转成本随连续失败收敛，同时保留"偶尔再试"（profile 可能被外部热化）。
    """
    base = min(300.0, 8.0 * (2 ** max(0, fail_streak - 1)))
    return base * (0.75 + random.random() * 0.5)


class MintCore:
    """持有 Chrome + 一个热页面；mint() 串行执行（并行无收益，见 minter 的实测注释）。"""

    def __init__(self):
        self.bc = None
        self.page = None
        # ★ 本进程**第一次**铸造用**冷启动预算**：冷 profile 的首铸可能远慢于常规线
        #   甚至卡死（E2E-AVM-004）。只给首轮放宽：常态用常规预算，免得把一次真卡死
        #   的铸造从 45s 拉长到 120s。
        #   ⚠️ `warmed` 只在**真的铸出过** token 后置位 —— `/healthz` 早就 ready
        #   而铸造一直超时的形态（报告 AVM12-MINT）正是靠它与 `ready` 分开表达。
        self.warmed = False

    def ensure(self):
        if self.bc is not None and self.page is not None:
            return
        M.start_chrome()
        ver = M.http_json(f"http://127.0.0.1:{M.PORT}/json/version")
        self.bc = M.CDP(ver["webSocketDebuggerUrl"])
        self.page = M.open_warm_page(self.bc)
        with _STATE_LOCK:
            _STATE["ready"] = True
            _STATE["browser"] = ver.get("Browser")

    def reset(self):
        self.bc = None
        self.page = None
        with _STATE_LOCK:
            _STATE["ready"] = False

    def reload_page(self) -> bool:
        """轻量恢复：**保留 Chrome 进程**，只换一个干净的热页面。

        为什么失败后不再整体 reset：CF 对"这个 profile 信誉如何"的判定落在 profile
        （卷）上，杀 Chrome 重启**不会**重置那个判定，只会白付一次冷启动 —— E2E-AVM-004
        里"失败 ⇒ reset ⇒ 杀/重启 Chrome ⇒ 再失败"以 ~2.5 分钟一周期无限空转，宿主侧
        净赚进程抖动。失败的 mint 里 JS 都能跑到 TIMEOUT ⇒ CDP 与页面本身是活的，
        换页重开挑战就够；reload 也失败（多半是 CDP 死了）才降级 reset()，
        交给下次 ensure() 重启浏览器。
        """
        if self.bc is None:
            return False
        try:
            if self.page:
                try:
                    self.bc.call("Target.closeTarget", targetId=self.page[0])
                except Exception:
                    pass
            self.page = M.open_warm_page(self.bc)
            return True
        except Exception as e:  # noqa: BLE001
            self.reset()
            with _STATE_LOCK:
                _STATE["last_error"] = f"reload_page failed: {type(e).__name__}: {e}"[:200]
            return False

    def mint(self) -> str:
        with _MINT_LOCK:
            self.ensure()
            budget = M.MINT_TIMEOUT_MS if self.warmed else M.COLD_MINT_TIMEOUT_MS
            tok, _dt, val = M.mint_on(self.bc, self.page[1], 0, timeout_ms=budget)
            if not tok:
                reason = M.failure_reason(val)
                state = val.get("state") if isinstance(val, dict) else None
                with _STATE_LOCK:
                    _STATE["stats"]["failed"] += 1
                    _STATE["last_error"] = f"mint failed: {reason}" + (f" state={state}" if state else "")
                    # 失败分类 + 页面内状态采样的完整证据，排障不必再翻容器日志：
                    #   interactive ⇒ 挑战要人点（低信誉 profile 的典型形态），等预算没有意义
                    #   timeout     ⇒ 打满预算仍无回调（state 里能看出 iframe 是否挂上）
                    #   error       ⇒ CF 主动报错，带原始错误码
                    _STATE["last_failure"] = {
                        "reason": reason,
                        "why": val.get("why") if isinstance(val, dict) else None,
                        "state": state,
                        "at": time.time(),
                    }
                # ★ 失败后**保留浏览器**只换页面（见 reload_page）—— INTERACTIVE/TIMEOUT
                #   都换：交互式挑战留下的半成品 widget 随旧页一起丢弃，下次干净开始。
                self.reload_page()
                raise RuntimeError(f"mint failed: {reason}")
            self.warmed = True      # 说明这个页面**真的**铸出过 token（heat ≠ ready）
            with _STATE_LOCK:
                _STATE["stats"]["minted"] += 1
            return tok


CORE = MintCore()


def take_token() -> dict:
    """优先池子；池空则现铸。返回 {token, source, age_ms}。"""
    with _STATE_LOCK:
        _prune()
        if _STATE["pool"]:
            tok, at = _STATE["pool"].pop(0)
            _STATE["stats"]["served"] += 1
            _WAKE.set()          # 通知补货线程
            return {"token": tok, "source": "pool", "age_ms": int((time.time() - at) * 1000)}
    t0 = time.time()
    tok = CORE.mint()            # 内部用铸造锁
    with _STATE_LOCK:
        _STATE["stats"]["served"] += 1
    return {"token": tok, "source": "live", "age_ms": int((time.time() - t0) * 1000)}


def refill_loop():
    """流水线：把池子维持到 POOL_TARGET（每次补一个，避免把 Chrome 打满）。"""
    fail_streak = 0
    while True:
        _WAKE.wait(timeout=1.0)
        _WAKE.clear()
        try:
            with _STATE_LOCK:
                _prune()
                need = POOL_TARGET - len(_STATE["pool"])
            if need <= 0:
                continue
            tok = CORE.mint()
            fail_streak = 0
            with _STATE_LOCK:
                _STATE["pool"].append((tok, time.time()))
            # 每次成功铸造之间随机喘息，避免形成"每 3.5 秒一个挑战"的机械流量。
            time.sleep(2 + random.random() * 6)   # 2~8s 随机

        except (Exception, SystemExit) as e:  # noqa: BLE001
            fail_streak += 1
            # ⚠️ 必须连 SystemExit 一起接：`ensure()` 起不来 Chrome 时抛的是 SystemExit，
            # 它是 BaseException 的子类 —— 只 catch Exception 会让**补货线程静默死亡**，
            # 表现为 ready=false / minting=false / last_error=null（实测在容器里踩到，
            # 因为没有 xauth，xvfb-run 起不来）。线程死掉还查不到原因是最坏的情况。
            with _STATE_LOCK:
                _STATE["last_error"] = f"{type(e).__name__}: {e}"[:200]
            # 退避：按连续失败次数指数拉长（带抖动）—— 固定短周期重试会把"卡死的铸造"
            # 变成宿主上无休止的 Chrome 杀/起循环（E2E-AVM-004 实测形态）。
            time.sleep(backoff_delay(fail_streak))


def health() -> dict:
    # ★ 只用状态锁（毫秒级）；铸造锁可能被持有数十秒，绝不能在这里等
    with _STATE_LOCK:
        _prune()
        st = dict(_STATE["stats"])
        pool = len(_STATE["pool"])
        st["uptime_s"] = int(time.time() - st.pop("spawned_at"))
        return {
            "ok": bool(_STATE["ready"]),
            "minting": _MINT_LOCK.locked(),
            "browser": _STATE["browser"],
            "ready": _STATE["ready"],
            # ⚠️ `ready`（Chrome/页面就绪）与 `warmed`（**真的铸出过** token）是两件事：
            #    冷启动/卡死期 `ready=true` 而铸造仍会失败 —— 只看 `ready` 会误判成
            #    "服务是好的"（报告 AVM12-MINT 就是这个形状）。再挂上两条实际生效的
            #    预算与最近一次失败的分类，排障时不必去翻环境变量和容器日志。
            "warmed": CORE.warmed,
            "render_timeout_ms": {"cold": M.COLD_MINT_TIMEOUT_MS, "normal": M.MINT_TIMEOUT_MS},
            "pool": {"size": pool, "target": POOL_TARGET, "ttl_s": TTL_S},
            "stats": st,
            "last_error": _STATE["last_error"],
            "last_failure": _STATE["last_failure"],
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        # 空 KEY ⇒ 放行：**只在回环绑定或显式 MINTER_ALLOW_INSECURE 下可达**
        # （非回环 + 无 key 的组合已被 bind_guard_error() 拦在启动之前）。
        if not KEY:
            return True
        return self.headers.get("X-Minter-Key") == KEY

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, health())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "bad or missing X-Minter-Key"})
        u = urlparse(self.path)
        if u.path != "/v1/turnstile/mint":
            return self._send(404, {"error": "not found"})
        n = int(parse_qs(u.query).get("n", ["1"])[0])
        try:
            got = [take_token() for _ in range(max(1, n))]
        except Exception as e:  # noqa: BLE001 降级：调用方据此走慢路径
            return self._send(503, {"error": f"{type(e).__name__}: {e}"[:200], **health()})
        if n == 1:
            return self._send(200, got[0])
        return self._send(200, {"tokens": [g["token"] for g in got],
                                "sources": [g["source"] for g in got]})

    def log_message(self, fmt, *args):   # 静音默认访问日志（避免刷屏）
        pass


def main():
    err = bind_guard_error(BIND_HOST, KEY, ALLOW_INSECURE)
    if err:
        print(f"[fatal] {err}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    if ALLOW_INSECURE and not KEY:
        print("[warn] MINTER_ALLOW_INSECURE 已开启且未设 MINTER_KEY —— "
              "请自行确保该端口不可从公网到达", flush=True)
    elif not KEY:
        print("[warn] MINTER_KEY 未设置 —— 仅回环绑定，切勿暴露公网", flush=True)
    threading.Thread(target=refill_loop, daemon=True).start()
    _WAKE.set()
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Turnstile 铸造服务已启动 http://{BIND_HOST}:{PORT}"
          f"（池子目标 {POOL_TARGET}，TTL {TTL_S}s，鉴权={'开' if KEY else '关（仅回环）'}）",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
