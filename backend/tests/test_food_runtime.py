"""HTTP form ownership and durable food/experience graph continuation."""
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import trip
from app.models.schemas import Restaurant, TripPlanResponse
from app.services import identity, planning, runner, store
from tests.factories import make_attraction, make_day, make_plan, make_request


def authenticated(username):
    identity.register(username, "valid-password")
    return {"X-Session-Token": identity.login(username, "valid-password")["token"]}


def test_signed_in_form_is_private_and_imports_without_generation(monkeypatch):
    client = TestClient(app)
    owner, stranger = authenticated("formowner"), authenticated("formstranger")
    req = make_request(travel_days=1, nights=0, must_visit=[])
    calls = []

    def generate(request, **kwargs):
        calls.append(kwargs)
        payload = make_plan([make_day(0, request.start_date, [make_attraction("真实景点")])])
        payload.update(hotel_nights=0, hotel=None)
        return TripPlanResponse(success=True, thread_id=kwargs["run_id"], data=payload)

    monkeypatch.setattr(planning, "plan", generate)
    response = client.post("/api/trip/plan", json=req.model_dump(), headers=owner)
    assert response.status_code == 200 and response.json()["success"]
    run_id = response.json()["thread_id"]
    assert isinstance(calls[0]["cache_scope"], str)
    assert client.get(f"/api/trip/plan/result/{run_id}", headers=stranger).status_code == 404
    assert client.get(f"/api/trip/plan/result/{run_id}").status_code == 401
    assert client.post("/api/conversations/from-run", json={"thread_id": run_id}, headers=stranger).status_code == 404
    imported = client.post("/api/conversations/from-run", json={"thread_id": run_id}, headers=owner)
    assert imported.status_code == 200 and imported.json()["result"]["thread_id"] == run_id
    repeated = client.post("/api/conversations/from-run", json={"thread_id": run_id}, headers=owner)
    assert repeated.json()["conversation_id"] == imported.json()["conversation_id"]
    assert len(calls) == 1


def test_signed_in_async_cache_is_scoped_and_can_be_imported(monkeypatch):
    client = TestClient(app)
    owner = authenticated("asyncowner")
    req = make_request(travel_days=1, nights=0, must_visit=[])
    fingerprints = []
    monkeypatch.setattr(store, "find_itinerary", lambda fp: fingerprints.append(fp) or {"plan": {}, "hit_count": 1})

    def cached(_hit, request, run_id, _fp, _elapsed):
        payload = make_plan([make_day(0, request.start_date, [make_attraction("真实景点")])])
        return TripPlanResponse(success=True, thread_id=run_id, data=payload)

    monkeypatch.setattr(trip, "_cached_response", cached)
    response = client.post("/api/trip/plan/async", json=req.model_dump(), headers=owner)
    run_id = response.json()["thread_id"]
    assert fingerprints[0] != store.fingerprint_of(req)
    assert store.load_run(run_id)["request"]["city"] == req.city
    assert client.post("/api/conversations/from-run", json={"thread_id": run_id}, headers=owner).status_code == 200


def test_collector_barrier_requires_restaurants_and_restores_typed_candidates():
    req = make_request()
    state = trip._initial_state(req)
    assert state["experience_enabled"] is True
    completed = ["validate_city", "collect_attractions", "collect_weather"]
    assert runner.can_resume_from(completed, state) is None
    place = Restaurant(name="真实餐厅", poi_id="B000TEST1", location={"longitude": 120.1, "latitude": 30.2},
                       avg_cost=66, avg_cost_status="estimated", cost_source="amap")
    store.save_run("food-checkpoint", req)
    store.journal_node("food-checkpoint", "collect_food", 1, {"restaurants": [place]})
    restored, _ = runner.rebuild_state(state, "food-checkpoint")
    assert isinstance(restored["restaurants"][0], Restaurant)
    assert restored["restaurants"][0].avg_cost == 66
    assert runner.can_resume_from([*completed, "collect_food"], restored) == "compose_plan"
    assert runner.can_resume_from(["resolve_hotel"], restored) == "enrich_experience"
    restored.update(validate_error="天气不适合户外", retries=1)
    assert runner.can_resume_from(["enrich_experience"], restored) == "compose_plan"
    restored["retries"] = 2
    assert runner.can_resume_from(["enrich_experience"], restored) == "patch_budget"
