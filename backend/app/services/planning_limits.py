"""Whole-run limits survive budget rounds and journal replay."""
import time
from ..config import get_settings


def available(state):
    cfg = get_settings()
    usage = state.get('usage') or {}
    return (int(state.get('planning_calls') or 0) < cfg.planning_max_calls
            and time.time() - float(state.get('planning_started_at') or time.time()) < cfg.planning_max_seconds
            and usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0) < cfg.planning_max_tokens)


def feasibility(state, selected):
    from .schedule_repair import estimated_minutes, visit_minutes
    from .food_recommendation import nearby_pool, distances
    from ..models.schemas import Restaurant
    from .requirements import restaurant_allowed
    req = state['request']
    food = [Restaurant.model_validate(r) for r in state.get('restaurants', [])]
    food = [r for r in food if restaurant_allowed(r, req.requirements.food)]
    pairs = []
    lunch_options = {}
    for attraction in selected:
        a = attraction.model_dump()
        nearby = nearby_pool(food, a, a, 'lunch', citywide=req.requirements.food.travel_scope == 'citywide')
        nearby = [r for r in nearby if req.requirements.food.travel_scope == 'citywide' or distances(r, a, a)[0] <= 4]
        lunch_options[a.get('poi_id') or a['name']] = [r.poi_id for r in nearby[:8]]
    hotels = state.get('hotels') or [None]
    for hotel in hotels:
        h = hotel.model_dump() if hasattr(hotel, 'model_dump') else hotel
        for attraction in selected:
            a = attraction.model_dump()
            minutes = visit_minutes(a) + 135 + 45
            if h:
                minutes += 2 * estimated_minutes(h, a, req.transportation)
            pairs.append({'hotel_id': (h.get('poi_id') or h.get('name')) if h else None,
                          'attraction_id': a.get('poi_id') or a['name'], 'rough_day_minutes': minutes,
                          'within_time_heuristic': minutes <= req.requirements.pace.max_day_minutes})
    # Bound hint size without hiding candidates from the main prompt pools.
    compact = []
    for a in selected:
        candidates = [p for p in pairs if p['attraction_id'] == (a.poi_id or a.name)]
        compact.extend(sorted(candidates, key=lambda p:p['rough_day_minutes'])[:3])
    return {'basis': '直线交通预估；不是路线工具核验。每景点列出最多3个近酒店组合提示，完整候选另列。单景点加三餐和休息的初筛，组合后还需复核。',
            'pairs': compact, 'lunch_options_by_attraction': lunch_options,
            'daily_minutes_limit': req.requirements.pace.max_day_minutes}
