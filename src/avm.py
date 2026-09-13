#!/usr/bin/env python3
"""aivideomaker.ai 视频生成 API 客户端。

用法:
  export AVM_KEY="ak_xxx"        # 或 --key 传入

  # 提交任务（--param 可重复，值自动转数字/布尔）
  avm.py submit t2v --param prompt="..." --param aspectRatio=16:9 --param duration=5
  avm.py submit seedance20 --param prompt="..." --param ratio=16:9 --param duration=5 --param resolution=720

  # 探测某模型的必填字段（发空 body，不会创建任务）
  avm.py probe seedance20

  # 查详情 / 查状态
  avm.py get <taskId>
  avm.py status <taskId>

  # 轮询等待，可选完成后下载
  avm.py wait <taskId> [--interval 25] [--timeout 900] [--download out.mp4]

  # 取消（积分全额退回）
  avm.py cancel <taskId>
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("AVM_BASE_URL", "https://aivideomaker.ai").rstrip("/")
TERMINAL = {"COMPLETED", "SUCCESS", "SUCCESSFUL", "FAILED", "FAILURE", "ERROR", "CANCEL", "CANCELLED"}


def _key(args):
    k = args.key or os.environ.get("AVM_KEY")
    if not k:
        sys.exit("缺少 API Key：设置环境变量 AVM_KEY 或用 --key 传入")
    return k


def _req(method, path, key, body=None, timeout=60, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("key", key)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def _coerce(v):
    """把 --param 的值尽量转成 number/bool，因为各模型类型要求不一致。"""
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def cmd_probe(args):
    code, res = _req("POST", f"/api/v1/generate/{args.model}", _key(args), {})
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if code == 400:
        print(f"\n[HTTP {code}] 以上是 {args.model} 的必填字段提示（未创建任务）", file=sys.stderr)
    return 0


def cmd_submit(args):
    params = {}
    for p in args.param:
        if "=" not in p:
            sys.exit(f"--param 格式错误：{p}（应为 key=value）")
        k, v = p.split("=", 1)
        params[k] = _coerce(v)
    if args.raw_type:  # 强制某些字段为字符串（t2v 的 duration 要求 string）
        for k in args.raw_type:
            if k in params:
                params[k] = str(params[k])
    hdrs = {}
    if args.max_credits is not None:
        hdrs["X-Max-Credits"] = args.max_credits
    if args.idempotency_key:
        hdrs["Idempotency-Key"] = args.idempotency_key
    code, res = _req("POST", f"/api/v1/generate/{args.model}", _key(args), params, headers=hdrs)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if code != 200:
        return 1
    if args.wait:
        return _wait(res["taskId"], _key(args), args.interval, args.timeout, args.download)
    return 0


def _wait(task_id, key, interval, timeout, download=None):
    path = f"/api/v1/tasks/{task_id}"
    deadline = time.time() + timeout
    while True:
        _, res = _req("GET", path, key, timeout=30)
        st = res.get("status")
        print(f"[{time.strftime('%H:%M:%S')}] {st}", flush=True)
        if st in TERMINAL:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            url = (res.get("output") or {}).get("url")
            if st in ("COMPLETED", "SUCCESS", "SUCCESSFUL") and url and download:
                urllib.request.urlretrieve(url, download)
                print(f"已下载 -> {download} ({os.path.getsize(download)} bytes)")
            return 0 if st in ("COMPLETED", "SUCCESS", "SUCCESSFUL") else 1
        if time.time() > deadline:
            print("轮询超时", file=sys.stderr)
            return 1
        time.sleep(interval)


def cmd_account(args):
    _, res = _req("GET", "/api/v1/account", _key(args))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_models(args):
    _, res = _req("GET", "/api/v1/account", _key(args))
    print("supportedModels:", ", ".join(res.get("supportedModels", [])))
    print("currentBalance:", res.get("currentBalance"))
    return 0


def cmd_quote(args):
    params = {}
    for p in args.param:
        if "=" not in p:
            sys.exit(f"--param 格式错误：{p}（应为 key=value）")
        k, v = p.split("=", 1)
        params[k] = v if args.raw else _coerce(v)
    for k in args.raw_type:
        if k in params:
            params[k] = str(params[k])
    code, res = _req("POST", f"/api/v1/quote/{args.model}", _key(args), params)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if code == 200:
        print(f"\n=> {res['credits']} credits (${res['listPriceUsd']}) "
              f"affordable={res['affordable']} billable={res['billable']}", file=sys.stderr)
    return 0 if code == 200 else 1


def cmd_list(args):
    _, res = _req("GET", "/api/v1/tasks", _key(args))
    tasks = res.get("tasks", [])
    for t in tasks:
        print(f"{t.get('createdAt','')[:19]}  {t['status']:10} {t['model']:12} "
              f"charge={t.get('creditsCharged')} refund={t.get('creditsRefunded')}  {t['id']}")
    return 0


def cmd_status(args):
    _, res = _req("GET", f"/api/v1/tasks/{args.task_id}/status", _key(args))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_get(args):
    _, res = _req("GET", f"/api/v1/tasks/{args.task_id}", _key(args))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    _, res = _req("GET", f"/api/v1/tasks/{args.task_id}/status", _key(args))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_wait(args):
    return _wait(args.task_id, _key(args), args.interval, args.timeout, args.download)


def cmd_cancel(args):
    code, res = _req("PUT", f"/api/v1/tasks/{args.task_id}/cancel", _key(args))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if code == 200:
        _, detail = _req("GET", f"/api/v1/tasks/{args.task_id}", _key(args))
        print(f"creditsCharged={detail.get('creditsCharged')} "
              f"creditsRefunded={detail.get('creditsRefunded')}")
    return 0 if code == 200 else 1


def main():
    ap = argparse.ArgumentParser(description="aivideomaker.ai API 客户端")
    ap.add_argument("--key", help="API Key（默认读环境变量 AVM_KEY）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="发空 body 探测模型必填字段")
    p.add_argument("model")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("account", help="体检：余额 / 限额 / supportedModels")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("models", help="只打印支持的模型列表")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("quote", help="非计费报价（submit 前必做）")
    p.add_argument("model")
    p.add_argument("--param", action="append", default=[], metavar="k=v")
    p.add_argument("--raw", action="store_true", help="所有值按字符串提交")
    p.add_argument("--raw-type", action="append", default=[], help="强制该字段以字符串提交")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("list", help="列出该 key 的任务")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("submit", help="创建生成任务")
    p.add_argument("model")
    p.add_argument("--param", action="append", default=[], metavar="k=v")
    p.add_argument("--raw-type", action="append", default=[], help="强制该字段以字符串提交")
    p.add_argument("--max-credits", type=int, help="X-Max-Credits 支出上限，超出则在计费前拒绝")
    p.add_argument("--idempotency-key", help="Idempotency-Key 防重复扣费")
    p.add_argument("--wait", action="store_true", help="提交后轮询等待")
    p.add_argument("--interval", type=int, default=25)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--download")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("get", help="查任务详情")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("status", help="查任务状态")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", help="轮询等待完成")
    p.add_argument("task_id")
    p.add_argument("--interval", type=int, default=25)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--download")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("cancel", help="取消任务（PUT，退回积分）")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_cancel)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
