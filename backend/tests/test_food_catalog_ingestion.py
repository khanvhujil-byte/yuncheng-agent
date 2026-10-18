import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import Location, Restaurant
from app.services import amap, catalog, food, ingestion, map_gateway


def restaurant(identifier="B123", **updates):
    return Restaurant(name="真餐馆", poi_id=identifier, location=Location(longitude=120, latitude=30), **updates)


def ranking_html(city="杭州", name="真餐馆", identifier="B123", position=2):
    data = {"@type": "ItemList", "name": f"{city}市2025高德状元榜·美食", "itemListElement": [
        {"position": position, "item": {"name": name}}]}
    return f'<script type="application/ld+json">{json.dumps(data,ensure_ascii=False)}</script><a class="poi-card" href="/place/{identifier}"><div class="poi-name">{name}</div></a>'


def test_ranking_parser_preserves_source_list_position():
    result = ingestion.parse_ranking(ranking_html(), city="杭州")
    assert result["ranking_name"] == "杭州市2025高德状元榜·美食"
    assert result["records"] == [{"name": "真餐馆", "poi_id": "B123", "rank": 2}]


@pytest.mark.parametrize("html", [ranking_html(city="北京"), ranking_html(identifier=""), ranking_html(position=True),
                                    ranking_html().replace('"@type": "ItemList"', '"@type": "Thing"')])
def test_ranking_rejects_unverifiable_entries(html):
    with pytest.raises(ValueError):
        ingestion.parse_ranking(html, city="杭州")


@pytest.mark.parametrize("url", ["http://www.amap.com/ranking/hangzhou", "https://evil.test/ranking/hangzhou",
                                   "https://www.amap.com/service/poiInfo", "https://user@www.amap.com/ranking/hangzhou",
                                   "https://www.amap.com/ranking/hangzhou?url=http://127.0.0.1", "https://www.amap.com:444/ranking/hangzhou",
                                   "https://www.amap.com/ranking/../service/foo"])
def test_crawler_rejects_untrusted_url(url):
    with pytest.raises(ValueError):
        ingestion.validate_url(url)


def test_crawler_obeys_robots_and_keeps_provenance():
    calls = []

    def fetch(url, *, interval):
        calls.append((url, interval))
        return 'User-agent: *\nAllow: /ranking/\nCrawl-delay: 8' if url.endswith('robots.txt') else ranking_html()

    result = ingestion.crawl_ranking("https://www.amap.com/ranking/hangzhou/food", city="杭州", fetch=fetch)
    assert result["stored"] == 1
    assert calls[1][1] == 8
    assert catalog.rankings("杭州市")[0]["source_url"] == result["source_url"]


def test_crawler_does_not_fetch_disallowed_page():
    calls = []

    def fetch(url, *, interval):
        calls.append(url)
        return "User-agent: *\nDisallow: /"

    with pytest.raises(ValueError, match="robots"):
        ingestion.crawl_ranking("https://www.amap.com/ranking/hangzhou/food", city="杭州", fetch=fetch)
    assert len(calls) == 1
    assert catalog.rankings("杭州") == []


def test_import_old_data_does_not_refresh_it():
    document = {"city": "杭州", "ranking_name": "杭州美食榜", "source_url": "https://www.amap.com/ranking/hangzhou/food",
                "captured_at": (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
                "records": [{"name": "老店", "poi_id": "B123", "rank": 1}]}
    assert ingestion.import_ranking(document) == 1
    assert catalog.rankings("杭州") == []


def test_catalog_upserts_and_isolates_city_and_kind():
    assert catalog.upsert_places("杭州", "restaurant", [restaurant(), restaurant()]) == 2
    catalog.upsert_places("北京", "restaurant", [restaurant()])
    assert len(catalog.list_places("杭州市", "restaurant")) == 1
    assert catalog.list_places("杭州", "hotel") == []
    assert catalog.list_places("苏州", "restaurant") == []


def test_persist_plan_copies_verified_candidate_not_model_rewrite():
    candidate = restaurant()
    generated = {"restaurant_id": candidate.poi_id, "name": "模型篡改店名", "type": "lunch", "estimated_cost": 0}
    plan = {"city": "杭州", "start_date": "2026-10-18", "end_date": "2026-10-18",
            "days": [{"date": "2026-10-18", "day_index": 0, "attractions": [],
                      "meals": [generated, {"restaurant_id": "fake", "name": "幻觉餐馆", "type": "dinner"}]}]}
    assert catalog.persist_plan(plan, "杭州", restaurants=[candidate])["restaurants"] == 1
    assert catalog.list_places("杭州", "restaurant")[0]["name"] == candidate.name


def test_food_ranking_requires_exact_branch_id(monkeypatch):
    catalog.save_rankings("杭州", [{"poi_id": "B123", "name": "同名店", "rank": 1}],
                          source_url="https://www.amap.com/ranking/hangzhou/food", ranking_name="杭州美食榜")
    monkeypatch.setattr(food, "search_restaurants", lambda *a, **k: [restaurant("OTHER")])
    result = food.collect_food({"request": SimpleNamespace(city="杭州"), "travel_days": 1, "experience_enabled": True})
    assert result["restaurants"][0].ranking_rank is None
    assert "普通高德" in "".join(result["warnings"])
    assert result["tool_calls"][0]["outcome"]["status"] == "ok"


def test_food_valid_ranking_survives_real_id_match(monkeypatch):
    catalog.save_rankings("杭州", [{"poi_id": "B123", "name": "真餐馆", "rank": 3}],
                          source_url="https://www.amap.com/ranking/hangzhou/food", ranking_name="杭州美食榜")
    monkeypatch.setattr(food, "search_restaurants", lambda *a, **k: [restaurant()])
    result = food.collect_food({"request": SimpleNamespace(city="杭州"), "travel_days": 1, "experience_enabled": True})
    assert result["restaurants"][0].ranking_rank == 3
    assert result["restaurants"][0].avg_cost is None


def test_food_nonretryable_key_error_does_not_repeat(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise amap.AmapError("AMAP_KEY_INVALID", "invalid")

    monkeypatch.setattr(food, "search_restaurants", fail)
    result = food.collect_food({"request": SimpleNamespace(city="杭州"), "travel_days": 1, "experience_enabled": True})
    assert len(calls) == 1
    assert "AMAP_KEY_INVALID" in result["errors"][0]


def test_restaurant_adapter_filters_kind_and_keeps_unknown_cost(monkeypatch):
    monkeypatch.setattr(amap, "_get", lambda *a, **k: {"pois": [
        {"id": "B1", "name": "餐馆", "location": "120,30", "typecode": "050100", "biz_ext": {"cost": []}},
        {"id": "B2", "name": "美容店", "location": "120,30", "typecode": "071100"},
        {"id": "B3", "name": "餐馆2", "location": "120,30", "typecode": "050100", "biz_ext": {"cost": "88.5"}},
    ]})
    rows = amap.search_restaurants("美食", "杭州")
    assert len(rows) == 2
    assert rows[0].avg_cost is None and rows[0].avg_cost_status == "unknown"
    assert rows[1].avg_cost == 88 and rows[1].avg_cost_status == "estimated"


@pytest.mark.parametrize("mode,path", [("步行", "/walking"), ("自驾", "/driving"), ("公共交通", "/transit/integrated")])
def test_route_uses_mode_specific_duration(monkeypatch, mode, path):
    calls = []

    def get(url, params):
        calls.append(url)
        return {"route": {"paths": [{"duration": "601", "distance": "1200"}],
                          "transits": [{"duration": "901", "distance": "2000"}]}}

    monkeypatch.setattr(amap, "_get", get)
    result = amap.travel_route(Location(longitude=120, latitude=30), Location(longitude=120.1, latitude=30), mode, "杭州")
    assert calls[0].endswith(path)
    assert result["duration_minutes"] == (16 if mode == "公共交通" else 11)


def test_route_missing_or_invalid_data_is_unknown(monkeypatch):
    monkeypatch.setattr(amap, "_get", lambda *a, **k: {"route": {"paths": [{"duration": "NaN", "distance": "8"}]}})
    result = amap.travel_route(Location(longitude=120, latitude=30), Location(longitude=121, latitude=30))
    assert result["status"] == "unknown" and result["duration_minutes"] is None


def test_mcp_route_rejects_false_known_or_nonfinite(monkeypatch, settings):
    monkeypatch.setattr(settings, "map_backend", "mcp")
    monkeypatch.setattr(map_gateway, "_call", lambda *a: {"status": "known", "source": "amap", "duration_minutes": None, "distance_meters": 2})
    with pytest.raises(amap.AmapUnreachable):
        map_gateway.travel_route(Location(longitude=120, latitude=30), Location(longitude=121, latitude=30))


def test_public_fetch_caps_bytes_and_refuses_private_resolution(monkeypatch):
    monkeypatch.setattr(ingestion.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="公开地址"):
        ingestion.fetch_public("https://www.amap.com/ranking/hangzhou/food")


def test_public_fetch_429_respects_retry_after_and_attempt_bound(monkeypatch):
    calls, waits = [], []

    class Response:
        status_code = 429
        headers = {"Retry-After": "7"}
        is_redirect = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            raise ValueError("rate limited")

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, *args):
            calls.append(args)
            return Response()

    monkeypatch.setattr(ingestion, "_public_host", lambda: None)
    monkeypatch.setattr(ingestion.httpx, "Client", Client)
    monkeypatch.setattr(ingestion, "crawl_gate", SimpleNamespace(acquire=lambda **k: None, defer=waits.append))
    with pytest.raises(ValueError, match="rate limited"):
        ingestion.fetch_public("https://www.amap.com/ranking/hangzhou/food")
    assert len(calls) == 2 and waits == [7]


def test_public_fetch_stops_stream_at_content_cap(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        is_redirect = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"x" * (ingestion.MAX_BYTES + 1)
            raise AssertionError("should stop after first oversized chunk")

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, *args):
            return Response()

    monkeypatch.setattr(ingestion, "_public_host", lambda: None)
    monkeypatch.setattr(ingestion.httpx, "Client", Client)
    monkeypatch.setattr(ingestion, "crawl_gate", SimpleNamespace(acquire=lambda **k: None))
    with pytest.raises(ValueError, match="1MB"):
        ingestion.fetch_public("https://www.amap.com/ranking/hangzhou/food")
