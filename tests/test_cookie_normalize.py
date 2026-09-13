#!/usr/bin/env python3
"""Cookie 头规范化的测试 —— 纯离线、零网络、零额度消耗。

为什么值得单独立一份测试：`AVM_COOKIE` / `--cookie` 的值会被**逐字**当作 `Cookie`
请求头发出去。只填裸 token 时，服务端当它是**格式错误的 Cookie 头**，表现与
「会话已过期」**完全一致** —— 排查时会去追一个根本没到期的 TTL。这属于沉默误诊。

同一条规则有三处实现：`src/ark_compat/cookie.py`（Python 唯一实现）、
`src/web_session.py`（刻意保持零依赖，故内联同一规则）、
`src/web-adapter/client.mjs` 的 `normalizeCookieHeader()`（JS）。

本文件的 `CASES` 表与 `src/web-adapter/tests/cookie-normalize-test.mjs` **逐条对应** ——
改一处必须改另一处，否则两侧规则会漂移。

运行：python3 tests/test_cookie_normalize.py
"""

import contextlib
import io
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import web_session  # noqa: E402
from ark_compat.cookie import normalize_cookie_header  # noqa: E402
from ark_compat.settings import Settings  # noqa: E402
from ark_compat.web_client import WebClient  # noqa: E402

TOKEN = "abcdef1234567890abcdef1234567890abcdef12"  # 40 位，^[a-z0-9]{40}$

# (用例名, 输入, 期望输出) —— 与 cookie-normalize-test.mjs 的用例**逐条对应**
# （JS 侧多一条 `undefined`，Python 没有该值，其余完全相同）。改一处必须改另一处。
CASES = (
    ("裸 token", TOKEN, f"auth_session={TOKEN}"),
    ("只有 auth_session", f"auth_session={TOKEN}", f"auth_session={TOKEN}"),
    ("分号后无空格", f"auth_session={TOKEN};NEXT_LOCALE=zh",
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("分号后有空格", f"auth_session={TOKEN}; NEXT_LOCALE=zh",
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("Cookie: 标签", f"Cookie: auth_session={TOKEN};NEXT_LOCALE=zh",
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("cookie: 标签小写", f"cookie: auth_session={TOKEN}", f"auth_session={TOKEN}"),
    ("Set-Cookie: 标签 + 属性",
     f"Set-Cookie: auth_session={TOKEN}; Path=/; HttpOnly; Secure; SameSite=Lax",
     f"auth_session={TOKEN}"),
    ("curl -H 包装", f"-H 'Cookie: auth_session={TOKEN}; NEXT_LOCALE=zh'",
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("curl --cookie 包装", f"--cookie 'auth_session={TOKEN}'", f"auth_session={TOKEN}"),
    ("shell 双引号包裹", f'"auth_session={TOKEN}"', f"auth_session={TOKEN}"),
    ("导出的 jar JSON",
     f'[{{"name":"auth_session","value":"{TOKEN}"}},{{"name":"NEXT_LOCALE","value":"zh"}}]',
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("cookie 名大小写保持", f"AuthSession={TOKEN};NEXT_LOCALE=zh",
     f"AuthSession={TOKEN}; NEXT_LOCALE=zh"),
    ("多行粘贴，每行以分号结尾",
     f"auth_session={TOKEN};\n  NEXT_LOCALE=zh;",
     f"auth_session={TOKEN}; NEXT_LOCALE=zh"),
    ("空串", "", ""),
    ("纯空白", "   ", ""),
    ("None", None, ""),
    ("太短（不猜）", "abc", "abc"),
    ("含空格（不猜）", "foo bar", "foo bar"),
    ("无关的键值对原样通过", "a=b; c=d", "a=b; c=d"),
    ("15 位（边界外）", "a" * 15, "a" * 15),
    ("16 位（边界内）", "a" * 16, "auth_session=" + "a" * 16),
    ("含中文（不猜）", "一二三四五六七八九十一二三四五六七八", "一二三四五六七八九十一二三四五六七八"),
    ("前后空白", f"  {TOKEN}  ", f"auth_session={TOKEN}"),
    ("尾分号", f"auth_session={TOKEN};", f"auth_session={TOKEN}"),
    # 故意畸形：有换行但没有分号 ⇒ 必须**原样**下发，让 HTTP 层明确报错。
    # 若把它折叠成一对，就会发出一个"看起来合法"的错误 cookie。
    ("换行无分号（原样通过）", f"auth_session={TOKEN}\n NEXTLOCALE=zh",
     f"auth_session={TOKEN}\n NEXTLOCALE=zh"),
)


class NormalizeTest(unittest.TestCase):
    def test_cases(self):
        for name, raw, want in CASES:
            with self.subTest(case=name):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self.assertEqual(normalize_cookie_header(raw), want)

    def test_bare_token_warns(self):
        """补全时必须告警 —— 静默"修正"会让用户永远学不会正确写法。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            normalize_cookie_header(TOKEN)
        self.assertEqual(len(w), 1)
        self.assertIn("裸 token", str(w[0].message))

    def test_full_header_does_not_warn(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            normalize_cookie_header(f"auth_session={TOKEN}; NEXT_LOCALE=zh")
        self.assertEqual(w, [])

    def test_web_session_impl_matches(self):
        """`src/web_session.py` 内联了同一规则，两侧必须完全一致（防漂移）。"""
        for name, raw, _want in CASES:
            with self.subTest(case=name):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    canon = normalize_cookie_header(raw)
                with contextlib.redirect_stderr(io.StringIO()):
                    inline = web_session.normalize_cookie_header(raw)
                self.assertEqual(canon, inline, f"{name}: 两处实现已漂移")


class WiringTest(unittest.TestCase):
    """规则接进了配置与客户端的入口 —— 只测实现不测接线，等于没接。"""

    def test_settings_from_env_expands_bare_token(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.from_env({"AVM_COOKIE": TOKEN})
        self.assertEqual(s.cookie, f"auth_session={TOKEN}")
        self.assertTrue(s.web_ready)

    def test_settings_keeps_full_header_untouched(self):
        full = f"auth_session={TOKEN}; NEXT_LOCALE=zh"
        s = Settings.from_env({"AVM_COOKIE": full})
        self.assertEqual(s.cookie, full)

    def test_web_client_expands_bare_token(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            c = WebClient(cookie=TOKEN)
        self.assertEqual(c.cookie, f"auth_session={TOKEN}")
        self.assertEqual(c._headers()["cookie"], f"auth_session={TOKEN}")

    def test_empty_credentials_still_rejected(self):
        """兜底不能把"没有凭据"变成"匿名凭据"。"""
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    WebClient(cookie=raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
