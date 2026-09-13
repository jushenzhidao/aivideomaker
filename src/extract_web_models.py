#!/usr/bin/env python3
"""从 aivideomaker.ai 前端页面/JS 中提取 web 侧的能力信息（模型清单、文案、计费口径）。

用途：web 逆向侧的情报提取。网页端的模型清单藏在 Next.js 的 i18n 文案里
（形如 `"model_seedance20_description": "..."`），未登录也能从公开页面拿到。

用法:
  python3 src/extract_web_models.py docs/web-reverse/captured/docs-zh.html
  python3 src/extract_web_models.py docs/web-reverse/captured/*.html --format md
"""

import argparse
import html
import re
import sys

TAG = re.compile(r"<[^>]+>")


def decode(s: str) -> str:
    s = (s.replace("\\u003c", "<").replace("\\u003e", ">")
          .replace("\\n", " ").replace('\\"', '"'))
    return html.unescape(TAG.sub("", s)).strip()


def extract_models(raw: str) -> dict:
    """提取 model_<key>_<remark|description> 形式的 i18n 条目。"""
    flat = raw.replace('\\"', '"')
    pat = re.compile(r'model_([A-Za-z0-9_]+?)_(remark|description)"\s*:\s*"((?:[^"\\]|\\.)*)"')
    out = {}
    for m in pat.finditer(flat):
        out.setdefault(m.group(1), {})[m.group(2)] = decode(m.group(3))
    return out


def extract_api_paths(raw: str) -> list:
    """从页面/JS 中提取疑似内部接口路径。"""
    flat = raw.replace('\\"', '"').replace("\\/", "/")
    found = set(re.findall(r'["\'](/api/[A-Za-z0-9/_\-{}\[\].$:]*)["\']', flat))
    return sorted(found)


def extract_hashed_chunks(raw: str) -> list:
    """提取 Next.js 的 _next/static/chunks 脚本路径。"""
    return sorted(set(re.findall(r'/_next/static/chunks/[^"\'\\ )]+\.js', raw)))


def main():
    ap = argparse.ArgumentParser(description="提取 aivideomaker web 侧能力信息")
    ap.add_argument("files", nargs="+", help="已抓取的 HTML / JS 文件")
    ap.add_argument("--format", choices=["text", "md", "json"], default="text")
    ap.add_argument("--api-paths", action="store_true", help="同时输出疑似内部接口路径")
    ap.add_argument("--chunks", action="store_true", help="同时输出 Next.js chunk 路径")
    args = ap.parse_args()

    import json

    models, paths, chunks = {}, set(), set()
    for f in args.files:
        try:
            raw = open(f, encoding="utf-8", errors="ignore").read()
        except OSError as e:
            print(f"跳过 {f}: {e}", file=sys.stderr)
            continue
        for k, v in extract_models(raw).items():
            models.setdefault(k, {}).update(v)
        if args.api_paths:
            paths.update(extract_api_paths(raw))
        if args.chunks:
            chunks.update(extract_hashed_chunks(raw))

    if args.format == "json":
        print(json.dumps(models, ensure_ascii=False, indent=2))
    elif args.format == "md":
        print("| 模型标识 | 定位 | 说明 |")
        print("|---|---|---|")
        for k, v in sorted(models.items()):
            desc = v.get("description", "").replace("|", "\\|")
            print(f"| `{k}` | {v.get('remark', '')} | {desc} |")
    else:
        print(f"共 {len(models)} 个模型条目：\n")
        for k, v in sorted(models.items()):
            print(f"● {k}  [{v.get('remark', '')}]")
            print(f"    {v.get('description', '')}")

    if args.api_paths:
        print(f"\n疑似内部接口路径（{len(paths)}）：")
        for p in paths:
            print("  ", p)
    if args.chunks:
        print(f"\nNext.js chunks（{len(chunks)}）：")
        for c in chunks:
            print("  ", c)


if __name__ == "__main__":
    main()
