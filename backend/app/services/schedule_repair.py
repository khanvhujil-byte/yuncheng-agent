"""Bounded local schedule decisions; no model or external route calls.

Geometry is only a planning heuristic. Final schedules always use the route
provider and retain its known/estimated status. Required stops, chosen food
anchors and partial edits are never sacrificed to make a check pass.
"""
import copy
import itertools
import math

from ..models.schemas import Location
from .tools import haversine_km

_DEFAULT_END = object()

START_MINUTES = 8 * 60 + 30
MEAL_MINUTES = {"breakfast": 30, "lunch": 45, "dinner": 60}


def location(item):
    raw = item.get("location") if isinstance(item, dict) else None
    try:
        return Location.model_validate(raw) if raw else None
    except (ValueError, TypeError):
        return None


def visit_minutes(spot):
    return max(30, int(spot.get("visit_duration") or 120))


def estimated_minutes(origin, destination, mode):
    a, b = location(origin), location(destination)
    if not a or not b:
        return 0
    if a == b:
        return 0
    speed = 4 if mode in ("步行", "walking") else (12 if mode in ("公共交通", "transit") else 22)
    return max(5, math.ceil(haversine_km(a, b) * 1.5 / speed * 60)
               + (0 if speed == 4 else 10))


def can_reduce(state, day_indices=None):
    request = state["request"]
    return (day_indices is None and not state.get("unified_planning") and not state.get("preserve_meals")
            and request.requirements.food.priority != "food_first")


def can_trim(state, day_indices=None):
    """May optional stops be dropped to satisfy comfort limits?

    Unified planning owns attraction choice, but that ownership is not a license
    to deliver a day that cannot fit. Required anchors and user-edited meals stay
    protected by ``_protected`` / ``preserve_meals``; only optional stops can be
    dropped. This is the deterministic fallback used after the model has had its
    bounded retries (see itinerary_experience.enrich_experience).
    """
    request = state["request"]
    return (day_indices is None and not state.get("preserve_meals")
            and request.requirements.food.priority != "food_first")


def dedupe_attractions(days, candidates=None):
    """Remove later occurrences of the same attraction across days.

    Returns human-readable notes. A day is never emptied: when every stop of a
    day already appeared earlier, try to substitute an unused candidate first;
    only when none exists keep one repeat (a repeated stop is less harmful than
    a blank day, and the note makes the compromise visible). Required stops are
    always kept on their first appearance.
    """
    notes = []
    seen: dict[str, str] = {}
    for day in days:
        is_dict = isinstance(day, dict)
        spots = day.get("attractions") if is_dict else day.attractions
        date = day.get("date") if is_dict else day.date
        kept = []
        for spot in spots or []:
            if isinstance(spot, dict):
                identity = spot.get("poi_id") or spot.get("name") or ""
                name = spot.get("name") or identity
            else:
                identity = spot.poi_id or spot.name
                name = spot.name
            if identity and identity in seen:
                notes.append(
                    f"{date} 重复安排景点「{name}」（{seen[identity]} 已安排过），已保留首次出现并去重。"
                )
                continue
            if identity:
                seen.setdefault(identity, date)
            kept.append(spot)
        # Never empty a day. Prefer an unused candidate; otherwise keep one
        # repeat and say why (the independent audit may still reject it).
        if not kept and spots:
            replacement = None
            for candidate in candidates or []:
                candidate_id = getattr(candidate, "poi_id", None) or getattr(candidate, "name", None)
                if candidate_id and candidate_id not in seen:
                    replacement = candidate
                    break
            if replacement is not None:
                candidate_id = getattr(replacement, "poi_id", None) or getattr(replacement, "name")
                seen[candidate_id] = date
                if is_dict:
                    kept = [replacement.model_dump() if hasattr(replacement, "model_dump") else dict(replacement)]
                else:
                    kept = [replacement.model_copy(deep=True) if hasattr(replacement, "model_copy") else replacement]
                notes.append(
                    f"{date} 的景点与前序日期重复，已替换为候选「{getattr(replacement, 'name', candidate_id)}」。"
                )
            else:
                kept = [spots[0]]
                notes.append(
                    f"{date} 只有与 {seen.get((spots[0].get('poi_id') if is_dict else spots[0].poi_id) or '')} "
                    f"重复的景点且没有其他候选，保留重复项。"
                )
        if is_dict:
            day["attractions"] = kept
        else:
            day.attractions = kept
    return notes


def _protected(spot, state):
    if spot.get("required"):
        return True
    anchors = state.get("must_visit_resolved") or []
    return any((getattr(a, "poi_id", None) and a.poi_id == spot.get("poi_id"))
               or getattr(a, "name", None) == spot.get("name") for a in anchors)


def restore_required_anchors(days, anchors, request, hotel=None):
    """Restore omitted user choices from factual anchors, never invent stops.

    This does not certify feasibility. All normal identity, theme, route,
    meal and budget checks run afterwards; fixed dates/themes aren't guessed.
    """
    from .day_goals import effective_goals
    if effective_goals(request):
        return []
    notes = []
    for anchor in anchors:
        if any((anchor.poi_id and s.poi_id == anchor.poi_id) or s.name == anchor.name
               for day in days for s in day.attractions):
            continue
        eligible = [d for d in days if len(d.attractions) < request.requirements.attractions.max_per_day]
        if not eligible:
            continue
        def score(day):
            spots = [s.model_dump() for s in day.attractions] + [anchor.model_dump()]
            return (sum(s.required for s in day.attractions),
                    sum(visit_minutes(s) for s in spots) + _distance_cost(spots, hotel, request.transportation),
                    day.day_index)
        target = min(eligible, key=score)
        target.attractions.append(anchor.model_copy(deep=True))
        notes.append(f'{target.date} 已从真实必去候选补回遗漏的「{anchor.name}」，餐饮、路程与预算仍需重新校验。')
    return notes


def _distance_cost(spots, hotel, mode):
    stops = ([hotel] if hotel and location(hotel) else []) + list(spots)
    if hotel and location(hotel):
        stops.append(hotel)
    return sum(estimated_minutes(a, b, mode) for a, b in zip(stops, stops[1:]))


def nearest_order(spots, hotel, mode):
    """At most four attractions: exact geometric ordering is tiny and stable."""
    if len(spots) < 2 or len(spots) > 4:
        return list(spots)
    return list(min(itertools.permutations(spots), key=lambda order: _distance_cost(order, hotel, mode)))


def _remove_optional(day, state, hotel=None):
    spots = day.get("attractions") or []
    if len(spots) <= 1:
        return None
    mode = state["request"].transportation
    from .day_goals import effective_goals, matches
    def preserves_theme(index):
        remaining = spots[:index] + spots[index + 1:]
        for goal in effective_goals(state['request']):
            before = sum(visit_minutes(s) for s in spots if matches(s, goal))
            after = sum(visit_minutes(s) for s in remaining if matches(s, goal))
            if (before and before * 2 >= sum(visit_minutes(s) for s in spots)
                    and (not after or after * 2 < sum(visit_minutes(s) for s in remaining))):
                return False
        return True
    candidates = [(index, spot) for index, spot in enumerate(spots)
                  if not _protected(spot, state) and preserves_theme(index)]
    if not candidates:
        return None
    # Drop the optional stop which consumes most visit + geometric travel time.
    # A tie retains higher rated places, then preserves the original earlier stop.
    baseline = _distance_cost(spots, hotel, mode)
    index, removed = max(candidates, key=lambda entry: (
        visit_minutes(entry[1]) + baseline - _distance_cost(spots[:entry[0]] + spots[entry[0] + 1:], hotel, mode),
        -(entry[1].get("rating") or 0), entry[0]))
    day["attractions"] = spots[:index] + spots[index + 1:]
    return removed


def _removal_note(day, removed):
    return (f"{day.get('date')} 为给三餐、真实通勤与休息留足时间，"
            f"未安排可选景点「{removed.get('name')}」（建议游玩{visit_minutes(removed)}分钟）；"
            "保留其他景点原建议时长与全部必去景点。")


def preflight_plan(plan, state):
    """Avoid paying for routes to a day already overfull on visit duration alone."""
    result = copy.deepcopy(plan)
    if not state.get("experience_enabled") or not can_trim(state):
        return result, []
    request = state["request"]
    # This is an explicit allocation heuristic, not a claim about road times:
    # reserve 135 min for meals, 120 for travel and 45 for buffers/rest.
    activity_limit = max(30, min(360, request.requirements.pace.max_day_minutes - 300))
    if request.requirements.pace.max_activity_minutes is not None:
        activity_limit = min(activity_limit, request.requirements.pace.max_activity_minutes)
    notes = []
    for day in result.get("days", []):
        while (sum(visit_minutes(s) for s in day.get("attractions", [])) > activity_limit
               or len(day.get("attractions", [])) > request.requirements.attractions.max_per_day):
            removed = _remove_optional(day, state)
            if removed is None:
                break
            notes.append(_removal_note(day, removed))
    return result, notes


def remove_one_optional(day, state, hotel=None):
    removed = _remove_optional(day, state, hotel)
    return _removal_note(day, removed) if removed else None


def build_sequence(day, hotel=None, mode="公共交通", minutes=None, comfort_limit=660):
    """Choose meal boundaries using the clock, not half the attraction count.

    Evaluate at most 15 boundaries for a four-stop day. A long indivisible visit
    may go after lunch/dinner; its stated duration is never shortened.

    ``comfort_limit`` keeps a small margin so that the *actual* route clock (which
    can be slower than the estimate used here) still stays under the user's
    comfort cap. Dinner can start at 17:30 or 17:00 when that is the only way to
    keep a low-activity day (one attraction plus three meals) inside the cap.
    """
    spots = day.get("attractions") or []
    meals = {m["type"]: m for m in day.get("meals", [])}
    duration = minutes or (lambda a, b: estimated_minutes(a, b, mode))
    soft_limit = max(60, int(comfort_limit))
    candidates = []
    for lunch_after in range(len(spots) + 1):
        for dinner_after in range(lunch_after, len(spots) + 1):
            sequence = []
            if meals.get("breakfast"):
                sequence.append(("meal", meals["breakfast"], 30))
            for boundary in range(len(spots) + 1):
                if boundary == lunch_after and meals.get("lunch"):
                    sequence.append(("meal", meals["lunch"], 45))
                if boundary == dinner_after and meals.get("dinner"):
                    sequence.append(("meal", meals["dinner"], 60))
                if boundary < len(spots):
                    spot = spots[boundary]
                    sequence.append(("attraction", spot, visit_minutes(spot)))
            for dinner_start in (None, 17 * 60 + 30, 17 * 60):
                variant = [(kind, ({**item, "_earliest_start": dinner_start}
                                   if dinner_start and item.get("type") == "dinner" else item), length)
                           for kind, item, length in sequence]
                timing = simulate(variant, day.get('route_start') if day.get('dated_boundaries') else hotel, duration,
                                  end_hotel=day.get('route_end') if day.get('dated_boundaries') else hotel,
                                  start_minutes=day.get('start_minutes', START_MINUTES),
                                  transfer_minutes=day.get('transfer_minutes', 0))
                # 18:00 remains preferred; an earlier dinner is allowed only when
                # it avoids an actual late meal or an overlong day (incl. return).
                score = (timing["late_meal_minutes"], max(0, timing["total_minutes"] - soft_limit),
                         timing["early_dinner_minutes"], timing["total_minutes"], timing["travel_minutes"],
                         int(lunch_after == 0), int(dinner_after < len(spots)))
                candidates.append((score, variant))
    return min(candidates, key=lambda item: item[0])[1]


def simulate(sequence, hotel, duration, *, end_hotel=_DEFAULT_END, start_minutes=START_MINUTES, transfer_minutes=0):
    """Mirror clock/buffer rules without invoking a route provider."""
    cursor = start_minutes + transfer_minutes//2
    end_hotel = hotel if end_hotel is _DEFAULT_END else end_hotel
    previous = hotel if hotel and location(hotel) else None
    active = 0
    late = 0
    early_dinner = 0
    travel = 0
    with_return = sequence + ([("return", end_hotel, 0)] if end_hotel and location(end_hotel) else [])
    for position, (kind, item, length) in enumerate(with_return):
        if previous and location(previous) and location(item):
            hop = duration(previous, item)
            cursor += hop + max(5, math.ceil(hop * .2))
            travel += hop
        if kind == "return":
            break
        if kind == "meal":
            meal = item.get("type")
            cursor = max(cursor, item.get("_earliest_start", {"breakfast": 7 * 60, "lunch": 12 * 60, "dinner": 18 * 60}.get(meal, cursor)))
            if meal == "dinner":
                early_dinner = max(0, 18 * 60 - cursor)
            limit = {"lunch": 14 * 60, "dinner": 20 * 60 + 30}.get(meal)
            late += max(0, cursor - limit) if limit is not None else 0
        cursor += length
        active += length if kind == "attraction" else 0
        if active >= 120 and position < len(sequence) - 1:
            cursor += 15
            active = 0
        if location(item):
            previous = item
    cursor += transfer_minutes - transfer_minutes//2
    return {"total_minutes": cursor - start_minutes, "late_meal_minutes": late,
            "early_dinner_minutes": early_dinner, "travel_minutes": travel}
