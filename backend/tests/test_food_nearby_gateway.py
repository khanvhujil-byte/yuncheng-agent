import pytest

from app.models.schemas import Location
from app.services import amap, map_gateway


def poi(identifier='R1', lng=120.121, kind='050100', cost='0'):
    return {'id':identifier, 'name':'真实餐厅', 'location':f'{lng},30.22', 'typecode':kind,
            'type':'餐饮服务;中餐厅', 'biz_ext':{'cost':cost,'rating':'4.5'}}


def test_around_query_binds_actual_coordinates_radius_category_and_distance_order(monkeypatch):
    calls = []
    monkeypatch.setattr(amap, '_get', lambda url, params: calls.append((url,params)) or {'pois':[poi()]})
    center = Location(longitude=120.12, latitude=30.22)
    result = map_gateway.search_restaurants_nearby('早餐','杭州',center)
    url, args = calls[0]
    assert url.endswith('/place/around')
    assert args['location'] == '120.12,30.22' and args['radius'] == 4000
    assert args['types'] == '050000' and args['sortrule'] == 'distance'
    assert args['keywords'] == '早餐'
    assert result[0].avg_cost == 0 and result[0].avg_cost_status == 'estimated'


def test_provider_out_of_radius_invalid_identity_and_nonfood_rows_are_discarded(monkeypatch):
    monkeypatch.setattr(amap, '_get', lambda *a: {'pois':[poi(),poi('far',lng=121),poi('',kind='050100'),poi('hotel',kind='100100')]})
    result = map_gateway.search_restaurants_nearby('餐厅','杭州',Location(longitude=120.12,latitude=30.22))
    assert [r.poi_id for r in result] == ['R1']


@pytest.mark.parametrize('center,radius,limit', [
    (Location(longitude=120,latitude=30),0,20),
    (Location(longitude=120,latitude=30),5000,20),
    (Location(longitude=120,latitude=30),4000,21),
    (Location(longitude=181,latitude=30),4000,20),
    (Location(longitude=float('nan'),latitude=30),4000,20),
])
def test_invalid_around_parameters_fail_before_any_external_call(monkeypatch,center,radius,limit):
    monkeypatch.setattr(amap, '_get', lambda *a: pytest.fail('invalid query must not reach upstream'))
    with pytest.raises(amap.AmapError) as error:
        amap.search_restaurants_nearby('餐厅','杭州',center,radius,limit)
    assert error.value.code == 'INVALID_REQUEST'


def test_mcp_around_uses_same_structured_domain_contract(monkeypatch,settings):
    monkeypatch.setattr(settings,'map_backend','mcp')
    calls = []
    def call(name,args):
        calls.append((name,args))
        return [{'name':'真实餐厅','poi_id':'R1','location':{'longitude':120.121,'latitude':30.22}},
                {'name':'远方餐厅','poi_id':'far','location':{'longitude':121,'latitude':30.22}}]
    monkeypatch.setattr(map_gateway,'_call',call)
    center = Location(longitude=120.12,latitude=30.22)
    assert [r.poi_id for r in map_gateway.search_restaurants_nearby('早餐','杭州',center)] == ['R1']
    name,args = calls[0]
    assert name == 'search_restaurants_nearby' and args['center'] == center.model_dump()
    assert args['radius'] == 4000 and args['limit'] == 20


def test_mcp_server_registers_readonly_around_tool_and_rejects_bad_radius(monkeypatch):
    from app import mcp_server
    monkeypatch.setattr(amap,'search_restaurants_nearby',lambda *a: pytest.fail('invalid radius'))
    result = mcp_server.search_restaurants_nearby('餐厅','杭州',Location(longitude=120,latitude=30),radius=9000)
    assert result == {'status':'error','error_code':'INVALID_REQUEST','retryable':False}
