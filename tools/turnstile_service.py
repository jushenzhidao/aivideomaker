#!/usr/bin/env python3
"""Turnstile token 铸造**服务**（常驻）—— 把 tools/turnstile_minter.py 包成 HTTP 服务。

为什么需要"常驻 + 流水线"：
  * 首次 render 有 **~46 秒冷启动** ⇒ 按需拉起必死，必须常驻预热；
  * token **单次有效**，且每条提交都要一个 ⇒ 需要"取一个、补一个"的流水线；
  * 调用方（适配层/其他服务）只想要"给我一个能用的 token"，不想关心 Chrome。

API（默认 127.0.0.1:8899）：
  GET  /healthz                     服务与浏览器状态、池子水位、铸造统计
  POST /v1/turnstile/mint           取一个 token（优先取池子；池空则现铸）
                                    → {"token": "...", "source": "pool|live", "age_ms": 1200}
  POST /v1/turnstile/mint?n=4       取 n 个 → {"tokens": [...], "sources": [...]}

鉴权：设了 `MINTER_KEY` 就要求 `X-Minter-Key` 头；没设则只应监听回环（启动会告警）。

降级：铸造失败一律返回 503 + 明确错误，调用方据此退回"等闸门衰减"的慢路径 ——
      **绝不能让上游把"铸造失败"当成"闸门放行"**。

环境变量：PORT / MINTER_KEY / POOL_TARGET / TOKEN_TTL_S / SITEKEY / LIGHT_URL / HEADLESS
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turnstile_minter as M  # noqa: E402  （同目录，复用铸造核心）

PORT = int(os.environ.get("PORT", "8899"))
KEY = os.environ.get("MINTER_KEY", "")
POOL_TARGET = int(os.environ.get("POOL_TARGET", "4"))
TTL_S = float(os.environ.get("TOKEN_TTL_S", "120"))   # 保守：CF 侧约 300s，站点校验按更短算

_STATE = {
    "pool": [],            # [(token, minted_at)]
    "stats": {"minted": 0, "failed": 0, "served": 0, "spawned_at": time.time()},
    "last_error": None,
    "ready": False,
    "browser": None,
}
# 两把锁**必须分开**：铸造一把（串行化 Chrome，可能持有数秒到 46 秒冷启动），
# 状态一把（毫秒级）。曾经共用一把 ⇒ `/healthz` 被正在进行的铸造阻塞、探活直接超时，
# 那是最糟的组合：服务看着"挂了"，其实只是忙。
_MINT_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_WAKE = threading.Event()


def _prune():
    now = time.time()
    _STATE["pool"] = [p for p in _STATE["pool"] if now - p[1] < TTL_S]


class MintCore:
    """持有 Chrome + 一个热页面；mint() 串行执行（并行无收益，见 minter 的实测注释）。"""

    def __init__(self):
        self.bc = None
        self.page = None

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

    def mint(self) -> str:
        with _MINT_LOCK:
            self.ensure()
            tok, dt = M.mint_on(self.bc, self.page[1], 0)
            if not tok:
                self.reset()
                with _STATE_LOCK:
                    _STATE["stats"]["failed"] += 1
                    _STATE["last_error"] = "mint failed (page/browser reset)"
                raise RuntimeError("mint failed")
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
            with _STATE_LOCK:
                _STATE["pool"].append((tok, time.time()))
        except Exception as e:  # noqa: BLE001 补货失败不影响已服务能力
            with _STATE_LOCK:
                _STATE["last_error"] = f"{type(e).__name__}: {e}"[:200]
            time.sleep(3)


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
            "pool": {"size": pool, "target": POOL_TARGET, "ttl_s": TTL_S},
            "stats": st,
            "last_error": _STATE["last_error"],
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
    if not KEY:
        print("[warn] MINTER_KEY 未设置 —— 只应监听回环，切勿暴露公网", flush=True)
    threading.Thread(target=refill_loop, daemon=True).start()
    _WAKE.set()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Turnstile 铸造服务已启动 http://127.0.0.1:{PORT}（池子目标 {POOL_TARGET}，TTL {TTL_S}s）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
