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
import threading
import time
import urllib.error
import urllib.request

# 测活超时（秒）。公益站普遍慢，给足 10 秒。
PROBE_TIMEOUT = 10
# 对话转发连接/读超时（秒）。流式回答可能持续几分钟，读超时给大值。
CHAT_TIMEOUT = 600

_LOCK = threading.Lock()
_STATIONS = []          # 内存态站点清单（含 api_key 明文）
_STATIONS_FILE = None   # accounts/stations.json 路径
_LOG = None             # 日志函数（wb_proxy.log）
# 模型名 -> (站点 id, 真实模型名) 的路由表；勾选/测活后重建
_ROUTE_TABLE = {}


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


def add_station(name, base_url, api_key, enabled=True):
    """新增站点；返回站点 dict。base_url 建议 .../v1 结尾（也兼容根地址）。"""
    st = {
        "id": _new_id(),
        "name": str(name or "").strip() or "未命名站点",
        "base_url": _norm_base_url(base_url),
        "api_key": str(api_key or "").strip(),
        "enabled": bool(enabled),
        "checkin_url": "",
        "models_selected": [],
        "models_all": [],
        "status": _station_default_status(),
    }
    with _LOCK:
        _STATIONS.append(st)
        save()
        _rebuild_routes()
    return st


def update_station(st_id, name=None, base_url=None, api_key=None, enabled=None,
                   checkin_url=None):
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
        if checkin_url is not None:
            new_url = str(checkin_url).strip()
            if new_url != str(st.get("checkin_url") or "").strip():
                st["checkin_url"] = new_url
                st.pop("last_checkin", None)
        # 地址或密钥变了，旧测活结果不再可信
        st["status"] = _station_default_status()
        st["models_all"] = []
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
    h = {"Content-Type": "application/json"}
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
    state, latency, detail, models = _probe_once(snap)
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
            # 新发现的模型不自动勾选；仅当用户从未勾选过时提示在面板上选
        save()
        _rebuild_routes()
        return json.loads(json.dumps(st))


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
                return ("no_models", latency, "HTTP %d 但响应不是 JSON" % resp.status, [])
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                items = []
            models = []
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else None
                if isinstance(mid, str) and mid and mid not in models:
                    models.append(mid)
            if not models:
                return ("no_models", latency, "接口通了但没返回模型", [])
            return ("ok", latency, "", models)
    except urllib.error.HTTPError as exc:
        latency = int((time.time() - t0) * 1000)
        try:
            exc.read(200)
        except Exception:
            pass
        return ("unavailable", latency, "HTTP %d" % exc.code, [])
    except urllib.error.URLError as exc:
        latency = int((time.time() - t0) * 1000)
        reason = getattr(exc, "reason", exc)
        return ("unavailable", latency, "连接失败: %s" % reason, [])
    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        return ("unavailable", latency, str(exc)[:120], [])


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
    """重建 聚合模型名 -> (站点, 真实模型名) 路由表。

    命名规则：已启用站点勾选的模型里，模型 ID 全局唯一时直接用模型 ID；
    有同名冲突的站加后缀 `模型ID@站点名`。完整限定形式 `模型ID@站点名`
    永远可用；裸模型 ID 在唯一时可用。
    """
    global _ROUTE_TABLE
    table = {}
    # 先收集所有已启用站点的勾选模型
    per_station = []
    for st in _STATIONS:
        if not st.get("enabled"):
            continue
        for mid in st.get("models_selected") or []:
            per_station.append((mid, st))
    # 统计同名冲突
    counts = {}
    for mid, _st in per_station:
        counts[mid] = counts.get(mid, 0) + 1
    for mid, st in per_station:
        if counts.get(mid, 0) > 1:
            exposed = "%s@%s" % (mid, st.get("name") or st.get("id"))
        else:
            exposed = mid
        table[exposed] = (st.get("id"), mid)
        # 完整限定名（带 @站点后缀）无论是否冲突都可解析
        qualified = "%s@%s" % (mid, st.get("name") or st.get("id"))
        table[qualified] = (st.get("id"), mid)
    _ROUTE_TABLE = table


def resolve(model):
    """把下游请求的模型名解析成 (站点 id, 真实模型名)；未命中返回 None。"""
    if not model or not isinstance(model, str):
        return None
    with _LOCK:
        hit = _ROUTE_TABLE.get(model.strip())
        if not hit:
            return None
        return hit


def models_entries():
    """构造公益站模型在 /v1/models 里的条目（OpenAI 兼容形状）。"""
    with _LOCK:
        entries = []
        for exposed, (st_id, mid) in sorted(_ROUTE_TABLE.items()):
            st = _find(st_id)
            if not st:
                continue
            meta = None
            for it in st.get("models_all") or []:
                if isinstance(it, str) and it == mid:
                    meta = {}
                    break
                if isinstance(it, dict) and it.get("id") == mid:
                    meta = it
                    break
            desc = (meta or {}).get("description") if isinstance(meta, dict) else None
            entry = {
                "id": exposed,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "station",
                "name": exposed,
                "description": desc or ("公益站 %s 的模型 %s"
                                        % (st.get("name"), mid)),
                "station": {
                    "id": st_id,
                    "name": st.get("name"),
                    "upstream_model": mid,
                },
            }
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
            # 余额展示（New API / One API 兼容探测）
            bal = st.get("balance")
            if isinstance(bal, dict):
                item["balance"] = bal
                if bal.get("error"):
                    note += "｜余额查询失败"
                else:
                    remain = bal.get("remain_quota")
                    if remain is None:
                        remain = bal.get("quota")
                    if remain is not None:
                        try:
                            note += "｜余额 $%.2f" % float(remain)
                        except Exception:
                            pass
            item["note"] = note
            item["checkin_url"] = st.get("checkin_url") or ""
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


def fetch_balance(st_id):
    """探测站点余额（New API / One API 的 /api/user/self）。

    宽容解析 quota / used_quota / remain_quota 字段，写入站点 balance。
    返回 (ok, balance_dict)；失败返回 (False, None)。
    """
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        return (False, None)
    t0 = time.time()
    try:
        headers = {"Accept": "application/json"}
        key = snap.get("api_key") or ""
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(_balance_url(snap), headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
            body = resp.read(512 * 1024).decode("utf-8", "replace")
        data = json.loads(body)
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return (False, None)
        # 不同版本字段各异，能做几个算几个
        balance = {"checked_at": time.time(), "latency_ms": int((time.time() - t0) * 1000)}
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
        with _LOCK:
            st = _find(st_id)
            if st:
                st["balance"] = balance
                save()
        _logmsg("stations   : 余额 %s -> %s" % (snap.get("name"), balance))
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


def checkin(st_id):
    """调用站点配置的签到接口（GET + Bearer 鉴权）。返回 (ok, message)。"""
    with _LOCK:
        st = _find(st_id)
        snap = json.loads(json.dumps(st)) if st else None
    if not snap:
        return (False, "站点不存在")
    url = str(snap.get("checkin_url") or "").strip()
    if not url:
        return (False, "未配置签到地址")
    try:
        headers = {"Accept": "application/json"}
        key = snap.get("api_key") or ""
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
            body = resp.read(256 * 1024).decode("utf-8", "replace")
        msg = body[:200]
        with _LOCK:
            st = _find(st_id)
            if st:
                st["last_checkin"] = {"at": time.time(), "ok": True, "resp": msg}
                save()
        _logmsg("stations   : 签到 %s -> ok" % snap.get("name"))
        return (True, msg)
    except urllib.error.HTTPError as exc:
        with _LOCK:
            st = _find(st_id)
            if st:
                st["last_checkin"] = {"at": time.time(), "ok": False,
                                      "resp": "HTTP %d" % exc.code}
                save()
        return (False, "HTTP %d" % exc.code)
    except Exception as exc:
        with _LOCK:
            st = _find(st_id)
            if st:
                st["last_checkin"] = {"at": time.time(), "ok": False,
                                      "resp": str(exc)[:120]}
                save()
        return (False, str(exc)[:120])


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

