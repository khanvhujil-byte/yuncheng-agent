"""pytest 公共配置与夹具。

两条硬规则（写在这里，所有测试自动继承）：

1. **测试绝不允许联网。** `no_network` 是 autouse 的：把 httpx 的 `get` / `post`
   换成直接抛断言错误。高德与模型的调用在测试里必须被 mock —— 否则后果是
   花钱 + 结果不稳定 + CI 上偶发失败，而且失败原因看不出是"网络抖了"。
   注意它只挡模块级函数，不影响 starlette 的 TestClient（它走 ASGITransport）。
2. **每个测试用独立的临时数据库。** `temp_db` 把 `store.DB_PATH` 指到 tmp_path，
   避免测试互相污染，也避免把真实行程库写脏。
"""

import sys
import ipaddress
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.services import store  # noqa: E402

# API module initialization also runs during test collection, before fixtures.
# Redirect that initialization so even collection cannot touch the real DB.
_collection_directory = tempfile.TemporaryDirectory(prefix='trip-pytest-collection-')
store.DB_PATH = Path(_collection_directory.name) / 'collection.sqlite'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """任何真实网络调用都让测试失败（而不是静默联网）。"""

    def _blocked(*args, **kwargs):
        raise AssertionError("测试不允许联网：高德 / 模型调用都应该被 mock 掉")

    monkeypatch.setattr(httpx, "get", _blocked)
    monkeypatch.setattr(httpx, "post", _blocked)
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    resolve = socket.getaddrinfo

    def local(host):
        try:
            return host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def guarded(original):
        def call(sock, address):
            if isinstance(address, tuple) and local(address[0]):
                return original(sock, address)
            return _blocked()
        return call

    def resolve_local(host, *args, **kwargs):
        if host is None or local(host):
            return resolve(host, *args, **kwargs)
        return _blocked()

    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    monkeypatch.setattr(socket, 'getaddrinfo', resolve_local)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """把行程库指到临时目录，并建好表。"""
    db_path = tmp_path / "test_trip.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db()
    return db_path


@pytest.fixture
def settings():
    """拿到 Settings 单例。测试里改开关请用 monkeypatch，它会自动还原。"""
    return get_settings()


@pytest.fixture(autouse=True)
def isolated_request_gate(monkeypatch, settings):
    """Avoid shared cooldown leaking between tests; rate-control tests opt in explicitly."""
    from app.services import amap
    from app.services.request_gate import RequestGate
    monkeypatch.setattr(amap, "request_gate", RequestGate())
    monkeypatch.setattr(settings, "amap_request_interval_seconds", 0.0)
    monkeypatch.setattr(settings, "amap_rate_limit_cooldown_seconds", 0.0)


@pytest.fixture(autouse=True)
def isolated_security():
    from app.api.security import _hits, _lock
    with _lock:
        _hits.clear()


@pytest.fixture(autouse=True)
def isolated_deployment_settings(monkeypatch, settings):
    """Local .env authentication/transport choices must not change test semantics."""
    monkeypatch.setattr(settings, 'api_key', '')
    monkeypatch.setattr(settings, 'rate_limit_per_minute', 0)
    monkeypatch.setattr(settings, 'map_backend', 'local')
    monkeypatch.setattr(settings, 'mcp_reuse_session', False)
    monkeypatch.setattr(settings, 'catalog_refresh_enabled', False)
    # Web-search behavior has explicit mocked integration tests. Existing
    # fixtures never accidentally access a new external provider.
    monkeypatch.setattr(settings, 'ticket_web_search_enabled', False)
    monkeypatch.setattr(settings, 'ticket_model_estimate_enabled', False)
    # Legacy call-sequence tests script one model response per compose attempt;
    # the in-node format-repair retry is covered by the evaluation suite.
    monkeypatch.setattr(settings, 'planner_format_retry_enabled', False)
    # Legacy tests exercise the historical hotel ReAct path. Fast-path tests
    # explicitly select the production default and verify zero model calls.
    monkeypatch.setattr(settings, 'hotel_use_agent', True)
    from app.services import java_bridge
    with java_bridge._hot_lock:
        java_bridge._hot_cache.clear()
    # Legacy fixtures cover the unknown-price contract; ticket feature tests
    # enable the new reserve/official-lookup policy explicitly.
    monkeypatch.setattr(settings, 'official_ticket_lookup_enabled', False)
    monkeypatch.setattr(settings, 'ticket_budget_reserve_enabled', False)
    # Coverage-specific tests explicitly enable bounded external fill.
    monkeypatch.setattr(settings, 'food_coverage_enabled', False)
    # Existing forecast fixtures use fixed travel dates. A fixed observation
    # clock keeps validity tests independent of the machine's calendar date.
    # Cache-specific tests provide their own advancing clock/cooldown.
    from app.services import weather_cache
    monkeypatch.setattr(weather_cache, '_utcnow', lambda: datetime(2026, 10, 10, tzinfo=timezone.utc))
    monkeypatch.setattr(settings, 'weather_cache_failure_cooldown_seconds', 0)
