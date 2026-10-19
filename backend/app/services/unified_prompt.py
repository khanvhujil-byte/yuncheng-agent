"""Compact factual context for a single joint planning decision."""
import json


def build_prompt(state, selected, total):
    from ..agents.hotel_agent import HOTEL_BUDGET_RESERVES, DEFAULT_HOTEL_BUDGET_RESERVE
    from ..agents.planner_agent import _format_weather
    from .itinerary_experience import city_characteristics
    from .requirements import context
    req = state['request']
    from .planning_limits import feasibility
    spots = [{k: a.model_dump().get(k) for k in ('poi_id', 'name', 'location', 'category', 'visit_duration',
              'required', 'rating', 'ticket_price', 'ticket_price_status', 'ticket_price_note')} for a in selected]
    for a in spots:
        a['poi_id'] = a['poi_id'] or a['name']
    restaurants = [{k: r.model_dump().get(k) for k in ('poi_id', 'name', 'location', 'cuisine', 'rating',
                    'avg_cost', 'avg_cost_status', 'ranking_name', 'ranking_rank')} for r in state.get('restaurants', [])]
    reserve = HOTEL_BUDGET_RESERVES.get(req.accommodation, DEFAULT_HOTEL_BUDGET_RESERVE)
    hotels = []
    for raw in state.get('hotels', []):
        h = raw.model_dump() if hasattr(raw, 'model_dump') else raw
        hotels.append({'hotel_id': h.get('poi_id') or h['name'], 'name': h['name'], 'location': h.get('location'),
                       'rating': h.get('rating'), 'type': h.get('type'),
                       'per_night': h['estimated_cost'] if h.get('cost_source') == 'imported' else reserve,
                       'price_basis': h.get('cost_status') if h.get('cost_source') == 'imported' else '预算预留，非房价'})
    anchor = state.get('hotel_anchor')
    return (json.dumps({'city': req.city, 'start_date': req.start_date, 'end_date': state['end_date'],
                        'travel_days': state['travel_days'], 'nights': state['nights'],
                        'budget_limit': req.budget_limit, 'transportation': req.transportation,
                        'accommodation': req.accommodation, 'preferences': req.preferences,
                        'free_text_input': req.free_text_input,
                        'locked_hotel_id': (anchor.poi_id or anchor.name) if req.fixed_hotel and anchor else None,
                        'max_day_minutes': req.requirements.pace.max_day_minutes,
                        'max_attractions_per_day': req.requirements.attractions.max_per_day,
                        'feasibility': feasibility(state, selected),
                        'city_characteristics': city_characteristics(req.city),
                        'attraction_pool_total': total, 'attractions_shown': len(spots),
                        'attractions': spots, 'restaurants': restaurants, 'hotels': hotels}, ensure_ascii=False)
            + '\n【各模块用户约束】\n' + context(req, 'food', 'attractions', 'hotel', 'transport', 'pace', 'weather', 'budget')
            + '\n【逐日天气】\n' + _format_weather(list(state.get('weather') or []))
            + '\n你负责统一选择景点、餐厅和酒店。餐饮与酒店展示本轮候选池全部条目；景点清单是否筛选已明确。'
              '未知门票会在内部数据未命中后进行网上查询；没有证据之前不可当作免费。')
