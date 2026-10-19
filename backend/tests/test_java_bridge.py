"""跨服务桥接层（`services/java_bridge.py`）的测试。

这一层是**新增的依赖边**：Python 依赖 Java。依赖边最容易出两类问题 ——
一是下游挂了把上游拖死，二是"降级了但没人知道"。所以测试的重点就是这两条。

注意 conftest 里的 `no_network` 是 autouse 的：默认情况下 httpx 调用会直接抛错，
所以"Java 不可用"这条路径**不用特意 mock 就已经被测到了**（返回 None 而不是炸）。
"""

import httpx
import pytest

from app.agents.city import validate_city
from app.api.routes.trip import _initial_state
from app.models.schemas import Location
from app.services import java_bridge
from tests.factories import make_attraction, make_request


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


HOT_PAYLOAD = {
    "stats": {
        "city": [{"key": "杭州", "score": 120}, {"key": "成都", "score": 90}],
        "attraction": [{"key": "西湖", "score": 88}],
        "preference": [{"key": "自然风光", "score": 150}],
    },
    "source": "java-community/tb_hot_stat",
}


class TestFetchHot:
    def test_reads_stats_from_java(self, monkeypatch):
        monkeypatch.setattr(java_bridge.httpx, "get", lambda *a, **k: FakeResponse(HOT_PAYLOAD))
        hot = java_bridge.fetch_hot()
        assert hot["city"][0]["key"] == "杭州"
        assert hot["preference"][0]["key"] == "自然风光"

    def test_java_down_returns_none_without_raising(self):
        """★ conftest 的 no_network 让 httpx.get 直接抛错 —— 这正是"Java 挂了"的场景。
        反哺是加分项，绝不能因此让生成失败。"""
        assert java_bridge.fetch_hot() is None

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(java_bridge.httpx, "get", lambda *a, **k: FakeResponse({}, status_code=500))
        assert java_bridge.fetch_hot() is None

    def test_empty_stats_returns_none(self, monkeypatch):
        """Java 活着但一条热度都没有 —— 应该当"没有数据"处理，而不是给个空 dict 让下游拼空提示。"""
        monkeypatch.setattr(java_bridge.httpx, "get", lambda *a, **k: FakeResponse({"stats": {}}))
        assert java_bridge.fetch_hot() is None

    def test_disabled_switch_short_circuits(self, monkeypatch, settings):
        """关掉开关就完全不发请求（联调时用来隔离"到底哪一侧的问题"）。"""
        monkeypatch.setattr(settings, "community_enabled", False)
        called = {"n": 0}

        def _should_not_be_called(*args, **kwargs):
            called["n"] += 1
            return FakeResponse(HOT_PAYLOAD)

        monkeypatch.setattr(java_bridge.httpx, "get", _should_not_be_called)
        assert java_bridge.fetch_hot() is None
        assert called["n"] == 0


class TestPublish:
    def test_publish_returns_itinerary_id(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse({"success": True, "itineraryId": 42})

        monkeypatch.setattr(java_bridge.httpx, "post", fake_post)
        itinerary_id = java_bridge.publish_itinerary(
            title="杭州两日", city="杭州", travel_days=2,
            start_date="2026-10-20", plan={"city": "杭州", "days": []},
            preferences=["自然风光", "博物馆"], session_token="verified-session")

        assert itinerary_id == 42
        assert captured["url"].endswith("/itineraries")
        assert captured["json"]["travelDays"] == 2          # 字段名按 Java 的驼峰契约
        assert captured["json"]["plan"]["city"] == "杭州"    # 行程原样透传，不做翻译
        # ★ 偏好必须显式传：plan 里没有它，而 Java 要靠它累积"常见偏好"热度
        assert captured["json"]["preferences"] == ["自然风光", "博物馆"]
        assert captured["headers"]["X-Session-Token"] == "verified-session"
        assert "X-User-Id" not in captured["headers"]

    def test_publish_failure_returns_none(self):
        """Java 不可用时返回 None（调用方据此给用户"稍后重试"，而不是抛 500）。"""
        assert java_bridge.publish_itinerary(
            title="x", city="杭州", travel_days=2, start_date="2026-10-20", plan={}) is None

    def test_publish_timeout_is_generous(self, monkeypatch):
        """发布要等 Java 回调 Python 生成摘要，超时必须放宽（否则必然超时）。"""
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen["timeout"] = timeout
            return FakeResponse({"itineraryId": 1})

        monkeypatch.setattr(java_bridge.httpx, "post", fake_post)
        java_bridge.publish_itinerary(title="x", city="杭州", travel_days=1,
                                      start_date="2026-10-20", plan={})
        assert seen["timeout"] >= 30


class TestHotHint:
    def test_renders_sections_and_user_priority_caveat(self):
        hint = java_bridge.format_hot_hint(HOT_PAYLOAD["stats"])
        assert "社区热度参考" in hint
        assert "杭州" in hint and "西湖" in hint and "自然风光" in hint
        # ★ 这半句是这条联动的"护栏"：热度不能盖过用户明确说的偏好
        assert "永远优先" in hint

    def test_empty_input_renders_nothing(self):
        """没有热度就整段不加 —— 不给模型塞空信息（省 token 也少噪声）。"""
        assert java_bridge.format_hot_hint(None) == ""
        assert java_bridge.format_hot_hint({}) == ""
        assert java_bridge.format_hot_hint({"city": []}) == ""


class TestGenerationSurvivesJavaDown:
    """★ 最重要的一条：Java 不可用时，Python 的生成链路必须完全正常。"""

    def test_city_preflight_marks_degraded_and_continues(self, monkeypatch, tmp_path):
        monkeypatch.setattr(java_bridge.httpx, "get",
                            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("Java 没起来")))
        monkeypatch.setattr("app.agents.city.geocode",
                            lambda *a, **k: (Location(longitude=120.2, latitude=30.25), "330100"))
        monkeypatch.setattr("app.agents.city.search_poi", lambda *a, **k: [make_attraction("杭州西湖风景名胜区")])

        result = validate_city(_initial_state(make_request(must_visit=["西湖"])))

        assert result["error_code"] is None                       # 预检照样通过
        assert result["community_hot"] is None                    # 没有热度数据
        assert result["sources"]["community_hot"] == "degraded"    # 但如实标出降级

    def test_hot_data_reaches_state(self, monkeypatch):
        monkeypatch.setattr(java_bridge.httpx, "get", lambda *a, **k: FakeResponse(HOT_PAYLOAD))
        monkeypatch.setattr("app.agents.city.geocode",
                            lambda *a, **k: (Location(longitude=120.2, latitude=30.25), "330100"))
        monkeypatch.setattr("app.agents.city.search_poi", lambda *a, **k: [make_attraction("杭州西湖风景名胜区")])

        result = validate_city(_initial_state(make_request(must_visit=["西湖"])))
        assert result["community_hot"]["city"][0]["key"] == "杭州"
        assert result["sources"]["community_hot"] == "java"


class TestPublishRoute:
    def test_publish_endpoint_forwards_to_java(self, monkeypatch):

        from app.api.main import app

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return FakeResponse({"success": True, "itineraryId": 99})

        monkeypatch.setattr(java_bridge.httpx, "post", fake_post)
        client = _authenticated_client(app)
        body = {"plan": {"city": "杭州", "start_date": "2026-10-20",
                         "days": [{"date": "2026-10-20", "day_index": 0}]},
                "preferences": ["自然风光", "美食"]}
        response = client.post("/api/community/publish", json=body)

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True and payload["itinerary_id"] == 99
        # 前端传的偏好要一路带到 Java（否则社区"常见偏好"永远是空的 —— 真发生过）
        assert captured["json"]["preferences"] == ["自然风光", "美食"]

    def test_publish_without_preferences_still_works(self, monkeypatch):
        """偏好是可选的：老客户端不传也不能失败，只是不累积偏好热度。"""

        from app.api.main import app

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return FakeResponse({"itineraryId": 5})

        monkeypatch.setattr(java_bridge.httpx, "post", fake_post)
        client = _authenticated_client(app)
        response = client.post("/api/community/publish", json={
            "plan": {"city": "杭州", "days": [{"date": "2026-10-20", "day_index": 0}]}})

        assert response.json()["success"] is True
        assert captured["json"]["preferences"] == []

    def test_publish_endpoint_degrades_when_java_is_down(self):
        """Java 挂了返回 200 + success=False（不是 500）—— 与项目既有的口径一致。"""

        from app.api.main import app

        client = _authenticated_client(app)
        body = {"plan": {"city": "杭州", "days": [{"date": "2026-10-20", "day_index": 0}]}}
        response = client.post("/api/community/publish", json=body)

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert "稍后重试" in response.json()["message"]

    def test_publish_rejects_incomplete_plan(self):

        from app.api.main import app

        client = _authenticated_client(app)
        response = client.post("/api/community/publish", json={"plan": {"days": []}})
        assert response.json()["success"] is False
        assert "无法发布" in response.json()["message"]

    def test_hot_endpoint_reports_unavailable_honestly(self):

        from app.api.main import app

        client = _authenticated_client(app)
        payload = client.get("/api/community/hot").json()
        assert payload["available"] is False
        assert "暂时不可用" in payload["message"]  # 不编数据，如实说


def _authenticated_client(app):
    from fastapi.testclient import TestClient
    from app.services import identity
    identity.register('bridge-user','valid-password-123')
    token=identity.login('bridge-user','valid-password-123')['token']
    return TestClient(app,headers={'X-Session-Token':token})
