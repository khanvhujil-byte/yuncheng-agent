from app.services import mcp_pool, map_gateway
import pytest
from tests.test_mcp_upgrade import stdio  # noqa: F401


@pytest.mark.usefixtures('stdio')
def test_shared_stdio_session_reuses_discovery_and_reconnects(settings,monkeypatch):
    from mcp import ClientSession
    monkeypatch.setattr(settings,'mcp_reuse_session',True)
    counts=[]
    original=ClientSession.initialize
    async def initialize(self,*args,**kwargs):
        counts.append(1)
        return await original(self,*args,**kwargs)
    monkeypatch.setattr(ClientSession,'initialize',initialize)
    try:
        assert map_gateway.geocode('杭州')[1]=='330100'
        assert map_gateway.search_poi('景点','杭州')[0].poi_id=='fixture-poi'
        assert len(counts)==1
        mcp_pool.close()
        assert map_gateway.geocode('杭州')[1]=='330100'
        assert len(counts)==2
    finally:
        mcp_pool.close()
