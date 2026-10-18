#!/usr/bin/env python3
"""wb_browser.py — 用无头浏览器完成「需要人机验证」的签到.

有些公益站（如幻城网安 api.hcnsec.cn）的签到强制 Cloudflare Turnstile：

    POST /api/user/checkin?turnstile=<token>

token 由 challenges.cloudflare.com 的 JS 在浏览器里算出来，一次性、约 5 分钟
过期，纯 HTTP 请求拿不到。唯一的自动化路径就是真的跑一个浏览器，让站点自己的
页面去解 Turnstile，然后点它自己的「签到」按钮。

本模块只做这一件事：
  1. 用 Playwright 起一个无头 Chromium（每个站点一个持久化 profile，
     登录态/Cloudflare cookie 都能跨天复用）；
  2. 注入站点配置里的 session cookie（有就不用再登录）；
  3. 打开站点的个人中心，等页面把 Turnstile 解出来；
  4. 点「签到」按钮，截获它自己发出的 POST /api/user/checkin 响应并解析。

依赖是可选的：没装 playwright（主镜像就是精简 Alpine）时 available() 返回
False，调用方退回纯 HTTP 逻辑并提示「只能手动签到」，不影响其它功能。

**实测结论（2026-10-18，幻城网安 api.hcnsec.cn）**：这条路径对该站点不可行。
用原版 Playwright 的无头 / 有头 Chromium、以及 patchright（stealth 版）+ 真实
Chrome 都试过：站点会正常调用 turnstile.execute()，也确实向
challenges.cloudflare.com 发出了 /fo、/pat、/ci 请求，但
`GET .../challenge-platform/h/b/pat/...` 返回 401，getResponse() 始终为空，
拿不到 token。也就是说 Cloudflare 拒绝了自动化会话，不是脚本写错了。

所以这个模块留给「Turnstile 较宽松的其它站点」用；对强校验的站点，真正的
自动化只有一条路——接入第三方打码服务（capsolver / 2captcha）拿 token，
再把 token 拼到 `?turnstile=` 上（`wb_stations.checkin(st_id, turnstile=...)`
已经支持）。
"""
import json
import os
import re
import time

# 签到按钮的候选文案（New API 系常见写法）
_CHECKIN_TEXTS = ("立即签到", "签到", "每日签到", "打卡")
# 已经签过的提示文案
_ALREADY_TEXTS = ("已签到", "今日已签", "已经签到", "明天再来")

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--lang=zh-CN",
]


def _load_sync_api():
    """按需加载 playwright.sync_api.sync_playwright（可选依赖）。

    用 __import__ 而不是 import 语句：playwright 只存在于 :browser 镜像里，
    精简镜像不该因为一个 import 语句就要求把它（以及 importlib）打进运行时
    清单——tests/_test_release_assets.py 会扫 wb_*.py 的所有 import。
    """
    try:
        module = __import__("playwright.sync_api", fromlist=["sync_playwright"])
        return module.sync_playwright
    except Exception:
        return None


def available():
    """当前环境能不能跑浏览器签到（装了 playwright 且浏览器已下载）。"""
    return _load_sync_api() is not None


def _log(log, msg):
    if log:
        try:
            log("browser    : " + msg)
        except Exception:
            pass


def _root(station):
    """站点根地址：去掉 /v1、/api/v1、/api 之类的尾巴。"""
    base = str(station.get("base_url") or "").strip().rstrip("/")
    for suffix in ("/v1", "/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/")


def _profile_dir(station, accounts_dir):
    """每个站点一个持久化 profile：登录态与 Cloudflare 的 cookie 都留在这里。"""
    sid = str(station.get("id") or "station")
    path = os.path.join(str(accounts_dir or "accounts"), "browser", sid)
    os.makedirs(path, exist_ok=True)
    return path


def _seed_cookie(context, station, root):
    """把站点配置里的 session cookie 注入浏览器上下文。"""
    raw = str(station.get("session_cookie") or "").strip()
    if not raw:
        return False
    if "=" in raw and (";" in raw or raw.startswith("session=")):
        value = raw.split("session=", 1)[1].split(";")[0] if "session=" in raw else raw
    else:
        value = raw
    host = re.sub(r"^https?://", "", root).split("/")[0]
    domain = host.split(":")[0]
    try:
        context.add_cookies([{
            "name": "session", "value": value.strip(),
            "domain": domain, "path": "/",
            "httpOnly": True, "secure": root.startswith("https"),
            "sameSite": "Lax",
        }])
        return True
    except Exception:
        return False


def _find_checkin_button(page):
    """在页面上找「签到」按钮；返回 locator 或 None。"""
    for text in _CHECKIN_TEXTS:
        for selector in (
            "button:has-text('%s')" % text,
            "a:has-text('%s')" % text,
            "[role=button]:has-text('%s')" % text,
        ):
            try:
                loc = page.locator(selector).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
    return None


def _page_says_already(page):
    try:
        text = page.inner_text("body")
    except Exception:
        return False
    return any(w in text for w in _ALREADY_TEXTS)


def _maybe_login(page, station, log):
    """被重定向到登录页时，用配置里的账号密码登录（页面里同样有人机验证）。"""
    username = str(station.get("login_username") or "").strip()
    password = str(station.get("login_password") or "").strip()
    if not username or not password:
        return False, "需要登录但没有配置账号密码"
    try:
        page.fill("input[name='username'], input[placeholder*='用户名'], input[placeholder*='账号']",
                  username, timeout=8000)
        page.fill("input[type='password']", password, timeout=8000)
        page.click("button[type='submit'], button:has-text('登录')", timeout=8000)
        page.wait_for_timeout(4000)
        return True, ""
    except Exception as exc:
        return False, "自动登录失败: %s" % str(exc)[:120]


def checkin(station, accounts_dir=None, log=None, headless=True, timeout=120):
    """跑浏览器签到，返回 (ok, message, raw)。

    ok=True 表示站点的签到接口确实返回了成功；raw 是那条 POST 的响应体。
    """
    sync_playwright = _load_sync_api()
    if sync_playwright is None:
        return (False, "未安装 playwright，无法使用浏览器签到", "")

    root = _root(station)
    if not root:
        return (False, "站点地址为空", "")
    deadline = time.time() + timeout
    _log(log, "开始浏览器签到 %s (%s)" % (station.get("name"), root))

    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                _profile_dir(station, accounts_dir),
                headless=headless,
                args=_LAUNCH_ARGS,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 820},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"),
            )
            try:
                context.add_init_script(_STEALTH_JS)
                _seed_cookie(context, station, root)
                page = context.pages[0] if context.pages else context.new_page()

                # 个人中心是 New API 系签到按钮所在的页面
                for path in ("/profile", "/console/personal", "/"):
                    if time.time() > deadline:
                        return (False, "浏览器签到超时（打开页面阶段）", "")
                    try:
                        page.goto(root + path, wait_until="domcontentloaded",
                                  timeout=30000)
                    except Exception:
                        continue
                    page.wait_for_timeout(2500)
                    if "login" in (page.url or "").lower():
                        ok, why = _maybe_login(page, station, log)
                        if not ok:
                            return (False, why, "")
                        page.goto(root + path, wait_until="domcontentloaded",
                                  timeout=30000)
                        page.wait_for_timeout(2500)
                    if _page_says_already(page):
                        return (True, "今天已签到（页面显示已签到）", "")
                    button = _find_checkin_button(page)
                    if button:
                        break
                else:
                    return (False, "没在站点页面上找到「签到」按钮"
                                   "（可能需要先登录，或站点改版了）", "")

                # 点按钮，同时截获它自己发出的签到请求
                raw = ""
                try:
                    with page.expect_response(
                            lambda r: "/checkin" in r.url
                            and r.request.method == "POST",
                            timeout=45000) as info:
                        button.click(timeout=10000)
                    resp = info.value
                    try:
                        raw = resp.text()
                    except Exception:
                        raw = ""
                except Exception as exc:
                    # 没截到响应：可能页面显示「已签到」了，也可能超时
                    if _page_says_already(page):
                        return (True, "今天已签到（页面显示已签到）", "")
                    return (False, "点了签到但没有截到签到请求: %s" % str(exc)[:140], "")

                ok, msg = _parse(raw)
                _log(log, "浏览器签到 %s -> %s (%s)"
                     % (station.get("name"), "ok" if ok else "fail", msg))
                return (ok, msg, raw)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        return (False, "浏览器启动失败: %s" % str(exc)[:160], "")


def _parse(raw):
    """解析签到接口的响应体，返回 (ok, message)。"""
    text = (raw or "").strip()
    if not text:
        return (True, "签到请求已发出（响应为空）")
    try:
        data = json.loads(text)
    except Exception:
        return (True, text[:160])
    if not isinstance(data, dict):
        return (True, text[:160])
    if data.get("success") is False:
        return (False, str(data.get("message") or "站点拒绝了签到")[:160])
    payload = data.get("data")
    reward = ""
    if isinstance(payload, dict):
        for key in ("quota_awarded", "checkin_quota", "quota", "reward"):
            if payload.get(key) not in (None, "", 0, "0"):
                reward = str(payload[key])
                break
    msg = str(data.get("message") or "签到成功").strip()
    if reward:
        msg += "（配额 +%s）" % reward
    return (True, msg[:160])
