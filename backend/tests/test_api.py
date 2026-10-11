"""HTTP 层（`api/routes/trip.py`）的测试。

这一层测的是"组装"和"状态码口径"，不是 Agent 逻辑 —— 所以图被整体替换成桩。
重点覆盖三件事：
    1. **预检失败**要返回明确 error_code（不是 500）
    2. **部分结果**也要 200（前端先看 data 再看 success）
    3. **缓存命中**要走同一套组装出口（`_cached_response`），且不花 LLM
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module
from app.api.main import app
from app.api.routes import trip as trip_module
from app.models.schemas import Location, WeatherInfo
from app.services import store
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request

HANGZHOU = Location(longitude=120.209903, latitude=30.246566)


class StubGraph:
    """替掉真图的桩：invoke 返回固定 state，stream 依次吐一条 updates 和一条 values。"""

    def __init__(self, final):
        self.final = final

    def invoke(self, state, *args, **kwargs):
        return self.final

    def stream(self, state, stream_mode=None, **kwargs):
        yield "updates", {"validate_city": {"adcode": "330100"}}
        yield "updates", {"patch_budget": {"plan": self.final.get("plan")}}
        yield "values", self.final


def _final_state(*, days=True, degraded=False, error_code=None):
    """构造一份"图跑完"的 state。"""
    plan = make_plan(
        days=[make_day(0, "2026-10-10", [make_attraction("西湖", required=True)])] if days else [],
        budget={"total": 1000} if days else None,
    )
    return {
        "adcode": None if error_code else "330100",
        "error_code": error_code,
        "city_center": HANGZHOU,
        "plan": plan if days else None,
        "weather": [WeatherInfo(date="2026-10-10", day_weather="晴")],
        "hotel": make_hotel(),
        "hotel_nights": 2,
        "nights": 2,
        "constraint_report": None,
        "warnings": ["测试警告"],
        "errors": [],
        "sources": {
            "attractions": "degraded" if degraded else "amap",
            "weather": "amap",
        },
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "llm_calls": 3},
        "tool_calls": [{"tool": "search_attractions"}],
        "retries": 1,
        "end_date": "2026-10-12",
    }


@pytest.fixture
def client(monkeypatch):
    """每个测试都拿到一个把真图换成桩的 TestClient。"""
    def _install(final):
        monkeypatch.setattr(trip_module, "trip_graph", StubGraph(final))
        monkeypatch.setattr(graph_module, "app", StubGraph(final))
        return TestClient(app)

    return _install


def _poll_until_done(http, thread_id: str, timeout: float = 5.0) -> dict:
    """轮询 /result 直到后台任务出结果。

    为什么要轮询：后台任务在工作线程里跑，提交接口是"立即返回"的 ——
    这正是异步接口的设计意图（前端不该阻塞），所以测试也必须尊重这个时序。
    """
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        result = http.get(f"/api/trip/plan/result/{thread_id}").json()
        if result.get("success") is True or "仍在生成中" not in str(result.get("message", "")):
            return result
        time.sleep(0.05)
    raise AssertionError(f"后台任务 {timeout} 秒内没有完成")


class TestPreflightFailure:
    def test_returns_error_code_not_500(self, client):
        http = client(_final_state(error_code="CONSTRAINT_INFEASIBLE"))
        response = http.post("/api/trip/plan", json=make_request().model_dump())
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "CONSTRAINT_INFEASIBLE"
        assert "容量" in body["message"]  # 用错误码表里给人看的那句话
        assert body["data"] is None

    def test_unknown_error_code_falls_back_to_generic_message(self, client):
        http = client(_final_state(error_code="SOMETHING_NEW"))
        body = http.post("/api/trip/plan", json=make_request().model_dump()).json()
        assert body["error_code"] == "SOMETHING_NEW"
        assert "预检" in body["message"]


class TestSuccessAndPartial:
    def test_full_success(self, client):
        http = client(_final_state())
        body = http.post("/api/trip/plan", json=make_request().model_dump()).json()

        assert body["success"] is True
        assert body["message"] == ""
        assert body["cached"] is False
        assert body["data"]["city"] == "杭州"
        assert body["data"]["end_date"] == "2026-10-12"
        assert len(body["data"]["days"]) == 1
        assert body["data"]["hotel"]["name"] == "测试酒店"

    def test_degraded_source_marks_partial_result_but_still_200(self, client):
        """★ 部分结果也要 200：前端先看 data 再看 success，非 2xx 会让 axios 抛异常、
        把已经算出来的行程丢掉。"""
        http = client(_final_state(degraded=True))
        response = http.post("/api/trip/plan", json=make_request().model_dump())
        body = response.json()

        assert response.status_code == 200
        assert body["success"] is False
        assert body["data"] is not None
        assert "attractions" in body["message"]

    def test_empty_plan_does_not_500(self, client):
        """连 days 都没有时，TripPlan 的三个必填字段来自 request，不能靠 plan。"""
        http = client(_final_state(days=False))
        response = http.post("/api/trip/plan", json=make_request().model_dump())
        assert response.status_code == 200
        assert response.json()["data"]["city"] == "杭州"


class TestCachePath:
    def test_cache_hit_is_served_without_touching_the_graph(self, client, monkeypatch):
        request = make_request()
        fingerprint = store.fingerprint_of(request)
        store.save_itinerary(fingerprint, request, make_plan(
            days=[make_day(0, "2026-10-10", [make_attraction("西湖")])],
            hotel=make_hotel(), hotel_nights=2, sources={"attractions": "amap"},
        ))

        # 命中路径仍要重取天气：把高德与工具执行都顶掉（不是省事，是必须不联网）
        monkeypatch.setattr(trip_module.amap, "geocode", lambda *a, **k: (HANGZHOU, "330100"))

        def fake_execute(role, name, args, ctx):
            ctx.weather = [WeatherInfo(date="2026-10-10", day_weather="晴")]
            return "ok"

        monkeypatch.setattr(trip_module, "execute", fake_execute)

        http = client(_final_state())  # 桩装着，但命中路径根本不该调用它
        body = http.post("/api/trip/plan", json=request.model_dump()).json()

        assert body["cached"] is True
        assert body["cache_hit_count"] == 1
        assert body["cache_saved_at"]
        assert "复用" in body["message"]

    def test_second_hit_increments_counter(self, client, monkeypatch):
        request = make_request()
        fingerprint = store.fingerprint_of(request)
        store.save_itinerary(fingerprint, request, make_plan(days=[], hotel=make_hotel()))
        monkeypatch.setattr(trip_module.amap, "geocode", lambda *a, **k: (HANGZHOU, "330100"))
        monkeypatch.setattr(trip_module, "execute", lambda *a, **k: "ok")

        http = client(_final_state())
        http.post("/api/trip/plan", json=request.model_dump())
        body = http.post("/api/trip/plan", json=request.model_dump()).json()
        assert body["cache_hit_count"] == 2


class TestStatsAndJournal:
    def test_stats(self, client):
        http = client(_final_state())
        http.post("/api/trip/plan", json=make_request().model_dump())

        body = http.get("/api/trip/stats").json()
        assert body["total_runs"] == 1
        assert body["cached_runs"] == 0
        assert body["llm_calls_generated"] == 3
        assert body["tokens_generated"] == 150

    def test_journal_of_unknown_thread(self, client):
        http = client(_final_state())
        body = http.get("/api/trip/journal/no-such-thread").json()
        assert body["completed_nodes"] == []
        assert body["can_resume_from"] is None


class TestAsyncAndStream:
    def test_async_result_endpoint(self, client):
        http = client(_final_state())
        started = http.post("/api/trip/plan/async", json=make_request().model_dump()).json()
        thread_id = started["thread_id"]
        assert thread_id

        # 后台任务在工作线程里跑，取结果要等它 —— 直接取会拿到"仍在生成中"
        result = _poll_until_done(http, thread_id)
        assert result["success"] is True
        assert result["data"]["city"] == "杭州"

    def test_async_cache_hit_is_immediately_done(self, client, monkeypatch):
        request = make_request()
        fingerprint = store.fingerprint_of(request)
        store.save_itinerary(fingerprint, request, make_plan(days=[], hotel=make_hotel()))
        monkeypatch.setattr(trip_module.amap, "geocode", lambda *a, **k: (HANGZHOU, "330100"))
        monkeypatch.setattr(trip_module, "execute", lambda *a, **k: "ok")

        http = client(_final_state())
        started = http.post("/api/trip/plan/async", json=request.model_dump()).json()
        assert "命中" in started["message"]

        result = http.get(f"/api/trip/plan/result/{started['thread_id']}").json()
        assert result["cached"] is True

    def test_stream_emits_node_events_and_done(self, client):
        http = client(_final_state())
        thread_id = http.post("/api/trip/plan/async", json=make_request().model_dump()).json()["thread_id"]

        with http.stream("GET", f"/api/trip/plan/stream/{thread_id}") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            raw = "".join(chunk for chunk in response.iter_text())

        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        assert events, "SSE 至少要有一条事件"
        node_events = [e for e in events if e["type"] == "node"]
        assert {e["node"] for e in node_events} == {"validate_city", "patch_budget"}
        assert node_events[0]["label"] == "解析城市"  # label 由后端给，前端不自己映射
        done = [e for e in events if e["type"] == "done"]
        assert done and done[0]["result"]["data"]["city"] == "杭州"

    def test_stream_unknown_thread_reports_error(self, client):
        http = client(_final_state())
        response = http.get("/api/trip/plan/stream/does-not-exist")
        assert '"type": "error"' in response.text or '"type":"error"' in response.text

    def test_resume_requires_known_run(self, client):
        http = client(_final_state())
        body = http.post("/api/trip/plan/resume/unknown-thread").json()
        assert body["success"] is False
        assert "找不到" in body["message"]


class TestInputValidation:
    def test_missing_required_field_is_422(self, client):
        """输入本身不合法才用 4xx（FastAPI/Pydantic 的 422）。"""
        http = client(_final_state())
        response = http.post("/api/trip/plan", json={"city": "杭州"})
        assert response.status_code == 422

    def test_travel_days_out_of_range_is_422(self, client):
        http = client(_final_state())
        payload = make_request().model_dump()
        payload["travel_days"] = 99
        assert http.post("/api/trip/plan", json=payload).status_code == 422

    def test_health(self, client):
        http = client(_final_state())
        assert http.get("/health").json()["status"] == "ok"
