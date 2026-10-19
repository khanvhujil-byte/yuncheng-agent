"""Local retrieval correctness: evidence scope, freshness and source preservation."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.services import catalog, store


def place(identifier, name, **fields):
    return {"poi_id": identifier, "name": name,
            "location": {"longitude": 120.15, "latitude": 30.25}, **fields}


def row(identifier):
    with sqlite3.connect(store.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM place_catalog WHERE poi_id=?", (identifier,)).fetchone())


def test_named_lookup_matches_provider_name_and_isolates_city_kind():
    catalog.upsert_places("杭州市", "attraction", [place("hz", "西湖风景名胜区"), place("other", "灵隐寺")])
    catalog.upsert_places("北京", "attraction", [place("bj", "西湖公园")])
    catalog.upsert_places("杭州", "hotel", [place("hotel", "西湖酒店")])
    results, report = catalog.search_places("杭州", "attraction", query="西湖景点")
    assert [p["poi_id"] for p in results] == ["hz"]
    assert report["fresh_count"] == 2 and report["matched_count"] == 1
    assert report["strategy"] == "sqlite_lexical"
    assert catalog.search_places("杭州", "attraction", query="故宫")[0] == []


def test_factual_cuisine_and_address_can_be_combined_but_not_hallucinated_description():
    catalog.upsert_places("杭州", "restaurant", [
        place("near", "山外山", cuisine="杭帮菜", address="西湖区玉泉路"),
        place("far", "江边小馆", cuisine="杭帮菜", address="萧山区"),
        place("fake", "普通餐厅", description="西湖区杭帮菜，特别适合老人，无台阶"),
    ])
    results, report = catalog.search_places("杭州", "restaurant", query="西湖 杭帮菜")
    assert [p["poi_id"] for p in results] == ["near"]
    assert report["records"][0]["tags"] == ["杭帮菜"]
    assert catalog.search_places("杭州", "restaurant", query="适合老人")[0] == []


def test_default_nature_preference_matches_prewarmed_provider_categories_only():
    catalog.upsert_places("杭州", "attraction", [
        place("scenic", "西湖", category="风景名胜", typecode="110000"),
        place("park", "湖滨", category="公园广场", typecode="110101"),
        place("mall", "自然风光公园风景名胜商场", category="购物中心", typecode="060101"),
        place("cafe", "湖泊公园咖啡厅", category="餐饮服务", typecode="050100"),
    ])
    results, _ = catalog.search_places("杭州", "attraction", query="自然风光")
    assert {p["poi_id"] for p in results} == {"scenic", "park"}
    assert catalog.matches_query(place("x", "西湖", category="风景名胜"), "attraction", "自然风光")
    assert not catalog.matches_query(place("x", "公园商店", category="购物中心"), "attraction", "自然风光")


@pytest.mark.parametrize("query", ["历史人文", "人文历史"])
def test_history_preference_expands_only_into_factual_categories(query):
    catalog.upsert_places("杭州", "attraction", [
        place("museum", "浙江省馆", category="博物馆"),
        place("temple", "灵隐寺", category="寺庙道观"),
        place("shop", "历史人文博物馆纪念馆商店", category="购物服务"),
        place("park", "湖滨公园", category="公园"),
    ])
    assert {p["poi_id"] for p in catalog.search_places("杭州", "attraction", query=query)[0]} == {"museum", "temple"}


def test_generic_browse_and_quick_hotel_filter_use_source_types():
    catalog.upsert_places("杭州", "hotel", [
        place("budget", "城南客栈", type="经济型酒店"),
        place("quick", "如家快捷酒店"),
        place("luxury", "豪华度假酒店", type="五星级宾馆"),
    ])
    assert len(catalog.search_places("杭州", "hotel", query="酒店")[0]) == 3
    assert {p["poi_id"] for p in catalog.search_places("杭州", "hotel", query="快捷酒店")[0]} == {"budget", "quick"}
    catalog.upsert_places("杭州", "restaurant", [place("food", "真餐厅")])
    assert len(catalog.search_places("杭州", "restaurant", query="当地特色美食")[0]) == 1


def test_online_candidate_relevance_uses_same_rules_as_local_retrieval():
    candidate = place("food", "玉泉小馆", cuisine="杭帮菜", address="西湖区")
    catalog.upsert_places("杭州", "restaurant", [candidate])
    for query in ("玉泉", "杭帮菜", "西湖 杭帮菜", "当地特色美食", "川菜", "不存在餐厅"):
        assert catalog.matches_query(candidate, "restaurant", query) == bool(
            catalog.search_places("杭州", "restaurant", query=query)[0])


def test_historical_import_stays_expired_and_metadata_retains_source_times():
    past = datetime.now(timezone.utc) - timedelta(days=40)
    catalog.upsert_places("杭州", "attraction", [place("old", "过期景点")], observed_at=past, ttl_days=7,
                          source="licensed_import", evidence_url="https://source.example/old")
    observed = datetime.now(timezone.utc) - timedelta(hours=1)
    expires = observed + timedelta(days=2)
    catalog.upsert_places("杭州", "attraction", [place("fresh", "新景点", category="公园")],
                          observed_at=observed.isoformat(), expires_at=expires.isoformat(),
                          source="amap", evidence_url="https://source.example/fresh")
    results, report = catalog.search_places("杭州", "attraction")
    assert [p["poi_id"] for p in results] == ["fresh"]
    assert report["stale_count"] == 1
    assert catalog.list_places("杭州", "attraction") == results
    evidence = report["records"][0]
    assert evidence["observed_at"] == observed.isoformat()
    assert evidence["expires_at"] == expires.isoformat()
    assert evidence["evidence_url"] == "https://source.example/fresh"
    assert evidence["tags"] == ["公园"]


def test_new_observation_replaces_index_but_older_import_cannot_undo_it():
    now = datetime.now(timezone.utc)
    catalog.upsert_places("杭州", "restaurant", [place("one", "旧店名", cuisine="川菜")], observed_at=now)
    catalog.upsert_places("杭州", "restaurant", [place("one", "新店名", cuisine="杭帮菜")],
                          observed_at=now + timedelta(seconds=1))
    assert catalog.upsert_places("杭州", "restaurant", [place("one", "旧店名", cuisine="川菜")],
                                 observed_at=now - timedelta(days=1)) == 0
    assert catalog.search_places("杭州", "restaurant", query="旧店名")[0] == []
    assert catalog.search_places("杭州", "restaurant", query="新店名")[0][0]["name"] == "新店名"
    assert catalog.search_places("杭州", "restaurant", query="川菜")[0] == []


def test_legacy_schema_migrates_once_without_refresh_or_duplicate_index_rows():
    observed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    original = place("legacy", "灵隐寺", category="寺庙", description="适合老人")
    with sqlite3.connect(store.DB_PATH) as db:
        db.execute("""CREATE TABLE place_catalog (
            city TEXT NOT NULL, kind TEXT NOT NULL, poi_id TEXT NOT NULL,
            name TEXT NOT NULL, payload_json TEXT NOT NULL, source TEXT NOT NULL,
            evidence_url TEXT, observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            PRIMARY KEY(city,kind,poi_id))""")
        db.execute("INSERT INTO place_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                   ("杭州", "attraction", "legacy", "灵隐寺", json.dumps(original), "amap", None, observed, expires))
    for _ in range(3):
        assert catalog.search_places("杭州", "attraction", query="灵隐寺")[0] == [original]
    saved = row("legacy")
    assert saved["observed_at"] == observed and saved["expires_at"] == expires
    assert "适合老人" not in saved["search_text"]
    with sqlite3.connect(store.DB_PATH) as db:
        count, unique = db.execute("SELECT COUNT(*), COUNT(DISTINCT token) FROM place_catalog_term").fetchone()
        assert count == unique
        assert any(r[1] == "idx_place_catalog_scope" for r in db.execute("PRAGMA index_list(place_catalog)"))


def test_persisting_selected_cached_candidate_never_renews_or_rewrites_it():
    observed = datetime.now(timezone.utc) - timedelta(days=20)
    candidate = place("one", "原始餐厅", cuisine="杭帮菜")
    catalog.upsert_places("杭州", "restaurant", [candidate], observed_at=observed, ttl_days=7,
                          source="imported", evidence_url="https://source.example/one")
    before = row("one")
    plan = {"city": "杭州", "start_date": "2026-10-18", "end_date": "2026-10-18", "days": [
        {"date": "2026-10-18", "day_index": 0, "attractions": [],
         "meals": [{"restaurant_id": "one", "name": "模型伪造名称", "type": "lunch"}]}]}
    selected = {**candidate, "name": "被后续步骤改变", "avg_cost": 0}
    for _ in range(2):
        assert catalog.persist_plan(plan, "杭州", restaurants=[selected])["restaurants"] == 0
    assert row("one") == before
    assert catalog.search_places("杭州", "restaurant")[0] == []


def test_catalog_never_carries_one_trips_required_anchor_into_another():
    from app.models.schemas import Attraction

    anchor = Attraction.model_validate(place("anchor", "用户甲指定景点", required=True))
    plan = {"city": "杭州", "start_date": "2026-10-18", "end_date": "2026-10-18", "days": [
        {"date": "2026-10-18", "day_index": 0, "attractions": [anchor.model_dump()], "meals": []}]}
    assert catalog.persist_plan(plan, "杭州", attractions=[anchor])["attractions"] == 1
    assert anchor.required  # Never mutate the original trip's actual anchor.
    assert json.loads(row("anchor")["payload_json"])["required"] is False
    assert catalog.list_places("杭州", "attraction")[0]["required"] is False
    assert catalog.search_places("杭州", "attraction", query="用户甲指定景点")[0][0]["required"] is False


def test_legacy_anchor_flags_are_sanitized_without_changing_source_observation():
    legacy = place("legacy-anchor", "历史必去景点", required=True)
    catalog.upsert_places("杭州", "attraction", [legacy])
    with sqlite3.connect(store.DB_PATH) as db:
        # Simulate a pre-fix source row, including one whose index already exists.
        db.execute("UPDATE place_catalog SET payload_json=? WHERE poi_id=?",
                   (json.dumps(legacy), legacy["poi_id"]))
    before = row(legacy["poi_id"])
    assert catalog.list_places("杭州", "attraction")[0]["required"] is False
    assert catalog.search_places("杭州", "attraction", query="历史必去景点")[0][0]["required"] is False
    assert row(legacy["poi_id"])["observed_at"] == before["observed_at"]
    assert row(legacy["poi_id"])["expires_at"] == before["expires_at"]


def test_hotel_catalog_preserves_identity_and_imports_but_not_trip_derived_state():
    from app.models.schemas import Hotel

    hotel = Hotel.model_validate(place("hotel", "真实酒店", nights=5, distance="距上个行程3公里",
                                       estimated_cost=350, cost_status="estimated", cost_source="llm_estimated"))
    catalog.upsert_places("杭州", "hotel", [hotel])
    stored = catalog.list_places("杭州", "hotel")[0]
    assert stored["name"] == hotel.name and stored["location"] == hotel.location.model_dump()
    assert stored["nights"] == 0 and stored["distance"] == ""
    assert stored["estimated_cost"] == 0 and stored["cost_status"] == "unknown"
    assert hotel.nights == 5 and hotel.estimated_cost == 350
    sourced = hotel.model_copy(update={"cost_source": "imported", "cost_status": "known",
                                       "price_evidence_url": "https://source.example/quote"})
    catalog.upsert_places("杭州", "hotel", [sourced])
    assert catalog.list_places("杭州", "hotel")[0]["estimated_cost"] == 350


def test_repeat_observation_deduplicates_results_and_preserves_branch_identity():
    first = place("branch-a", "同名饭店", address="西湖区")
    second = place("branch-b", "同名饭店", address="上城区")
    catalog.upsert_places("杭州", "restaurant", [first, first, second])
    results, report = catalog.search_places("杭州", "restaurant", query="同名饭店", limit=1)
    assert len(results) == 1 and report["matched_count"] == 2
    assert len(catalog.search_places("杭州", "restaurant", query="同名饭店")[0]) == 2


@pytest.mark.parametrize("kwargs", [
    {"observed_at": "2026-10-18T00:00:00"},
    {"observed_at": "2026-10-18T00:00:00+00:00", "expires_at": "2026-10-17T00:00:00+00:00"},
])
def test_invalid_observation_bounds_rejected(kwargs):
    with pytest.raises(ValueError):
        catalog.upsert_places("杭州", "attraction", [place("bad", "坏数据")], **kwargs)
