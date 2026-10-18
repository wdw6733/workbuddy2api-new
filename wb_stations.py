#!/usr/bin/env python3
"""公益站（第三方 OpenAI 兼容站点）聚合模块.

管理一组 OpenAI 兼容的第三方站点（"公益站"），提供：
  - 站点增删改查与持久化（accounts/stations.json）
  - 测活：GET {base}/v1/models，记录状态 / 响应时间 / 站点模型清单
  - 模型发现与勾选：按站点勾选要暴露给下游客户端的模型
  - 统一 /v1/models 聚合：勾选的模型以唯一名暴露（跨站同名自动加 @站点后缀）
  - 请求路由：resolve(模型名) -> (站点, 真实模型名)，供 wb_proxy 转发

只用标准库。线程安全：一把模块级锁保护内存状态与落盘。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import http.cookiejar

try:  # 模型上下文/输出上限的四级查找（上游值 > 内建表 > model.json > models.dev）
    import wb_modelsdev
except Exception:  # pragma: no cover - 单文件运行时降级
    wb_modelsdev = None

# 浏览器 UA：公益站普遍套了 Cloudflare/WAF，Python 默认 UA（Python-urllib）
# 会被直接 403，全部请求统一带浏览器 UA。
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 测活超时（秒）。公益站普遍慢，给足 10 秒。
PROBE_TIMEOUT = 10
# 对话转发连接/读超时（秒）。流式回答可能持续几分钟，读超时给大值。
CHAT_TIMEOUT = 600

_LOCK = threading.Lock()
_STATIONS = []          # 内存态站点清单（含 api_key 明文）
_STATIONS_FILE = None   # accounts/stations.json 路径
_LOG = None             # 日志函数（wb_proxy.log）
# 暴露模型名 -> [(站点 id, 真实模型名), ...] 候选列表；勾选/测活后重建。
# 同名模型自动合并：裸模型 ID 的候选覆盖所有提供它的启用站点，故障转移按此切换。
_ROUTE_TABLE = {}
# 失败冷却：站点 id -> 最近失败时间戳。冷却期内该站不参与路由。
_COOLING = {}
_COOLDOWN_SECONDS = int(os.environ.get("WB_STATIONS_COOLDOWN", "60"))


def init(accounts_dir, log=None):
    """启动时挂载：确定存储路径、载入站点、后台测活已启用的站点。"""
    global _STATIONS_FILE, _LOG
    _STATIONS_FILE = os.path.join(accounts_dir, "stations.json")
    _LOG = log
    load()
    # 给面板一个立刻可用的路由表（哪怕还没测活过）
    _rebuild_routes()
    probe_all_async()
    fetch_balance_all_async()
    start_daily_checkin()
    if _LOG:
        _LOG("stations   : %d 个公益站已载入 (%s)" % (len(_STATIONS), _STATIONS_FILE))


def _logmsg(msg):
    if _LOG:
        try:
            _LOG(msg)
        except Exception:
            pass


def load():
    global _STATIONS
    if not _STATIONS_FILE:
        return
    try:
        with open(_STATIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            _STATIONS = [s for s in data if isinstance(s, dict)]
    except FileNotFoundError:
        _STATIONS = []
    except Exception as exc:
        _logmsg("stations   : 载入失败 %s（按空清单启动）" % exc)
        _STATIONS = []


def save():
    if not _STATIONS_FILE:
        return
    os.makedirs(os.path.dirname(_STATIONS_FILE), exist_ok=True)
    tmp = _STATIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_STATIONS, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _STATIONS_FILE)


def _norm_base_url(url):
    url = str(url or "").strip().rstrip("/")
    return url


def _new_id():
    return "st%08x" % (int(time.time() * 1000) & 0xFFFFFFFF)


def _station_default_status():
    return {"state": "unknown", "latency_ms": None, "detail": "", "checked_at": None}


def add_station(name, base_url, api_key, enabled=True, priority=0, checkin_url="",
                login_username="", login_password=""):
    """新增站点；返回站点 dict。base_url 建议 .../v1 结尾（也兼容根地址）。"""
    try:
        priority = int(priority)
    except Exception:
        priority = 0
    st = {
        "id": _new_id(),
        "name": str(name or "").strip() or "未命名站点",
        "base_url": _norm_base_url(base_url),
        "api_key": str(api_key or "").strip(),
        "enabled": bool(enabled),
        "checkin_url": str(checkin_url or "").strip(),
        "checkin_method": "POST",
        "checkin_token": "",
        "checkin_user_id": "",
        "login_username": str(login_username or "").strip(),
        "login_password": str(login_password or "").strip(),
        "session_cookie": "",
        "priority": priority,
        "models_selected": [],
        "models_all": [],
        "models_meta": {},
        "status": _station_default_status(),
    }
    with _LOCK:
        _STATIONS.append(st)
        save()
        _rebuild_routes()
    return st


def update_station(st_id, name=None, base_url=None, api_key=None, enabled=None,
                   checkin_url=None, priority=None, checkin_method=None,
                   checkin_token=None, checkin_user_id=None,
                   login_username=None, login_password=None, session_cookie=None):
    """更新站点；api_key 传 None 或空串表示保留原值。返回更新后的站点或 None。"""
    with _LOCK:
        st = _find(st_id)
        if not st:
            return None
        if name is not None and str(name).strip():
            st["name"] = str(name).strip()
        if base_url is not None and str(base_url).strip():
            st["base_url"] = _norm_base_url(base_url)
        if api_key is not None and str(api_key).strip():
            st["api_key"] = str(api_key).strip()
        if enabled is not None:
            st["enabled"] = bool(enabled)
        if priority is not None:
            try:
                st["priority"] = int(priority)
            except Exception:
                st["priority"] = 0
        if checkin_url is not None:
            new_url = str(checkin_url).strip()
            if new_url != str(st.get("checkin_url") or "").strip():
                st["checkin_url"] = new_url
                st.pop("last_checkin", None)
        if checkin_method is not None:
            m = str(checkin_method).strip().upper()
            st["checkin_method"] = m if m in ("GET", "POST") else "GET"
        if checkin_token is not None and str(checkin_token).strip():
            st["checkin_token"] = str(checkin_token).strip()
        if checkin_user_id is not None:
            st["checkin_user_id"] = str(checkin_user_id).strip()
        if login_username is not None:
            st["login_username"] = str(login_username).strip()
        if login_password is not None and str(login_password).strip():
            st["login_password"] = str(login_password).strip()
        if session_cookie is not None and str(session_cookie).strip():
            st["session_cookie"] = str(session_cookie).strip()
        # 地址或密钥变了，旧测活结果不再可信
        st["status"] = _station_default_status()
        st["models_all"] = []
        st["models_meta"] = {}
        save()
        _rebuild_routes()
        return st


def delete_station(st_id):
    with _LOCK:
        st = _find(st_id)
        if not st:
            return False
        _STATIONS.remove(st)
        save()
        _rebuild_routes()
        return True


def set_selected_models(st_id, models):
    if not isinstance(models, list):
        models = []
    cleaned = []
    for m in models:
        if isinstance(m, str) and m.strip() and m.strip() not in cleaned:
            cleaned.append(m.strip())
    with _LOCK:
        st = _find(st_id)
        if not st:
            return None
        st["models_selected"] = cleaned
        save()
        _rebuild_routes()
        return st


def _find(st_id):
    for st in _STATIONS:
        if st.get("id") == st_id:
            return st
    return None


def get_station(st_id):
    with _LOCK:
        st = _find(st_id)
        return json.loads(json.dumps(st)) if st else None


# ---- 测活 ------------------------------------------------------------

def _models_url(st):
    base = st.get("base_url") or ""
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def _chat_url(st):
    base = st.get("base_url") or ""
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _auth_headers(st):
    h = {"Content-Type": "application/json", "User-Agent": _BROWSER_UA}
    key = st.get("api_key") or ""
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def probe(st_id):
    """测活一个站点：更新状态 / 延迟 / 站点模型清单。返回站点 dict 或 None。"""
    with _LOCK:
        st = _find(st_id)
        if not st:
            return None
        snap = json.loads(json.dumps(st))
    state, latency, detail, models, meta = _probe_once(snap)
    with _LOCK:
        st = _find(st_id)
        if not st:
            return None
        st["status"] = {
            "state": state,
            "latency_ms": latency,
            "detail": detail,
            "checked_at": time.time(),
        }
        if state == "ok":
            st["models_all"] = models
            # 站点自己在 /v1/models 里给出的能力字段（上下文/输出/思考）留档，
            # 供 /v1/models 聚合时优先采用；站点不报就由内建知识表兜底。
            st["models_meta"] = meta
            # 测活通过说明站点已恢复，清除它的失败冷却
            clear_cooling(st_id)
            # 新发现的模型不自动勾选；仅当用户从未勾选过时提示在面板上选
        save()
        _rebuild_routes()
        return json.loads(json.dumps(st))


def _int_or_zero(value):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= 10 ** 9 else 0


def _model_meta_from_item(it):
    """从站点 /v1/models 的单项里抽出能力相关字段。

    公益站的上游五花八门（New API / One API / OpenRouter 兼容层 / vLLM），
    字段命名没有统一标准，这里把见过的写法都收一遍；取不到就留空，
    交给 _model_caps 的知识表兜底。
    """
    if not isinstance(it, dict):
        return {}
    meta = {}
    top = it.get("top_provider") if isinstance(it.get("top_provider"), dict) else {}
    arch = it.get("architecture") if isinstance(it.get("architecture"), dict) else {}
    caps = it.get("capabilities") if isinstance(it.get("capabilities"), dict) else {}
    for field in ("context_length", "context_window", "max_input_tokens",
                  "max_context_length", "max_model_len", "context_size"):
        v = _int_or_zero(it.get(field))
        if v:
            meta["context_length"] = v
            break
    if "context_length" not in meta:
        v = _int_or_zero(top.get("context_length"))
        if v:
            meta["context_length"] = v
    for field in ("max_output_tokens", "max_completion_tokens", "max_tokens",
                  "max_output"):
        v = _int_or_zero(it.get(field))
        if v:
            meta["max_output_tokens"] = v
            break
    if "max_output_tokens" not in meta:
        v = _int_or_zero(top.get("max_completion_tokens"))
        if v:
            meta["max_output_tokens"] = v

    params = it.get("supported_parameters")
    params = [str(p).lower() for p in params] if isinstance(params, list) else []

    vision = it.get("vision")
    if vision is None:
        vision = it.get("supports_vision")
    if vision is None:
        vision = caps.get("vision")
    if vision is None:
        mods = arch.get("input_modalities") or it.get("input_modalities")
        if isinstance(mods, list):
            vision = "image" in [str(x).lower() for x in mods]
    if isinstance(vision, bool):
        meta["vision"] = vision

    tools = it.get("supports_tool_calls")
    if tools is None:
        tools = caps.get("tool_calls")
    if tools is None and params:
        tools = "tools" in params or "tool_choice" in params
    if isinstance(tools, bool):
        meta["tool_calls"] = tools

    thinks = it.get("supports_reasoning")
    if thinks is None:
        thinks = caps.get("reasoning")
    if thinks is None:
        r = it.get("reasoning") if isinstance(it.get("reasoning"), dict) else None
        if r is not None:
            thinks = True
        elif params:
            thinks = any(p in params for p in ("reasoning", "reasoning_effort",
                                               "thinking", "include_reasoning"))
    if isinstance(thinks, bool):
        meta["reasoning"] = thinks

    efforts = it.get("reasoning_efforts")
    if not isinstance(efforts, list):
        r = it.get("reasoning") if isinstance(it.get("reasoning"), dict) else {}
        efforts = r.get("supportedEfforts") or r.get("efforts") or r.get("levels")
    if isinstance(efforts, list) and efforts:
        meta["reasoning_efforts"] = [str(e) for e in efforts if str(e).strip()]
    return meta


def _probe_once(st):
    t0 = time.time()
    try:
        req = urllib.request.Request(
            _models_url(st), headers=_auth_headers(st), method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            body = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
            latency = int((time.time() - t0) * 1000)
            try:
                data = json.loads(body)
            except Exception:
                return ("no_models", latency, "HTTP %d 但响应不是 JSON" % resp.status, [], {})
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                items = []
            models = []
            meta_map = {}
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else None
                if isinstance(mid, str) and mid and mid not in models:
                    models.append(mid)
                    m = _model_meta_from_item(it)
                    if m:
                        meta_map[mid] = m
            if not models:
                return ("no_models", latency, "接口通了但没返回模型", [], {})
            return ("ok", latency, "", models, meta_map)
    except urllib.error.HTTPError as exc:
        latency = int((time.time() - t0) * 1000)
        try:
            exc.read(200)
        except Exception:
            pass
        return ("unavailable", latency, "HTTP %d" % exc.code, [], {})
    except urllib.error.URLError as exc:
        latency = int((time.time() - t0) * 1000)
        reason = getattr(exc, "reason", exc)
        return ("unavailable", latency, "连接失败: %s" % reason, [], {})
    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        return ("unavailable", latency, str(exc)[:120], [], {})


def probe_all_async():
    """后台线程把所有已启用站点测活一遍。"""
    def _run():
        with _LOCK:
            ids = [s["id"] for s in _STATIONS if s.get("enabled")]
        for sid in ids:
            probe(sid)
        _logmsg("stations   : 后台测活完成（%d 个站点）" % len(ids))
    threading.Thread(target=_run, daemon=True,
                     name="wb-stations-probe").start()


def probe_one_async(st_id):
    def _run():
        st = probe(st_id)
        if st:
            _logmsg("stations   : 测活 %s -> %s %sms" % (
                st.get("name"), st.get("status", {}).get("state"),
                st.get("status", {}).get("latency_ms")))
    threading.Thread(target=_run, daemon=True,
                     name="wb-stations-probe-one").start()


# ---- 模型聚合与路由 --------------------------------------------------

def _rebuild_routes():
    """重建路由表：暴露模型名 -> 候选站点列表。

    命名规则：
      - 裸模型 ID：合并所有提供该模型的已启用站点，请求时按「优先级 + 延迟」
        自动选一个，失败自动切换到下一个（故障转移）。
      - `模型ID@站点名`：强制指定某个站点（调试/绕开故障站时用）。
    """
    global _ROUTE_TABLE
    table = {}
    by_model = {}
    for st in _STATIONS:
        if not st.get("enabled"):
            continue
        for mid in st.get("models_selected") or []:
            by_model.setdefault(mid, []).append((st["id"], mid))
    # 裸 ID：合并所有站
    for mid, cands in by_model.items():
        table[mid] = list(cands)
    # @站点名：强制指定
    for st in _STATIONS:
        if not st.get("enabled"):
            continue
        sname = st.get("name") or st.get("id")
        for mid in st.get("models_selected") or []:
            table["%s@%s" % (mid, sname)] = [(st["id"], mid)]
    _ROUTE_TABLE = table


def _station_priority(st_id):
    st = _find(st_id)
    if not st:
        return 999
    try:
        return int(st.get("priority") or 0)
    except Exception:
        return 0


def _station_latency(st_id):
    st = _find(st_id)
    if not st:
        return 999999
    lat = st.get("status", {}).get("latency_ms")
    return lat if lat is not None else 999999


def _station_sort_key(item):
    st_id, _mid = item
    return (_station_priority(st_id), _station_latency(st_id))


def _is_cooling(st_id):
    t = _COOLING.get(st_id)
    if not t:
        return False
    return (time.time() - t) < _COOLDOWN_SECONDS


def mark_failed(st_id):
    """记录站点一次失败，进入冷却期（默认 60s，可用 WB_STATIONS_COOLDOWN 调）。"""
    _COOLING[st_id] = time.time()
    _logmsg("stations   : 站点 %s 标记失败，冷却 %ds" % (st_id, _COOLDOWN_SECONDS))


def clear_cooling(st_id=None):
    """清除冷却。不传 id 清空全部（测活通过时调用）。"""
    if st_id:
        _COOLING.pop(st_id, None)
    else:
        _COOLING.clear()


def resolve(model):
    """把模型名解析成候选站点列表 [(站点 id, 真实模型名), ...]。

    已按「优先级升序 + 最近测活延迟升序」排序，并排除冷却中的站点。
    返回空列表表示未命中（或无可用站点）。
    """
    if not model or not isinstance(model, str):
        return []
    key = model.strip()
    with _LOCK:
        cands = _ROUTE_TABLE.get(key)
        if not cands:
            return []
        alive = [(s, m) for (s, m) in cands if not _is_cooling(s)]
        alive.sort(key=_station_sort_key)
        return alive


def _accounts_dir():
    """accounts 目录（模型知识表缓存 model.json 也放这儿）。"""
    return os.path.dirname(_STATIONS_FILE) if _STATIONS_FILE else "."


# 模型能力的名称推断表。公益站的 /v1/models 大多只回 id，不报上下文/输出/
# 思考档位；这里按模型族给出共识值，站点自己报了就以站点为准。
# 顺序有意义：先匹配到的先用，所以具体族在前、泛化规则在后。
# 每项：(正则, 思考档位, 能否关闭思考, 固定档位)
_REASONING_HINTS = (
    (r"^(gpt-5|gpt-6|o[1-9]($|-|_))", ("minimal", "low", "medium", "high"), False, None),
    (r"^gpt-oss", ("low", "medium", "high"), False, None),
    (r"^claude.*(think|reason)|^claude-(3[.-]?7|[4-9])|^claude.*(sonnet|opus)",
     ("low", "medium", "high"), True, None),
    (r"^deepseek.*(r1|reasoner)", ("low", "medium", "high"), True, None),
    (r"^deepseek-v[3-9]", ("low", "medium", "high"), True, None),
    (r"^(glm-?z1|glm-?[4-9])", ("low", "medium", "high"), True, None),
    (r"^(qwen3|qwq)", ("low", "medium", "high"), True, None),
    (r"^gemini-[2-9]", ("low", "medium", "high"), True, None),
    (r"^grok-[3-9]", ("low", "medium", "high"), True, None),
    (r"^(minimax-m[2-9]|step-[23]|doubao.*(think|pro)|hunyuan.*(t1|think))",
     ("low", "medium", "high"), True, None),
    (r"(think|reason)", ("low", "medium", "high"), True, None),
)

# 视觉能力只认明确的型号标记：误报会让客户端把图片发给不支持读图的上游。
_VISION_HINTS = re.compile(
    r"(vision|multimodal|-vl\b|[-_]vl[-_]|4v\b|omni|pixtral|llava|internvl"
    r"|^gpt-4o|^gpt-5|^gpt-6|^gemini|^claude|^qwen.*vl|^glm-4\.?[0-9]?v)", re.I)


def _model_caps(mid, meta=None):
    """推断一个公益站模型的能力：上下文窗口 / 最大输出 / 思考档位 / 视觉 / 工具。

    优先级：站点 /v1/models 上报的字段 > wb_modelsdev 四级查找（内建表 +
    model.json + models.dev 缓存）> 名称推断。返回值直接铺进 /v1/models 条目，
    面板的「上下文窗口 / 最大输出 / 思考档位」三列就是读这些字段。
    """
    meta = meta if isinstance(meta, dict) else {}
    remote_ctx = _int_or_zero(meta.get("context_length"))
    remote_out = _int_or_zero(meta.get("max_output_tokens"))
    ctx, out, src = remote_ctx or 1000000, remote_out or None, "upstream"
    if wb_modelsdev is not None:
        try:
            ctx, out, info = wb_modelsdev.lookup(
                mid, remote_ctx, remote_out, directory=_accounts_dir())
            src = "upstream" if remote_ctx or remote_out else (
                "knowledge" if (info or {}).get("context") in ("table", "cache") else "default")
        except Exception:
            pass
    caps = {"context_length": ctx, "source": src}
    if out:
        caps["max_output_tokens"] = out

    name = str(mid or "").lower()
    vision = meta.get("vision")
    if not isinstance(vision, bool):
        vision = bool(_VISION_HINTS.search(name))
    tools = meta.get("tool_calls")
    if not isinstance(tools, bool):
        tools = True  # OpenAI 兼容站基本都支持 tool_calls
    thinks = meta.get("reasoning")
    efforts = meta.get("reasoning_efforts") or []
    can_disable = None
    fixed = None
    if not isinstance(thinks, bool):
        thinks = False
        for pattern, hint_efforts, hint_can_disable, hint_fixed in _REASONING_HINTS:
            if re.search(pattern, name):
                thinks = True
                efforts = efforts or list(hint_efforts)
                can_disable = hint_can_disable
                fixed = hint_fixed
                break
    caps.update({
        "vision": bool(vision),
        "tool_calls": bool(tools),
        "reasoning": bool(thinks),
        "reasoning_efforts": list(efforts),
        "reasoning_can_disable": can_disable,
        "reasoning_fixed_effort": fixed,
    })
    return caps


def _apply_caps(entry, caps):
    """把能力字段铺到 /v1/models 条目上（字段名对齐官方模型条目，面板直接读）。"""
    ctx = caps.get("context_length")
    out = caps.get("max_output_tokens")
    vision = bool(caps.get("vision"))
    tools = bool(caps.get("tool_calls"))
    thinks = bool(caps.get("reasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    entry["context_length"] = ctx
    entry["max_input_tokens"] = ctx
    if out:
        entry["max_output_tokens"] = out
        entry["max_completion_tokens"] = out
    entry["capabilities"] = {"vision": vision, "tool_calls": tools, "reasoning": thinks}
    entry["supports_vision"] = vision
    entry["supports_images"] = vision
    entry["supports_tool_calls"] = tools
    entry["supports_reasoning"] = thinks
    entry["vision"] = vision
    entry["multimodal"] = vision
    entry["abilities"] = {"vision": vision, "functionCall": tools,
                          "function_call": tools, "reasoning": thinks}
    entry["input_modalities"] = inputs
    entry["output_modalities"] = ["text"]
    entry["modalities"] = {"input": inputs, "output": ["text"]}
    entry["architecture"] = {"input_modalities": inputs,
                             "output_modalities": ["text"],
                             "modality": "+".join(inputs) + "->text"}
    efforts = caps.get("reasoning_efforts") or []
    if efforts:
        entry["reasoning_efforts"] = efforts
    if caps.get("reasoning_fixed_effort"):
        entry["reasoning_fixed_effort"] = caps["reasoning_fixed_effort"]
    if caps.get("reasoning_can_disable") is not None:
        entry["reasoning_can_disable"] = bool(caps["reasoning_can_disable"])
    entry["capability_source"] = caps.get("source") or "default"
    return entry


def models_entries():
    """构造公益站模型在 /v1/models 里的条目（OpenAI 兼容形状）。

    同名模型合并为单一裸 ID 条目（附 upstreams 列出所有提供站）；
    `模型ID@站点名` 作为强制指定保留解析能力，但不在列表里列出，
    避免模型选择器刷出一堆重复项。

    能力字段（上下文窗口 / 最大输出 / 思考档位 / 视觉 / 工具）优先取站点
    /v1/models 自报的值，站点没报就按模型族知识表推断，字段名与官方模型
    条目一致，面板「网关模型清单」的三列能力直接就能显示。
    """
    with _LOCK:
        entries = []
        for mid in sorted(_ROUTE_TABLE.keys()):
            if "@" in mid:
                continue  # @站点 形式不列进模型清单
            cands = _ROUTE_TABLE[mid]
            upstreams = []
            desc = None
            meta = None
            for st_id, real_mid in cands:
                st = _find(st_id)
                if not st:
                    continue
                m = (st.get("models_meta") or {}).get(real_mid)
                if isinstance(m, dict) and m:
                    meta = m  # 第一个报了能力的站点优先
                if desc is None:
                    for it in st.get("models_all") or []:
                        if isinstance(it, dict) and it.get("id") == real_mid:
                            desc = it.get("description")
                            break
                upstreams.append({
                    "id": st_id,
                    "name": st.get("name"),
                    "upstream_model": real_mid,
                    "priority": _station_priority(st_id),
                    "state": (st.get("status") or {}).get("state"),
                    "latency_ms": (st.get("status") or {}).get("latency_ms"),
                })
            n = len(upstreams)
            first = upstreams[0] if upstreams else {}
            entry = {
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "station",
                "name": mid,
                "description": desc or ("公益站模型，由 %d 个站点提供" % n),
                "station": {
                    "id": first.get("id"),
                    "name": first.get("name"),
                    "upstream_model": first.get("upstream_model"),
                    "providers": n,
                    "upstreams": upstreams,
                },
            }
            _apply_caps(entry, _model_caps(mid, meta))
            entries.append(entry)
        return entries


# ---- 面板视图 --------------------------------------------------------

def _mask_key(key):
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "***" + key[-4:]


def view():
    """给面板的站点清单：脱敏 api_key，附说明文案。"""
    with _LOCK:
        out = []
        for st in _STATIONS:
            item = json.loads(json.dumps(st))
            item["api_key_masked"] = _mask_key(st.get("api_key"))
            item.pop("api_key", None)
            item.pop("checkin_token", None)
            status = st.get("status") or {}
            state = status.get("state") or "unknown"
            selected = len(st.get("models_selected") or [])
            total = len(st.get("models_all") or [])
            if state == "ok":
                note = ("已选 %d 个模型 / 站点共 %d 个" % (selected, total)
                        if selected else "未选模型 (站点有 %d 个)" % total)
            elif state == "no_models":
                note = status.get("detail") or "接口通了但没返回模型"
            elif state == "unavailable":
                note = status.get("detail") or "不可用"
            else:
                note = "尚未测活"
            item["note"] = note
            # 余额展示（New API / One API 兼容探测，已按站点配额单位换算）
            bal = st.get("balance")
            if isinstance(bal, dict):
                item["balance"] = bal
                if bal.get("error"):
                    note += "｜余额查询失败"
                elif bal.get("display"):
                    note += "｜余额 " + str(bal["display"])
            item["note"] = note
            item["models_meta"] = st.get("models_meta") or {}
            item["checkin_url"] = st.get("checkin_url") or ""
            item["checkin_method"] = st.get("checkin_method") or "GET"
            item["checkin_token_set"] = bool(str(st.get("checkin_token") or "").strip())
            item["checkin_user_id"] = st.get("checkin_user_id") or ""
            item["login_username"] = st.get("login_username") or ""
            item["login_password_set"] = bool(str(st.get("login_password") or "").strip())
            item.pop("login_password", None)
            item["session_cookie_set"] = bool(str(st.get("session_cookie") or "").strip())
            item.pop("session_cookie", None)
            item["last_checkin"] = st.get("last_checkin")
            out.append(item)
        return out


# ---- 对话转发 --------------------------------------------------------

def open_chat(st_id, real_model, payload):
    """把一次 chat/completions 请求转发到站点，返回 urllib 响应对象。

    payload 是下游请求体（dict）；模型名会被替换为站点真实模型名。
    调用方负责关闭响应、透传流式/非流式内容并记录用量。
    """
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        raise LookupError("station %s not found" % st_id)
    body = dict(payload or {})
    body["model"] = real_model
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _chat_url(snap), data=data, headers=_auth_headers(snap), method="POST")
    return urllib.request.urlopen(req, timeout=CHAT_TIMEOUT)


# ---- 余额查询（New API / One API 兼容） ------------------------------

BALANCE_TIMEOUT = 8


def _root_url(st):
    """从 base_url 推导站点根地址（去掉 /v1、/api 等尾巴）。"""
    base = str(st.get("base_url") or "").rstrip("/")
    for suffix in ("/v1", "/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/")


def _balance_url(st):
    return _root_url(st) + "/api/user/self"


# New API / One API 的余额是整数配额（quota），不是货币：默认 500000 quota
# = $1。直接把 quota 当美元显示会放大 50 万倍（面板曾显示 ¥1003188200，
# 实际 ¥14646.55）。站点的 /api/status 会给出真实换算参数，优先用它。
QUOTA_PER_UNIT_DEFAULT = float(os.environ.get("WB_STATIONS_QUOTA_PER_UNIT", "500000") or 500000)
EXCHANGE_RATE_DEFAULT = float(os.environ.get("WB_STATIONS_USD_CNY", "7.3") or 7.3)


def _fetch_status_units(st):
    """GET {root}/api/status：读站点的配额换算参数。

    New API 返回 quota_per_unit / display_in_currency / usd_exchange_rate
    （不同版本大小写不一），One API 一般只有 quota_per_unit。取不到就用默认值。
    """
    info = {}
    try:
        req = urllib.request.Request(
            _root_url(st) + "/api/status",
            headers={"Accept": "application/json", "User-Agent": _BROWSER_UA},
            method="GET")
        with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
            data = json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
    except Exception:
        return info
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return info
    for key in ("quota_per_unit", "QuotaPerUnit", "quotaPerUnit"):
        try:
            v = float(d.get(key))
        except (TypeError, ValueError):
            continue
        if v > 0:
            info["quota_per_unit"] = v
            break
    for key in ("usd_exchange_rate", "UsdExchangeRate", "usdExchangeRate"):
        try:
            v = float(d.get(key))
        except (TypeError, ValueError):
            continue
        if v > 0:
            info["usd_exchange_rate"] = v
            break
    for key in ("display_in_currency", "DisplayInCurrencyEnabled",
                "displayInCurrency", "display_in_currency_enabled"):
        if isinstance(d.get(key), bool):
            info["display_in_currency"] = d[key]
            break
    return info


def _apply_money(balance, quota_value, qpu, rate, currency):
    """把整数配额换算成金额，写进 balance 的 usd / cny / display 字段。"""
    try:
        quota = float(quota_value)
    except (TypeError, ValueError):
        return balance
    if quota < 0 or qpu <= 0:
        return balance
    usd = quota / qpu
    cny = usd * rate
    balance["usd"] = round(usd, 4)
    balance["cny"] = round(cny, 2)
    if currency == "USD":
        balance["display"] = "$%.2f" % usd
        balance["display_alt"] = "¥%.2f" % cny
    else:
        balance["display"] = "¥%.2f" % cny
        balance["display_alt"] = "$%.2f" % usd
    return balance


_AUTH_WORDS = ("无权", "未登录", "未提供", "无效", "令牌", "token 无效",
               "token无效", "unauthorized", "forbidden", "not logged")


def _auth_failed(status, body):
    """判断一次用户级接口调用是不是「鉴权没通过」（而不是业务失败）。

    New API 鉴权失败经常回 HTTP 200 + {"success": false, "message": "无权进行
    此操作，access token 无效"}，只看状态码会误判成成功。
    """
    if status in (401, 403):
        return True
    text = (body or "")[:600]
    if not text:
        return False
    try:
        data = json.loads(text)
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("success") is True:
        return False
    msg = str(data.get("message") or data.get("msg") or "").lower()
    return any(w in msg for w in _AUTH_WORDS)


def fetch_balance(st_id):
    """探测站点余额（New API / One API 的 /api/user/self）。

    宽容解析 quota / used_quota / remain_quota 字段，并按站点的
    quota_per_unit / usd_exchange_rate 换算成金额写入站点 balance。
    返回 (ok, balance_dict)；失败返回 (False, None)。
    """
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        return (False, None)
    t0 = time.time()
    try:
        body = ""
        styles = _auth_styles(snap)
        for idx, style in enumerate(styles):
            headers = _user_api_headers(snap, style)
            req = urllib.request.Request(_balance_url(snap), headers=headers,
                                         method="GET")
            try:
                with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
                    body = resp.read(512 * 1024).decode("utf-8", "replace")
                    status = getattr(resp, "status", 200)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and idx + 1 < len(styles):
                    continue
                raise
            if _auth_failed(status, body) and idx + 1 < len(styles):
                # 令牌写法不对（New API 老版本不剥 Bearer）→ 换一种再试
                continue
            break
        data = json.loads(body)
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            # 有的站把失败原因放在 message 里（HTTP 200 + success:false）
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or "")[:80]
            raise ValueError(msg or "返回结构里没有 data")
        # 不同版本字段各异，能做几个算几个
        balance = {"checked_at": time.time(),
                   "latency_ms": int((time.time() - t0) * 1000)}
        for f in ("quota", "used_quota", "remain_quota", "request_count"):
            if d.get(f) is not None:
                balance[f] = d[f]
        if "remain_quota" not in balance and "quota" in balance and "used_quota" in balance:
            try:
                balance["remain_quota"] = balance["quota"] - balance["used_quota"]
            except Exception:
                pass
        # 用户名用于展示
        if d.get("username"):
            balance["username"] = d["username"]
        # 配额换算参数：站点 /api/status 优先，取不到用默认 500000 quota = $1
        units = _fetch_status_units(snap)
        qpu = units.get("quota_per_unit") or QUOTA_PER_UNIT_DEFAULT
        rate = units.get("usd_exchange_rate") or EXCHANGE_RATE_DEFAULT
        currency = "CNY" if units.get("display_in_currency", True) else "USD"
        balance.update({"quota_per_unit": qpu, "usd_exchange_rate": rate,
                        "currency": currency})
        remain = balance.get("remain_quota")
        if remain is None:
            remain = balance.get("quota")
        if remain is not None:
            _apply_money(balance, remain, qpu, rate, currency)
        elif d.get("balance") is not None:
            # 少数 fork 直接回金额
            try:
                amount = float(d["balance"])
                balance["display"] = ("¥%.2f" % amount if currency == "CNY"
                                      else "$%.2f" % amount)
            except (TypeError, ValueError):
                pass
        if balance.get("used_quota") is not None:
            try:
                balance["used_usd"] = round(float(balance["used_quota"]) / qpu, 4)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        with _LOCK:
            st = _find(st_id)
            if st:
                st["balance"] = balance
                save()
        _logmsg("stations   : 余额 %s -> %s" % (snap.get("name"), balance.get("display")))
        return (True, balance)
    except Exception as exc:
        with _LOCK:
            st = _find(st_id)
            if st:
                st["balance"] = {"checked_at": time.time(), "error": str(exc)[:120]}
                save()
        return (False, None)


def fetch_balance_all_async():
    def _run():
        with _LOCK:
            ids = [s["id"] for s in _STATIONS if s.get("enabled")]
        ok = 0
        for sid in ids:
            if fetch_balance(sid)[0]:
                ok += 1
        _logmsg("stations   : 余额刷新完成（%d/%d 站点成功）" % (ok, len(ids)))
    threading.Thread(target=_run, daemon=True, name="wb-stations-balance").start()


# ---- 签到（可配置接口 + 每日自动调用） --------------------------------

def set_checkin_url(st_id, checkin_url):
    with _LOCK:
        st = _find(st_id)
        if not st:
            return None
        st["checkin_url"] = str(checkin_url or "").strip()
        st.pop("last_checkin", None)
        save()
        return st


def _auth_styles(st):
    """用户级接口的 Authorization 写法候选（按成功率排序）。

    New API 的 UserAuth 中间件直接把 Authorization 头当 access token 查库，
    老版本 / One API 不剥 "Bearer " 前缀；新版本才剥。所以先发原始令牌，
    失败再发 "Bearer xxx"。只有 API Key（sk-）时才反过来。
    """
    if str(st.get("session_cookie") or "").strip():
        return ["cookie"]
    token = str(st.get("checkin_token") or "").strip()
    if token:
        return ["raw", "bearer"]
    if str(st.get("api_key") or "").strip():
        return ["bearer", "raw"]
    return ["none"]


def _user_api_headers(st, style="auto"):
    """用户级接口（签到/余额）的请求头。

    支持三种鉴权（按优先级）：
      1. session cookie —— OAuth 登录站（AnyRouter / AgentRouter 等）专用，
         登录后浏览器里的 session cookie + New-Api-User 头
      2. 系统访问令牌 —— New API 系的 checkin_token（个人设置生成），
         原始值直接放 Authorization（不加 Bearer）
      3. API Key —— 少数用 sk- 鉴权的站
    style: "auto"/"raw"/"bearer"/"cookie" 控制 Authorization 的写法。
    """
    headers = {"Accept": "application/json", "User-Agent": _BROWSER_UA}
    if style == "auto":
        styles = _auth_styles(st)
        style = styles[0] if styles else "none"
    cookie = str(st.get("session_cookie") or "").strip()
    if cookie and style != "bearer":
        # 用户填了纯 session 值 → 拼成 session=xxx；填了完整 cookie 头 → 原样用
        headers["Cookie"] = cookie if ("=" in cookie and (";" in cookie or cookie.startswith("session="))) else ("session=" + cookie)
    elif style != "cookie":
        token = str(st.get("checkin_token") or "").strip() or str(st.get("api_key") or "").strip()
        if token:
            if style == "raw":
                headers["Authorization"] = token
            elif style == "bearer":
                headers["Authorization"] = "Bearer " + token
    uid = str(st.get("checkin_user_id") or "").strip()
    if uid:
        headers["New-Api-User"] = uid
    return headers


_ALREADY_CHECKED = ("已签到", "已经签到", "已领取", "今日已", "重复签到",
                    "already", "checked in", "already checked")
_FAIL_WORDS = ("未登录", "未提供", "无权", "无权限", "失败", "错误", "非法",
               "token", "unauthorized", "forbidden", "error", "fail")


# 真正「签到入账」才会出现的奖励字段
_CHECKIN_REWARD_KEYS = ("checkin_quota", "quota", "reward", "added_quota",
                        "credit", "points", "checkin_reward", "checkinQuota",
                        "quota_awarded", "awarded_quota", "checkin_quota_awarded")
# 「今天是否已签到」的布尔标记（各家写法不一，且常常嵌在 data.stats 里）
_CHECKIN_TODAY_KEYS = ("checked_in_today", "checkedInToday", "today_checked",
                       "is_checkin_today", "isCheckinToday", "checkin_today",
                       "checked_in", "checkedIn", "is_checkin", "checked")


def _walk_dicts(obj, depth=0, limit=4):
    """递归遍历响应里的所有 dict（深度受限），用于找嵌套的状态字段。"""
    if depth > limit:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            for item in _walk_dicts(value, depth + 1, limit):
                yield item
    elif isinstance(obj, list):
        for value in obj[:5]:
            for item in _walk_dicts(value, depth + 1, limit):
                yield item


def _checkin_stats(data):
    """从签到响应里取出 (今天是否已签, 累计次数, 今日奖励配额)。

    真实站点的状态是嵌套的，例如：
        {"data": {"enabled": true, "max_quota": 10000000,
                  "stats": {"checked_in_today": true, "checkin_count": 4,
                            "records": [{"checkin_date": "2026-10-18",
                                         "quota_awarded": 1234567}]}}}
    所以不能只看 data 顶层，要递归找。
    """
    checked = None
    count = None
    reward = ""
    today = time.strftime("%Y-%m-%d")
    for node in _walk_dicts(data):
        if checked is None:
            for key in _CHECKIN_TODAY_KEYS:
                if isinstance(node.get(key), bool):
                    checked = node[key]
                    break
        if count is None and isinstance(node.get("checkin_count"), int):
            count = node["checkin_count"]
        records = node.get("records")
        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                day = str(rec.get("checkin_date") or rec.get("date") or "")[:10]
                if day != today:
                    continue
                if checked is None:
                    checked = True          # 今天有记录 = 已签到
                for key in _CHECKIN_REWARD_KEYS:
                    if rec.get(key) not in (None, "", 0, "0"):
                        reward = str(rec[key])
                        break
                break
    return checked, count, reward


def _checkin_evidence(data, stats=None, is_action=False):
    """判定响应属于哪一种：

    - ``confirmed`` 真的签到入账（带奖励字段 / 明确的成功文案）
    - ``already``   今天已经签过（状态是好的，但没有新增收益）
    - ``status``    只回了签到状态或空壳 success（**没执行签到**）
    - ``none``      有文案但既不是成功也不是已签到

    is_action=True 表示这次请求本身就是「执行签到」（POST），此时状态里
    checked=True 可以视为刚刚签到成功。
    """
    if not isinstance(data, dict):
        return "none"
    checked, _count, reward = stats if stats is not None else _checkin_stats(data)
    msg = str(data.get("message") or data.get("msg") or "").strip()
    low = msg.lower()
    payload = data.get("data")
    # 1. 明确的「今天已签到」状态最可信
    if checked is True:
        return "confirmed" if is_action else "already"
    # 2. 带奖励字段 = 真的入账
    if reward:
        return "confirmed"
    if isinstance(payload, dict):
        for key in _CHECKIN_REWARD_KEYS:
            if payload.get(key) not in (None, "", 0, "0"):
                return "confirmed"
    # 3. 明确「今天还没签」= 只是状态查询
    if checked is False:
        return "status"
    # 4. 文案判断
    if any(w in msg or w in low for w in _ALREADY_CHECKED):
        return "already"
    if any(w in msg for w in ("签到成功", "签到获得", "签到奖励", "已获得")):
        return "confirmed"
    if isinstance(payload, (int, float)) and payload:
        return "confirmed"
    if msg:
        return "none"
    # 既无文案也无 data —— 空壳 success，当作状态查询
    return "status"


def _checkin_reward(data, stats=None):
    """从响应里取签到奖励配额（取不到返回空串）。"""
    if stats is not None and stats[2]:
        return stats[2]
    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, dict):
        for key in _CHECKIN_REWARD_KEYS:
            v = payload.get(key)
            if v not in (None, "", 0, "0"):
                return str(v)
    if isinstance(payload, (int, float)) and payload:
        return str(payload)
    return ""


def _already_message(stats=None):
    """「今天已签到」的文案：带上站点的累计次数，让人一眼看出是真签过。"""
    count = stats[1] if stats else None
    if isinstance(count, int) and count > 0:
        return "今天已签到（站点累计 %d 次）" % count
    return "今天已签到（无需重复）"


def _checkin_interpret(body, status=200, is_action=False):
    """解析签到响应，返回 (ok, message, evidence)。

    只认 HTTP 2xx 会把两种「假成功」当成签到成功，面板提示成功、站内却没有
    记录，正是之前的问题：
      1. HTTP 200 + {"success": false, "message": "无权进行此操作..."}
      2. HTTP 200 + {"success": true}（空壳，其实是签到状态查询的响应）

    所以除了 success，还要看**有没有签到证据**：带奖励字段、明确的成功文案，
    或者状态里 checked_in_today 为真才算 ``confirmed``；明确「今天还没签」或
    空壳 success 记成 ``status``，由调用方换个方法/地址再试。

    is_action=True 表示这次请求本身是「执行签到」（POST），此时状态里的
    checked_in_today=True 就可以直接视为签到成功。
    """
    text = (body or "").strip()
    if not text:
        return (False, "签到接口返回空内容（可能端点不对）", "status")
    if text[:1] == "<":
        return (False, "接口返回的是网页而不是 JSON：鉴权未通过，"
                       "请填站点「系统访问令牌」或 session cookie", "none")
    low = text[:600].lower()
    try:
        data = json.loads(text)
    except Exception:
        if any(w in text or w in low for w in _ALREADY_CHECKED):
            return (True, _already_message(), "already")
        if any(w in text or w in low for w in _FAIL_WORDS):
            return (False, text[:120], "none")
        return (True, text[:120], "none")
    if not isinstance(data, dict):
        return (True, text[:120], "none")
    ok = data.get("success")
    msg = str(data.get("message") or data.get("msg") or "").strip()
    stats = _checkin_stats(data)
    evidence = _checkin_evidence(data, stats, is_action=is_action)
    if ok is False:
        if evidence in ("already", "confirmed"):
            return (True, _already_message(stats), "already")
        return (False, msg or "站点拒绝了签到请求", "none")
    if ok is True:
        if evidence == "already":
            return (True, _already_message(stats), "already")
        reward = _checkin_reward(data, stats)
        out = msg or ("签到成功" if evidence == "confirmed" else "站点返回成功")
        if reward:
            out += "（配额 +%s）" % reward
        return (True, out, evidence)
    # 没有 success 字段：按文案判断
    if evidence == "already":
        return (True, _already_message(stats), "already")
    if evidence == "confirmed":
        return (True, msg[:120] or "签到成功", "confirmed")
    if any(w in msg or w in msg.lower() for w in _FAIL_WORDS):
        return (False, msg[:120] or "签到失败", "none")
    return (True, msg[:120] or "站点返回成功", evidence)


def _checkin_url_variants(url):
    """签到地址的备选写法：New API 是 /checkin，部分 fork 是 /check_in。"""
    url = str(url or "").strip()
    if not url:
        return []
    out = [url]
    if url.endswith("/checkin"):
        out.append(url[: -len("/checkin")] + "/check_in")
    elif url.endswith("/check_in"):
        out.append(url[: -len("/check_in")] + "/checkin")
    return out


def _checkin_status_probe(snap, urls, styles):
    """GET 站点的签到状态，返回 (checked, stats, raw, url)。

    checked 为 True/False/None（None = 该接口不返回状态字段）。
    用于两件事：签之前先看今天签没签（避免重复签到），签之后再复核一次。
    """
    for try_url in urls:
        for style in styles:
            try:
                req = urllib.request.Request(
                    try_url, headers=_user_api_headers(snap, style), method="GET")
                with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
                    raw = resp.read(256 * 1024).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    continue          # 换鉴权写法
                break                 # 这个地址没有状态查询能力
            except Exception:
                break
            try:
                data = json.loads(raw)
            except Exception:
                continue
            stats = _checkin_stats(data)
            if stats[0] is not None:
                return (stats[0], stats, raw, try_url)
        # 这个地址不带状态字段，换下一个地址写法
    return (None, None, "", "")


def checkin(st_id):
    """调用站点签到接口，返回 (ok, message, raw_body)。

    签到接口在不同 fork 上差异很大：`/checkin` 与 `/check_in`、GET 与 POST，
    而且很多站点的 **GET 只查今天签没签、POST 才真正入账**——这正是「面板报
    签到成功、站内却没有记录」的根源。

    因此按这个顺序走：
      1. 先 GET 查状态：今天已签就直接返回「已签到」，**不发签到动作**，
         避免重复签到刷出一堆记录；
      2. 未签（或查不到状态）才执行签到动作，按「方法 × 地址 × 鉴权写法」
         逐个尝试，只认带签到证据（奖励字段 / checked_in_today / 明确文案）的响应；
      3. 动作做完再复核一次状态，确认真的入账；
      4. 全部试完仍未确认，就如实返回失败并附上原始返回，不再谎报成功。
    """
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        return (False, "站点不存在", "")
    url = str(snap.get("checkin_url") or "").strip()
    if not url:
        return (False, "未配置签到地址", "")
    configured = str(snap.get("checkin_method") or "POST").upper()
    if configured not in ("GET", "POST"):
        configured = "POST"
    # 执行动作时先按配置的方法，再试另一种
    methods = [configured] + [m for m in ("POST", "GET") if m != configured]
    urls = _checkin_url_variants(url)
    styles = _auth_styles(snap)

    last_error = ""
    raw = ""
    fallback = None          # (msg, body, url, method)：成功但没签到证据

    def _record(ok, msg, extra=None):
        with _LOCK:
            st = _find(st_id)
            if st:
                rec = {"at": time.time(), "ok": bool(ok), "message": msg,
                       "resp": (raw or "")[:2000]}
                if extra:
                    rec.update(extra)
                st["last_checkin"] = rec
                save()

    # ---- 1. 先查状态 ----
    checked, stats, status_raw, status_url = _checkin_status_probe(snap, urls, styles)
    if checked is True:
        raw = status_raw
        msg = _already_message(stats)
        _record(True, msg, {"url": status_url, "method": "GET", "phase": "status"})
        _logmsg("stations   : 签到 %s -> 已签到，跳过动作 (%s)" % (snap.get("name"), msg))
        return (True, msg, raw)

    # ---- 2. 执行签到动作 ----
    attempts = 0
    max_attempts = 6
    for method in methods:
        method_done = False
        for try_url in urls:
            if method_done:
                break
            for style in styles:
                if attempts >= max_attempts:
                    break
                attempts += 1
                headers = _user_api_headers(snap, style)
                payload = None
                if method == "POST":
                    headers["Content-Type"] = "application/json"
                    payload = b"{}"
                try:
                    req = urllib.request.Request(try_url, data=payload,
                                                 headers=headers, method=method)
                    with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
                        raw = resp.read(256 * 1024).decode("utf-8", "replace")
                        status = getattr(resp, "status", 200)
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 403):
                        last_error = ("HTTP %d：鉴权未通过，请在站点里填「系统访问令牌」"
                                      "或 session cookie" % exc.code)
                        continue          # 换鉴权写法
                    if exc.code in (404, 405):
                        last_error = "HTTP %d（地址或方法不对）" % exc.code
                        break             # 换地址 / 换方法
                    last_error = "HTTP %d" % exc.code
                    break
                except Exception as exc:
                    last_error = str(exc)[:120]
                    break

                ok, msg, evidence = _checkin_interpret(
                    raw, status, is_action=(method == "POST"))
                if ok and evidence in ("confirmed", "already"):
                    _record(True, msg, {"url": try_url, "method": method,
                                        "auth": style, "phase": "action"})
                    _logmsg("stations   : 签到 %s -> ok (%s) %s %s resp=%s"
                            % (snap.get("name"), msg, method, try_url, raw[:400]))
                    return (True, msg, raw)
                if ok:
                    # 成功但没证据：空壳 success / 只回了状态。
                    # 地址是通的（能返回合法 JSON），问题在方法，换方法即可。
                    if fallback is None:
                        fallback = (msg, raw, try_url, method)
                    _logmsg("stations   : 签到 %s -> 无签到证据（%s %s），换方法重试 resp=%s"
                            % (snap.get("name"), method, try_url, raw[:200]))
                    method_done = True
                    break
                last_error = msg
                if _auth_failed(status, raw):
                    continue              # 换鉴权写法
                break                     # 业务失败，换地址 / 方法

    # ---- 3. 动作后复核状态 ----
    checked2, stats2, status_raw2, status_url2 = _checkin_status_probe(
        snap, urls, styles)
    if checked2 is True:
        raw = status_raw2 or raw
        count = stats2[1] if stats2 else None
        msg = "签到成功" + ("（站点累计 %d 次）" % count if isinstance(count, int) and count else "")
        _record(True, msg, {"url": status_url2, "method": "GET", "phase": "verify"})
        _logmsg("stations   : 签到 %s -> ok（动作后复核已签到）%s"
                % (snap.get("name"), msg))
        return (True, msg, raw)

    if fallback:
        _msg, body, u, m = fallback
        note = ("该接口只返回了签到状态，没有执行签到（%s %s），站点显示今天"
                "尚未签到。请在站点上点一次「签到」并用 F12 → Network 抓出真正"
                "的请求地址与方法，填到「签到地址 / 签到方法」里。"
                "原始返回：%s" % (m, u, body[:400]))
        _record(False, note, {"url": u, "method": m, "unverified": True})
        _logmsg("stations   : 签到 %s -> unverified resp=%s"
                % (snap.get("name"), body[:400]))
        return (False, note, body)

    _record(False, last_error or "签到失败")
    _logmsg("stations   : 签到 %s -> fail (%s)" % (snap.get("name"), last_error))
    return (False, last_error or "签到失败", raw)



def _daily_checkin_loop():
    """每日 08:00 后对所有配置了签到地址的站点执行一次签到。"""
    _CHECKIN_HOUR = int(os.environ.get("WB_STATIONS_CHECKIN_HOUR", "8"))
    done_dates = {}
    while True:
        try:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if now.tm_hour >= _CHECKIN_HOUR and done_dates.get("date") != today:
                done_dates["date"] = today
                with _LOCK:
                    targets = [s["id"] for s in _STATIONS
                               if s.get("enabled") and str(s.get("checkin_url") or "").strip()]
                for sid in targets:
                    checkin(sid)
                if targets:
                    _logmsg("stations   : 每日自动签到完成（%d 个站点）" % len(targets))
        except Exception:
            pass
        time.sleep(1800)  # 每半小时检查一次


def start_daily_checkin():
    threading.Thread(target=_daily_checkin_loop, daemon=True,
                     name="wb-stations-checkin").start()


# ---- 账号登录：自动获取系统访问令牌 ----------------------------------

LOGIN_TIMEOUT = 15


def login_station(st_id):
    """用账号密码登录站点（New API 标准 /api/user/login），自动获取系统访问令牌。

    流程：POST /api/user/login → session cookie → 若响应未带 access_token，
    再 GET /api/user/token 生成。拿到后写入 checkin_token / checkin_user_id，
    之后的余额查询与签到就都自动可用了。
    返回 (ok, message)。
    """
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        return (False, "站点不存在")
    username = str(snap.get("login_username") or "").strip()
    password = str(snap.get("login_password") or "").strip()
    if not username or not password:
        return (False, "请先在站点编辑里填写登录账号和密码")
    root = _root_url(snap)
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    base_headers = {"Accept": "application/json", "User-Agent": _BROWSER_UA}
    try:
        # 1. 登录
        body = json.dumps({"username": username, "password": password},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            root + "/api/user/login", data=body,
            headers=dict(base_headers, **{"Content-Type": "application/json"}),
            method="POST")
        with opener.open(req, timeout=LOGIN_TIMEOUT) as resp:
            data = json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
        if not (isinstance(data, dict) and data.get("success")):
            msg = str((data or {}).get("message") or "未知错误")[:120]
            return (False, "登录失败: " + msg)
        user = data.get("data") or {}
        uid = str(user.get("id") or "").strip()
        token = str(user.get("access_token") or "").strip()
        # 登录成功就拿到的 session cookie 也存下来：令牌生成失败时它是唯一可用的
        # 用户级凭证（OAuth 站 / 部分 fork 只认 cookie）。
        cookie_val = ""
        for ck in cj:
            if ck.name in ("session", "token"):
                cookie_val = ck.value
                break
        # 2. 响应没带令牌时，用 session cookie 调生成接口
        if not token:
            try:
                req2 = urllib.request.Request(root + "/api/user/token",
                                              headers=base_headers, method="GET")
                with opener.open(req2, timeout=LOGIN_TIMEOUT) as resp2:
                    d2 = json.loads(resp2.read(256 * 1024).decode("utf-8", "replace"))
                if isinstance(d2, dict) and d2.get("success"):
                    token = str(d2.get("data") or "").strip()
            except Exception:
                pass
        if not token and not cookie_val:
            return (False, "登录成功，但站点未返回访问令牌（可能需要先在站点个人设置里生成）")
        with _LOCK:
            st = _find(st_id)
            if st:
                if token:
                    st["checkin_token"] = token
                if cookie_val:
                    st["session_cookie"] = cookie_val
                if uid:
                    st["checkin_user_id"] = uid
                save()
        _logmsg("stations   : 登录 %s -> %s%s"
                % (snap.get("name"),
                   "已获取系统访问令牌" if token else "已保存会话 Cookie",
                   ("（用户 %s）" % uid) if uid else ""))
        # 3. 立刻用新凭证查一次余额，验证真的能用
        okb, bal = fetch_balance(st_id)
        kind = "系统访问令牌" if token else "会话 Cookie"
        head = "已获取%s%s" % (kind, ("（用户 %s）" % uid if uid else ""))
        if okb and isinstance(bal, dict) and bal.get("display"):
            return (True, head + "，余额 " + str(bal["display"]) + "，签到已可用")
        return (True, head + "，但余额校验未通过："
                + str((bal or {}).get("error") or "接口未返回数据"))
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            return (False, "登录失败: HTTP 400（站点可能开启了人机验证 Turnstile，"
                           "请在站点里改用「系统访问令牌」或 session cookie）")
        return (False, "登录失败: HTTP %d" % exc.code)
    except Exception as exc:
        return (False, "登录失败: " + str(exc)[:120])

