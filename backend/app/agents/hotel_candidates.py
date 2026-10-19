"""Collect dated factual accommodation candidates; selection belongs to planner."""
from ..models.schemas import Hotel
from ..services.tools import ToolContext, execute
from .hotel_agent import _candidate_key, MAX_SEARCH_KEYWORDS


def collect_hotels(state):
    req, nights = state['request'], state['nights']
    if nights <= 0:
        return {'hotels': [], 'hotel_candidate_key': _candidate_key(req, nights)}
    if req.fixed_hotel:
        anchor = state.get('hotel_anchor')
        return {'hotels': [Hotel.model_validate(anchor)] if anchor else [],
                'hotel_candidate_key': _candidate_key(req, nights)}
    from ..services.candidate_search import minimum_candidates, priced_allowed
    key = _candidate_key(req, nights)
    candidates = [Hotel.model_validate(h) for h in state.get('hotels', [])] if state.get('hotel_candidate_key') == key else []
    candidates = priced_allowed(req.city, 'hotel', candidates, req.requirements.hotel,
                                start_date=req.start_date, nights=nights)
    ctx = ToolContext(city=req.city, adcode=state.get('adcode'), start_date=req.start_date,
                      travel_days=state['travel_days'], nights=nights, requirements=req.requirements)
    ctx.hotels = {h.poi_id or h.name: h for h in candidates}
    if not ctx.hotels:
        keywords = list(dict.fromkeys([*req.requirements.hotel.keywords, req.accommodation or '酒店']))
        for keyword in keywords[:MAX_SEARCH_KEYWORDS]:
            result = execute('hotels', 'search_hotels', {'keyword': keyword}, ctx)
            if len(ctx.hotels) >= minimum_candidates('hotel') or (result.status == 'error' and not result.retryable):
                break
    return {'hotels': list(ctx.hotels.values()), 'hotel_candidate_key': key,
            'tool_calls': ctx.calls, 'errors': list(ctx.errors),
            'warnings': [] if ctx.hotels else ['未检索到符合要求的酒店候选，编排不得虚构住宿。'],
            'sources': {'hotel': 'catalog' if candidates else 'amap' if ctx.hotels else 'degraded'}}
