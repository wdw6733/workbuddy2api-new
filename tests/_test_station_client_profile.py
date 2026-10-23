"""公益站出站身份（客户端伪装）与自动检测。

有些公益站只放行特定客户端：AnyRouter 要求 Codex CLI，另有站要求
Claude Code / VS Code 扩展。它们通常只看 User-Agent 和几个特征头。
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_stations

_startup_dir = tempfile.TemporaryDirectory(prefix="station-client-")


class FakeResponse(object):
    def __init__(self, payload, status=200):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.payload = payload
        self.status = status
        self.headers = {"Content-Encoding": ""}

    def read(self, limit=None):
        return self.payload[:limit] if limit else self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def reset():
    wb_stations._STATIONS = []
    wb_stations._ROUTE_TABLE = {}
    wb_stations._COOLING.clear()
    wb_stations._STATIONS_FILE = os.path.join(_startup_dir.name, "stations.json")
    if os.path.exists(wb_stations._STATIONS_FILE):
        os.remove(wb_stations._STATIONS_FILE)


def add(name="mock", **kw):
    st = wb_stations.add_station(name, "https://api.example.com/v1", "sk-test-123456", **kw)
    return st["id"]


class ClientHeaderTests(unittest.TestCase):
    """_auth_headers 必须按身份换头，且默认行为与改造前完全一致。"""

    def setUp(self):
        reset()

    def test_browser_is_default_and_unchanged(self):
        h = wb_stations._auth_headers({"api_key": "sk-x"})
        self.assertIn("Mozilla", h["User-Agent"])
        self.assertEqual(h["Authorization"], "Bearer sk-x")
        # 这两项是老行为：不压缩（SSE/大响应靠它）与 JSON 内容类型
        self.assertEqual(h["Accept-Encoding"], "identity")
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertNotIn("originator", h)

    def test_codex_profile_looks_like_codex_cli(self):
        h = wb_stations._auth_headers({"api_key": "sk-x", "client_profile": "codex"})
        self.assertTrue(h["User-Agent"].startswith("codex_cli_rs/"))
        self.assertEqual(h["originator"], "codex_cli_rs")
        self.assertEqual(h["OpenAI-Beta"], "responses=experimental")
        self.assertTrue(h["session_id"])
        # 鉴权头不能被身份覆盖掉
        self.assertEqual(h["Authorization"], "Bearer sk-x")

    def test_claude_profile(self):
        h = wb_stations._auth_headers({"api_key": "sk-x", "client_profile": "claude"})
        self.assertTrue(h["User-Agent"].startswith("claude-cli/"))
        self.assertEqual(h["x-app"], "cli")
        self.assertEqual(h["anthropic-version"], "2023-06-01")

    def test_vscode_and_cli_profiles(self):
        vsc = wb_stations._auth_headers({"client_profile": "vscode"})
        self.assertIn("vscode/", vsc["User-Agent"])
        self.assertEqual(vsc["x-app"], "vscode")
        cli = wb_stations._auth_headers({"client_profile": "cli"})
        self.assertTrue(cli["User-Agent"].startswith("CLI/"))

    def test_unknown_profile_falls_back_to_browser(self):
        h = wb_stations._auth_headers({"client_profile": "definitely-not-a-profile"})
        self.assertIn("Mozilla", h["User-Agent"])

    def test_profile_survives_roundtrip(self):
        sid = add("anyrouter", client_profile="codex")
        self.assertEqual(wb_stations.get_station(sid)["client_profile"], "codex")
        # 面板读的 view() 也要带出来，否则下拉框回显不了
        row = [s for s in wb_stations.view() if s["id"] == sid][0]
        self.assertEqual(row["client_profile"], "codex")

    def test_update_normalizes_and_rejects_garbage(self):
        sid = add()
        wb_stations.update_station(sid, client_profile="CODEX")
        self.assertEqual(wb_stations.get_station(sid)["client_profile"], "codex")
        wb_stations.update_station(sid, client_profile="not-real")
        self.assertEqual(wb_stations.get_station(sid)["client_profile"], "browser")

    def test_old_station_without_field_reads_as_browser(self):
        sid = add()
        with wb_stations._LOCK:
            wb_stations._find(sid).pop("client_profile", None)
        row = [s for s in wb_stations.view() if s["id"] == sid][0]
        self.assertEqual(row["client_profile"], "browser")


class DetectClientTests(unittest.TestCase):
    """detect_client_profile：按顺序试身份，第一个通的胜出。"""

    def setUp(self):
        reset()

    def _station(self):
        sid = add("anyrouter")
        wb_stations.update_station(sid, client_profile="browser")
        with wb_stations._LOCK:
            wb_stations._find(sid)["models_selected"] = ["gpt-6.1-sol"]
        wb_stations._rebuild_routes()
        return sid

    def _fake_urlopen(self, accept_profiles, record=None):
        """accept_profiles: 这些身份返回 200，其余返回 401。"""
        def fake(req, timeout=None):
            profile = "browser"
            ua = req.get_header("User-agent") or ""
            if ua.startswith("codex_cli_rs"):
                profile = "codex"
            elif ua.startswith("claude-cli"):
                profile = "claude"
            elif "vscode/" in ua:
                profile = "vscode"
            elif ua.startswith("CLI/"):
                profile = "cli"
            if record is not None:
                record.append(profile)
            if profile in accept_profiles:
                return FakeResponse({"id": "chatcmpl-x", "choices": []})
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         None)
        return fake

    def test_first_working_profile_wins(self):
        sid = self._station()
        seen = []
        with mock.patch.object(wb_stations.urllib.request, "urlopen",
                               self._fake_urlopen({"codex"}, seen)):
            result = wb_stations.detect_client_profile(sid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "codex")
        self.assertEqual(result["model"], "gpt-6.1-sol")
        # 试过 browser（失败）再试 codex（成功），后面的不再试
        self.assertEqual(seen, ["browser", "codex"])

    def test_browser_working_stops_immediately(self):
        sid = self._station()
        seen = []
        with mock.patch.object(wb_stations.urllib.request, "urlopen",
                               self._fake_urlopen({"browser"}, seen)):
            result = wb_stations.detect_client_profile(sid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "browser")
        self.assertEqual(seen, ["browser"])

    def test_all_failing_reports_every_attempt(self):
        sid = self._station()
        with mock.patch.object(wb_stations.urllib.request, "urlopen",
                               self._fake_urlopen(set())):
            result = wb_stations.detect_client_profile(sid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["profile"], "")
        profiles = [t["profile"] for t in result["tried"]]
        self.assertEqual(profiles, ["browser", "codex", "claude", "vscode", "cli"])
        self.assertTrue(all(t["status"] == 401 for t in result["tried"]))

    def test_no_models_returns_clear_error(self):
        sid = add("empty")
        result = wb_stations.detect_client_profile(sid)
        self.assertFalse(result["ok"])
        self.assertIn("模型", result["error"])

    def test_missing_station(self):
        result = wb_stations.detect_client_profile("st-nope")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])


if __name__ == "__main__":
    unittest.main()
