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
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.services import store  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """任何真实网络调用都让测试失败（而不是静默联网）。"""

    def _blocked(*args, **kwargs):
        raise AssertionError("测试不允许联网：高德 / 模型调用都应该被 mock 掉")

    monkeypatch.setattr(httpx, "get", _blocked)
    monkeypatch.setattr(httpx, "post", _blocked)


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
