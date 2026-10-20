"""Fault injection through real graph nodes, SQLite journal, and resume HTTP API."""
import json
from tests.joint_fixtures import restaurants_at, hotel_pool, joint_payload
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module, planner_agent
from app.api.main import app
from app.api.routes import trip
from app.services import runner, store
from tests.factories import make_attraction, make_day, make_hotel, make_request, make_weather


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(runner, "_jobs", {})
    monkeypatch.setattr(runner, "_active_runs", set())


def install_scenario(monkeypatch, choices=(0, 1, 1, 2)):
    """One failed itinerary audit, followed by two budget replans and success."""
    req = make_request(travel_days=1, nights=1, must_visit=[], budget_limit=600)
    spots = [
        make_attraction("排不下", visit_duration=700, ticket_price_status="known"),
        make_attraction("昂贵", ticket_price=900, ticket_price_status="known"),
        make_attraction("免费", ticket_price_status="known"),
    ]
    calls = []
    audits = []
    restaurants = restaurants_at(spots[0].location)

    def model(_system, _user, *, extra_user="", max_tokens=None):
        spot = spots[choices[len(calls)]]
        calls.append(spot.name)
        return json.dumps(joint_payload([make_day(0, req.start_date, [spot])], restaurants, "JHOTEL"), ensure_ascii=False), {
            "llm_calls": 1, "prompt_tokens": 40,
        }

    real_audit = graph_module.verify_plan

    def audit(state):
        audits.append(state["plan"]["days"][0]["attractions"][0]["name"])
        return real_audit(state)

    monkeypatch.setattr(graph_module, "validate_city", lambda _s: {"adcode": "330100"})
    monkeypatch.setattr(graph_module, "collect_attractions", lambda _s: {
        "attractions": spots, "sources": {"attractions": "amap"},
        "usage": {"llm_calls": 1, "prompt_tokens": 10},
    })
    monkeypatch.setattr(graph_module, "collect_weather", lambda _s: {
        "weather": [make_weather(req.start_date)], "sources": {"weather": "amap"},
        "usage": {"llm_calls": 1, "prompt_tokens": 20},
    })
    # This scenario isolates retry/journal invariants; food/comfort has its own
    # active graph coverage with real restaurant and route fixtures.
    monkeypatch.setattr(graph_module, "collect_food", lambda _s: {"experience_enabled": False, "restaurants": restaurants})
    monkeypatch.setattr(graph_module, "collect_hotels", lambda s: {
        **hotel_pool(s, make_hotel(price=100, nights=1)), "usage": {"llm_calls": 1, "prompt_tokens": 5}})
    monkeypatch.setattr(graph_module, "verify_plan", audit)
    monkeypatch.setattr(planner_agent, "chat_json", model)
    return req, calls, audits


class InterruptAfter:
    def __init__(self, graph, cut):
        self.graph = graph
        self.cut = cut

    def stream(self, *args, **kwargs):
        count = 0
        for mode, chunk in self.graph.stream(*args, **kwargs):
            yield mode, chunk
            if mode == "updates":
                count += len(chunk)
                if count == self.cut:
                    raise RuntimeError("injected interruption after committed node")


def execute(thread_id, req, graph):
    store.save_run(thread_id, req)
    job = runner.Job(thread_id=thread_id, request=req)
    runner._jobs[thread_id] = job
    runner._execute(job, trip._initial_state(req), lambda state: trip._finalize(
        state, req, thread_id, store.fingerprint_of(req), 0,
    ), graph)
    return job


# Sequence: city, two collectors, two compose/audit attempts, hotel, budget patch,
# budget audit, prepare, compose/audit, hotel, patch, budget audit, prepare,
# compose/audit, hotel, patch, budget audit. Every supported interruption is covered.
@pytest.mark.parametrize("cut", range(5, 26))
def test_each_committed_checkpoint_matches_uninterrupted_execution(monkeypatch, cut):
    req, calls, audits = install_scenario(monkeypatch)
    baseline = execute("baseline", req, graph_module.build_graph())
    assert baseline.status == "done" and baseline.result["success"]
    expected, _ = runner.rebuild_state(trip._initial_state(req), "baseline")
    baseline_records = store.load_journal("baseline")
    assert len(baseline_records) == 25
    calls.clear()
    audits.clear()

    thread_id = f"cut-{cut}"
    failed = execute(thread_id, req, InterruptAfter(graph_module.build_graph(), cut))
    assert failed.status == "error"
    assert len(store.load_journal(thread_id)) == cut
    if cut == 9:
        # Old failed audit exists, but this new plan still needs its own audit.
        assert audits == ["排不下"]
        assert trip.get_journal(thread_id)["can_resume_from"] == "verify_plan"
    runner._jobs.clear()  # Restore from SQLite rather than a live Job object.
    response = TestClient(app).post(f"/api/trip/plan/resume/{thread_id}")
    assert response.status_code == 200
    assert response.json()["success"] is True
    actual, completed = runner.rebuild_state(trip._initial_state(req), thread_id)
    # Independently executed baseline/recovery have different measured wall
    # times. Their node identities, order and all business state must match.
    for state in (actual, expected):
        assert all(t["elapsed_ms"] >= 0 for t in state.get("trace", []))
    def business_state(state):
        # Parallel collectors share a step and may commit in either order.
        trace = [{k: v for k, v in entry.items() if k != "elapsed_ms"}
                 for entry in state.get("trace", [])]
        business = {k:v for k,v in state.items() if k != 'planning_started_at'}
        return {**business, "trace": sorted(trace, key=lambda entry: (entry["step"], entry["node"]))}
    assert business_state(actual) == business_state(expected)
    assert calls == ["排不下", "昂贵", "昂贵", "免费"]
    assert audits == calls
    records = store.load_journal(thread_id)
    assert [r["seq"] for r in records] == list(range(1, 26))
    # The parallel collectors may finish in either order; later nodes are sequential.
    assert set(completed[:5]) == {r["node"] for r in baseline_records[:5]}
    assert completed[5:] == [r["node"] for r in baseline_records[5:]]
    assert actual["usage"] == {"llm_calls": 7, "prompt_tokens": 195}
    assert actual["budget_retries"] == 2
    stored = store.load_job(thread_id)
    assert stored["status"] == "done" and stored["error"] == ""
    assert stored["result"] == response.json()
    assert [e["seq"] for e in stored["events"]] == list(range(1, 26))
    assert trip.get_journal(thread_id)["can_resume_from"] is None
    runner._jobs.clear()
    assert TestClient(app).get(f"/api/trip/plan/result/{thread_id}").json() == response.json()
    # Repeated resume returns the already persisted result, with no new work/log row.
    assert TestClient(app).post(f"/api/trip/plan/resume/{thread_id}").json() == response.json()
    with store._connect() as conn:
        runs = conn.execute("SELECT * FROM run_log WHERE thread_id=?", (thread_id,)).fetchall()
    assert len(runs) == 1 and runs[0]["llm_calls"] == 7


def test_second_interruption_during_resume_can_be_resumed_again(monkeypatch):
    req, calls, audits = install_scenario(monkeypatch)
    execute("twice", req, InterruptAfter(graph_module.build_graph(), 9))
    build = trip.build_graph
    monkeypatch.setattr(trip, "build_graph", lambda **kw: InterruptAfter(build(**kw), 1))
    client = TestClient(app)
    first = client.post("/api/trip/plan/resume/twice").json()
    assert first["error_code"] == "RESUME_FAILED"
    assert store.load_job("twice")["status"] == "error"
    assert trip.get_journal("twice")["can_resume_from"] == "enrich_experience"
    monkeypatch.setattr(trip, "build_graph", build)
    runner._jobs.clear()
    second = client.post("/api/trip/plan/resume/twice").json()
    assert second["success"]
    assert calls == audits == ["排不下", "昂贵", "昂贵", "免费"]
    assert len(store.load_journal("twice")) == 25


@pytest.mark.parametrize(("choices", "cut", "expected_rounds"), [
    pytest.param((0, 0), 10, 0, id="second-audit-failed-at-limit"),
    pytest.param((1, 1, 1), 18, 2, id="last-budget-replan-needed"),
    pytest.param((1, 1, 1), 23, 2, id="final-budget-failure-before-finalize"),
])
def test_exhausted_failures_stay_failed_after_resume(monkeypatch, choices, cut, expected_rounds):
    req, calls, audits = install_scenario(monkeypatch, choices=choices)
    execute("exhausted", req, InterruptAfter(graph_module.build_graph(), cut))
    response = TestClient(app).post("/api/trip/plan/resume/exhausted").json()
    assert response["success"] is False
    assert response["data"] is not None and response["message"]
    assert len(calls) == len(audits) == len(choices)
    state, _ = runner.rebuild_state(trip._initial_state(req), "exhausted")
    assert state["budget_retries"] == expected_rounds
    if expected_rounds:
        assert state["budget_verdict"]["status"] == "known_exceeded"
    else:
        assert state["retries"] == 2 and state["validate_error"]
    assert store.find_itinerary(store.fingerprint_of(req)) is None
    assert store.load_job("exhausted")["result"] == response
    assert runner._active_runs == set()


@pytest.mark.parametrize("kind", ["failed-audit", "budget-retry", "budget-final"])
def test_resume_routing_respects_existing_attempt_limits(kind):
    if kind == "failed-audit":
        state = {"validate_error": "still invalid", "retries": 2}
        assert runner.can_resume_from(["compose_plan", "verify_plan"], state) == "resolve_hotel"
    else:
        state = {"budget_verdict": {"status": "known_exceeded"},
                 "budget_retries": 1 if kind == "budget-retry" else 2}
        expected = "prepare_budget_retry" if kind == "budget-retry" else runner.FINALIZE_ONLY
        assert runner.can_resume_from(["verify_budget"], state) == expected


def test_existing_committed_execution_is_immutable():
    store.save_run("immutable", make_request())
    store.journal_node("immutable", "compose_plan", 1, {"usage": {"llm_calls": 1}})
    with pytest.raises(sqlite3.IntegrityError):
        store.journal_node("immutable", "verify_plan", 1, {"validate_error": "overwrite"})
    assert store.load_journal("immutable")[0]["node"] == "compose_plan"


def test_journal_commit_precedes_done_event_and_failure_emits_no_false_done(monkeypatch):
    req, _calls, _audits = install_scenario(monkeypatch)
    real_emit = runner._emit

    def checked_emit(job, event):
        assert store.load_journal(job.thread_id)[-1]["seq"] == event["seq"]
        real_emit(job, event)

    monkeypatch.setattr(runner, "_emit", checked_emit)
    real_journal = store.journal_node

    def disk_error(thread_id, node, seq, update, elapsed_ms):
        if seq == 6:
            raise OSError("disk write failed")
        return real_journal(thread_id, node, seq, update, elapsed_ms)

    monkeypatch.setattr(store, "journal_node", disk_error)
    job = execute("disk-error", req, graph_module.build_graph())
    assert job.status == "error"
    assert len(job.events) == len(store.load_journal("disk-error")) == 5


def test_resume_reconstructs_event_missing_after_journal_commit(monkeypatch):
    req, _calls, _audits = install_scenario(monkeypatch)
    emit = runner._emit

    def interrupt_emit(job, event):
        if event["seq"] == 6:
            raise RuntimeError("crash after commit before notification")
        return emit(job, event)

    monkeypatch.setattr(runner, "_emit", interrupt_emit)
    job = execute("missing-event", req, graph_module.build_graph())
    assert len(job.events) == 5 and len(store.load_journal("missing-event")) == 6
    monkeypatch.setattr(runner, "_emit", emit)
    response = TestClient(app).post("/api/trip/plan/resume/missing-event").json()
    assert response["success"]
    assert [e["seq"] for e in store.load_job("missing-event")["events"]] == list(range(1, 26))


def test_concurrent_resume_runs_one_execution(monkeypatch):
    req, _calls, _audits = install_scenario(monkeypatch)
    execute("concurrent", req, InterruptAfter(graph_module.build_graph(), 6))
    entered = threading.Event()
    release = threading.Event()
    real_audit = graph_module.verify_plan

    def blocked_audit(state):
        entered.set()
        assert release.wait(timeout=10)
        return real_audit(state)

    monkeypatch.setattr(graph_module, "verify_plan", blocked_audit)
    responses = []
    thread = threading.Thread(target=lambda: responses.append(
        TestClient(app).post("/api/trip/plan/resume/concurrent").json()
    ))
    thread.start()
    try:
        assert entered.wait(timeout=10)
        duplicate = TestClient(app).post("/api/trip/plan/resume/concurrent").json()
        assert duplicate["error_code"] == "JOB_ALREADY_RUNNING"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(responses) == 1 and responses[0]["success"]
    assert len(store.load_journal("concurrent")) == 25


def test_background_execution_claim_blocks_resume_and_releases_on_finish(monkeypatch):
    req, _calls, _audits = install_scenario(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def blocked_city(_state):
        entered.set()
        assert release.wait(timeout=10)
        return {"adcode": "330100"}

    monkeypatch.setattr(graph_module, "validate_city", blocked_city)
    monkeypatch.setattr(graph_module, "app", graph_module.build_graph())
    finished = threading.Event()
    original = runner._background_run

    def tracked(*args):
        try:
            original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(runner, "_background_run", tracked)
    runner.start_job("background-lock", req, trip._initial_state(req), lambda _s: {"success": True})
    try:
        assert entered.wait(timeout=10)
        response = TestClient(app).post("/api/trip/plan/resume/background-lock").json()
        assert response["error_code"] == "JOB_ALREADY_RUNNING"
    finally:
        release.set()
        assert finished.wait(timeout=10)
    with runner.execution_guard("background-lock"):
        assert store.load_job("background-lock")["status"] == "done"


def test_legacy_database_migration_keeps_records_but_refuses_replay(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    monkeypatch.setattr(store, "DB_PATH", legacy)
    with sqlite3.connect(legacy) as conn:
        conn.executescript("""
            CREATE TABLE graph_run (thread_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                                    created_at TEXT NOT NULL);
            CREATE TABLE graph_journal (thread_id TEXT NOT NULL, node TEXT NOT NULL,
                seq INTEGER NOT NULL, update_json TEXT NOT NULL, elapsed_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, PRIMARY KEY (thread_id, node));
        """)
        conn.execute("INSERT INTO graph_run VALUES (?, ?, ?)",
                     ("old", json.dumps(make_request().model_dump()), "old-time"))
        conn.execute("INSERT INTO graph_journal VALUES (?, ?, ?, ?, ?, ?)",
                     ("old", "compose_plan", 6, '{"retries": 2}', 10, "old-time"))
    store.init_db()
    store.init_db()  # Migration is idempotent.
    assert store.load_run("old")["journal_version"] == 1
    assert store.load_journal("old")[0]["update"] == {"retries": 2}
    store.save_run("old", make_request())
    assert store.load_run("old")["journal_version"] == 1
    with pytest.raises(runner.LegacyJournalError):
        runner.rebuild_state({}, "old")
    client = TestClient(app)
    assert client.get("/api/trip/journal/old").json()["error_code"] == "LEGACY_JOURNAL_UNSAFE"
    assert client.post("/api/trip/plan/resume/old").json()["error_code"] == "LEGACY_JOURNAL_UNSAFE"
    store.save_run("new", make_request())
    assert store.load_run("new")["journal_version"] == 2
    store.journal_node("new", "compose_plan", 1, {})
    store.journal_node("new", "compose_plan", 2, {})
    assert len(store.load_journal("new")) == 2
