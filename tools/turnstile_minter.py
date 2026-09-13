#!/usr/bin/env python3
"""Turnstile token 铸造器（Linux + **Xvfb 有头** Chrome + 原生 CDP）

2026-09-14 实测（node-064 / Ubuntu 22.04 / Chrome 153 / 出口 64.81.112.31）：

| 模式              | 结果                              |
|-------------------|-----------------------------------|
| `--headless=new`  | **0/4**，每个 46 秒 TIMEOUT（挂死）|
| 有头（xvfb-run）  | **8/8**，平均 **3.7 秒/个**        |

⇒ **headless 不可行，Xvfb 有头可行**。别再为省资源去试无头。

用法:
    N=3 /usr/bin/python3 /tmp/minter.py               # 有头（Xvfb）
    N=3 HEADLESS=1 /usr/bin/python3 /tmp/minter.py     # --headless=new
    KEEP=1 ...                                          # 复用已运行的 Chrome

产出 /tmp/tokens.txt（一行一个 token），并打印每个 token 的铸造耗时。

要点:
  * 必须在**真实 origin** 的页面里 turnstile.render()（sitekey 与页面同源，否则 110200）
  * token 单次有效 ⇒ 每提交一次要重新铸一个
  * 铸造与提交应走**同一个出口 IP**（绑定强度待实测）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

PORT = 9222
SITEKEY = os.environ.get("SITEKEY", "0x4AAAAAABrddy3Hsje8mwB_")
ORIGIN = os.environ.get("ORIGIN", "https://aivideomaker.ai/zh/ai-video-generator")
N = int(os.environ.get("N", "1"))
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
KEEP = os.environ.get("KEEP", "0") == "1"


def http_json(url, timeout=15):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def cdp_ok():
    try:
        http_json(f"http://127.0.0.1:{PORT}/json/version")
        return True
    except Exception:
        return False


def start_chrome():
    if cdp_ok() and KEEP:
        return
    subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    time.sleep(2)
    args = ["/usr/bin/google-chrome", f"--remote-debugging-port={PORT}",
            "--remote-debugging-address=127.0.0.1", "--user-data-dir=/root/avm-chrome",
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check", "--window-size=1280,900",
            "--remote-allow-origins=*"]
    if HEADLESS:
        args.append("--headless=new")
    cmd = ["setsid", "nohup", "xvfb-run", "-a", "--server-args=-screen 0 1280x900x24"] + args + ["about:blank"]
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
        self.ws = websocket.create_connection(ws_url, timeout=150, suppress_origin=True)
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


MINT_JS = """
(async () => {
  if (typeof window.turnstile === 'undefined') return {ok:false, why:'no turnstile api'};
  return await new Promise((resolve) => {
    const t = setTimeout(() => resolve({ok:false, why:'TIMEOUT'}), 45000);
    try {
      const host = document.createElement('div');
      host.style.cssText = 'position:fixed;left:10px;top:10px;width:300px;height:65px;z-index:99999;background:#fff';
      document.body.appendChild(host);
      window.turnstile.render(host, {
        sitekey: %s,
        callback: (tok) => { clearTimeout(t); resolve({ok:true, token:String(tok)}); },
        'error-callback': (e) => { clearTimeout(t); resolve({ok:false, why:'ERR '+String(e)}); },
      });
    } catch (e) { clearTimeout(t); resolve({ok:false, why:'EX '+String(e)}); }
  });
})()
""" % json.dumps(SITEKEY)

PROBE = "typeof window.turnstile + '|' + document.readyState + '|' + location.href"


def mint(bc, i):
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
        val = val_of(bc.call("Runtime.evaluate", session=sess, expression=MINT_JS,
                             awaitPromise=True, returnByValue=True))
        dt = time.time() - t0
        if isinstance(val, dict) and val.get("ok"):
            print(f"  #{i} token len={len(val['token'])} 铸造耗时={dt:.1f}s head={val['token'][:24]}...")
            return val["token"], dt
        print(f"  #{i} 失败：{val}（耗时 {dt:.1f}s）")
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
    for i in range(1, N + 1):
        tok, dt = mint(bc, i)
        if tok:
            tokens.append(tok)
        times.append(dt)
    with open("/tmp/tokens.txt", "w") as f:
        f.write("\n".join(tokens))
    if times:
        print(f"\n成功 {len(tokens)}/{N}，平均铸造耗时 {sum(times)/len(times):.1f}s（min {min(times):.1f} / max {max(times):.1f}）")
    print("token 已写入 /tmp/tokens.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
