"""公益站模块：余额单位换算、签到结果解析、模型能力字段。

Run with: python tests/_test_stations.py
No network: station HTTP calls are stubbed via unittest.mock.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="stations-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_stations


class FakeResponse(object):
    def __init__(self, payload, status=200):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.payload = payload
        self.status = status

    def read(self, limit=None):
        return self.payload[:limit] if limit else self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def reset():
    """每个用例前重置模块状态到一份干净的站点清单。"""
    wb_stations._STATIONS = []
    wb_stations._ROUTE_TABLE = {}
    wb_stations._COOLING.clear()
    wb_stations._STATIONS_FILE = os.path.join(_startup_dir.name, "stations.json")
    if os.path.exists(wb_stations._STATIONS_FILE):
        os.remove(wb_stations._STATIONS_FILE)


def add(name="mock", base="https://api.example.com/v1", **kw):
    st = wb_stations.add_station(name, base, "sk-test-1234567890", **kw)
    return st["id"]


# ---- 余额：整数配额 -> 金额 -------------------------------------------

class BalanceUnitsTests(unittest.TestCase):
    """New API 的 quota 是整数单位（默认 500000 = $1），不是美元。"""

    def setUp(self):
        reset()

    def test_quota_is_converted_not_shown_raw(self):
        sid = add()
        self_home = {"success": True, "data": {"quota": 1003188200,
                                              "used_quota": 0,
                                              "username": "wdw6734"}}
        status = {"data": {"quota_per_unit": 500000,
                           "display_in_currency": True,
                           "usd_exchange_rate": 7.3}}

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            if url.endswith("/api/status"):
                return FakeResponse(status)
            return FakeResponse(self_home)

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, bal = wb_stations.fetch_balance(sid)
        self.assertTrue(ok)
        # 1003188200 / 500000 = $2006.38；× 7.3 = ¥14646.55（站点页面上的数字）
        self.assertEqual(bal["display"], "¥14646.55")
        self.assertEqual(bal["display_alt"], "$2006.38")
        self.assertAlmostEqual(bal["cny"], 14646.55, places=2)
        self.assertEqual(bal["quota_per_unit"], 500000)

    def test_defaults_when_status_endpoint_missing(self):
        sid = add()
        self_home = {"success": True, "data": {"quota": 500000, "used_quota": 0}}

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/status"):
                raise OSError("no status endpoint")
            return FakeResponse(self_home)

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, bal = wb_stations.fetch_balance(sid)
        self.assertTrue(ok)
        # 默认 500000 quota = $1，且默认按人民币展示
        self.assertEqual(bal["display"], "¥7.30")
        self.assertEqual(bal["usd"], 1.0)

    def test_view_note_uses_money_not_raw_quota(self):
        sid = add()
        st = wb_stations.get_station(sid)
        st["balance"] = {"quota": 1003188200, "display": "¥14646.55"}
        with wb_stations._LOCK:
            wb_stations._find(sid)["balance"] = st["balance"]
        note = wb_stations.view()[0]["note"]
        self.assertIn("¥14646.55", note)
        self.assertNotIn("1003188200", note)


# ---- 签到：HTTP 200 + success:false 不算成功 ---------------------------

class CheckinInterpretTests(unittest.TestCase):

    def test_success_with_reward_is_confirmed(self):
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": True, "message": "",
                        "data": {"quota": 500000}}))
        self.assertTrue(ok)
        self.assertEqual(evidence, "confirmed")
        self.assertIn("500000", msg)

    def test_bare_success_is_not_confirmed(self):
        """空壳 {"success": true} 是「签到状态查询」的响应，不能算签到成功。"""
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": True}))
        self.assertEqual(evidence, "status")
        self.assertNotIn("签到成功", msg)

    def test_status_payload_is_not_confirmed(self):
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": True, "data": {"checked_in": False}}))
        self.assertEqual(evidence, "status")

    def test_success_false_is_failure(self):
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": False, "message": "签到接口维护中"}))
        self.assertFalse(ok)
        self.assertEqual(msg, "签到接口维护中")

    def test_auth_failure_message_is_surfaced(self):
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": False,
                        "message": "无权进行此操作，access token 无效"}))
        self.assertFalse(ok)
        self.assertIn("access token", msg)

    def test_already_checked_in_is_not_a_failure(self):
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"success": False, "message": "今天已经签到过了"}))
        self.assertTrue(ok)
        self.assertEqual(evidence, "already")
        self.assertIn("已签到", msg)

    def test_html_body_means_auth_failed(self):
        ok, msg, evidence = wb_stations._checkin_interpret("<html><body>login</body></html>")
        self.assertFalse(ok)
        self.assertIn("鉴权", msg)

    def test_turnstile_is_reported_as_such(self):
        """幻城网安这类站点：签到强制 Cloudflare Turnstile，要单独识别。"""
        ok, msg, evidence = wb_stations._checkin_interpret(
            json.dumps({"message": "Turnstile token 为空", "success": False}))
        self.assertFalse(ok)
        self.assertEqual(evidence, "turnstile")
        self.assertIn("人机验证", msg)

    def test_checkin_url_variants(self):
        self.assertEqual(wb_stations._checkin_url_variants("https://a/api/user/checkin"),
                         ["https://a/api/user/checkin", "https://a/api/user/check_in"])
        self.assertEqual(wb_stations._checkin_url_variants("https://a/api/user/check_in"),
                         ["https://a/api/user/check_in", "https://a/api/user/checkin"])


class CheckinRequestTests(unittest.TestCase):

    def setUp(self):
        reset()

    def test_checkin_reports_station_verdict(self):
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")

        def fake_urlopen(req, timeout=None):
            return FakeResponse({"success": False, "message": "今日已签到"})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertTrue(ok)
        self.assertIn("已签到", msg)
        self.assertTrue(wb_stations.get_station(sid)["last_checkin"]["ok"])

    def test_bare_success_get_falls_back_to_post(self):
        """GET 只回空壳 success（查状态）时，应改用 POST 真正签到。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="GET")
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_method())
            if req.get_method() == "POST":
                return FakeResponse({"success": True, "message": "",
                                     "data": {"checkin_quota": 500000}})
            return FakeResponse({"success": True})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertTrue(ok)
        # 签到动作最后落在 POST 上
        self.assertEqual(seen[-1], "POST")
        self.assertIn("500000", msg)

    def test_nested_checked_in_today_means_already(self):
        """真实站点把状态嵌在 data.stats 里，必须能认出来并跳过签到动作。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")
        payload = {"data": {"enabled": True, "max_quota": 10000000,
                            "min_quota": 1,
                            "stats": {"checked_in_today": True,
                                      "checkin_count": 4,
                                      "records": [{"checkin_date":
                                                   time.strftime("%Y-%m-%d"),
                                                   "quota_awarded": 1234567}]}}}
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_method())
            return FakeResponse(payload)

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertTrue(ok)
        self.assertIn("已签到", msg)
        self.assertIn("4", msg)
        # 已经签过就只查状态，不再发签到动作
        self.assertEqual(seen, ["GET"])

    def test_nested_not_checked_in_triggers_action(self):
        """状态显示今天没签时，才发签到动作，并在动作后复核。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")
        state = {"checked": False}

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                state["checked"] = True
                return FakeResponse({"success": True, "data": {"checkin_quota": 500000}})
            return FakeResponse({"data": {"stats": {
                "checked_in_today": state["checked"], "checkin_count": 5}}})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertTrue(ok)
        self.assertIn("500000", msg)

    def test_bare_success_everywhere_is_not_reported_as_success(self):
        """所有方法都只回空壳 success 时，必须如实报未确认，不能谎报成功。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="GET")

        def fake_urlopen(req, timeout=None):
            return FakeResponse({"success": True})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertFalse(ok)
        self.assertIn("没有执行签到", msg)
        self.assertFalse(wb_stations.get_station(sid)["last_checkin"]["ok"])

    def test_turnstile_station_is_flagged_and_stops_retrying(self):
        """站点要求人机验证时：只发一次动作就收工，并把站点标记出来。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")
        posts = []

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                posts.append(req.full_url)
                return FakeResponse({"message": "Turnstile token 为空",
                                     "success": False})
            return FakeResponse({"data": {"stats": {"checked_in_today": False}}})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertFalse(ok)
        self.assertIn("人机验证", msg)
        self.assertEqual(len(posts), 1)          # 不在别的方法/地址上白试
        self.assertTrue(wb_stations.get_station(sid)["needs_turnstile"])

    def test_turnstile_token_is_appended_to_the_action_url(self):
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append((req.get_method(), req.full_url))
            if req.get_method() == "POST":
                return FakeResponse({"message": "签到成功", "success": True,
                                     "data": {"checkin_date": "2026-10-18",
                                              "quota_awarded": 2246295}})
            return FakeResponse({"data": {"stats": {"checked_in_today": False}}})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid, turnstile="1.0_abc")
        self.assertTrue(ok)
        post_urls = [u for m, u in seen if m == "POST"]
        self.assertEqual(post_urls, ["https://api.example.com/api/user/checkin?turnstile=1.0_abc"])
        self.assertIn("2246295", msg)
        self.assertFalse(wb_stations.get_station(sid)["needs_turnstile"])

    def test_checkin_failure_is_recorded(self):
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")

        def fake_urlopen(req, timeout=None):
            return FakeResponse({"success": False, "message": "服务异常"})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin(sid)
        self.assertFalse(ok)
        rec = wb_stations.get_station(sid)["last_checkin"]
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["message"], "服务异常")


# ---- 站点 id ---------------------------------------------------------

class StationIdTests(unittest.TestCase):
    """id 只用毫秒时间戳会在同一毫秒内撞车（Linux 上批量添加必现）。"""

    def setUp(self):
        reset()

    def test_ids_are_unique_within_one_millisecond(self):
        ids = [wb_stations._new_id() for _ in range(500)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_two_stations_added_back_to_back_get_distinct_ids(self):
        a = wb_stations.add_station("A", "https://a.example.com/v1", "k1")
        b = wb_stations.add_station("B", "https://b.example.com/v1", "k2")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(wb_stations.view()), 2)
        # 各自勾的模型必须落在各自的站点上，不能互相覆盖
        wb_stations.set_selected_models(a["id"], ["m-a"])
        wb_stations.set_selected_models(b["id"], ["m-b"])
        self.assertEqual(wb_stations.get_station(a["id"])["models_selected"], ["m-a"])
        self.assertEqual(wb_stations.get_station(b["id"])["models_selected"], ["m-b"])


# ---- 浏览器签到（可选依赖） -------------------------------------------

class BrowserCheckinTests(unittest.TestCase):
    """wb_browser 在没有 cloakbrowser/playwright 的镜像里必须「惰性可用」。"""

    def test_available_and_engine_do_not_raise(self):
        import wb_browser
        self.assertIn(wb_browser.available(), (True, False))
        self.assertIn(wb_browser.engine(), ("cloakbrowser", "playwright", ""))

    def test_mint_without_sitekey_fails_cleanly(self):
        import wb_browser
        token, why = wb_browser.mint_turnstile({"base_url": "https://a.example.com"},
                                              "")
        self.assertEqual(token, "")
        self.assertIn("sitekey", why)

    def test_root_strips_the_api_suffix(self):
        import wb_browser
        st = {"base_url": "https://api.example.com/v1"}
        self.assertEqual(wb_browser._root(st), "https://api.example.com")

    def test_checkin_without_browser_falls_back_to_hint(self):
        """镜像里没有浏览器时，签到要给出可读的手动提示，而不是崩掉。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakeResponse({"message": "Turnstile token 为空",
                                     "success": False})
            return FakeResponse({"data": {"stats": {"checked_in_today": False}}})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            with mock.patch.object(wb_stations, "_browser_checkin",
                                   lambda snap, urls, styles: None):
                ok, msg, raw = wb_stations.checkin(sid)
        self.assertFalse(ok)
        self.assertIn("人机验证", msg)


# ---- 打码服务 / 浏览器 token -----------------------------------------

class SolverTests(unittest.TestCase):
    """wb_solver 只在配置了 key 时生效，未配置必须完全惰性。"""

    def setUp(self):
        reset()
        os.environ.pop("WB_CAPSOLVER_KEY", None)

    def tearDown(self):
        os.environ.pop("WB_CAPSOLVER_KEY", None)

    def test_not_configured_without_key(self):
        import wb_solver
        self.assertFalse(wb_solver.configured())

    def test_fetch_sitekey_from_status(self):
        import wb_solver

        def fake_urlopen(req, timeout=None):
            return FakeResponse({"data": {"turnstile_check": True,
                                          "turnstile_site_key": "0x4AAAAAAFIovBqwE9xMkrm_"}})

        with mock.patch.object(wb_solver.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(wb_solver.fetch_sitekey("https://api.example.com/v1"),
                             "0x4AAAAAAFIovBqwE9xMkrm_")

    def test_fetch_sitekey_returns_empty_when_absent(self):
        import wb_solver

        def fake_urlopen(req, timeout=None):
            return FakeResponse({"data": {"turnstile_check": False}})

        with mock.patch.object(wb_solver.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(wb_solver.fetch_sitekey("https://api.example.com"), "")

    def test_checkin_with_token_posts_the_token(self):
        """页面回传的 token 必须拼到签到地址上，且结果写进 last_checkin。"""
        sid = add()
        wb_stations.update_station(sid, checkin_url="https://api.example.com/api/user/checkin",
                                   checkin_method="POST")
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return FakeResponse({"data": {"checkin_date": "2026-10-18",
                                          "quota_awarded": 2246295},
                                 "message": "签到成功", "success": True})

        with mock.patch.object(wb_stations.urllib.request, "urlopen", fake_urlopen):
            ok, msg, raw = wb_stations.checkin_with_token(sid, "1.0_page_token")
        self.assertTrue(ok)
        self.assertEqual(seen[0], "https://api.example.com/api/user/checkin?turnstile=1.0_page_token")
        rec = wb_stations.get_station(sid)["last_checkin"]
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["phase"], "page-token")
        self.assertIn("2246295", msg)


# ---- 模型能力字段 -----------------------------------------------------

class ModelCapabilityTests(unittest.TestCase):

    def setUp(self):
        reset()

    def test_upstream_reported_limits_win(self):
        sid = add()
        wb_stations.set_selected_models(sid, ["claude-sonnet-4.5"])
        with wb_stations._LOCK:
            st = wb_stations._find(sid)
            st["models_all"] = ["claude-sonnet-4.5"]
            st["models_meta"] = {"claude-sonnet-4.5": {
                "context_length": 200000, "max_output_tokens": 64000}}
        entry = wb_stations.models_entries()[0]
        self.assertEqual(entry["context_length"], 200000)
        self.assertEqual(entry["max_output_tokens"], 64000)
        self.assertEqual(entry["max_completion_tokens"], 64000)
        self.assertEqual(entry["capability_source"], "upstream")

    def test_reasoning_family_gets_efforts(self):
        sid = add()
        wb_stations.set_selected_models(sid, ["gpt-5.2"])
        with wb_stations._LOCK:
            wb_stations._find(sid)["models_all"] = ["gpt-5.2"]
        entry = wb_stations.models_entries()[0]
        self.assertTrue(entry["supports_reasoning"])
        self.assertIn("medium", entry["reasoning_efforts"])
        self.assertFalse(entry["reasoning_can_disable"])

    def test_plain_model_is_not_marked_reasoning(self):
        sid = add()
        wb_stations.set_selected_models(sid, ["some-unknown-model-xyz"])
        with wb_stations._LOCK:
            wb_stations._find(sid)["models_all"] = ["some-unknown-model-xyz"]
        entry = wb_stations.models_entries()[0]
        self.assertFalse(entry["supports_reasoning"])
        self.assertTrue(entry["supports_tool_calls"])
        # 未知模型也要给出一个上下文值，不能缺字段
        self.assertGreater(entry["context_length"], 0)

    def test_shared_model_lists_all_upstreams(self):
        a = add(name="站A", base="https://a.example.com/v1")
        b = add(name="站B", base="https://b.example.com/v1")
        for sid in (a, b):
            wb_stations.set_selected_models(sid, ["gpt-5.2"])
        entry = wb_stations.models_entries()[0]
        self.assertEqual(entry["id"], "gpt-5.2")
        self.assertEqual(entry["station"]["providers"], 2)
        self.assertEqual(len(entry["station"]["upstreams"]), 2)
        # @站点 形式不进清单
        self.assertEqual(len(wb_stations.models_entries()), 1)
        self.assertTrue(wb_stations.resolve("gpt-5.2@站A"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
