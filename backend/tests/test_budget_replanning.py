"""Exercise budget retry counts through the real graph and completion boundary."""
import json
from tests.joint_fixtures import restaurants_at, hotel_pool, joint_payload

import pytest

from app.agents import graph as graph_module
from app.agents import planner_agent
from app.api.routes.trip import _finalize, _initial_state
from app.services import store
from tests.factories import make_attraction, make_day, make_hotel, make_request, make_weather


@pytest.mark.parametrize(
    ("outputs", "expected_rounds", "expected_success"),
    [
        pytest.param(("cheap",), 0, True, id="within-without-replanning"),
        pytest.param(("expensive", "cheap"), 1, True, id="one-replan"),
        pytest.param(("expensive", "expensive", "cheap"), 2, True, id="two-replans"),
        pytest.param(("expensive",) * 3, 2, False, id="budget-still-exceeded"),
        pytest.param(("expensive", "bad-json", "cheap"), 1, True, id="repair-new-round"),
        pytest.param(("bad-json", "expensive") * 3, 2, False, id="maximum-path"),
    ],
)
def test_budget_replanning_is_counted_when_entered(
    monkeypatch, settings, outputs, expected_rounds, expected_success
):
    monkeypatch.setattr(settings, 'planner_format_retry_enabled', True)
    request = make_request(travel_days=1, nights=1, must_visit=[], budget_limit=600)
    expensive = make_attraction("收费景点", ticket_price=900, ticket_price_status="known")
    cheap = make_attraction("免费景点", ticket_price=0, ticket_price_status="known")
    pool = {"expensive": expensive, "cheap": cheap}
    restaurants = restaurants_at(cheap.location)
    monkeypatch.setattr(graph_module, "collect_food", lambda s: {"restaurants": restaurants})
    responses = iter(outputs)
    entries = []
    notes = []

    monkeypatch.setattr(graph_module, "validate_city", lambda _s: {"adcode": "330100"})
    monkeypatch.setattr(graph_module, "collect_attractions", lambda _s: {
        "attractions": list(pool.values()), "sources": {"attractions": "amap"},
    })
    monkeypatch.setattr(graph_module, "collect_weather", lambda _s: {
        "weather": [make_weather("2026-10-10")], "sources": {"weather": "amap"},
    })
    monkeypatch.setattr(graph_module, "collect_hotels", lambda s: hotel_pool(s, make_hotel(price=100, nights=1)))

    def recorded_compose(state):
        entries.append((state.get("budget_retries", 0), state.get("retries", 0)))
        return planner_agent.compose_plan(state)

    def fake_model(_system, _user, *, extra_user="", max_tokens=None):
        notes.append(extra_user)
        choice = next(responses)
        if choice == "bad-json":
            return "invalid json", {"llm_calls": 1, "prompt_tokens": 10}
        return json.dumps(joint_payload([make_day(0, "2026-10-10", [pool[choice]])], restaurants, "JHOTEL"),
                          ensure_ascii=False), {"llm_calls": 1, "prompt_tokens": 10}

    monkeypatch.setattr(graph_module, "compose_plan", recorded_compose)
    monkeypatch.setattr(planner_agent, "chat_json", fake_model)

    # This suite isolates the original budget loop; active food/time graph
    # coverage lives in test_itinerary_experience.py.
    final = graph_module.build_graph().invoke({**_initial_state(request), "experience_enabled":False})
    fingerprint = store.fingerprint_of(request)
    response = _finalize(final, request, "budget-replanning", fingerprint, elapsed_ms=0)

    assert len(notes) == len(outputs)
    assert final.get("budget_retries", 0) == expected_rounds
    assert final["usage"]["llm_calls"] == len(outputs)
    assert final["usage"]["prompt_tokens"] == 10 * len(outputs)
    assert response.success is expected_success
    assert (store.find_itinerary(fingerprint) is not None) is expected_success
    assert final["budget_verdict"]["status"] == ("within" if expected_success else "known_exceeded")

    expected_entries = []
    round_index = 0
    format_repair = False
    for choice in outputs:
        # One bounded syntax repair happens inside the same compose node;
        # provider calls above still count both attempts and their usage.
        if not format_repair:
            expected_entries.append((round_index, 0))
        format_repair = choice == "bad-json" and not format_repair
        if choice == "expensive":
            round_index += 1
    assert entries == expected_entries
    for index, choice in enumerate(outputs[:-1]):
        if choice == "expensive":
            assert "预算复核不通过" in notes[index + 1]
    if not expected_success:
        assert "预算" in response.message
