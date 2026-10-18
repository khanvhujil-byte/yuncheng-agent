"""城市预检与锚点解析（`agents/city.py`）的测试。

这个节点的价值是"**花钱之前拦住**"，所以它的每一条失败路径都必须被测到 ——
漏一条就意味着一种白白烧掉 40 秒和一万多 token 的请求。
所有高德调用都用 monkeypatch 顶掉（conftest 里的 no_network 会兜住漏网的）。
"""

import pytest

from app.agents import city as city_module
from app.agents.city import MAX_MUST_VISIT, validate_city
from app.api.routes.trip import _initial_state
from app.models.schemas import Location
from app.services.amap import AmapError, AmapUnreachable
from tests.factories import make_attraction, make_request

HANGZHOU = Location(longitude=120.209903, latitude=30.246566)


def _state(request):
    return _initial_state(request)


@pytest.fixture
def geocode_ok(monkeypatch):
    """高德地理编码成功：返回杭州中心坐标 + adcode。"""
    monkeypatch.setattr(city_module, "geocode", lambda address, city=None: (HANGZHOU, "330100"))


@pytest.fixture
def search_returns(monkeypatch):
    """按关键词返回 POI 的假搜索，默认每个词都给一条像样的结果。"""

    def _install(mapping=None, default=None):
        def fake(keyword, city, offset=20, page=1):
            if mapping and keyword in mapping:
                return list(mapping[keyword])
            if default is not None:
                return list(default)
            return [make_attraction(f"{keyword}景区")]

        monkeypatch.setattr(city_module, "search_poi", fake)

    return _install


class TestHappyPath:
    def test_success_writes_adcode_and_center(self, geocode_ok, search_returns):
        search_returns()
        result = validate_city(_state(make_request(must_visit=[])))
        assert result["adcode"] == "330100"
        assert result["city_center"].longitude == pytest.approx(120.209903)
        assert result["error_code"] is None
        assert result["must_visit_resolved"] == []

    def test_anchor_is_resolved_and_marked_required(self, geocode_ok, search_returns):
        search_returns(default=[make_attraction("杭州西湖风景名胜区")])
        result = validate_city(_state(make_request(must_visit=["西湖"])))

        assert [a.name for a in result["must_visit_resolved"]] == ["杭州西湖风景名胜区"]
        assert result["must_visit_resolved"][0].required is True
        # 解析改名必须说出来 —— 否则用户以为系统去的是他说的那个地方
        assert any("已解析为" in w for w in result["warnings"])

    def test_no_warning_when_name_unchanged(self, geocode_ok, search_returns):
        search_returns(default=[make_attraction("西湖")])
        result = validate_city(_state(make_request(must_visit=["西湖"])))
        assert result["warnings"] == []


class TestPreflightRules:
    """四条预检：前三条拦住，第四条（预报窗口）不拦。"""

    def test_date_range_mismatch(self, geocode_ok):
        result = validate_city(_state(make_request(start_date="2026-10-10",
                                                   end_date="2026-10-05", travel_days=3)))
        assert result["error_code"] == "DATE_RANGE_MISMATCH"
        assert result["adcode"] is None

    def test_bad_start_date(self, geocode_ok):
        result = validate_city(_state(make_request(start_date="不是日期")))
        assert result["error_code"] == "DATE_RANGE_MISMATCH"

    def test_nights_out_of_range(self, geocode_ok):
        result = validate_city(_state(make_request(travel_days=3, nights=5)))
        assert result["error_code"] == "INVALID_REQUEST"

    def test_capacity_exceeded(self, geocode_ok):
        many = [f"景点{i}" for i in range(12)]
        result = validate_city(_state(make_request(travel_days=2, must_visit=many)))
        assert result["error_code"] == "CONSTRAINT_INFEASIBLE"
        assert "最多安排 6 个" in result["errors"][0]

    def test_preflight_failures_never_call_the_model(self, geocode_ok):
        """★ 预检失败必须是 0 次 LLM 调用 —— 这条靠"图根本走不到 compose_plan"保证，
        这里断言它连高德都不需要调（容量检查在 geocode 之前）。"""
        result = validate_city(_state(make_request(travel_days=2, must_visit=["a"] * 12)))
        assert result["adcode"] is None

    def test_forecast_window_is_not_blocking(self, geocode_ok, search_returns):
        search_returns()
        result = validate_city(_state(make_request(travel_days=6, must_visit=[])))
        assert result["error_code"] is None
        # v0.3 起用户可见的天气警告统一由 weather_agent 给（避免两条重复的话）
        assert not any("预报窗口" in w for w in result["warnings"])


class TestGeocodeFailureMapping:
    def test_unresolvable_address_becomes_city_not_found(self, monkeypatch):
        """实测：高德把"地址解析不出来"归到 30001 ENGINE_RESPONSE_DATA_ERROR，
        与"参数被破坏"同一个码。只有本节点知道这次调用是"解析城市名"，所以要改写码。"""
        def raise_invalid(*args, **kwargs):
            raise AmapError("INVALID_REQUEST", "infocode=30001 info=ENGINE_RESPONSE_DATA_ERROR")

        monkeypatch.setattr(city_module, "geocode", raise_invalid)
        result = validate_city(_state(make_request()))
        assert result["error_code"] == "CITY_NOT_FOUND"

    def test_key_error_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(city_module, "geocode",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AmapError("AMAP_KEY_INVALID", "infocode=10001")))
        result = validate_city(_state(make_request()))
        assert result["error_code"] == "AMAP_KEY_INVALID"

    def test_network_error_becomes_upstream_timeout(self, monkeypatch):
        monkeypatch.setattr(city_module, "geocode",
                            lambda *a, **k: (_ for _ in ()).throw(AmapUnreachable("超时")))
        result = validate_city(_state(make_request()))
        assert result["error_code"] == "UPSTREAM_TIMEOUT"

    def test_empty_geocode_result(self, monkeypatch):
        monkeypatch.setattr(city_module, "geocode", lambda *a, **k: (None, None))
        result = validate_city(_state(make_request()))
        assert result["error_code"] == "CITY_NOT_FOUND"


class TestAnchorResolution:
    def test_no_search_result(self, geocode_ok, search_returns):
        search_returns(mapping={"不存在的景点": []})
        result = validate_city(_state(make_request(must_visit=["不存在的景点"])))
        assert result["error_code"] == "ATTRACTION_NOT_FOUND"

    def test_irrelevant_results_are_rejected(self, geocode_ok, search_returns):
        """★ 实测：搜垃圾关键词，高德会返回"丰盛九玺"这种无关结果。
        硬约束不能被静默换成一个用户没说的地方 —— 必须当成搜不到。"""
        search_returns(mapping={"这个景点绝对不存在xyz9": [make_attraction("丰盛九玺")]})
        result = validate_city(_state(make_request(must_visit=["这个景点绝对不存在xyz9"])))
        assert result["error_code"] == "ATTRACTION_NOT_FOUND"
        assert "没有匹配结果" in result["errors"][0]

    def test_first_relevant_hit_wins(self, geocode_ok, search_returns):
        """第一条不相关、第二条相关时，应取第二条而不是硬用第一条。"""
        search_returns(mapping={"西湖": [make_attraction("完全不相关的地方"),
                                        make_attraction("杭州西湖风景名胜区")]})
        result = validate_city(_state(make_request(must_visit=["西湖"])))
        assert result["must_visit_resolved"][0].name == "杭州西湖风景名胜区"

    def test_search_error_is_passed_through_not_masked_as_not_found(self, geocode_ok, monkeypatch):
        """搜索时报配置错误，说成"景点不存在"就是撒谎：用户会去改景点名，而问题在 Key。"""
        monkeypatch.setattr(city_module, "geocode", lambda *a, **k: (HANGZHOU, "330100"))
        monkeypatch.setattr(city_module, "search_poi",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AmapError("AMAP_QUOTA_EXCEEDED", "infocode=10003")))
        result = validate_city(_state(make_request(must_visit=["西湖"])))
        assert result["error_code"] == "AMAP_QUOTA_EXCEEDED"

    def test_too_many_must_visit_is_rejected_without_silent_truncation(self, geocode_ok, search_returns):
        """配额保护：每个必去景点 = 1 次搜索调用，超上限就截断并如实告知。

        这里不指定 default，让假搜索按关键词造名字（`景点0` → `景点0景区`）——
        否则相关性判定会正确地把它当成"搜不到"（那也是另一条测试在守的行为）。
        """
        search_returns()
        names = [f"景点{i}" for i in range(MAX_MUST_VISIT + 3)]
        result = validate_city(_state(make_request(travel_days=30, must_visit=names)))

        assert result["error_code"] == "CONSTRAINT_INFEASIBLE"
        assert result.get("must_visit_resolved", []) == []
