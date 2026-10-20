"""Durable, deduplicated verified POIs and separately sourced public ranking entries.

Ranking inclusion never implies a verified location, ticket or room price. Those
remain separate facts and missing amounts remain unknown.
"""
import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

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
CREATE INDEX IF NOT EXISTS idx_place_catalog_scope ON place_catalog(city, kind, expires_at);
"""

KINDS = {"attraction", "hotel", "restaurant"}
_SCHEMA_LOCK = threading.RLock()
_GENERIC_QUERIES = {
    "attraction": {"", "景点", "景区", "旅游景点", "风景名胜", "景点推荐"},
    "hotel": {"", "酒店", "住宿", "住宿服务", "宾馆", "酒店住宿"},
    "restaurant": {"", "餐厅", "餐馆", "餐饮", "美食", "当地特色美食", "特色美食", "当地美食", "餐饮服务"},
}
# These are retrieval expansions into provider category labels, not new claims
# about facilities, age suitability, accessibility or allergy safety. Only the
# category/type fields can satisfy them; a scenic-sounding shop name cannot.
_NATURE_CATEGORIES = ("风景名胜", "公园", "景区", "山岳", "自然保护", "森林公园", "海滨", "湖泊")
_HISTORY_CATEGORIES = ("博物馆", "纪念馆", "文物古迹", "古迹", "寺庙", "寺院", "历史遗迹", "故居", "祠堂", "古镇")
_CATEGORY_QUERIES = {
    "自然风光": _NATURE_CATEGORIES,
    "历史人文": _HISTORY_CATEGORIES,
    "人文历史": _HISTORY_CATEGORIES,
}
_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS place_catalog_term (
 city TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL, poi_id TEXT NOT NULL,
 PRIMARY KEY(city, kind, token, poi_id)
);
"""


class _QueryGroup(NamedTuple):
    terms: tuple[str, ...]
    category_only: bool = False


def _normalize(value):
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", unicodedata.normalize("NFKC", str(value)).casefold()))


def _tokens(value):
    """Chinese bigrams plus literal words; no model-generated tags or embeddings."""
    result = set()
    for word in _normalize(value).split():
        result.add(word)
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.update(word[i:i + 2] for i in range(len(word) - 1))
    return result


def _search_fields(payload):
    # Descriptions / suitability labels may be model-written. Do not index them
    # as source facts. Tags copy only provider category, cuisine and type fields.
    tags = list(dict.fromkeys(str(payload[key]).strip() for key in ("category", "cuisine", "type", "typecode")
                              if payload.get(key)))
    text = _normalize(" ".join([str(payload.get("name", "")), str(payload.get("address", "")), *tags]))
    return text, tags


def _catalog_payload(kind, payload):
    """Keep shared POI facts separate from another trip's execution state.

    Also applied on reads so pre-migration rows cannot leak former anchors.
    Date-specific imported evidence remains independently checked by callers.
    """
    payload = dict(payload)
    if kind == "attraction" and "required" in payload:
        payload["required"] = False
    if kind == "hotel":
        for field, empty in (("nights", 0), ("distance", "")):
            if field in payload:
                payload[field] = empty
        if payload.get("cost_source") == "llm_estimated":
            payload.update(estimated_cost=0, cost_status="unknown", cost_source="unknown",
                           price_range="", price_evidence_url=None)
    return payload


def _write_index(db, city, kind, identifier, payload):
    searchable, tags = _search_fields(payload)
    db.execute("UPDATE place_catalog SET search_text=?, tags_json=? WHERE city=? AND kind=? AND poi_id=?",
               (searchable, json.dumps(tags, ensure_ascii=False), city, kind, identifier))
    db.execute("DELETE FROM place_catalog_term WHERE city=? AND kind=? AND poi_id=?", (city, kind, identifier))
    db.executemany("INSERT INTO place_catalog_term(city,kind,token,poi_id) VALUES (?,?,?,?)",
                   [(city, kind, term, identifier) for term in sorted(_tokens(searchable))])


def _migrate_index(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(place_catalog)")}
    rebuild = "search_text" not in columns or not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='place_catalog_term'").fetchone()
    if "search_text" not in columns:
        db.execute("ALTER TABLE place_catalog ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
    if "tags_json" not in columns:
        db.execute("ALTER TABLE place_catalog ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    db.executescript(_INDEX_SCHEMA)
    if rebuild:
        # Additive migration indexes historical rows without changing their
        # provider payloads or observation / expiry timestamps.
        for row in db.execute("SELECT city,kind,poi_id,payload_json FROM place_catalog").fetchall():
            _write_index(db, row["city"], row["kind"], row["poi_id"], json.loads(row["payload_json"]))
        db.commit()


@contextmanager
def _connection():
    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store.DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        # Initialization can be reached by parallel city collectors. Serialize
        # migration in this application's supported single-process mode.
        with _SCHEMA_LOCK:
            db.executescript(SCHEMA)
            _migrate_index(db)
        with db:
            yield db
    finally:
        db.close()


def _city(value):
    return value.strip().removesuffix("市")


def _observation_time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("POI observation / expiry time must include timezone")
    return result.astimezone(timezone.utc)


def upsert_places(city, kind, places, *, source="amap", evidence_url=None, ttl_days=7,
                  observed_at=None, expires_at=None, preserve_existing=False):
    """Store real observations; callers must not pass cached reads as new facts.

    Imports retain their original observed_at and TTL. preserve_existing is for
    selected source candidates whose original observation is already catalogued.
    """
    if kind not in KINDS:
        raise ValueError("unsupported place kind")
    now = _observation_time(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    expiry = _observation_time(expires_at) if expires_at is not None else now + timedelta(days=ttl_days)
    if expiry < now:
        raise ValueError("POI expiry cannot precede its observation")
    rows = []
    for place in places:
        payload = place.model_dump(mode="json") if hasattr(place, "model_dump") else dict(place)
        payload = _catalog_payload(kind, payload)
        identifier, name = payload.get("poi_id"), payload.get("name")
        if not identifier or not name or not payload.get("location"):
            continue
        rows.append((_city(city), kind, identifier, name, json.dumps(payload, ensure_ascii=False),
                     source, evidence_url or payload.get("evidence_url"), now.isoformat(), expiry.isoformat()))
    written = 0
    with _connection() as db:
        for row in rows:
            if preserve_existing and db.execute(
                    "SELECT 1 FROM place_catalog WHERE city=? AND kind=? AND poi_id=?", row[:3]).fetchone():
                continue
            result = db.execute("""INSERT INTO place_catalog
            (city,kind,poi_id,name,payload_json,source,evidence_url,observed_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(city,kind,poi_id) DO UPDATE SET name=excluded.name,
            payload_json=excluded.payload_json, source=excluded.source,
            evidence_url=excluded.evidence_url, observed_at=excluded.observed_at,
            expires_at=excluded.expires_at
            WHERE excluded.observed_at>=place_catalog.observed_at""", row)
            if result.rowcount:
                _write_index(db, row[0], kind, row[2], json.loads(row[4]))
                written += 1
    return written


def list_places(city, kind, *, limit=50):
    with _connection() as db:
        rows = db.execute("""SELECT payload_json FROM place_catalog
            WHERE city=? AND kind=? AND expires_at>? ORDER BY observed_at DESC LIMIT ?""",
                          (_city(city), kind, datetime.now(timezone.utc).isoformat(), min(max(limit, 1), 100))).fetchall()
    return [_catalog_payload(kind, json.loads(row[0])) for row in rows]


def get_places_by_ids(city, kind, poi_ids):
    """Read exact fresh observations without a relevance-search result limit.

    Chunk SQL parameters, not the requested IDs: an already selected restaurant
    must not disappear simply because a city contains more than 100 POIs.
    """
    if kind not in KINDS:
        raise ValueError("unsupported place kind")
    identifiers = list(dict.fromkeys(identifier for identifier in poi_ids if identifier))
    if not identifiers:
        return {}
    result = {}
    with _connection() as db:
        now = datetime.now(timezone.utc).isoformat()
        for offset in range(0, len(identifiers), 400):
            chunk = identifiers[offset:offset + 400]
            marks = ",".join("?" for _ in chunk)
            rows = db.execute(f"""SELECT poi_id,payload_json FROM place_catalog
                WHERE city=? AND kind=? AND expires_at>? AND poi_id IN ({marks})""",
                              (_city(city), kind, now, *chunk)).fetchall()
            result.update({row["poi_id"]: _catalog_payload(kind, json.loads(row["payload_json"])) for row in rows})
    return result


def _query_groups(query, kind):
    normalized = _normalize(query)
    if normalized in _GENERIC_QUERIES[kind]:
        return []
    if kind == "attraction" and normalized in _CATEGORY_QUERIES:
        return [_QueryGroup(_CATEGORY_QUERIES[normalized], category_only=True)]
    if kind == "hotel" and normalized in {"快捷酒店", "经济型酒店", "经济酒店"}:
        return [_QueryGroup(("快捷", "经济型", "经济"))]
    groups = []
    for part in normalized.split():
        # A named lookup must not match every row through its generic suffix.
        for suffix in sorted(_GENERIC_QUERIES[kind] - {""}, key=len, reverse=True):
            if part.endswith(suffix) and len(part) - len(suffix) >= 2:
                part = part[:-len(suffix)]
                break
        groups.append(_QueryGroup((part,)))
    return groups


def _match_score(payload, text, groups):
    score = 0.0
    for group in groups:
        match_text = _normalize(" ".join(str(payload.get(key) or "") for key in ("category", "type"))) if group.category_only else text
        best = 0.0
        for query in group.terms:
            if query in match_text:
                best = max(best, 10.0 + (10.0 if not group.category_only and query in _normalize(payload.get("name", "")) else 0.0))
                continue
            # Chinese phrases may be expressed with punctuation or whitespace.
            # Require near-complete bigram coverage; one shared city/category
            # token is not evidence that a named POI was found.
            terms = _tokens(query) - {query}
            if terms:
                coverage = len(terms & _tokens(match_text)) / len(terms)
                if coverage >= 0.8:
                    best = max(best, coverage)
        if not best:
            return 0.0
        score += best
    return score


def matches_query(payload, kind, query):
    """Apply the same source-fact relevance rule to fresh upstream candidates."""
    if kind not in KINDS:
        raise ValueError("unsupported place kind")
    payload = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
    groups = _query_groups(str(query or "").strip()[:200], kind)
    return not groups or bool(_match_score(payload, _search_fields(payload)[0], groups))


def search_places(city, kind, *, query="", limit=50):
    """Fresh scoped lexical retrieval with source metadata kept outside schemas.

    This is lexical retrieval over provider facts, not a claim of vector RAG or
    measured recall. Hard requirements and date-specific prices remain the
    caller's responsibility after retrieval.
    """
    if kind not in KINDS:
        raise ValueError("unsupported place kind")
    city = _city(city)
    query = str(query or "").strip()[:200]
    limit = min(max(int(limit), 1), 100)
    groups = _query_groups(query, kind)
    now = datetime.now(timezone.utc).isoformat()
    with _connection() as db:
        totals = db.execute("""SELECT COUNT(*) AS total,
            COALESCE(SUM(expires_at>?),0) AS fresh FROM place_catalog WHERE city=? AND kind=?""",
                            (now, city, kind)).fetchone()
        if groups:
            terms = sorted(set().union(*(_tokens(word) for group in groups for word in group.terms)))[:400]
            marks = ",".join("?" for _ in terms)
            rows = db.execute(f"""SELECT DISTINCT p.* FROM place_catalog p
                JOIN place_catalog_term t ON p.city=t.city AND p.kind=t.kind AND p.poi_id=t.poi_id
                WHERE p.city=? AND p.kind=? AND p.expires_at>? AND t.token IN ({marks})""",
                              (city, kind, now, *terms)).fetchall() if terms else []
        else:
            rows = db.execute("""SELECT * FROM place_catalog WHERE city=? AND kind=? AND expires_at>?
                ORDER BY observed_at DESC, poi_id DESC LIMIT ?""", (city, kind, now, limit)).fetchall()
    matches = []
    for row in rows:
        payload = _catalog_payload(kind, json.loads(row["payload_json"]))
        score = _match_score(payload, row["search_text"], groups) if groups else 1.0
        if score:
            matches.append((score, row, payload))
    matches.sort(key=lambda entry: (entry[0], entry[1]["observed_at"], entry[1]["poi_id"]), reverse=True)
    selected = matches[:limit]
    report = {
        "city": city, "kind": kind, "query": query, "strategy": "sqlite_lexical",
        "fresh_count": totals["fresh"], "stale_count": totals["total"] - totals["fresh"],
        "matched_count": len(matches) if groups else totals["fresh"], "returned_count": len(selected),
        "records": [{"poi_id": row["poi_id"], "source": row["source"], "evidence_url": row["evidence_url"],
                     "observed_at": row["observed_at"], "expires_at": row["expires_at"],
                     "tags": json.loads(row["tags_json"]), "score": score} for score, row, _ in selected],
    }
    return [payload for _, _, payload in selected], report


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
    selected_hotels = {s.hotel.poi_id for s in plan.stays}
    if plan.hotel:
        selected_hotels.add(plan.hotel.poi_id)
    return {
        "attractions": upsert_places(city, "attraction", [p for p in attractions if p.poi_id in selected_attractions], preserve_existing=True),
        "hotels": upsert_places(city, "hotel", [p for p in hotels if p.poi_id in selected_hotels], preserve_existing=True),
        "restaurants": upsert_places(city, "restaurant", [p for p in restaurants if p.poi_id in selected_restaurants], preserve_existing=True),
    }
