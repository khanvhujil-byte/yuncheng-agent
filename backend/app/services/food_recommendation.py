"""Geographic candidate gating followed by deterministic diversity reranking.

Inspired by MMR's relevance/novelty trade-off, without embeddings, learned user
profiles or claims of measured recommendation quality. Provider facts determine
identity and location; name/category hints never prove opening hours.
"""
import re

from ..models.schemas import Location
from .schedule_repair import visit_minutes
from .tools import haversine_km

BREAKFAST_HINTS = ('早餐', '早茶', '早饭', '豆浆', '包子', '包点', '肠粉', '粥', '面馆', '面店', '面食', '小吃', '茶餐厅')
DINNER_ONLY_HINTS = ('火锅', '烤肉', '烧烤', '酒吧', '夜宵', '海鲜酒楼')
NEARBY_KM = 4.0
DETOUR_SLACK_KM = 1.5


def location(item):
    item = item.model_dump() if hasattr(item, 'model_dump') else item
    raw = (item or {}).get('location')
    return Location.model_validate(raw) if raw else None


def meal_contexts(day, hotel):
    spots = day.get('attractions') or []
    count, minutes = 0, 0
    for spot in spots:
        if minutes + visit_minutes(spot) > 180:
            break
        count += 1
        minutes += visit_minutes(spot)
    contexts = []
    for kind, index in (('breakfast', 0), ('lunch', max(0, count - 1)), ('dinner', -1)):
        anchor = hotel if kind == 'breakfast' and location(hotel) else spots[index] if spots else hotel
        following = (spots[count] if kind == 'lunch' and count < len(spots)
                     else hotel if kind == 'dinner' else spots[0] if kind == 'breakfast' and spots else None)
        contexts.append((kind, anchor, following))
    return contexts


def breakfast_hint(restaurant):
    text = restaurant.name + (restaurant.cuisine or '')
    return any(word in text for word in BREAKFAST_HINTS)


def suitability(restaurant, kind):
    text = restaurant.name + (restaurant.cuisine or '')
    if kind == 'breakfast':
        return 0 if breakfast_hint(restaurant) else 2 if any(w in text for w in DINNER_ONLY_HINTS) else 1
    return 1 if any(w in text for w in ('早餐', '早饭', '豆浆', '包子', '包点')) else 0


def distances(restaurant, anchor, following):
    start, end = location(anchor), location(following)
    distance = haversine_km(start, restaurant.location) if start else 0
    detour = (max(0, distance + haversine_km(restaurant.location, end) - haversine_km(start, end))
              if start and end else distance)
    return distance, detour


def nearby_pool(restaurants, anchor, following, kind, *, citywide=False):
    if not restaurants:
        return []
    if citywide:
        candidates = list(restaurants)
    else:
        metrics = {r.poi_id: distances(r, anchor, following) for r in restaurants}
        best = min(value[1] for value in metrics.values())
        candidates = [r for r in restaurants if metrics[r.poi_id][0] <= NEARBY_KM
                      and metrics[r.poi_id][1] <= min(3.0, best + DETOUR_SLACK_KM)]
        if not candidates:
            # Do not invent a local restaurant, nor add a large detour purely to
            # make all identities distinct. The caller reports this shortage.
            candidates = sorted(restaurants, key=lambda r: (*metrics[r.poi_id][::-1], r.poi_id))[:1]
    best_fit = min(suitability(r, kind) for r in candidates)
    if kind == 'breakfast' and best_fit == 2:
        return []
    return [r for r in candidates if suitability(r, kind) == best_fit]


def _brand(restaurant):
    return re.split(r'[（(]', restaurant.name, maxsplit=1)[0].strip().casefold()


def _cuisine(restaurant):
    value = re.split(r'[;；]', restaurant.cuisine or '')[-1]
    return '' if value in ('', '餐饮服务', '餐饮相关', '中餐厅', '餐厅') else value


def choose_restaurant(restaurants, kind, anchor, following, used, today, needs, budget_limit):
    from .catalog import matches_query
    candidates = nearby_pool(restaurants, anchor, following, kind, citywide=needs.travel_scope == 'citywide')
    if not candidates:
        return None
    previous = [r for r in restaurants if used.get(r.poi_id, 0)]

    def score(restaurant):
        distance, detour = distances(restaurant, anchor, following)
        rating = restaurant.rating if restaurant.rating is not None else 3.0
        rank_bonus = 1 / max(1, restaurant.ranking_rank or 1) if restaurant.ranking_name else 0
        cuisine = _cuisine(restaurant)
        similarity = max((1.0 if _brand(restaurant) == _brand(old) else
                          .5 if cuisine and cuisine == _cuisine(old) else 0 for old in previous), default=0)
        # Strong identity diversity applies only *after* geographic and meal
        # relevance gating. Exhaustion rotates least-used local candidates.
        geography_cost = (0 if needs.travel_scope == 'citywide' else
                          .4 * min(detour / 3, 1) + .15 * min(distance / NEARBY_KM, 1))
        relevance_cost = geography_cost + .25 * (1 - min(max(rating, 0), 5) / 5) - .2 * rank_bonus
        if any(matches_query(restaurant, 'restaurant', word) for word in needs.keywords):
            relevance_cost -= .2
        if budget_limit is not None:
            relevance_cost += .2 * min((restaurant.avg_cost if restaurant.avg_cost is not None else 60) / 100, 2)
        return (restaurant.poi_id in today, used.get(restaurant.poi_id, 0),
                relevance_cost + .25 * similarity, restaurant.poi_id)

    return min(candidates, key=score)


def coverage_gaps(plan, hotel, restaurants, needs):
    """Check usable neighbourhood/meal coverage, rather than city-wide count."""
    gaps = []
    total_meals = 3 * len(plan.get('days', []))
    for day in plan.get('days', []):
        for kind, anchor, following in meal_contexts(day, hotel):
            candidates = nearby_pool(restaurants, anchor, following, kind, citywide=needs.travel_scope == 'citywide')
            genuinely_near = [r for r in candidates if needs.travel_scope == 'citywide'
                              or distances(r, anchor, following)[0] <= NEARBY_KM]
            if len(genuinely_near) < min(3, total_meals) or (kind == 'breakfast' and not any(breakfast_hint(r) for r in genuinely_near)):
                gaps.append((kind, anchor, following))
    return gaps
