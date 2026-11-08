#!/usr/bin/env python3
"""wb_solver.py — 用第三方打码服务解 Cloudflare Turnstile.

有些公益站的签到强制 Turnstile（`POST /api/user/checkin?turnstile=<token>`），
而 Cloudflare 会拒绝无头浏览器的挑战会话（实测 challenge-platform 的 /pat 返回
401），所以浏览器自动化走不通。剩下唯一能自动化的路是打码服务：它在自己的
浏览器集群里解出 token，我们拿回来拼到请求上。

支持 capsolver.com（目前只接了这家，接口最简单）：
  createTask  {"type": "AntiTurnstileTaskProxyLess",
               "websiteURL": ..., "websiteKey": ...}
  getTaskResult -> {"status": "ready", "solution": {"token": "0.xxx"}}

配置（环境变量）：
  WB_CAPSOLVER_KEY     打码服务的 clientKey；为空则整个功能关闭
  WB_SOLVER_PROXY      访问打码服务 API 的代理（国内直连不稳时用）
  WB_SOLVER_TIMEOUT    单次求解的最长等待秒数（默认 120）

token 是一次性的、约 5 分钟有效，所以拿到就必须立刻用掉，不能缓存。
"""
import json
import os
import time
import urllib.error
import urllib.request

CAPSOLVER_API = "https://api.capsolver.com"
POLL_INTERVAL = 3

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def client_key():
    return str(os.environ.get("WB_CAPSOLVER_KEY", "") or "").strip()


def configured():
    return bool(client_key())


def _opener():
    proxy = str(os.environ.get("WB_SOLVER_PROXY", "") or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _post(path, payload, timeout=60):
    req = urllib.request.Request(
        CAPSOLVER_API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    with _opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read(512 * 1024).decode("utf-8", "replace"))


def fetch_sitekey(root_url, timeout=8):
    """从 New API 的 /api/status 里读 Turnstile 的 sitekey。

    New API 把是否开启人机验证与 sitekey 都放在 /api/status：
        {"data": {"turnstile_check": true,
                  "turnstile_site_key": "0x4AAAAAAA..."}}
    """
    root = str(root_url or "").rstrip("/")
    if not root:
        return ""
    try:
        req = urllib.request.Request(
            root + "/api/status",
            headers={"Accept": "application/json", "User-Agent": _BROWSER_UA},
            method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(512 * 1024).decode("utf-8", "replace"))
    except Exception:
        return ""
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return ""
    for key in ("turnstile_site_key", "turnstileSiteKey", "TurnstileSiteKey"):
        val = str(d.get(key) or "").strip()
        if val:
            return val
    return ""


def solve_turnstile(website_url, website_key, log=None, timeout=None):
    """解一次 Turnstile，返回 token；失败抛异常（调用方转成可读提示）。"""
    key = client_key()
    if not key:
        raise RuntimeError("未配置 WB_CAPSOLVER_KEY")
    if not website_key:
        raise RuntimeError("拿不到 sitekey")
    timeout = int(timeout or os.environ.get("WB_SOLVER_TIMEOUT", "120") or 120)
    created = _post("/createTask", {
        "clientKey": key,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        },
    })
    if created.get("errorId"):
        raise RuntimeError("%s %s" % (created.get("errorCode"),
                                      created.get("errorDescription") or ""))
    task_id = created.get("taskId")
    if not task_id:
        raise RuntimeError("打码服务没有返回 taskId")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        result = _post("/getTaskResult", {"clientKey": key, "taskId": task_id})
        if result.get("errorId"):
            raise RuntimeError("%s %s" % (result.get("errorCode"),
                                          result.get("errorDescription") or ""))
        if result.get("status") == "ready":
            token = str((result.get("solution") or {}).get("token") or "").strip()
            if not token:
                raise RuntimeError("打码服务返回 ready 但没有 token")
            if log:
                try:
                    log("solver     : Turnstile 已解出（%d 字符）" % len(token))
                except Exception:
                    pass
            return token
    raise RuntimeError("打码超时（%ds）" % timeout)
