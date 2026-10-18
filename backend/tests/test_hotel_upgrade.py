"""Deterministic hotel identity, hard locking, zero-night costs and provenance."""
import pytest

from app.agents import city, hotel_agent
from app.agents.budget import patch_budget
from app.models.schemas import ConstraintReport
from app.services import planning
from tests.factories import make_attraction, make_day, make_hotel, make_request


@pytest.fixture
def fixed_hotel_search(monkeypatch):
    monkeypatch.setattr(city, 'geocode', lambda *a, **k: (make_attraction('center').location, '330100'))
    monkeypatch.setattr(city.java_bridge, 'fetch_hot', lambda: None)
    def install(hits):
        monkeypatch.setattr(city, 'search_poi', lambda *a, **k: hits)
    return install


def test_hotel_address_disambiguation_and_immutable_selection(fixed_hotel_search, monkeypatch):
    candidates = [make_attraction('如家酒店', poi_id='branch-a', typecode='100100', address='甲路1号'),
                  make_attraction('如家酒店', poi_id='branch-b', typecode='100100', address='乙路2号')]
    fixed_hotel_search(candidates)
    req = make_request(must_visit=[], fixed_hotel='如家酒店', fixed_hotel_address='乙路2号')
    state = planning._initial_state(req)
    state.update(city.validate_city(state))
    assert state['hotel_anchor'].poi_id == 'branch-b'
    monkeypatch.setattr(hotel_agent, 'run_agent', lambda *a, **k: pytest.fail('fixed hotel must not invoke a model'))
    selected = hotel_agent.resolve_hotel(state)
    assert selected['hotel'].poi_id == 'branch-b' and selected['sources']['hotel'] == 'user'
    assert selected['constraint_report'].hotel_source == 'user'


@pytest.mark.parametrize(('hits', 'code'), [([], 'HOTEL_NOT_FOUND'),
    ([make_attraction('如家酒店', poi_id='a', typecode='100100'), make_attraction('如家酒店', poi_id='b', typecode='100100')], 'HOTEL_AMBIGUOUS'),
    ([make_attraction('如家酒店', typecode='050100')], 'HOTEL_NOT_FOUND'),
    ([make_attraction('如家酒店', typecode='100100', lng=100, lat=20)], 'CITY_MISMATCH')])
def test_fixed_hotel_errors_are_reported_before_planning(fixed_hotel_search, hits, code):
    fixed_hotel_search(hits)
    result = city.validate_city(planning._initial_state(make_request(must_visit=[], fixed_hotel='如家酒店')))
    assert result['error_code'] == code


def test_no_accommodation_is_not_a_failure_or_unknown_cost(monkeypatch):
    req = make_request(travel_days=1, nights=0, must_visit=[])
    state = planning._initial_state(req)
    monkeypatch.setattr(hotel_agent, 'run_agent', lambda *a, **k: pytest.fail('zero-night must not invoke model'))
    selected = hotel_agent.resolve_hotel(state)
    assert selected['hotel'] is None and selected['sources'] == {}
    state.update(selected)
    state['plan'] = {'days': [make_day(0, req.start_date, [make_attraction('西湖')])]}
    patched = patch_budget(state)
    hotel_items = [i for i in patched['plan']['budget']['items'] if i['category'] == 'hotel']
    assert hotel_items
    assert all(i['status'] == 'known' and i['amount'] == 0 for i in hotel_items)
