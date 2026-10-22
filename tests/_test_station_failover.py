"""公益站故障转移与候选排序。

用户报障：客户端调 gpt-6.1-sol 报「API Key 无效或者没有权限访问该模型」，
但面板上同名模型有两个站点提供、其中一个测活正常。根因有两处：

1. `_station_open` 只对 429/5xx 换站，401/403/404 直接回错——第一个站的
   Key 失效就把整条链路打死，后面的好站根本没机会。
2. 候选排序只看「优先级 + 延迟」，已知异常（不可用 / Key 失效）的站点
   延迟可能更小，于是每次请求都先撞一遍坏站。
"""
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_stations


class FakeHandler(object):
    """只保留 _station_open 用到的两个钩子。"""

    def __init__(self):
        self.errors = []

    def _error(self, code, message, *_a, **_kw):
        self.errors.append((code, message))

    def _key_id(self):
        return "k-test"


def http_error(code, detail="{}"):
    """构造带可读 body 的 HTTPError（_station_open 会 read() 它）。"""
    import io
    return urllib.error.HTTPError(
        "https://up.example.com/v1/chat/completions", code, "err", {},
        io.BytesIO(detail.encode("utf-8")))


class StationFailoverTests(unittest.TestCase):
    """_station_open 的状态码路由：哪些该换站、哪些该直接回错。"""

    def setUp(self):
        self.handler = FakeHandler()
        self.open = wb_proxy.Handler._station_open.__get__(self.handler)
        self.marked = []
        self._patches = [
            mock.patch.object(wb_proxy, "record_error", lambda *a, **k: None),
            mock.patch.object(wb_proxy, "log", lambda *a, **k: None),
            mock.patch.object(wb_stations, "mark_failed",
                              lambda sid: self.marked.append(sid)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _run(self, outcomes, candidates=None):
        """outcomes: {station_id: 返回值或异常}，按调用顺序消费。"""
        cands = candidates or [("st-a", "gpt-6.1-sol"), ("st-b", "gpt-6.1-sol")]
        calls = []

        def fake_open(st_id, real_model, payload):
            calls.append(st_id)
            out = outcomes[st_id]
            if isinstance(out, Exception):
                raise out
            return out

        with mock.patch.object(wb_stations, "open_chat", fake_open):
            result = self.open({"model": "gpt-6.1-sol"}, cands, "gpt-6.1-sol", 0.0)
        return result, calls

    # ---- 该换站的 ----

    def test_401_falls_through_to_next_station(self):
        """第一站 Key 失效（401）必须切到第二站，而不是直接回 401。"""
        sentinel = object()
        result, calls = self._run({"st-a": http_error(401, '{"error":"无效的令牌"}'),
                                   "st-b": sentinel})
        self.assertEqual(result, (sentinel, "st-b", "gpt-6.1-sol"))
        self.assertEqual(calls, ["st-a", "st-b"])
        self.assertEqual(self.handler.errors, [])
        self.assertIn("st-a", self.marked)

    def test_403_falls_through_to_next_station(self):
        sentinel = object()
        result, calls = self._run({"st-a": http_error(403), "st-b": sentinel})
        self.assertEqual(result[1], "st-b")
        self.assertEqual(calls, ["st-a", "st-b"])

    def test_404_falls_through_to_next_station(self):
        """站点清单过时（该站其实没这个模型）也应换站。"""
        sentinel = object()
        result, _ = self._run({"st-a": http_error(404), "st-b": sentinel})
        self.assertEqual(result[1], "st-b")

    def test_429_and_5xx_still_fail_over(self):
        for code in (429, 500, 502, 503, 504):
            sentinel = object()
            result, _ = self._run({"st-a": http_error(code), "st-b": sentinel})
            self.assertEqual(result[1], "st-b", "HTTP %d 应换站" % code)

    def test_connection_error_falls_through(self):
        sentinel = object()
        result, _ = self._run({"st-a": OSError("connection refused"), "st-b": sentinel})
        self.assertEqual(result[1], "st-b")

    # ---- 不该换站的 ----

    def test_400_does_not_fail_over(self):
        """请求体/参数错，换站也一样错：直接回错，不再打第二站。"""
        result, calls = self._run({"st-a": http_error(400, '{"error":"bad param"}'),
                                   "st-b": object()})
        self.assertEqual(result, (None, None, None))
        self.assertEqual(calls, ["st-a"])  # 第二站没被打
        self.assertEqual(self.handler.errors[0][0], 400)
        self.assertIn("bad param", self.handler.errors[0][1])

    def test_422_does_not_fail_over(self):
        result, calls = self._run({"st-a": http_error(422), "st-b": object()})
        self.assertEqual(calls, ["st-a"])
        self.assertEqual(self.handler.errors[0][0], 422)

    # ---- 全部耗尽 ----

    def test_all_stations_failing_reports_each_attempt(self):
        result, calls = self._run({"st-a": http_error(401, '{"error":"无效的令牌"}'),
                                   "st-b": http_error(403)})
        self.assertEqual(result, (None, None, None))
        self.assertEqual(calls, ["st-a", "st-b"])
        code, message = self.handler.errors[0]
        self.assertEqual(code, 502)
        # 报错要能让用户看出是「站点侧凭据问题」，而不是网关 key 的问题
        self.assertIn("st-a(HTTP 401", message)
        self.assertIn("st-b(HTTP 403", message)
        self.assertIn("无效的令牌", message)


class CandidateOrderTests(unittest.TestCase):
    """候选排序：已知异常的站点要垫底，不能因为延迟小就抢先。"""

    def setUp(self):
        wb_stations._STATIONS = []
        wb_stations._ROUTE_TABLE = {}
        wb_stations._COOLING.clear()

    def _add(self, name, state, latency, priority=0):
        st = wb_stations.add_station(name, "https://%s.example.com/v1" % name, "sk-x")
        with wb_stations._LOCK:
            row = wb_stations._find(st["id"])
            row["status"] = {"state": state, "latency_ms": latency}
            row["priority"] = priority
            row["models_selected"] = ["gpt-6.1-sol"]
        wb_stations._rebuild_routes()
        return st["id"]

    def test_healthy_station_wins_over_faster_broken_one(self):
        broken = self._add("broken", "unavailable", 20, priority=0)
        healthy = self._add("healthy", "ok", 800, priority=5)
        route = wb_stations.resolve("gpt-6.1-sol")
        self.assertEqual([sid for sid, _m in route], [healthy, broken])

    def test_key_invalid_station_is_last(self):
        bad_key = self._add("badkey", "key_invalid", 30, priority=0)
        healthy = self._add("healthy", "ok", 900, priority=9)
        route = wb_stations.resolve("gpt-6.1-sol")
        self.assertEqual([sid for sid, _m in route], [healthy, bad_key])

    def test_unprobed_station_is_not_penalised(self):
        """没测活过的站不该被当成坏站（没证据）。"""
        unprobed = self._add("new", "unknown", None, priority=0)
        healthy = self._add("healthy", "ok", 900, priority=5)
        route = wb_stations.resolve("gpt-6.1-sol")
        self.assertEqual([sid for sid, _m in route], [unprobed, healthy])


if __name__ == "__main__":
    unittest.main()
