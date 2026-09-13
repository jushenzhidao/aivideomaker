#!/usr/bin/env python3
"""风控闸门 / 余额的**只读**观测器。

为什么单独一个脚本，而不是复用 `web_session.py probe`：`probe` 是**一次性全量诊断**，
一次要打七八个端点（含若干匿名对照路由）。长期盯着闸门只需要两个 query：

    auth.getUser（拿 userId，needsCaptcha 的入参）→ model.needsCaptcha → credits.getCredits

少打一个端点就少一份"我们自己在制造速率"的嫌疑 —— 而**速率正是这个闸门的触发维度**。

用法::

    python3 src/watch_captcha.py                    # 每 600s 采一次，持续
    python3 src/watch_captcha.py --interval 60      # 每 60s
    python3 src/watch_captcha.py --once             # 只采一次（自动化里用这个）
    python3 src/watch_captcha.py --ticks 6 --interval 60

每次采样的结果**追加一行**到 `--log`（默认 `/tmp/avm-captcha-watch.log`），并在
`needsCaptcha` 发生翻转时在 stdout 上放大标注 —— 那正是"触发/衰减时长"的实测点。

三条纪律：
  1. **只读**：不创建任务、不计费、不改任何远端状态。
  2. **不落凭据**：日志里只有账号指纹前 6 位，绝不写 cookie。
  3. **不因一次失败而停**：单次异常记一行错误继续跑（观测器自身不该成为故障源）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ark_compat.web_client import WebClient  # noqa: E402

DEFAULT_LOG = "/tmp/avm-captcha-watch.log"
COOKIE_JAR_CANDIDATES = (
    "avm-proxy/cookies.json",
    "src/web-adapter/cookies.json",
    "cookies.json",
    ".cookies.json",
)


def load_cookie(explicit: str | None) -> str:
    """优先 `--cookie` / `AVM_COOKIE`，否则找一份导出的 cookie jar。"""
    if explicit or os.environ.get("AVM_COOKIE"):
        return explicit or os.environ["AVM_COOKIE"]
    for rel in COOKIE_JAR_CANDIDATES:
        p = Path(rel)
        if not p.exists():
            continue
        try:
            jar = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        val = next(
            (c.get("value") for c in jar if isinstance(c, dict) and c.get("name") == "auth_session"),
            None,
        )
        if val:
            return f"auth_session={val}"
    sys.exit(
        "找不到凭据：用 --cookie / AVM_COOKIE，或在 "
        + " / ".join(COOKIE_JAR_CANDIDATES)
        + " 放一份导出的 cookie jar"
    )


def read_last(log_path: Path) -> tuple[bool | None, int | None]:
    """读日志最后一条有效记录 —— 翻转判定要跟"上一次"比，不是跟"上一次进程"比。"""
    if not log_path.exists():
        return None, None
    for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
        parts = dict(
            (kv.split("=", 1)[0], kv.split("=", 1)[1])
            for kv in line.split()
            if "=" in kv
        )
        if "needsCaptcha" in parts:
            g = parts["needsCaptcha"].strip().lower() == "true"
            c = int(parts["totalRemaining"]) if parts.get("totalRemaining", "").isdigit() else None
            return g, c
    return None, None


def sample(client: WebClient) -> dict:
    """采一次。任何一步失败都让异常抛给调用方去记账。"""
    user_id = client.get_user_id()
    return {
        "userId_prefix": (user_id or "")[:6],
        "needsCaptcha": bool(client.needs_captcha()),
        "totalRemaining": client.get_credits(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读观测 aivideomaker 的风控闸门与余额")
    ap.add_argument("--cookie", help="Cookie 头（默认读 AVM_COOKIE 或导出的 jar）")
    ap.add_argument("--interval", type=float, default=600.0, help="采样间隔秒数（默认 600）")
    ap.add_argument("--ticks", type=int, default=0, help="采多少次后退出；0=一直采")
    ap.add_argument("--once", action="store_true", help="等价于 --ticks 1")
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"追加日志（默认 {DEFAULT_LOG}）")
    args = ap.parse_args(argv)
    if args.once:
        args.ticks = 1

    cookie = load_cookie(args.cookie)
    log_path = Path(args.log)
    last_gate, _ = read_last(log_path)

    client = WebClient(cookie, base_url=os.environ.get("AVM_BASE_URL", "https://aivideomaker.ai"))
    ticks = 0
    try:
        while True:
            ticks += 1
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                s = sample(client)
                line = (
                    f"{stamp} needsCaptcha={s['needsCaptcha']} "
                    f"totalRemaining={s['totalRemaining']} user={s['userId_prefix']}"
                )
                flip = ""
                if last_gate is not None and s["needsCaptcha"] != last_gate:
                    flip = " ⚑翻转" if s["needsCaptcha"] else " ⚑已衰减"
                    line += flip
                last_gate = s["needsCaptcha"]
                print(("⚑ " if flip else "") + line, flush=True)
            except Exception as e:  # noqa: BLE001 观测器不该因一次失败而停
                line = f"{stamp} error={type(e).__name__}: {str(e)[:160]}"
                print(line, flush=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")  # 一行一次 write：并发追加也不会互相截断
            if args.ticks and ticks >= args.ticks:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止。", flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
