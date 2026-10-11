"""运维能力的测试：鉴权、限流、监控指标、任务状态外置。

这些是"玩具"和"能上线"的分界线，所以必须有测试守住它们 ——
尤其是**默认关闭**这个行为：本地开发要能直接跑，生产才显式打开。
"""

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module
from app.api.main import app
from app.api.routes import trip as trip_module
from app.api.security import reset_rate_limit_state
from app.services import runner, store
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request
from tests.test_api import StubGraph, _final_state


@pytest.fixture(autouse=True)
def clean_rate_limit():
    """限流窗口是模块级全局状态，用例之间必须清干净，否则会互相干扰。"""
    reset_rate_limit_state()
    yield
    reset_rate_limit_state()


@pytest.fixture
def http(monkeypatch):
    final = _final_state()
    monkeypatch.setattr(trip_module, "trip_graph", StubGraph(final))
    monkeypatch.setattr(graph_module, "app", StubGraph(final))
    return TestClient(app)


class TestAuth:
    def test_disabled_by_default(self, http, settings, monkeypatch):
        """本地开发与 CI 不该被迫配密钥。"""
        monkeypatch.setattr(settings, "api_key", "")
        assert http.post("/api/trip/plan", json=make_request().model_dump()).status_code == 200

    def test_missing_key_is_401(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "secret-key")
        response = http.post("/api/trip/plan", json=make_request().model_dump())
        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"

    def test_wrong_key_is_401(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "secret-key")
        response = http.post("/api/trip/plan", json=make_request().model_dump(),
                             headers={"X-API-Key": "wrong"})
        assert response.status_code == 401

    def test_correct_key_passes(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "secret-key")
        response = http.post("/api/trip/plan", json=make_request().model_dump(),
                             headers={"X-API-Key": "secret-key"})
        assert response.status_code == 200

    def test_bearer_token_also_works(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "secret-key")
        response = http.post("/api/trip/plan", json=make_request().model_dump(),
                             headers={"Authorization": "Bearer secret-key"})
        assert response.status_code == 200

    def test_health_and_metrics_are_open(self, http, settings, monkeypatch):
        """健康检查与指标必须免鉴权 —— 否则监控系统自己就调不通。"""
        monkeypatch.setattr(settings, "api_key", "secret-key")
        assert http.get("/health").status_code == 200
        assert http.get("/metrics").status_code == 200


class TestRateLimit:
    def test_disabled_by_default(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
        for _ in range(3):
            assert http.post("/api/trip/plan", json=make_request().model_dump()).status_code == 200

    def test_exceeding_limit_is_429_with_retry_after(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 2)

        assert http.post("/api/trip/plan", json=make_request().model_dump()).status_code == 200
        assert http.post("/api/trip/plan", json=make_request().model_dump()).status_code == 200

        response = http.post("/api/trip/plan", json=make_request().model_dump())
        assert response.status_code == 429
        assert response.json()["detail"]["error_code"] == "RATE_LIMITED"
        assert int(response.headers["Retry-After"]) >= 1

    def test_limit_does_not_leak_into_health(self, http, settings, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        http.post("/api/trip/plan", json=make_request().model_dump())
        # 限流挂在 /api/trip 路由上，健康检查不受影响
        assert http.get("/health").status_code == 200


class TestMetrics:
    def test_prometheus_text_format(self, http):
        response = http.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "# TYPE trip_runs_total counter" in body
        assert "trip_cache_hit_ratio" in body

    def test_metrics_reflect_run_log(self, http):
        http.post("/api/trip/plan", json=make_request().model_dump())
        body = http.get("/metrics").text
        assert "trip_runs_total 1" in body
        assert "trip_llm_calls_total 3" in body  # 桩状态里 usage.llm_calls = 3

    def test_metrics_work_on_empty_db(self, http):
        """没有任何运行时也必须能返回（监控不该因为"还没人用过"就报错）。"""
        assert "trip_runs_total 0" in http.get("/metrics").text


class TestJobPersistence:
    def test_job_survives_memory_loss(self):
        """★ 任务状态外置的意义：清空内存缓存后仍能从库里恢复。

        这就是"多副本部署 / 进程重启后 /result 还能用"的保证 ——
        否则用户在 A 副本提交、拉流打到 B 副本就直接找不到任务。
        """
        runner.register_done_job("t-persist", make_request(), {"success": True, "data": {"city": "杭州"}})

        runner._jobs.clear()  # 模拟进程重启

        recovered = runner.get_job("t-persist")
        assert recovered is not None
        assert recovered.status == "done"
        assert recovered.result["data"]["city"] == "杭州"
        assert recovered.elapsed_ms >= 0

    def test_events_are_persisted_incrementally(self):
        job = runner.register_done_job("t-events", make_request(), {"success": True})
        runner._emit(job, {"type": "node", "node": "validate_city", "label": "解析城市"})

        stored = store.load_job("t-events")
        assert stored is not None
        assert stored["events"][0]["node"] == "validate_city"
        assert stored["status"] == "done"

    def test_unknown_job_returns_none(self):
        runner._jobs.clear()
        assert runner.get_job("never-existed") is None


class TestJobStateStore:
    def test_save_and_load_roundtrip(self):
        store.save_job(
            "t-store",
            status="running",
            events=[{"type": "node", "node": "compose_plan"}],
            started_at=1000.0,
        )
        loaded = store.load_job("t-store")
        assert loaded["status"] == "running"
        assert loaded["events"][0]["node"] == "compose_plan"
        assert loaded["result"] is None

    def test_upsert_updates_existing_row(self):
        store.save_job("t-upsert", status="running", events=[])
        store.save_job("t-upsert", status="done", events=[{"type": "done"}],
                       result={"success": True}, finished_at=2000.0)

        loaded = store.load_job("t-upsert")
        assert loaded["status"] == "done"
        assert loaded["result"] == {"success": True}
        assert loaded["finished_at"] == 2000.0

    def test_persist_handles_pydantic_payloads(self):
        """结果里可能混着 Pydantic 对象（复用缓存路径），落盘不能炸。"""
        store.save_job("t-pyd", status="done", events=[],
                       result=make_plan(days=[make_day(0, "2026-10-10", [make_attraction("西湖")])],
                                        hotel=make_hotel()))
        assert store.load_job("t-pyd")["result"]["hotel"]["name"] == "测试酒店"
