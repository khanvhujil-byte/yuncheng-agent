"""Input errors must be rejected before cache lookup or planning side effects."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.services import planning
from app.api.routes import trip
from app.services import runner, store
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request


@pytest.mark.parametrize("endpoint", ["/api/trip/plan", "/api/trip/plan/async"])
@pytest.mark.parametrize("cache_exists", [False, True], ids=["miss", "hit"])
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        pytest.param({"start_date": "不是日期"}, "DATE_RANGE_MISMATCH", id="bad-date"),
        pytest.param({"start_date": "2026-02-30"}, "DATE_RANGE_MISMATCH", id="nonexistent-date"),
        pytest.param({"start_date": "9999-12-31"}, "DATE_RANGE_MISMATCH", id="date-overflow"),
        pytest.param({"end_date": "2026-10-15"}, "DATE_RANGE_MISMATCH", id="date-days-conflict"),
        pytest.param({"nights": 4}, "INVALID_REQUEST", id="too-many-nights"),
        pytest.param({"nights": -1}, "INVALID_REQUEST", id="negative-nights"),
        pytest.param({"must_visit": [f"景点{i}" for i in range(10)]},
                     "CONSTRAINT_INFEASIBLE", id="exceeds-capacity"),
    ],
)
def test_invalid_input_cannot_reach_cache_or_planning(
    monkeypatch, endpoint, cache_exists, overrides, error_code
):
    request = make_request(**overrides)
    fingerprint = store.fingerprint_of(request)
    if cache_exists:
        store.save_itinerary(fingerprint, request, make_plan(
            [make_day(0, "2026-10-10", [make_attraction("西湖")])],
            hotel=make_hotel(),
        ))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Invalid input reached cache, weather refresh, or planning")

    monkeypatch.setattr(store, "find_itinerary", forbidden)
    monkeypatch.setattr(trip, "trip_graph", type("ForbiddenGraph", (), {"invoke": forbidden})())
    monkeypatch.setattr(runner, "start_job", forbidden)
    monkeypatch.setattr(planning, "_reuse_with_new_dates", forbidden)

    with TestClient(app) as client:
        response = client.post(endpoint, json=request.model_dump())
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == error_code
        assert body.get("data") is None
        assert body.get("cached", False) is False
        if endpoint.endswith("async"):
            result = client.get(f"/api/trip/plan/result/{body['thread_id']}").json()
            assert result["success"] is False
            assert result["error_code"] == error_code

    # A rejected request must not inflate cache hits or model usage.
    with store._connect() as connection:
        row = connection.execute("SELECT hit_count FROM itinerary WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
        assert (row["hit_count"] if row else None) == (0 if cache_exists else None)
        run = connection.execute("SELECT * FROM run_log").fetchone()
        assert run["cached"] == 0
        assert run["success"] == 0
        assert run["error_code"] == error_code
        assert run["llm_calls"] == run["prompt_tokens"] == run["completion_tokens"] == 0


@pytest.mark.parametrize("endpoint", ["/api/trip/plan", "/api/trip/plan/async"])
@pytest.mark.parametrize("start_date", ["2026-10-10", "2028-02-28"])
def test_valid_request_still_reuses_cache(monkeypatch, endpoint, start_date):
    end_date = (date.fromisoformat(start_date) + timedelta(days=2)).isoformat()
    request = make_request(start_date=start_date, end_date=end_date, nights=0)
    historical = make_request(nights=0)
    fingerprint = store.fingerprint_of(historical)
    assert fingerprint == store.fingerprint_of(request)
    store.save_itinerary(fingerprint, historical, make_plan(
        [make_day(0, "2026-10-10", [make_attraction("西湖")])],
        hotel=make_hotel(nights=0),
    ))
    reused = []

    def reuse(plan, req):
        reused.append(req.start_date)
        return plan

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Cache hit must not start a planning graph")

    monkeypatch.setattr(planning, "_reuse_with_new_dates", reuse)
    monkeypatch.setattr(trip, "trip_graph", type("ForbiddenGraph", (), {"invoke": forbidden})())
    monkeypatch.setattr(runner, "start_job", forbidden)
    with TestClient(app) as client:
        response = client.post(endpoint, json=request.model_dump())
        assert response.status_code == 200
        body = response.json()
        if endpoint.endswith("async"):
            body = client.get(f"/api/trip/plan/result/{body['thread_id']}").json()
        assert body["cached"] is True
        assert body["cache_hit_count"] == 1
        assert reused == [start_date]
