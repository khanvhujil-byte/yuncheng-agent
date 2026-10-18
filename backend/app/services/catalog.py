"""Durable, deduplicated verified POIs and separately sourced public ranking entries.

Ranking inclusion never implies a verified location, ticket or room price. Those
remain separate facts and missing amounts remain unknown.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from ..models.schemas import Attraction, Hotel, Restaurant, TripPlan
from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS place_catalog (
 city TEXT NOT NULL, kind TEXT NOT NULL, poi_id TEXT NOT NULL,
 name TEXT NOT NULL, payload_json TEXT NOT NULL, source TEXT NOT NULL,
 evidence_url TEXT, observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(city, kind, poi_id)
);
CREATE TABLE IF NOT EXISTS food_ranking (
 city TEXT NOT NULL, poi_id TEXT NOT NULL, name TEXT NOT NULL,
 ranking_name TEXT NOT NULL, ranking_rank INTEGER NOT NULL,
 source_url TEXT NOT NULL, observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(city, poi_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_food_ranking_city ON food_ranking(city, ranking_rank);
"""


@contextmanager
def _connection():
    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store.DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    try:
        with db:
            yield db
    finally:
        db.close()


def _city(value):
    return value.strip().removesuffix("市")


def upsert_places(city, kind, places, *, source="amap", evidence_url=None, ttl_days=7):
    if kind not in {"attraction", "hotel", "restaurant"}:
        raise ValueError("unsupported place kind")
    now = datetime.now(timezone.utc)
    rows = []
    for place in places:
        payload = place.model_dump(mode="json") if hasattr(place, "model_dump") else dict(place)
        identifier, name = payload.get("poi_id"), payload.get("name")
        if not identifier or not name or not payload.get("location"):
            continue
        rows.append((_city(city), kind, identifier, name, json.dumps(payload, ensure_ascii=False),
                     source, evidence_url, now.isoformat(), (now + timedelta(days=ttl_days)).isoformat()))
    with _connection() as db:
        db.executemany("""INSERT INTO place_catalog VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(city,kind,poi_id) DO UPDATE SET name=excluded.name,
            payload_json=excluded.payload_json, source=excluded.source,
            evidence_url=excluded.evidence_url, observed_at=excluded.observed_at,
            expires_at=excluded.expires_at""", rows)
    return len(rows)


def list_places(city, kind, *, limit=50):
    with _connection() as db:
        rows = db.execute("""SELECT payload_json FROM place_catalog
            WHERE city=? AND kind=? AND expires_at>? ORDER BY observed_at DESC LIMIT ?""",
                          (_city(city), kind, datetime.now(timezone.utc).isoformat(), min(max(limit, 1), 100))).fetchall()
    return [json.loads(row[0]) for row in rows]


def save_rankings(city, records, *, source_url, ranking_name, ttl_days=30, observed_at=None):
    """Only ingestion passes validated public ranking records here."""
    now = datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("榜单采集时间必须含时区")
    now = now.astimezone(timezone.utc)
    rows = []
    for entry in records:
        if not entry.get("poi_id") or not entry.get("name") or not isinstance(entry.get("rank"), int) or entry["rank"] < 1:
            continue
        rows.append((_city(city), entry["poi_id"], entry["name"], ranking_name, entry["rank"],
                     source_url, now.isoformat(), (now + timedelta(days=ttl_days)).isoformat()))
    with _connection() as db:
        db.executemany("""INSERT INTO food_ranking VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(city,poi_id,source_url) DO UPDATE SET name=excluded.name,
            ranking_name=excluded.ranking_name,ranking_rank=excluded.ranking_rank,
            observed_at=excluded.observed_at,expires_at=excluded.expires_at""", rows)
    return len(rows)


def rankings(city, *, limit=20):
    with _connection() as db:
        rows = db.execute("""SELECT * FROM food_ranking WHERE city=? AND expires_at>?
            ORDER BY observed_at DESC, ranking_rank LIMIT ?""",
                          (_city(city), datetime.now(timezone.utc).isoformat(), min(max(limit, 1), 100))).fetchall()
    # A restaurant may be present on multiple lists. Latest source wins.
    unique = {}
    for row in rows:
        unique.setdefault(row["poi_id"], dict(row))
    return list(unique.values())


def persist_plan(plan, city, *, attractions=(), hotels=(), restaurants=()):
    """Persist selected entities only after matching a verified candidate by ID.

    Copy source candidates, not model-written names/addresses/prices. Calling
    without source candidates deliberately writes nothing.
    """
    plan = TripPlan.model_validate(plan)
    attractions = [Attraction.model_validate(item) for item in attractions]
    hotels = [Hotel.model_validate(item) for item in hotels]
    restaurants = [Restaurant.model_validate(item) for item in restaurants]
    selected_attractions = {item.poi_id for day in plan.days for item in day.attractions if item.poi_id}
    selected_restaurants = {getattr(item, "restaurant_id", None) for day in plan.days for item in day.meals}
    selected_hotel = plan.hotel.poi_id if plan.hotel else None
    return {
        "attractions": upsert_places(city, "attraction", [p for p in attractions if p.poi_id in selected_attractions]),
        "hotels": upsert_places(city, "hotel", [p for p in hotels if p.poi_id == selected_hotel]),
        "restaurants": upsert_places(city, "restaurant", [p for p in restaurants if p.poi_id in selected_restaurants]),
    }
