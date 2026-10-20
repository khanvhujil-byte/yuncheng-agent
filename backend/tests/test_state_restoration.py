"""Persist real domain models, restore them, and execute real continuation nodes."""
import json

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module
from app.agents import planner_agent
from app.api.main import app
from app.api.routes import trip
from app.models.schemas import Attraction, ConstraintReport, Hotel, Location, TripRequest, WeatherInfo
from app.services import runner, store
from tests.factories import make_attraction, make_day, make_hotel, make_plan, make_request, make_weather


def persist_collectors(thread_id, request):
    spot = make_attraction("西湖", required=True, ticket_price_status="known")
    store.save_run(thread_id, request)
    store.journal_node(thread_id, "validate_city", 1, {
        "adcode": "330100", "city_center": spot.location, "must_visit_resolved": [spot],
        "experience_enabled": False,
    })
    store.journal_node(thread_id, "collect_attractions", 2, {
        "attractions": [spot], "sources": {"attractions": "amap"},
        "usage": {"llm_calls": 1, "prompt_tokens": 10},
    })
    store.journal_node(thread_id, "collect_weather", 3, {
        "weather": [make_weather(request.start_date)], "sources": {"weather": "amap"},
        "usage": {"llm_calls": 1, "prompt_tokens": 20},
    })
    return spot


def test_rebuild_restores_model_fields_and_leaves_plan_as_mapping():
    request = make_request(travel_days=1, nights=1)
    spot = persist_collectors("restore-types", request)
    hotel = make_hotel(nights=1)
    report = ConstraintReport(required_attractions=[spot.name], satisfied=[spot.name], feasible=True)
    store.journal_node("restore-types", "compose_plan", 4, {
        "request": request, "plan": make_plan([make_day(0, request.start_date, [spot])]),
        "constraint_report": report,
    })
    store.journal_node("restore-types", "resolve_hotel", 5, {
        "hotels": [hotel], "hotel": hotel, "hotel_anchor": hotel,
    })
    state, _ = runner.rebuild_state({}, "restore-types")
    assert isinstance(state["request"], TripRequest)
    assert isinstance(state["city_center"], Location)
    assert isinstance(state["attractions"][0], Attraction)
    assert isinstance(state["attractions"][0].location, Location)
    assert isinstance(state["must_visit_resolved"][0], Attraction)
    assert isinstance(state["weather"][0], WeatherInfo)
    assert isinstance(state["hotel"], Hotel)
    assert isinstance(state["hotels"][0], Hotel)
    assert isinstance(state["hotel_anchor"], Hotel)
    assert isinstance(state["constraint_report"], ConstraintReport)
    assert isinstance(state["plan"], dict)
    assert isinstance(state["plan"]["days"][0], dict)
    assert state["usage"] == {"llm_calls": 2, "prompt_tokens": 30}
    # Storage stays JSON data; restoring does not rewrite the historical payload.
    raw = store.load_journal("restore-types")
    assert isinstance(raw[1]["update"]["attractions"][0], dict)


@pytest.mark.parametrize("checkpoint", ["collectors", "hotel"])
def test_resume_endpoint_executes_real_nodes_after_json_roundtrip(monkeypatch, checkpoint):
    request = make_request(travel_days=1, nights=1)
    thread_id = f"continue-{checkpoint}"
    spot = persist_collectors(thread_id, request)
    model_calls = []
    hotel_calls = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Completed collection nodes must not run again")

    def fake_model(_system, user, *, extra_user="", max_tokens=None):
        model_calls.append(user)
        assert "西湖" in user and "晴" in user
        return json.dumps({"days": [make_day(0, request.start_date, [spot])]},
                          ensure_ascii=False), {"llm_calls": 1, "prompt_tokens": 40}

    def hotel_node(_state):
        hotel_calls.append(True)
        return {"hotel": make_hotel(price=200, nights=1), "sources": {"hotel": "llm_estimated"}}

    if checkpoint == "hotel":
        # A valid plan was already audited before the interruption after hotel selection.
        store.journal_node(thread_id, "compose_plan", 4, {
            "plan": make_plan([make_day(0, request.start_date, [spot])]), "retries": 1,
            "constraint_report": ConstraintReport(
                required_attractions=[spot.name], satisfied=[spot.name], feasible=True,
            ),
            "usage": {"llm_calls": 1, "prompt_tokens": 40},
        })
        store.journal_node(thread_id, "verify_plan", 5, {})
        store.journal_node(thread_id, "resolve_hotel", 6, hotel_node({}))
        hotel_calls.clear()

    monkeypatch.setattr(graph_module, "validate_city", forbidden)
    monkeypatch.setattr(graph_module, "collect_attractions", forbidden)
    monkeypatch.setattr(graph_module, "collect_weather", forbidden)
    monkeypatch.setattr(graph_module, "resolve_hotel", hotel_node)
    monkeypatch.setattr(planner_agent, "chat_json", fake_model)
    if checkpoint == "hotel":
        monkeypatch.setattr(graph_module, "compose_plan", forbidden)
    client = TestClient(app)
    response = client.post(f"/api/trip/plan/resume/{thread_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["days"][0]["attractions"][0]["name"] == "西湖"
    assert body["data"]["budget"]["total_hotels"] == 200
    assert body["data"]["constraint_report"]["satisfied"] == ["西湖"]
    assert len(model_calls) == len(hotel_calls) == (1 if checkpoint == "collectors" else 0)
    with store._connect() as connection:
        run = connection.execute("SELECT * FROM run_log WHERE thread_id=?", (thread_id,)).fetchone()
        assert run["llm_calls"] == 3
        assert run["prompt_tokens"] == 70


@pytest.mark.parametrize("endpoint", ["journal", "plan/resume"])
def test_invalid_persisted_model_declines_restoration(monkeypatch, endpoint):
    request = make_request(travel_days=1, nights=1)
    thread_id = "invalid-state-" + endpoint.replace("/", "-")
    persist_collectors(thread_id, request)
    # The stored attraction lacks its required name and location.
    with store._connect() as connection:
        connection.execute(
            "UPDATE graph_journal_event SET update_json=? WHERE thread_id=? AND seq=2",
            (json.dumps({"attractions": [{}]}), thread_id),
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Malformed state must not enter a graph")

    monkeypatch.setattr(trip, "build_graph", forbidden)
    client = TestClient(app)
    response = (client.get if endpoint == "journal" else client.post)(f"/api/trip/{endpoint}/{thread_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["error_code"] == "STATE_RESTORE_FAILED"
    assert "恢复" in body["message"]
    if endpoint == "journal":
        assert body["can_resume_from"] is None
    else:
        assert body["success"] is False
        assert body["data"] is None
