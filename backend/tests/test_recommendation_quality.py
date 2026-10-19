"""Regression cases for nine identical meals and geographically thin pools."""
from collections import Counter
import copy

from app.models.requirements import FoodNeeds
from app.models.schemas import Location, Restaurant
from app.services import food, itinerary_experience as experience
from app.services.food_recommendation import choose_restaurant, coverage_gaps, meal_contexts
from tests.factories import make_attraction, make_day, make_hotel, make_request


def restaurant(index, *, name=None, lng=120.12, lat=30.22, price=50, cuisine='中餐厅'):
    return Restaurant(name=name or f'本地餐厅{index}', poi_id=f'R{index}',
                      location=Location(longitude=lng, latitude=lat), avg_cost=price,
                      avg_cost_status='estimated', cost_source='amap', cuisine=cuisine)


def day(index=0):
    return make_day(index, f'2026-10-{19+index}', [make_attraction('片区景点', visit_duration=90)])


def test_three_days_have_nine_distinct_nearby_meals_when_pool_supports_it():
    pool = [restaurant(i, name=f'早餐面馆{i}') for i in range(3)] + [restaurant(i) for i in range(3, 12)]
    used = Counter()
    meals = [m for i in range(3) for m in experience._select_meals(day(i), pool, None, used)]
    assert len({m['restaurant_id'] for m in meals}) == 9
    assert all('早餐面馆' in m['name'] for m in meals if m['type'] == 'breakfast')


def test_no_long_detour_just_to_avoid_reusing_a_nearby_place():
    near = restaurant(0)
    far = restaurant(1, lng=120.9)
    selected = choose_restaurant([near, far], 'lunch', day()['attractions'][0], None,
                                 Counter({near.poi_id:3}), {near.poi_id}, FoodNeeds(), None)
    assert selected.poi_id == near.poi_id


def test_missing_price_is_not_treated_as_zero_and_zero_price_is_not_missing():
    paid, unknown, free = restaurant(0, price=60), restaurant(1, price=None), restaurant(2, price=0)
    selected = choose_restaurant([paid, unknown, free], 'lunch', None, None, Counter(), set(), FoodNeeds(), 100)
    assert selected.poi_id == free.poi_id


def test_exhausted_pool_rotates_least_used_candidates_without_inventing_places():
    pool = [restaurant(i) for i in range(3)]
    used = Counter()
    for i in range(3):
        experience._select_meals(day(i), pool, None, used)
    assert sorted(used.values()) == [3,3,3]


def test_partial_edit_preserves_explicit_meals_even_when_reranker_prefers_others():
    pool = [restaurant(i) for i in range(5)]
    original = day()
    original['meals'] = [experience.meal_from_restaurant(pool[0], kind) for kind in ('breakfast','lunch','dinner')]
    assert experience._select_meals(original, pool, None, Counter(), preserve=True) == original['meals']


def test_food_first_preserves_explicit_restaurant_anchors():
    pool = [restaurant(i) for i in range(5)]
    original = day()
    original['meals'] = [experience.meal_from_restaurant(pool[4], kind) for kind in ('breakfast','lunch','dinner')]
    selected = experience._select_meals(original, pool, None, Counter(), needs=FoodNeeds(priority='food_first'))
    assert [m['restaurant_id'] for m in selected] == ['R4'] * 3


def test_hard_food_price_cap_and_exclusions_apply_before_reranking():
    pool = [restaurant(0, price=80), restaurant(1, price=30), restaurant(2, price=20)]
    selected = experience._select_meals(day(), pool, None, Counter(),
                                      needs=FoodNeeds(max_cost_per_meal=40, excluded_names=['本地餐厅2']))
    assert {m['restaurant_id'] for m in selected} == {'R1'}


def test_citywide_permission_can_choose_high_quality_food_outside_neighbourhood():
    local, distant = restaurant(0), restaurant(1, lng=120.9)
    selected = choose_restaurant([local, distant], 'lunch', day()['attractions'][0], None,
                                 Counter({'R0':1}), set(), FoodNeeds(travel_scope='citywide'), None)
    assert selected.poi_id == 'R1'


def test_many_remote_restaurants_do_not_satisfy_neighbourhood_coverage():
    plan = {'days':[day()]}
    assert coverage_gaps(plan, None, [restaurant(i, lng=121+i*.01) for i in range(20)], FoodNeeds())


def test_breakfast_hints_are_ranking_signals_not_invented_opening_hours():
    regular, breakfast = restaurant(0), restaurant(1, name='本地豆浆面馆')
    selected = choose_restaurant([regular, breakfast], 'breakfast', None, None, Counter(), set(), FoodNeeds(), None)
    assert selected.poi_id == breakfast.poi_id
    assert '营业时间需到店前确认' in experience.meal_from_restaurant(selected, 'breakfast')['description']


def test_only_dinner_specific_places_leave_breakfast_unresolved_instead_of_claiming_suitability():
    selected = choose_restaurant([restaurant(0, name='夜宵火锅酒吧')], 'breakfast', None, None,
                                 Counter(), set(), FoodNeeds(), None)
    assert selected is None


def test_same_brand_different_branch_penalty_does_not_overrule_actual_locality():
    selected = choose_restaurant([restaurant(0, name='品牌(甲店)'), restaurant(1, name='品牌(乙店)'), restaurant(2, name='不同菜系')],
                                 'lunch', None, None, Counter({'R0':1}), set(), FoodNeeds(), None)
    assert selected.poi_id == 'R2'


def test_provider_cuisine_preference_breaks_otherwise_equal_nearby_ties():
    ordinary = restaurant(0, cuisine='川菜')
    preferred = restaurant(1, cuisine='广东菜(粤菜)')
    selected = choose_restaurant([ordinary,preferred], 'lunch', None, None, Counter(), set(),
                                 FoodNeeds(keywords=['粤菜']), None)
    assert selected.poi_id == preferred.poi_id


def test_scoped_fill_fixes_local_coverage_and_does_not_repeat_query_on_repair(monkeypatch, settings):
    monkeypatch.setattr(settings, 'food_coverage_enabled', True)
    request = make_request(start_date='2026-10-19', travel_days=1, must_visit=[])
    state = {'request':request,'travel_days':1,'restaurants':[restaurant(0, lng=121)],'hotel':None}
    plan = {'days':[day()]}
    pool = [restaurant(i, name=f'早餐面馆{i}') for i in range(1, 5)] + [restaurant(i) for i in range(5, 10)]
    called = []
    monkeypatch.setattr(food, 'search_restaurants_nearby', lambda city, center, keywords, limit=20: called.append(keywords) or pool)
    calls, warnings = food.ensure_food_coverage(plan, state)
    assert len(called) == 1 and '早餐' == called[0]
    assert calls[0]['args']['center'] == {'longitude':120.12, 'latitude':30.22}
    assert calls[0]['attempt_count'] == 1 and not warnings
    before = copy.deepcopy(state['food_coverage_queries'])
    assert food.ensure_food_coverage(plan, state)[0] == []
    assert state['food_coverage_queries'] == before and len(called) == 1


def test_local_fill_uses_fresh_neighbourhood_candidates_before_any_network(monkeypatch, settings):
    from app.services import catalog
    monkeypatch.setattr(settings, 'food_coverage_enabled', True)
    request = make_request(travel_days=1, must_visit=[])
    pool = [restaurant(i, name=f'早餐面馆{i}') for i in range(3)] + [restaurant(i) for i in range(3, 9)]
    catalog.upsert_places(request.city, 'restaurant', pool)
    state = {'request':request,'travel_days':1,'restaurants':[]}
    monkeypatch.setattr(food, 'search_restaurants_nearby', lambda *a, **k: (_ for _ in ()).throw(AssertionError('local sufficient')))
    assert food.ensure_food_coverage({'days':[day()]}, state) == ([], [])
    assert len(state['restaurants']) == 9


def test_permanent_key_error_stops_fill_across_repair_rounds(monkeypatch, settings):
    monkeypatch.setattr(settings, 'food_coverage_enabled', True)
    state = {'request':make_request(travel_days=1), 'travel_days':1, 'restaurants':[]}
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise food.map_gateway.AmapError('AMAP_KEY_INVALID', 'invalid key')
    monkeypatch.setattr(food, 'search_restaurants_nearby', fail)
    plan = {'days':[day()]}
    food.ensure_food_coverage(plan, state)
    food.ensure_food_coverage(plan, state)
    assert len(calls) == 1 and state['food_coverage_blocked_code'] == 'AMAP_KEY_INVALID'


def test_query_limit_is_shared_across_repair_rounds(monkeypatch, settings):
    monkeypatch.setattr(settings, 'food_coverage_enabled', True)
    monkeypatch.setattr(settings, 'food_coverage_query_limit', 2)
    state = {'request':make_request(), 'travel_days':3, 'restaurants':[]}
    calls = []
    monkeypatch.setattr(food, 'search_restaurants_nearby', lambda *a, **kw: calls.append(1) or [])
    plan = {'days':[day(i) for i in range(3)]}
    food.ensure_food_coverage(plan, state)
    food.ensure_food_coverage(plan, state)
    assert len(calls) == 2 and len(state['food_coverage_queries']) == 2


def test_exhausted_time_budget_performs_no_more_queries(monkeypatch, settings):
    monkeypatch.setattr(settings, 'food_coverage_enabled', True)
    state = {'request':make_request(), 'travel_days':3, 'restaurants':[],
             'food_coverage_elapsed_seconds':settings.food_coverage_budget_seconds}
    monkeypatch.setattr(food, 'search_restaurants_nearby', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('budget exhausted')))
    assert food.ensure_food_coverage({'days':[day()]}, state)[0] == []


def test_breakfast_origin_is_hotel_and_lunch_target_follows_visit_duration():
    original = day()
    original['attractions'] += [make_attraction('第二景点', visit_duration=180).model_dump()]
    hotel = make_hotel()
    contexts = meal_contexts(original, hotel)
    assert contexts[0][1] is hotel
    assert contexts[1][1]['name'] == '片区景点' and contexts[1][2]['name'] == '第二景点'


def test_comfort_pruning_reselects_food_around_surviving_attractions(monkeypatch):
    anchor = make_attraction('必去片区', required=True, visit_duration=90)
    removed = make_attraction('太满的远处可选景点', lng=120.18, visit_duration=480)
    pool = [restaurant(i) for i in range(3)] + [restaurant(9, lng=120.18)]
    state = {'request':make_request(start_date='2026-10-19',travel_days=1,nights=0,must_visit=['必去片区']),
             'travel_days':1,'nights':0,'hotel':None,'restaurants':pool,
             'attractions':[anchor,removed],'must_visit_resolved':[anchor],
             'experience_enabled':True,'route_cache':{},'weather':[]}
    monkeypatch.setattr(experience.amap,'travel_route',lambda *a, **kw:
                        {'duration_minutes':5,'distance_meters':500,'status':'known','source':'amap'})
    updated, _, _ = experience.enrich_experience({'days':[make_day(0,'2026-10-19',[anchor,removed])]},state)
    assert [a['name'] for a in updated['days'][0]['attractions']] == ['必去片区']
    assert all(m['restaurant_id'] != 'R9' for m in updated['days'][0]['meals'])
