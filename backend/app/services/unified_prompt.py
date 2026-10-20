"""Compact factual context for a single joint planning decision."""
import json


def _table(rows):
    """All rows and facts remain visible; repeat column names only once."""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return {'columns': columns, 'rows': [[row.get(key) for key in columns] for row in rows]}


def build_prompt(state, selected, total):
    from ..agents.hotel_agent import HOTEL_BUDGET_RESERVES, DEFAULT_HOTEL_BUDGET_RESERVE
    from ..agents.planner_agent import _format_weather
    from .itinerary_experience import city_characteristics
    from .requirements import context
    from .stays import stay_policy, night_dates
    req = state['request']
    from .day_goals import prompt_goals
    from .planning_limits import feasibility
    spots = [{k: a.model_dump().get(k) for k in ('poi_id', 'name', 'location', 'category', 'visit_duration',
              'required', 'rating', 'ticket_price', 'ticket_price_status', 'ticket_price_note')} for a in selected]
    for a in spots:
        a['poi_id'] = a['poi_id'] or a['name']
    restaurants = [{k: r.model_dump().get(k) for k in ('poi_id', 'name', 'location', 'cuisine', 'rating',
                    'avg_cost', 'avg_cost_status', 'ranking_name', 'ranking_rank')} for r in state.get('restaurants', [])]
    reserve = HOTEL_BUDGET_RESERVES.get(req.accommodation, DEFAULT_HOTEL_BUDGET_RESERVE)
    hotels = []
    from .platform_hotels import quoted
    for raw in state.get('hotels', []):
        raw = quoted(raw, req.start_date)
        h = raw.model_dump() if hasattr(raw, 'model_dump') else raw
        hotels.append({'hotel_id': h.get('poi_id') or h['name'], 'name': h['name'], 'location': h.get('location'),
                       'rating': h.get('rating'), 'type': h.get('type'),
                       'per_night': h['estimated_cost'] if h.get('cost_source') == 'imported' else reserve,
                       'price_basis': h.get('cost_status') if h.get('cost_source') == 'imported' else '预算预留，非房价'})
    anchor = state.get('hotel_anchor')
    hints = feasibility(state, selected)
    hints['pairs'] = _table(hints['pairs'])
    from datetime import date, timedelta
    from .day_window import policy
    from .intercity import time_windows
    windows = {d: policy(req, d) for i in range(req.travel_days)
               if (d := (date.fromisoformat(req.start_date)+timedelta(days=i)).isoformat())}
    return (json.dumps({'city': req.city, 'start_date': req.start_date, 'end_date': state['end_date'],
                        'origin_city': req.origin_city, 'adult_count': req.adult_count, 'room_count': req.room_count,
                        'intercity_mode': req.intercity_mode, 'return_trip': req.return_trip,
                        'transport_candidates': state.get('transport_candidates', {}),
                        'transport_time_windows': time_windows(state),
                        'travel_days': state['travel_days'], 'nights': state['nights'],
                        'required_stay_check_in_dates': night_dates(req),
                        'budget_limit': req.budget_limit, 'transportation': req.transportation,
                        'accommodation': req.accommodation, 'preferences': req.preferences,
                        'free_text_input': req.free_text_input,
                        'thematic_day_requirements': prompt_goals(req, selected),
                        'stay_policy': stay_policy(req),
                        'hotel_route_facts': state.get('stay_route_hints', []),
                        'regional_weather': state.get('regional_weather', []),
                        'planning_data_requests': state.get('planning_data_requests', []),
                        'planner_draft': state.get('planner_draft'),
                        'dated_hotel_locks': {d: h.get('poi_id') or h['name'] for d, h in (state.get('stay_locks') or {}).items()},
                        'accommodation_version': state.get('accommodation_version', 0),
                        'arrival_time': req.arrival_time, 'departure_time': req.departure_time,
                        'daily_time_windows': windows,
                        'trip_endpoints': state.get('trip_endpoints', {}),
                        'locked_hotel_id': (anchor.poi_id or anchor.name) if req.fixed_hotel and anchor else None,
                        'max_day_minutes': req.requirements.pace.max_day_minutes,
                        'max_attractions_per_day': req.requirements.attractions.max_per_day,
                        'feasibility': hints,
                        'city_characteristics': city_characteristics(req.city),
                        'attraction_pool_total': total, 'attractions_shown': len(spots),
                        'attractions': _table(spots), 'restaurants': _table(restaurants), 'hotels': _table(hotels)}, ensure_ascii=False, separators=(',', ':'))
            + '\n【各模块用户约束】\n' + context(req, 'food', 'attractions', 'hotel', 'transport', 'pace', 'weather', 'budget', include_raw=False)
            + '\n【逐日天气】\n' + _format_weather(list(state.get('weather') or []))
            + '\n你负责统一选择景点、餐厅和酒店。餐饮与酒店展示本轮候选池全部条目；景点清单是否筛选已明确。'
              '每晚住宿仅覆盖required_stay_check_in_dates：用stays逐晚输出，或stay_segments按入住/退房日期压缩连续入住；两者只选一种。'
              '比较连续远郊活动住当地与回市区：兼顾次日动线、总通勤、预算、每次换住60分钟和行李负担。'
              '市内通勤合理优先连住，prefer_same提高换住门槛，must_same禁止更换。'
              'dated_hotel_locks按日期锁定；locked_hotel_id则全程锁定。首日无到达点不得假设从酒店出发。'
              'daily_time_windows是代码派生的逐日可用时间与必需餐次；显式晚到早离只安排其中meal_types，allow_empty_attractions为true的边界日可仅接驳住宿。'
              '未知门票会在内部数据未命中后进行网上查询；没有证据之前不可当作免费。'
            + '\n【交通与团体预算】成人门票、餐饮和交通按adult_count，住宿按room_count计费。'
              'intercity_mode不为none时同时返回transport_choices:{"outbound":"真实offer_id","return":"真实offer_id"}；单程return=null。'
              '只能选择本方向候选ID；某方向候选为空则选null并在建议中明确未完成交通。不得自编班次、价格和时间。'
              'outbound的local_window_time是出站后可开始接驳的时间；return的是须抵达车站/机场的截止时间，已含候车预留。'
              '首末日据所选班次时间选择可用餐次：早餐07:00-10:00需30分，午餐12:00-14:00需45分，晚餐17:00-20:30需60分。'
              '到达晚于18:00或返程截止早于11:00的边界日可无景点；同日去返程不能时间冲突。'
              '按时间、预算、总通勤、换乘负担综合比较火车/飞机，不能只看票面低价；飞机税费、行李与余票仍待确认。')
