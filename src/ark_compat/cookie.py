"""Cookie 头的规范化 —— Python 侧的唯一实现，由 settings / web_client 共用。

**为什么需要它**：`AVM_COOKIE` 的值会被**逐字**当作 `Cookie` 请求头发出去 —— 它是一个
请求头，**不是一个 token**。所以格式不对时**不会报错**，服务端只是把你当成未登录，
而这与「会话已过期」**表现完全一致** —— 于是排查时会去追一个根本没到期的 TTL。

因此这里接受人们**实际会粘贴**的各种形态，并在**修过**的时候明确告警：

    'abc…'                            （裸 token）      -> 'auth_session=abc…' + 告警
    'auth_session=abc…'                                 -> 原样
    'auth_session=abc…;NEXT_LOCALE=zh'                  -> 原样（分隔符规范成 "; "）
    'Cookie: auth_session=abc…; NEXT_LOCALE=zh'         -> 剥掉标签
    "-H 'Cookie: auth_session=abc…'"                    -> 剥掉 cURL 包装
    'Set-Cookie: auth_session=abc…; Path=/; HttpOnly'   -> 丢掉属性
    '[{"name":"auth_session","value":"abc…"}]'          -> jar JSON 拼成头

认不出来的一律**原样返回**，让服务端（而不是本函数）给出错误。**绝不猜。**

规则与 JS 侧 `src/web-adapter/client.mjs` 的 `normalizeCookieHeader()` 必须一致，
两侧由 `tests/test_cookie_normalize.py` 与 `src/web-adapter/tests/cookie-normalize-test.mjs`
的**同一份用例表**锁死。
"""

from __future__ import annotations

import json
import re
import warnings

AUTH_COOKIE_NAME = "auth_session"

# 裸 token：不含 `=` 与 `;`，且整串都是 token 字符集、长度 ≥ 16
_BARE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{16,}$")

# shell / header 噪声，逐层剥掉
_SHELL_HEADER = re.compile(r"^(?:-H|--header)\s+", re.IGNORECASE)
_SHELL_COOKIE = re.compile(r"^(?:--cookie|-b)\s+", re.IGNORECASE)
_LABEL = re.compile(r"^(?:set-)?cookie\s*:\s*", re.IGNORECASE)

# Set-Cookie 的**属性**：在 Cookie 请求头里永远非法，粘贴整段响应头时必须丢掉。
# （`HttpOnly` / `Secure` / `Partitioned` 没有 `=`，会按"空段"被丢弃。）
_SET_COOKIE_ATTRS = frozenset({
    "path", "domain", "expires", "max-age", "samesite", "priority",
    "partitioned", "comment", "version",
})

_QUOTES = ("'", '"', "`")


def _strip_quotes(value) -> str:
    """剥掉一层成对的引号（从 shell 里复制出来的值常带）。"""
    t = str(value).strip()
    if len(t) >= 2 and t[0] in _QUOTES and t[-1] == t[0]:
        return t[1:-1].strip()
    return t


def normalize_cookie_header(raw) -> str:
    """把用户实际粘贴的内容规范成真正的 `Cookie` 头值（规则见模块 docstring）。"""
    s = _strip_quotes(raw or "")

    # 整份导出的 cookie jar，以 JSON 形式粘贴
    if s.startswith("["):
        try:
            jar = json.loads(s)
        except (ValueError, TypeError):
            jar = None
        if isinstance(jar, list):
            pairs = [
                f"{c['name']}={c['value']}"
                for c in jar
                if isinstance(c, dict) and c.get("name") and c.get("value")
            ]
            if pairs:
                s = "; ".join(pairs)

    # 逐层剥 shell / header 噪声。用循环是因为层会嵌套：
    # `-H 'Cookie: …'` 需要两轮才剥得干净。有界，且每轮都在变短。
    for _ in range(5):
        before = s
        s = _strip_quotes(s)
        s = _SHELL_HEADER.sub("", s)
        s = _SHELL_COOKIE.sub("", s)
        s = _LABEL.sub("", s)
        s = _strip_quotes(s)
        if s == before:
            break
    # 刻意**不**做全局空白折叠：多行粘贴（每行以 `;` 结尾）靠逐段 strip 就能正确解析；
    # 而"换行但缺 `;`"是真正的畸形输入 —— 那种必须**原样**下发、让 HTTP 层明确报错，
    # 而不是被折叠成"看似合法的一对"而**静默发错**。

    if "=" in s:
        parts = []
        for seg in s.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            name, sep, _ = seg.partition("=")
            if not sep:  # 光秃秃的 `HttpOnly` / `Secure`
                continue
            if name.strip().lower() in _SET_COOKIE_ATTRS:
                continue
            parts.append(seg)
        return "; ".join(parts)

    # 一个 `=` 都没有 —— 要么是裸 token，要么是我们拒绝猜的东西
    if _BARE_TOKEN.match(s):
        warnings.warn(
            "AVM_COOKIE 看起来是裸 token 而不是 Cookie 头，已自动补成 "
            f"'{AUTH_COOKIE_NAME}=<token>'。建议直接写完整形式。",
            UserWarning,
            stacklevel=2,
        )
        return f"{AUTH_COOKIE_NAME}={s}"
    return s
