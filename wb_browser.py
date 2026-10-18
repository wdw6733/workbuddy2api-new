#!/usr/bin/env python3
"""wb_browser.py — 用无头浏览器给需要人机验证的站点「铸」一个 Turnstile token.

背景：有些公益站（如幻城网安 api.hcnsec.cn）的签到强制 Cloudflare Turnstile：

    POST /api/user/checkin?turnstile=<token>

token 由 challenges.cloudflare.com 的 JS 生成、一次性、约 5 分钟过期。普通
Playwright 起的 Chromium 会被 Cloudflare 识破（challenge-platform 的 /pat 返回
401，getResponse() 永远是空），所以这里用 **cloakbrowser**（打过指纹补丁的
Chromium）来跑。

取 token 的方式不是「等站点页面自己渲染」，而是我们自己注入
`turnstile/v0/api.js?render=explicit`，把 widget 渲染到一个隐藏 div，再轮询
`getResponse(widgetId)`，同时点掉 challenge iframe 里的 checkbox。实测幻城网安：
首页注入后能拿到 794 字符的 token，带它 POST 签到接口会走到鉴权
（`AUTH_UNAUTHORIZED`）而不是「Turnstile token 为空」——说明 token 被接受了。

拿到 token 后不在浏览器里点签到：网关本来就有站点的访问令牌，直接 HTTP POST
更快也更稳。所以本模块只负责「铸 token」，签到由 wb_stations 完成。

依赖是可选的：没装 cloakbrowser / playwright（主镜像就是精简 Alpine）时
available() 返回 False，调用方退回纯 HTTP 逻辑并提示「只能手动签到」。
"""
import os
import re
import threading
import time
from urllib.parse import urlsplit

# 站点可能把 widget 渲染在这几个路由上；先试首页（多数 SPA 都加载了 api.js）
_CANDIDATE_PATHS = ("", "/sign-in", "/profile", "/login")

TURNSTILE_API_URL = (
    "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
)

# 自己渲染 widget 并等它出 token。widget id 拿不到就换下一个路由。
MINT_JS = """async ([sitekey, apiUrl]) => {
	const wait = (ms) => new Promise(r => setTimeout(r, ms));
	for (let i = 0; i < 40 && typeof window.turnstile !== 'object'; i++) await wait(250);
	if (typeof window.turnstile !== 'object' && apiUrl) {
		await new Promise((resolve) => {
			const s = document.createElement('script');
			s.src = apiUrl; s.async = true;
			s.onload = resolve; s.onerror = resolve;
			document.head.appendChild(s);
		});
		for (let i = 0; i < 40 && typeof window.turnstile !== 'object'; i++) await wait(250);
	}
	if (typeof window.turnstile !== 'object') return null;
	const box = document.createElement('div');
	box.style.cssText = 'position:fixed;bottom:4px;right:4px;width:300px;height:65px;z-index:99999';
	document.body.appendChild(box);
	try { return window.turnstile.render(box, {sitekey}); } catch (e) { return null; }
}"""

GET_RESPONSE_JS = """(widgetId) => {
	try { return window.turnstile.getResponse(widgetId) || null; } catch (e) { return null; }
}"""

# cloakbrowser 免费版只允许 1 个并发会话，签到又是一个接一个跑的，直接串行化。
_BROWSER_LOCK = threading.Lock()


def _log(log, msg):
    if log:
        try:
            log("browser    : " + msg)
        except Exception:
            pass


def _load_cloak():
    """cloakbrowser（首选：指纹打过补丁，能过 Cloudflare）。"""
    try:
        return __import__("cloakbrowser", fromlist=["launch_persistent_context"])
    except Exception:
        return None


def _load_playwright():
    """playwright（兜底：能跑，但对强校验的 Turnstile 大概率拿不到 token）。"""
    try:
        return __import__("playwright.sync_api", fromlist=["sync_playwright"])
    except Exception:
        return None


def available():
    """当前镜像里有没有能用的浏览器。"""
    return _load_cloak() is not None or _load_playwright() is not None


def engine():
    """返回实际会用的引擎名（面板/日志用）。"""
    if _load_cloak() is not None:
        return "cloakbrowser"
    if _load_playwright() is not None:
        return "playwright"
    return ""


def _root(station):
    """站点根地址：去掉 /v1、/api/v1、/api 之类的尾巴。"""
    base = str(station.get("base_url") or "").strip().rstrip("/")
    for suffix in ("/v1", "/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/")


def _profile_dir(station, accounts_dir):
    """每个站点一个持久化 profile：Cloudflare 的 cookie 与站点会话都留在里面。"""
    sid = str(station.get("id") or "station")
    path = os.path.join(str(accounts_dir or "accounts"), "browser", sid)
    os.makedirs(path, exist_ok=True)
    return path


def _click_turnstile(page):
    """点掉 challenge iframe 里的 checkbox（managed 模式会要求点一下）。"""
    for frame in getattr(page, "frames", ()):
        try:
            if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
                continue
            box = frame.get_by_role("checkbox", checked=False).first
            if box.is_visible():
                box.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _launch(kind, profile, headless):
    """起一个持久化浏览器上下文。cloakbrowser 与 playwright 的签名不同，分开处理。

    容器里通常以 root 运行，Chromium 的沙箱在 root 下起不来，必须加 --no-sandbox
    （容器本身就是隔离边界，这里不额外损失什么）。
    """
    extra = []
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            extra.append("--no-sandbox")
    except Exception:
        pass
    if kind == "cloakbrowser":
        mod = _load_cloak()
        kwargs = {"headless": headless, "humanize": True, "human_preset": "careful",
                  "viewport": {"width": 1920, "height": 1080}}
        if extra:
            kwargs["args"] = extra
        return mod.launch_persistent_context(profile, **kwargs)
    mod = _load_playwright()
    pw = mod.sync_playwright().start()
    return pw.chromium.launch_persistent_context(
        profile, headless=headless,
        args=extra + ["--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1366, "height": 820}, locale="zh-CN")


def mint_turnstile(station, sitekey, accounts_dir=None, log=None, timeout=90):
    """给站点铸一个 Turnstile token，返回 (token, message)。

    token 是一次性的、约 5 分钟有效，拿到就必须立刻用掉，不能缓存。
    """
    if not sitekey:
        return ("", "没有拿到站点的 turnstile sitekey")
    kind = engine()
    if not kind:
        return ("", "未安装 cloakbrowser / playwright，无法铸 token")
    root = _root(station)
    if not root:
        return ("", "站点地址为空")

    with _BROWSER_LOCK:
        ctx = None
        try:
            _log(log, "%s 铸 Turnstile token（%s）" % (station.get("name"), kind))
            ctx = _launch(kind, _profile_dir(station, accounts_dir), True)
            page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
            deadline = time.time() + timeout
            for path in _CANDIDATE_PATHS:
                if time.time() > deadline:
                    break
                try:
                    page.goto(root + path, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    continue
                time.sleep(3)
                try:
                    widget = page.evaluate(MINT_JS, [sitekey, TURNSTILE_API_URL])
                except Exception:
                    widget = None
                if not widget:
                    continue
                _log(log, "widget 已渲染（%s），等 token…" % (path or "/"))
                while time.time() < deadline:
                    try:
                        token = page.evaluate(GET_RESPONSE_JS, widget)
                    except Exception:
                        token = None
                    if token:
                        _log(log, "拿到 token（%d 字符）" % len(token))
                        return (token, "")
                    _click_turnstile(page)
                    time.sleep(0.5)
            return ("", "浏览器没能拿到 Turnstile token（可能被 Cloudflare 拒绝）")
        except Exception as exc:
            return ("", "浏览器铸 token 失败: %s" % str(exc)[:140])
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass


def sitekey_from_page(station, accounts_dir=None, log=None):
    """兜底：从站点 /api/status 拿不到 sitekey 时，去页面里找 0x4 开头的 key。"""
    kind = engine()
    if not kind:
        return ""
    root = _root(station)
    ctx = None
    try:
        ctx = _launch(kind, _profile_dir(station, accounts_dir), True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        page.goto(root + "/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        found = re.findall(r"0x4[A-Za-z0-9_-]{10,}", page.content())
        return found[0] if found else ""
    except Exception:
        return ""
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


# ---- 授权登录（linux.do / GitHub）----------------------------------------
#
# 有些站点（anyrouter / agentrouter）的「每日奖励」是登录动作本身发的，没有签到
# 端点。OAuth 这一跳需要 IdP 的登录态，所以把用户粘贴的 IdP cookie 注入浏览器
# profile，之后每次「退出 → 重新授权」都能无头跑完：
#
#   站点 → connect.linux.do/oauth2/authorize → linux.do/session/sso_provider
#        → 回到站点并下发新会话（奖励在这一步到账）

# 站点上「用 X 登录」按钮的文案
_OAUTH_TEXTS = {
    "linuxdo": ("LINUX DO", "LinuxDO", "LINUX.DO", "Linux DO", "linux.do"),
    "github": ("GitHub", "Github", "github"),
}
# IdP 的 cookie 属于哪个域
_IDP_HOSTS = {"linuxdo": "linux.do", "github": "github.com"}
# IdP 授权页上的「同意」按钮
_CONSENT_TEXTS = ("授权", "允许", "同意", "确认授权", "Authorize", "Allow", "Accept")


def _parse_cookie_header(raw):
    """把 "a=1; b=2" 解析成 [(a,1),(b,2)]。"""
    pairs = []
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def _host_of(url):
    return (urlsplit(url).hostname or "").lower()


def _domain_matches(domain, host):
    domain = (domain or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _seed_idp_cookies(context, provider, raw, log=None):
    """把用户粘贴的 IdP 会话 cookie 写进浏览器上下文。"""
    host = _IDP_HOSTS.get(provider)
    pairs = _parse_cookie_header(raw)
    if not host or not pairs:
        return 0
    cookies = [{"name": n, "value": v, "domain": "." + host, "path": "/",
                "secure": True, "sameSite": "Lax"} for n, v in pairs]
    try:
        context.add_cookies(cookies)
        _log(log, "已注入 %d 个 %s cookie" % (len(pairs), host))
        return len(pairs)
    except Exception as exc:
        _log(log, "注入 %s cookie 失败: %s" % (host, str(exc)[:100]))
        return 0


def _click_text(page, texts, timeout=4000):
    """按文案点一个可见的按钮/链接，返回命中的文案（没点到返回空串）。"""
    for text in texts:
        for sel in ("button:has-text('%s')" % text, "a:has-text('%s')" % text,
                    "text=%s" % text):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=timeout)
                    return text
            except Exception:
                continue
    return ""


def _site_quota(page, root):
    """在站点页面里 fetch /api/user/self：已登录返回 quota，否则 None。"""
    try:
        data = page.evaluate("""async (root) => {
            try {
                const r = await fetch(root + '/api/user/self', {credentials: 'include'});
                return await r.json();
            } catch (e) { return null; }
        }""", root)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    d = data.get("data")
    if not isinstance(d, dict):
        return None
    for key in ("quota", "remain_quota"):
        try:
            if d.get(key) is not None:
                return float(d[key])
        except (TypeError, ValueError):
            continue
    return None


def oauth_relogin(station, accounts_dir=None, log=None, timeout=180):
    """退出后重新走一次授权登录，返回 (ok, message, cookie)。

    成功后把站点的新 session cookie 一并返回，调用方存下来给 HTTP 路径用。
    """
    provider = str(station.get("oauth_provider") or "").strip().lower()
    if provider not in _OAUTH_TEXTS:
        return (False, "没有配置授权登录方式（linux.do / GitHub）", "")
    idp_cookie = str(station.get("idp_cookie") or "").strip()
    if not idp_cookie:
        return (False, "缺少 %s 的会话 cookie：请在站点编辑里粘贴一次"
                       % _IDP_HOSTS.get(provider, provider), "")
    kind = engine()
    if not kind:
        return (False, "未安装 cloakbrowser / playwright，无法授权登录", "")
    root = _root(station)
    if not root:
        return (False, "站点地址为空", "")

    with _BROWSER_LOCK:
        ctx = None
        try:
            _log(log, "%s 授权登录（%s）" % (station.get("name"), provider))
            ctx = _launch(kind, _profile_dir(station, accounts_dir), True)
            # 1) 先「退出」：清掉站点域的 cookie，但保留 IdP 的 —— 奖励是在
            #    「新的一次登录」时发的，带着旧会话再点一次不会触发。
            site_host = _host_of(root)
            keep = [c for c in ctx.cookies()
                    if not _domain_matches(c.get("domain"), site_host)]
            ctx.clear_cookies()
            if keep:
                try:
                    ctx.add_cookies(keep)
                except Exception:
                    pass
            # 2) 注入 IdP 会话
            if not _seed_idp_cookies(ctx, provider, idp_cookie, log):
                return (False, "IdP cookie 注入失败（格式应为 name=value; name2=value2）", "")

            page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
            try:
                page.goto(root + "/", wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                return (False, "打开站点失败: %s" % str(exc)[:120], "")
            time.sleep(3)

            if not _click_text(page, _OAUTH_TEXTS[provider]):
                return (False, "没找到「%s 登录」按钮" % _IDP_HOSTS.get(provider, provider), "")
            _log(log, "已点 %s 登录入口，等授权跳转…" % provider)

            deadline = time.time() + timeout
            consent_done = False
            while time.time() < deadline:
                time.sleep(2)
                if _site_quota(page, root) is not None:
                    cookies = ["%s=%s" % (c["name"], c["value"])
                               for c in ctx.cookies()
                               if _domain_matches(c.get("domain"), site_host)]
                    _log(log, "授权登录成功（%d 个站点 cookie）" % len(cookies))
                    return (True, "授权登录成功", "; ".join(cookies))
                if not consent_done:
                    for p in list(getattr(ctx, "pages", []) or []):
                        try:
                            if _click_text(p, _CONSENT_TEXTS, timeout=2500):
                                consent_done = True
                                _log(log, "已点授权确认")
                                break
                        except Exception:
                            continue
                for p in list(getattr(ctx, "pages", []) or []):
                    _click_turnstile(p)

            if consent_done:
                return (False, "授权页点过了但没回到已登录状态（IdP 会话可能过期）", "")
            return (False, "授权登录超时：多半是 %s 的会话过期了，请重新粘贴 cookie"
                    % _IDP_HOSTS.get(provider, provider), "")
        except Exception as exc:
            return (False, "授权登录失败: %s" % str(exc)[:140], "")
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
