"""Use the official MCP client against real stdio and Streamable HTTP transports."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from app.services import amap, map_gateway, mcp_adapter
from app.services.tool_retry import deadline

FIXTURE = Path(__file__).parent / 'fixtures' / 'mcp_map_server.py'


@pytest.fixture
def stdio(settings, monkeypatch):
    monkeypatch.setattr(settings, 'map_backend', 'mcp')
    monkeypatch.setattr(settings, 'mcp_transport', 'stdio')
    monkeypatch.setattr(settings, 'mcp_command', sys.executable)
    monkeypatch.setattr(settings, 'mcp_args', ['-B', '-S', str(FIXTURE)])
    monkeypatch.setattr(settings, 'mcp_env', {'PYTHONPATH': os.pathsep.join(sys.path), 'PYTHONIOENCODING': 'utf-8'})
    monkeypatch.setattr(settings, 'mcp_timeout', 15.0)


def test_actual_stdio_discovery_validation_and_domain_parity(stdio):
    from app.models.schemas import Location
    location, adcode = map_gateway.geocode('杭州')
    assert adcode == '330100' and isinstance(location, Location)
    assert map_gateway.search_poi('景点', '杭州')[0].poi_id == 'fixture-poi'
    assert map_gateway.get_weather(adcode)[0].day_weather == '晴'
    assert map_gateway.measure_distance(location, location, mode=1) == 1.25
    with pytest.raises(amap.AmapUnreachable) as exc:
        mcp_adapter.call('search_poi', {'keywords': 123, 'city': '杭州'})
    assert exc.value.code == 'INVALID_ARGUMENT' and not exc.value.retryable


@pytest.mark.parametrize(('address', 'expected', 'retryable'), [('invalid-key', 'AMAP_KEY_INVALID', False), ('rate-limited', 'UPSTREAM_RATE_LIMITED', True)])
def test_actual_mcp_preserves_typed_errors(stdio, address, expected, retryable):
    with pytest.raises((amap.AmapError, amap.AmapUnreachable)) as exc:
        map_gateway.geocode(address)
    assert exc.value.code == expected
    assert getattr(exc.value, 'retryable', False) == retryable
    if retryable:
        assert exc.value.retry_after_seconds == 2


def test_mcp_does_not_reset_exhausted_deadline_and_disallows_unknown_tools(stdio):
    token = deadline.set(time.monotonic()-1)
    try:
        with pytest.raises(amap.AmapUnreachable):
            map_gateway.geocode('杭州')
    finally:
        deadline.reset(token)
    with pytest.raises(amap.AmapUnreachable) as exc:
        mcp_adapter.call('delete_file', {})
    assert exc.value.code == 'TOOL_FORBIDDEN' and not exc.value.retryable


def test_mcp_startup_validates_transport_without_demanding_local_map_key(stdio, settings, monkeypatch):
    from app.config import ConfigError, validate_config
    monkeypatch.setattr(settings, 'llm_api_key', 'sk-offline-fixture')
    monkeypatch.setattr(settings, 'amap_api_key', '')
    assert validate_config() is settings
    monkeypatch.setattr(settings, 'mcp_command', '')
    with pytest.raises(ConfigError):
        validate_config()


def test_actual_streamable_http_transport(stdio, settings, monkeypatch):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, **settings.mcp_env}
    with subprocess.Popen([sys.executable, '-B', '-S', str(FIXTURE), str(port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as child:
        try:
            started = time.monotonic()
            with httpx.Client(timeout=.5, trust_env=False) as client:
                while True:
                    try:
                        client.get(f'http://127.0.0.1:{port}/mcp')
                        break
                    except httpx.TransportError:
                        if child.poll() is not None or time.monotonic()-started > 12:
                            pytest.fail('offline HTTP MCP fixture failed to start')
                        time.sleep(.05)
            monkeypatch.setattr(settings, 'mcp_transport', 'streamable_http')
            monkeypatch.setattr(settings, 'mcp_url', f'http://127.0.0.1:{port}/mcp')
            assert map_gateway.geocode('杭州')[1] == '330100'
        finally:
            child.terminate()
            child.wait(timeout=10)
