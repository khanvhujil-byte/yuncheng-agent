"""Production MCP server with offline map I/O, in an independent child process."""
import socket
import sys
import ipaddress

from app import mcp_server
from app.models.schemas import Attraction, Location, WeatherInfo
from app.services import amap


_connect = socket.socket.connect


def blocked(sock, address):
    host = address[0] if isinstance(address, tuple) else ''
    if host == 'localhost' or (host and ipaddress.ip_address(host).is_loopback):
        return _connect(sock, address)
    raise AssertionError('MCP fixture must not access external services')


socket.socket.connect = blocked


def geocode(address, city=None):
    if address == 'invalid-key':
        raise amap.AmapError('AMAP_KEY_INVALID', 'offline fixture')
    if address == 'rate-limited':
        raise amap.AmapUnreachable('offline 429', code='UPSTREAM_RATE_LIMITED', retry_after_seconds=2)
    return Location(longitude=120.2, latitude=30.2), '330100'


amap.geocode = geocode
amap.search_poi = lambda *a, **k: [Attraction(name='真实协议景点', location=Location(longitude=120.2, latitude=30.2), poi_id='fixture-poi')]
amap.get_weather = lambda adcode: [WeatherInfo(date='2026-10-20', day_weather='晴')]
amap.measure_distance = lambda *a, **k: 1.25

if len(sys.argv) > 1:
    mcp_server.server.settings.host = '127.0.0.1'
    mcp_server.server.settings.port = int(sys.argv[1])
    mcp_server.server.run(transport='streamable-http')
else:
    mcp_server.server.run(transport='stdio')
