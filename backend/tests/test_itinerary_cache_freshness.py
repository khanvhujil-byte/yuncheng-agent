"""A full-plan cache must not bypass fresh local-first evidence indefinitely."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import store
from tests.factories import make_plan, make_request


NOW = datetime(2026, 10, 18, 16, 10, tzinfo=timezone.utc)


def saved(monkeypatch, *, created_at, payload=None):
    monkeypatch.setattr(store, "_cache_now", lambda: NOW)
    req = make_request()
    fingerprint = store.fingerprint_of(req)
    store.save_itinerary(fingerprint, req, make_plan(days=[]))
    with store._connect() as conn:
        conn.execute("UPDATE itinerary SET created_at=?,hit_count=7,last_hit_at=? WHERE fingerprint=?",
                     (created_at, "previous-hit", fingerprint))
        if payload is not None:
            conn.execute("UPDATE itinerary SET plan_json=? WHERE fingerprint=?", (payload, fingerprint))
    return fingerprint


def stored(fingerprint):
    with store._connect() as conn:
        return dict(conn.execute("SELECT * FROM itinerary WHERE fingerprint=?", (fingerprint,)).fetchone())


@pytest.mark.parametrize("created_at", [
    (NOW - timedelta(seconds=store.ITINERARY_CACHE_TTL_SECONDS)).isoformat(),
    (NOW - timedelta(days=100)).isoformat(),
    (NOW + timedelta(microseconds=1)).isoformat(),
    "not-a-timestamp", "2026-10-18", "2026-10-18T16:00:00", "",
])
def test_stale_future_or_invalid_timestamp_is_miss_without_erasing_history(monkeypatch, created_at):
    fingerprint = saved(monkeypatch, created_at=created_at)
    before = stored(fingerprint)
    assert store.find_itinerary(fingerprint) is None
    assert stored(fingerprint) == before


def test_fresh_cache_reuses_across_midnight_without_sliding_its_expiry(monkeypatch):
    # 23:50 China time -> 00:10 next day, twenty minutes apart.
    created = NOW - timedelta(minutes=20)
    fingerprint = saved(monkeypatch, created_at=created.isoformat())
    before = stored(fingerprint)
    shifted = make_request(start_date="2026-12-31")
    assert store.fingerprint_of(shifted) == fingerprint
    hit = store.find_itinerary(fingerprint)
    assert hit is not None and hit["hit_count"] == 8
    assert stored(fingerprint)["created_at"] == before["created_at"]

    monkeypatch.setattr(store, "_cache_now", lambda: created + timedelta(seconds=store.ITINERARY_CACHE_TTL_SECONDS))
    assert store.find_itinerary(fingerprint) is None
    assert stored(fingerprint)["hit_count"] == 8
    assert stored(fingerprint)["created_at"] == before["created_at"]


def test_timestamp_timezone_offsets_are_compared_as_instants(monkeypatch):
    china = timezone(timedelta(hours=8))
    created = (NOW - timedelta(minutes=30)).astimezone(china)
    fingerprint = saved(monkeypatch, created_at=created.isoformat())
    assert store.find_itinerary(fingerprint) is not None


@pytest.mark.parametrize("payload", ["not-json", json.dumps(["wrong-shape"])])
def test_corrupt_plan_is_miss_and_does_not_inflate_hits(monkeypatch, payload):
    fingerprint = saved(monkeypatch, created_at=NOW.isoformat(), payload=payload)
    before = stored(fingerprint)
    assert store.find_itinerary(fingerprint) is None
    assert stored(fingerprint) == before


def test_new_retrieval_contract_does_not_hit_legacy_v4_cache(monkeypatch):
    request = make_request()
    current = store.fingerprint_of(request)
    with monkeypatch.context() as legacy:
        legacy.setattr(store, "FINGERPRINT_CONTRACT_VERSION", 4)
        old = store.fingerprint_of(request)
    assert current != old
    store.save_itinerary(old, request, make_plan(days=[]))
    assert store.find_itinerary(current) is None
    assert stored(old)["hit_count"] == 0


def test_regeneration_refreshes_time_but_preserves_prior_hit_count(monkeypatch):
    fingerprint = saved(monkeypatch, created_at=(NOW - timedelta(days=1)).isoformat())
    assert store.find_itinerary(fingerprint) is None
    monkeypatch.setattr(store, "_now", lambda: NOW.isoformat())
    store.save_itinerary(fingerprint, make_request(), make_plan(days=[], overall_suggestions="newly generated"))
    hit = store.find_itinerary(fingerprint)
    assert hit["hit_count"] == 8
    assert hit["plan"]["overall_suggestions"] == "newly generated"
