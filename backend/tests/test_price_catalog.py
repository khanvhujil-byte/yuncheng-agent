from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models.schemas import Attraction, Hotel, Location, Restaurant
from app.services import price_catalog


def observation(**updates):
    now = datetime.now(timezone.utc)
    result = {"city": "杭州", "kind": "attraction", "poi_id": "B123", "amount": 80,
              "unit": "admission", "status": "known", "source_url": "https://official.example/tickets",
              "observed_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
              "service_from": "2026-11-01", "service_to": "2026-11-05"}
    result.update(updates)
    return result


def attraction(identifier="B123", **updates):
    return Attraction(name="真实景点", poi_id=identifier, location=Location(longitude=120, latitude=30), **updates)


def test_import_price_applies_only_exact_id_city_date():
    price_catalog.import_prices([observation()])
    candidate = attraction()
    priced = price_catalog.apply_prices("杭州", "attraction", [candidate, attraction("OTHER")],
                                        service_date="2026-11-02", travel_days=3)
    assert priced[0].ticket_price == 80 and priced[0].ticket_price_status == "known"
    assert priced[0].ticket_price_source == "imported"
    assert priced[1].ticket_price is None and candidate.ticket_price is None
    assert price_catalog.apply_prices("北京", "attraction", [candidate], service_date="2026-11-02")[0].ticket_price is None
    assert price_catalog.apply_prices("杭州", "attraction", [candidate], service_date="2026-11-05", travel_days=2)[0].ticket_price is None


def test_zero_admission_is_real_free_and_not_unknown():
    price_catalog.import_prices([observation(amount=0)])
    item = price_catalog.apply_prices("杭州", "attraction", [attraction()], service_date="2026-11-02")[0]
    assert item.ticket_price == 0 and item.ticket_price_status == "known"


def test_hotel_observation_covers_stay_nights_not_checkout():
    price_catalog.import_prices([observation(kind="hotel", unit="night", amount=350, service_to="2026-11-02")])
    hotel = Hotel(name="真实酒店", poi_id="B123")
    priced = price_catalog.apply_prices("杭州", "hotel", [hotel], service_date="2026-11-01", nights=2)[0]
    assert priced.estimated_cost == 350 and priced.cost_status == "known" and priced.cost_source == "imported"
    assert price_catalog.apply_prices("杭州", "hotel", [hotel], service_date="2026-11-01", nights=3)[0].estimated_cost == 0


def test_restaurant_average_is_always_estimated():
    price_catalog.import_prices([observation(kind="restaurant", unit="person_meal", amount=65, status="estimated",
                                             service_from=None, service_to=None)])
    item = Restaurant(name="真实餐厅", poi_id="B123", location=Location(longitude=120, latitude=30))
    priced = price_catalog.apply_prices("杭州", "restaurant", [item])[0]
    assert priced.avg_cost == 65 and priced.avg_cost_status == "estimated" and priced.cost_source == "imported"


def test_expired_observation_does_not_survive_in_cached_entity():
    now = datetime.now(timezone.utc)
    price_catalog.import_prices([observation(observed_at=(now - timedelta(days=3)).isoformat(),
                                             expires_at=(now - timedelta(days=1)).isoformat())])
    item = attraction(ticket_price=80, ticket_price_source="imported", ticket_price_status="known")
    updated = price_catalog.apply_prices("杭州", "attraction", [item], service_date="2026-11-02")[0]
    assert updated.ticket_price is None and updated.ticket_price_status == "unknown"


@pytest.mark.parametrize("changes", [{"amount": True}, {"amount": -1}, {"amount": "80"},
                                      {"kind": "hotel"}, {"source_url": "http://example.test"},
                                      {"service_from": None, "service_to": None}, {"service_to": "2026-10-01"},
                                      {"observed_at": "2026-01-01T00:00:00"},
                                      {"unexpected": "field"}, {"kind": "restaurant", "unit": "person_meal"}])
def test_import_rejects_invalid_or_unscoped_price(changes):
    with pytest.raises(ValidationError):
        price_catalog.import_prices([observation(**changes)])


def test_import_batch_is_atomic_on_invalid_entry():
    with pytest.raises(ValidationError):
        price_catalog.import_prices([observation(), observation(amount=-1)])
    assert price_catalog.apply_prices("杭州", "attraction", [attraction()], service_date="2026-11-02")[0].ticket_price is None
