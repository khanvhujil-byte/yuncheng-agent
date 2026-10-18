"""Exercise real cached responses and weather tools while replacing external I/O."""
import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import trip
from app.models.schemas import Location
from app.services import amap, runner, store
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request, make_weather


@pytest.mark.parametrize("endpoint", ["/api/trip/plan", "/api/trip/plan/async"])
@pytest.mark.parametrize(
    ("scenario", "start_date"),
    [
        pytest.param("geocode-timeout", "2026-11-10", id="city-query-fails"),
        pytest.param("missing-adcode", "2026-11-10", id="city-not-found"),
        pytest.param("weather-timeout", "2026-11-10", id="weather-query-fails"),
        pytest.param("weather-recovers", "2026-11-10", id="weather-retry-recovers"),
        pytest.param("invalid-key", "2026-11-10", id="weather-key-invalid"),
        pytest.param("fresh", "2026-11-10", id="new-date-fresh-weather"),
        pytest.param("fresh", "2026-10-10", id="same-date-still-refreshes"),
        pytest.param("outside-window", "2026-11-10", id="no-forecast-for-trip-date"),
    ],
)
def test_cache_never_returns_previous_weather(monkeypatch, endpoint, scenario, start_date):
    original_request = make_request(travel_days=1, nights=0, must_visit=[])
    request = make_request(travel_days=1, nights=0, must_visit=[], start_date=start_date)
    old_weather = make_weather("2026-10-10").model_dump()
    old_weather["day_weather"] = "旧天气"
    historical_plan = make_plan(
        [make_day(0, "2026-10-10", [make_attraction("西湖")])],
        end_date="2026-10-10", hotel=None, hotel_nights=0,
        weather_info=[old_weather], warnings=["原有景点说明"],
        sources={"attractions": "amap", "weather": "amap"},
    )
    fingerprint = store.fingerprint_of(request)
    assert fingerprint == store.fingerprint_of(original_request)
    store.save_itinerary(fingerprint, original_request, historical_plan)
    weather_calls = []

    def geocode(_city):
        if scenario == "geocode-timeout":
            raise amap.AmapUnreachable("mock city timeout")
        if scenario == "missing-adcode":
            return None, None
        return Location(longitude=120.2, latitude=30.2), "330100"

    def get_weather(adcode):
        weather_calls.append(adcode)
        if scenario == "weather-timeout":
            raise amap.AmapUnreachable("mock forecast timeout")
        if scenario == "weather-recovers" and len(weather_calls) == 1:
            raise amap.AmapUnreachable("mock first forecast timeout")
        if scenario == "invalid-key":
            raise amap.AmapError("AMAP_KEY_INVALID", "mock invalid key")
        if scenario == "outside-window":
            return [make_weather("2026-10-17")]
        weather = make_weather(start_date)
        weather.day_weather = "新天气"
        return [weather]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Cached itinerary must not run a planning graph")

    monkeypatch.setattr(amap, "geocode", geocode)
    monkeypatch.setattr(amap, "get_weather", get_weather)
    monkeypatch.setattr(trip, "trip_graph", type("ForbiddenGraph", (), {"invoke": forbidden})())
    monkeypatch.setattr(runner, "start_job", forbidden)

    client = TestClient(app)
    response = client.post(endpoint, json=request.model_dump())
    assert response.status_code == 200
    body = response.json()
    if endpoint.endswith("async"):
        result = client.get(f"/api/trip/plan/result/{body['thread_id']}")
        assert result.status_code == 200
        body = result.json()

    data = body["data"]
    assert body["cached"] is True
    assert data["start_date"] == data["end_date"] == start_date
    assert data["days"][0]["date"] == start_date
    assert data["days"][0]["attractions"][0]["name"] == "西湖"
    assert data["hotel"] is None
    assert "原有景点说明" in data["warnings"]
    assert "旧天气" not in json.dumps(data["weather_info"], ensure_ascii=False)
    expected_calls = 0 if scenario in ("geocode-timeout", "missing-adcode") else (
        2 if scenario in ("weather-timeout", "weather-recovers") else 1)
    assert len(weather_calls) == expected_calls

    if scenario in ("fresh", "outside-window", "weather-recovers"):
        assert body["success"] is True
        assert data["sources"]["weather"] == "amap"
        weather = data["weather_info"][0]
        assert weather["date"] == start_date
        assert weather["beyond_window"] is (scenario == "outside-window")
        if scenario in ("fresh", "weather-recovers"):
            assert weather["day_weather"] == "新天气"
        else:
            assert weather["day_weather"] == ""
            assert any("窗口" in warning for warning in body["warnings"])
    else:
        assert body["success"] is False
        assert data["weather_info"] == []
        assert data["sources"]["weather"] == "degraded"
        assert any("天气查询失败" in warning for warning in body["warnings"])
        assert any(start_date in warning for warning in body["warnings"])

    # Refresh affects only this response, never the saved historical snapshot.
    with store._connect() as connection:
        row = connection.execute("SELECT plan_json FROM itinerary WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
        assert json.loads(row["plan_json"]) == historical_plan
        run = connection.execute("SELECT * FROM run_log").fetchone()
        assert run["success"] == int(body["success"])
        assert run["cached"] == 1
        assert run["llm_calls"] == run["prompt_tokens"] == run["completion_tokens"] == 0
