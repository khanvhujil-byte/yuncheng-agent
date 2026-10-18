"""Exercise real planning retries and the success/cache boundary without APIs."""

import json

import pytest

from app.agents import graph as graph_module
from app.agents import planner_agent
from app.api.routes.trip import _finalize, _initial_state
from app.services import store
from tests.factories import make_attraction, make_day, make_hotel, make_request, make_weather


@pytest.mark.parametrize(
    ("model_outputs", "expected_success"),
    [
        pytest.param(("invalid", "invalid"), False, id="invalid-after-retry"),
        pytest.param(("invalid", "valid"), True, id="corrected-on-retry"),
        pytest.param(("valid",), True, id="valid-first-attempt"),
    ],
)
def test_candidate_validation_controls_success_and_cache(
    monkeypatch, model_outputs, expected_success
):
    request = make_request(travel_days=2, nights=1, must_visit=["西湖"])
    anchor = make_attraction("西湖", required=True)
    other = make_attraction("博物馆")
    invented = make_attraction("候选池外景点")

    def validate(_state):
        return {"adcode": "330100", "must_visit_resolved": [anchor]}

    def collect_attractions(_state):
        return {"attractions": [anchor, other], "sources": {"attractions": "amap"}}

    def collect_weather(_state):
        return {
            "weather": [make_weather("2026-10-10"), make_weather("2026-10-11")],
            "sources": {"weather": "amap"},
        }

    def resolve_hotel(_state):
        return {
            "hotel": make_hotel(nights=1),
            "hotel_source": "llm_estimated",
            "sources": {"hotel": "llm_estimated"},
        }

    responses = iter(model_outputs)
    calls = []

    def fake_model(_system, _user, *, extra_user=""):
        calls.append(extra_user)
        choice = next(responses)
        payload = {
            "days": [
                make_day(0, "2026-10-10", [anchor]),
                make_day(1, "2026-10-11", [other if choice == "valid" else invented]),
            ],
        }
        return json.dumps(payload, ensure_ascii=False), {"llm_calls": 1}

    monkeypatch.setattr(graph_module, "validate_city", validate)
    monkeypatch.setattr(graph_module, "collect_attractions", collect_attractions)
    monkeypatch.setattr(graph_module, "collect_weather", collect_weather)
    monkeypatch.setattr(graph_module, "resolve_hotel", resolve_hotel)
    monkeypatch.setattr(planner_agent, "chat_json", fake_model)

    final = graph_module.build_graph().invoke({**_initial_state(request), "experience_enabled":False})
    fingerprint = store.fingerprint_of(request)
    response = _finalize(final, request, "candidate-validation", fingerprint, elapsed_ms=0)

    assert len(calls) == len(model_outputs)
    assert final["usage"]["llm_calls"] == len(model_outputs)
    if len(calls) > 1:
        assert "候选池外景点" in calls[1]
    assert response.success is expected_success
    assert (store.find_itinerary(fingerprint) is not None) is expected_success
    if expected_success:
        assert final.get("validate_error") is None
        assert response.data.days[1].attractions[0].poi_id == other.poi_id
    else:
        assert "候选池外景点" in final["validate_error"]
        assert "候选池外景点" in response.message
        assert response.data is not None
