"""Explicit factual fixtures for the joint ID output contract."""
from app.agents.hotel_agent import _candidate_key
from app.models.schemas import Location, Restaurant


def restaurants_at(location, count=6):
    return [Restaurant(name=f'测试餐厅{i}', poi_id=f'J{i}', location=location, avg_cost=50,
                       avg_cost_status='estimated', cost_source='amap') for i in range(count)]


def hotel_pool(state, hotel):
    hotel = hotel.model_copy(update={'poi_id': hotel.poi_id or 'JHOTEL', 'cost_status': 'known', 'cost_source': 'imported',
                                    'location': Location(longitude=120.12, latitude=30.22)})
    return {'hotels': [hotel], 'hotel_candidate_key': _candidate_key(state['request'], state['nights']),
            'sources': {'hotel': 'amap'}}


def joint_payload(days, restaurants, hotel_id=None):
    return {'hotel_id': hotel_id, 'hotel_reason': '候选酒店位于日程片区附近，综合住宿预算选择。',
            'days': [{**day, 'meals': [{'type': kind, 'restaurant_id': restaurants[(3*i+j) % len(restaurants)].poi_id}
                                      for j, kind in enumerate(('breakfast', 'lunch', 'dinner'))]}
                     for i, day in enumerate(days)]}
