#!/usr/bin/env python3
"""web 侧（网页端）会话工具：验证会话、调 tRPC、诊断有效期与滚动续期。

网页端不是 REST，而是 **tRPC over /api**：
    GET  /api/{procedure}?batch=1&input={"0":{"json":null,"meta":{"values":["undefined"]}}}
注意路径**没有 /trpc 前缀**，procedure 直接拼在 /api 之后（如 /api/auth.user）。
仅支持 GET 形式的 query；用 POST 调 query 会返回
`No "mutation"-procedure on path "..."`（说明该 procedure 是 query 类型）。

会话凭据：浏览器 Cookie 中的 `auth_session`（40 位不透明随机串，**不是 JWT，无法自解析**）。
服务端对应 Prisma 模型 `UserSession { id, userId, expiresAt, impersonatorId }`
—— 过期时间未通过任何接口暴露，**但导出的 cookie jar 里有 `expirationDate`**。

用法:
  export AVM_COOKIE="auth_session=xxxx; NEXT_LOCALE=zh"
  python3 src/web_session.py verify            # 验证会话并打印账号
  python3 src/web_session.py call auth.user    # 直接调用任意 query procedure
  python3 src/web_session.py probe             # ★ 只读诊断：TTL / 滚动续期 / 匿名对照
  python3 src/web_session.py watch --interval 1800   # 持续监测会话是否失效

`probe` 是全只读的：只发身份 / 闸门 / 余额这类 query，**不创建任何生成任务、不计费**。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AVM_BASE_URL", "https://aivideomaker.ai").rstrip("/")
# 与 client.mjs / session-diagnose.mjs / ark_compat/web_client.py 三处**逐字一致**。
# 本文件刻意零依赖标准库，所以不 import 共享常量 —— 一致性改由
# tests/test_ua_consistency.py 锁死。这类散落的字面量曾靠人肉 grep 才发现分叉
# （四处里有一处版本号不一致），不能再依赖人肉。
# 注：此处**刻意不写具体版本号** —— 注释里的版本号会污染 grep 结果，
# 也会被门禁测试当成"又一处 UA 定义"。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# tRPC 的"无参数"要编码成 null + meta 标记 undefined。auth.user / credits.getCredits
# 都要这个形状 —— **少了 meta 会被 zod 判成 400**（实测踩过：auth.user 400 而
# credits.getCredits 却 200，差别就在 meta）。
NULL_INPUT = json.dumps(
    {"0": {"json": None, "meta": {"values": ["undefined"], "v": 1}}},
    separators=(",", ":"),
)

PAGE = "/zh/ai-video-generator"

# 浏览器导出的 cookie jar 常见落位（含项目内已知位置）
COOKIE_JAR_CANDIDATES = (
    "avm-proxy/cookies.json",
    "src/web-adapter/cookies.json",
    "cookies.json",
)


_QUOTES = ("'", '"', "`")
_BARE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{16,}$")
_SET_COOKIE_ATTRS = frozenset({
    "path", "domain", "expires", "max-age", "samesite", "priority",
    "partitioned", "comment", "version",
})


def _strip_quotes(value) -> str:
    """剥掉一层成对的引号（从 shell 里复制出来的值常带）。"""
    t = str(value).strip()
    if len(t) >= 2 and t[0] in _QUOTES and t[-1] == t[0]:
        return t[1:-1].strip()
    return t


def normalize_cookie_header(raw) -> str:
    """把粘贴进来的东西规范成真正的 `Cookie` 头值。

    `AVM_COOKIE` / `--cookie` 会被**逐字**当作 `Cookie` 请求头发出去，它**不是一个 token**。
    格式不对时**不会报错** —— 服务端只是把你当成未登录，而这与「会话已过期」**完全一致**
    —— 于是排查时会去追一个根本没到期的 TTL。这属于沉默误诊，必须在入口堵掉。

    接受的形态：裸 token / `auth_session=…` / 整段 `Cookie:` 头 / cURL 的 `-H 'Cookie: …'`
    / 整段 `Set-Cookie:`（属性会被丢掉）/ 导出的 jar JSON。修过就**打印提示**，不静默。

    规则与 `ark_compat/cookie.py`（Python 唯一实现）和 `client.mjs` 的
    `normalizeCookieHeader()`（JS）一致，三处由 `tests/` 下的同一份用例表锁死。

    本文件刻意保持**零依赖标准库**，所以不 import `ark_compat.cookie`，而是内联同一规则。
    """
    s = _strip_quotes(raw or "")

    if s.startswith("["):  # 整份导出的 cookie jar
        try:
            jar = json.loads(s)
        except (ValueError, TypeError):
            jar = None
        if isinstance(jar, list):
            pairs = [f"{c['name']}={c['value']}" for c in jar
                     if isinstance(c, dict) and c.get("name") and c.get("value")]
            if pairs:
                s = "; ".join(pairs)

    for _ in range(5):  # 层会嵌套：`-H 'Cookie: …'` 要两轮
        before = s
        s = _strip_quotes(s)
        s = re.sub(r"^(?:-H|--header)\s+", "", s, flags=re.IGNORECASE)
        s = re.sub(r"^(?:--cookie|-b)\s+", "", s, flags=re.IGNORECASE)
        s = re.sub(r"^(?:set-)?cookie\s*:\s*", "", s, flags=re.IGNORECASE)
        s = _strip_quotes(s)
        if s == before:
            break
    # 刻意**不**做全局空白折叠：多行粘贴（每行以 `;` 结尾）靠逐段 strip 就能解析；
    # 而"换行但缺 `;`"是真正的畸形输入，必须原样下发让 HTTP 层明确报错，
    # 而不是被折叠成"看似合法的一对"而**静默发错**。

    if "=" in s:
        parts = []
        for seg in s.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            name, sep, _ = seg.partition("=")
            if not sep or name.strip().lower() in _SET_COOKIE_ATTRS:
                continue
            parts.append(seg)
        return "; ".join(parts)

    if _BARE_TOKEN.match(s):
        print("提示：检测到裸 token，已自动补成 auth_session=<token>；建议直接写完整形式。",
              file=sys.stderr)
        return f"auth_session={s}"
    return s  # 拿不准 → 原样交给服务端，让它给出明确错误


def cookie_header(args) -> str:
    ck = normalize_cookie_header(args.cookie or os.environ.get("AVM_COOKIE"))
    if not ck:
        jar_path = find_jar(None)
        if jar_path:
            ck = jar_cookie_header(load_jar(jar_path))
    if not ck:
        sys.exit("缺少 Cookie：设置环境变量 AVM_COOKIE（至少包含 auth_session=...）或用 --cookie 传入")
    return ck


def trpc_call(procedure: str, cookie: str, input_json: str = NULL_INPUT, timeout: int = 25):
    """调用一个 tRPC query procedure，返回 (http_status, 解析后的 JSON 或原始文本)。"""
    qs = urllib.parse.urlencode({"batch": "1", "input": input_json})
    url = f"{BASE}/api/{procedure}?{qs}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Cookie", cookie)
    req.add_header("User-Agent", UA)
    req.add_header("trpc-accept", "application/jsonl")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    except Exception as e:
        return -1, str(e)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def unwrap(payload):
    """从 tRPC 批量响应中取出 data。"""
    if isinstance(payload, list) and payload:
        item = payload[0]
        if isinstance(item, dict) and "result" in item:
            return item["result"].get("data", {}).get("json")
    return None


def cmd_verify(args):
    status, payload = trpc_call("auth.user", cookie_header(args))
    user = unwrap(payload)
    if status == 200 and user:
        print("会话有效 ✓")
        for k in ("id", "email", "name", "role", "onboardingComplete"):
            if k in user:
                print(f"  {k:20} {user[k]}")
        print("\n注：该响应不含会话过期时间。服务端 UserSession.expiresAt 未通过接口暴露。")
        return 0
    print(f"会话无效或已过期 ✗  (HTTP {status})")
    print(json.dumps(payload, ensure_ascii=False)[:400] if not isinstance(payload, str) else payload[:400])
    return 1


def cmd_call(args):
    inp = args.input or NULL_INPUT
    status, payload = trpc_call(args.procedure, cookie_header(args), inp)
    print(f"HTTP {status}")
    print(json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload[:2000])
    return 0 if status == 200 else 1


def cmd_watch(args):
    """持续轮询，记录会话从有效变为无效的时刻，用于实测 TTL。"""
    cookie = cookie_header(args)
    start = time.time()
    last_ok = None
    n = 0
    print(f"开始监测（每 {args.interval}s 一次）。Ctrl-C 停止。")
    while True:
        n += 1
        status, payload = trpc_call("auth.user", cookie)
        user = unwrap(payload)
        ok = status == 200 and bool(user)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        elapsed = (time.time() - start) / 3600
        if ok:
            last_ok = now
            print(f"[{now}] #{n} 有效  (已监测 {elapsed:.1f} 小时)")
        else:
            print(f"[{now}] #{n} 失效  HTTP {status}  (最后一次有效：{last_ok})")
            print("会话已过期，监测结束。")
            return 0
        time.sleep(args.interval)


# ============================================================ 会话诊断（只读）====


def fingerprint(value: str) -> str:
    """凭据指纹：只露头尾。**绝不打印完整凭据值。**"""
    v = (value or "").strip()
    if not v:
        return "(空)"
    return f"{v[:6]}…{v[-4:]}" if len(v) > 12 else "…"


def find_jar(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for p in COOKIE_JAR_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def load_jar(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def jar_cookie_header(jar: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in jar if c.get("name") and c.get("value"))


def auth_session_expiry(jar: list) -> int | None:
    """从 jar 读 `auth_session` 的 expirationDate（epoch 秒）。

    这是**唯一**能拿到绝对过期时间的地方：token 不可自解析、接口不暴露、
    响应头也不重下发。
    """
    for c in jar:
        if c.get("name") == "auth_session" and c.get("expirationDate"):
            try:
                return int(c["expirationDate"])
            except (TypeError, ValueError):
                return None
    return None


def fmt_epoch(ts: int | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "未知"


def set_cookie_names(headers) -> list:
    """只取 Set-Cookie 的**名字**，不打印值。"""
    out = []
    for raw in (headers.get_all("Set-Cookie") or []) if headers else []:
        name = raw.split("=", 1)[0].strip()
        if name:
            out.append(name)
    return out


def http_get(url: str, cookie: str | None, *, accept: str = "*/*", referer: str | None = None,
             timeout: int = 25):
    """返回 (status, headers, body)。`cookie=None` 即匿名对照。"""
    req = urllib.request.Request(url, method="GET")
    if cookie:
        req.add_header("Cookie", cookie)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", accept)
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")
    except Exception as e:  # 网络/代理故障 —— 不能当成"会话失效"
        raise SystemExit(f"请求失败（这是网络问题，不是会话问题）：{type(e).__name__}: {e}") from None


def trpc_url(procedure: str, inp=None) -> str:
    # inp=None 必须走 NULL_INPUT（带 meta 的 void 编码），否则 zod 直接 400
    payload = NULL_INPUT if inp is None else json.dumps({"0": {"json": inp}}, separators=(",", ":"))
    return f"{BASE}/api/{procedure}?" + urllib.parse.urlencode({"batch": "1", "input": payload})


def _user_from(body: str):
    try:
        j = json.loads(body)
        return (j[0]["result"]["data"]["json"]) or None
    except Exception:
        return None


def cmd_probe(args):
    jar_path = find_jar(args.jar)
    jar = load_jar(jar_path) if jar_path else []
    # 裸 token 也接受（自动补 auth_session= 前缀并告警）—— 见 normalize_cookie_header()
    cookie = (normalize_cookie_header(args.cookie or os.environ.get("AVM_COOKIE"))
              or (jar_cookie_header(jar) if jar else ""))
    if not cookie:
        sys.exit("找不到 cookie：用 --cookie / AVM_COOKIE，或放一份导出的 cookie jar")

    m = re.search(r"auth_session=([^;\s]+)", cookie)
    session_val = m.group(1) if m else cookie
    fp = fingerprint(session_val)
    expires = auth_session_expiry(jar)
    now = int(time.time())

    report = {
        "fingerprint": fp,
        "checked_at": fmt_epoch(now),
        "cookie_source": jar_path or "--cookie/AVM_COOKIE",
        "expiry": {
            "source": "jar.expirationDate" if expires else None,
            "epoch": expires,
            "utc": fmt_epoch(expires),
            "remaining_days": round((expires - now) / 86400, 1) if expires else None,
        },
        "applied_rounds": [],
        "anonymous_control": {},
        "page_request": {},
        "framework_routes": {},
        "gate": {},
    }

    line = "=" * 64
    print(line)
    print(f"凭据指纹 : {fp}        （只露头尾，不打印完整值）")
    print(f"凭据来源 : {jar_path or '--cookie / AVM_COOKIE'}")
    if re.fullmatch(r"[A-Za-z0-9_-]{40}", session_val):
        print("token 形态: 40 位不透明随机串（不是 JWT）⇒ 客户端**无法**自解析过期时间")
    if expires:
        print(f"过期时间 : {fmt_epoch(expires)}  剩余 {(expires - now) / 86400:.1f} 天")
        print("           ← 来自导出 jar 的 expirationDate（绝对时间，不必实测推断）")
    else:
        print("过期时间 : 未知（没有 jar 或 jar 里无 expirationDate）—— 只能用探活兜底")
    print(line)

    # ---- 1) 带凭据连发多次只读请求，盯 Set-Cookie ----
    print(f"\n[1] 带凭据连续 {args.rounds} 次只读 auth.user")
    rounds_ok = True
    seen_set_cookie: list[str] = []
    for i in range(1, args.rounds + 1):
        st, hdrs, body_txt = http_get(trpc_url("auth.user"), cookie, referer=f"{BASE}{PAGE}")
        names = set_cookie_names(hdrs)
        user = _user_from(body_txt)
        ok = st == 200 and bool(user)
        rounds_ok = rounds_ok and ok
        seen_set_cookie.extend(names)
        report["applied_rounds"].append(
            {"round": i, "status": st, "valid": ok, "set_cookie_names": names}
        )
        print(f"    #{i}  HTTP {st}  {'有效' if ok else '无效'}   Set-Cookie: {names or '无'}")

    # ---- 2) 匿名对照 ----
    print("\n[2] 匿名对照：同一条请求，去掉凭据再跑一遍")
    st, _, body_txt = http_get(trpc_url("auth.user"), None, referer=f"{BASE}{PAGE}")
    anon_user = _user_from(body_txt)
    report["anonymous_control"] = {
        "procedure": "auth.user",
        "status": st,
        "returned_user": bool(anon_user),
        "body_head": body_txt[:200],
    }
    print(f"    auth.user（无 cookie）    HTTP {st}   返回用户实体: {'是' if anon_user else '否'}")

    # 注意：这里传的是**假 userId**，服务端不校验它 —— 所以返回值只能说明
    # "这个接口不校验凭据"，**不代表真实账号当前的闸门状态**。
    st2, _, body2 = http_get(
        trpc_url("model.needsCaptcha", {"userId": "anonymous"}), None, referer=f"{BASE}{PAGE}"
    )
    report["anonymous_control"]["needsCaptcha_no_cookie"] = {
        "status": st2,
        "body_head": body2[:200],
        "caveat": "用了假 userId；服务端不校验它，该结果不代表真实账号的闸门状态",
    }
    print(f"    needsCaptcha（无 cookie，假 userId）HTTP {st2}   body: {body2.strip()[:90]}")
    print("        ↑ 仅证明该接口不校验凭据；假 userId 的结果不代表真实账号状态")

    # ---- 3) 页面请求下发什么 cookie ----
    print("\n[3] 页面请求的 Set-Cookie（看向下发的 cookie 名）")
    st, hdrs, _ = http_get(f"{BASE}{PAGE}", cookie, accept="text/html")
    page_names = set_cookie_names(hdrs)
    report["page_request"] = {"status": st, "set_cookie_names": page_names}
    print(f"    GET {PAGE}   HTTP {st}   Set-Cookie: {page_names or '无'}")

    # ---- 4) 框架标准会话路由 ----
    print("\n[4] Auth.js 标准会话路由（期望 404 ⇒ 非标准实现，别照框架文档推断）")
    for path in ("/api/auth/session", "/api/auth/csrf", "/api/auth/providers"):
        st, _, _ = http_get(BASE + path, cookie)
        report["framework_routes"][path] = st
        print(f"    {path:26} HTTP {st}")

    # ---- 5) 闸门与余额（只读）----
    print("\n[5] 闸门与余额（只读、免费）")
    st, _, body_txt = http_get(trpc_url("auth.user"), cookie, referer=f"{BASE}{PAGE}")
    user = _user_from(body_txt) or {}
    uid = user.get("id")
    gate = None
    if uid:
        _, _, gate_body = http_get(
            trpc_url("model.needsCaptcha", {"userId": uid}), cookie, referer=f"{BASE}{PAGE}"
        )
        try:
            gate = json.loads(gate_body)[0]["result"]["data"]["json"]
        except Exception:
            gate = gate_body.strip()[:120]
    _, _, bal_body = http_get(trpc_url("credits.getCredits"), cookie, referer=f"{BASE}{PAGE}")
    bal = None
    try:
        bal = (json.loads(bal_body)[0]["result"]["data"]["json"] or {}).get("totalRemaining")
    except Exception:
        pass
    report["gate"] = {"needsCaptcha": gate, "totalRemaining": bal}
    print(f"    needsCaptcha    : {gate}")
    print(f"    totalRemaining  : {bal}")

    # ---- 结论 ----
    rolling = bool(seen_set_cookie)
    print("\n" + line)
    print("结论")
    print(line)
    print(f"· 凭据当前状态   : {'有效' if rounds_ok else '无效 / 异常（见上表）'}")
    print(f"· 绝对过期时间   : {fmt_epoch(expires)}"
          + (f"（剩余 {(expires - now) / 86400:.1f} 天）" if expires else "（未知）"))
    print(f"· TTL 来源       : {'导出 jar 的 expirationDate' if expires else '—'}")
    if rolling:
        print(f"· 滚动续期       : ⚠️ 观察到 Set-Cookie（{sorted(set(seen_set_cookie))}）—— 需人工确认是否含 auth_session")
    else:
        print("· 滚动续期       : 未观察到任何 Set-Cookie ⇒ 服务端不重下发会话 cookie，靠请求续不了期")
    print(f"· 匿名可用       : {'是（该 query 无需凭据）' if anon_user else '否 ⇒ 必须有 cookie'}")
    print("· 到期处理       : 把到期日当**可排期事件**，届时用浏览器重新导出 cookie 覆盖 jar")
    print(line)

    out = args.report or f"/tmp/session-probe-{now}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告已写入 {out}（按约定落 /tmp，不入库）")
    return 0 if rounds_ok else 1


def main():
    ap = argparse.ArgumentParser(description="web 侧会话工具（tRPC over /api）")
    ap.add_argument("--cookie", help="完整 Cookie 字符串（默认读 AVM_COOKIE，再退回 cookie jar）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="验证会话并打印账号信息")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("call", help="调用任意 query procedure，如 auth.user / credits.getCredits")
    p.add_argument("procedure")
    p.add_argument("--input", help="tRPC input JSON（默认 null）")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("probe", help="★ 只读诊断：TTL / 滚动续期 / 匿名对照 / 闸门 / 余额")
    p.add_argument("--jar", help="浏览器导出的 cookie jar 路径（默认自动找）")
    p.add_argument("--rounds", type=int, default=3, help="带凭据连发几轮（默认 3）")
    p.add_argument("--report", help="报告输出路径（默认 /tmp/session-probe-<ts>.json）")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("watch", help="持续监测，记录会话失效时刻")
    p.add_argument("--interval", type=int, default=1800, help="轮询间隔秒数，默认 1800")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
