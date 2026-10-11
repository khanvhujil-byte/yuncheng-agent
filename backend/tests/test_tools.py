"""工具层纯函数的测试（对应 `services/tools.py`）。

这一层是"规则住在哪里"的答案，出错的后果也最直接：
判错了就会把餐馆当成景点、把垃圾输入当成了真实地点。所以判例都用**真实接口返回**构造。
"""

import pytest

from app.models.schemas import Location
from app.services.tools import (
    _is_attraction,
    _is_hotel,
    _trip_dates,
    haversine_km,
    is_relevant,
)

from tests.factories import make_attraction


class TestRelevance:
    """相关性判定：只用于"按名字解析具体地点"，实测案例来自高德真实返回。"""

    @pytest.mark.parametrize(
        ("keyword", "candidate"),
        [
            ("西湖", "杭州西湖风景名胜区"),          # 子串包含
            ("西湖景区", "杭州西湖风景名胜区"),        # 逐字重叠 4/4
            ("雷峰塔", "雷峰塔景区"),
            ("灵隐寺", "灵隐寺"),
            ("杭州西湖风景名胜区", "杭州西湖风景名胜区"),
        ],
    )
    def test_accepts_real_matches(self, keyword, candidate):
        assert is_relevant(keyword, candidate) is True

    @pytest.mark.parametrize(
        ("keyword", "candidate"),
        [
            ("这个景点绝对不存在xyz9", "丰盛九玺"),
            ("这个景点绝对不存在xyz9", "绝对呼吸(杭州湖滨in77D区店)"),
            ("这个景点绝对不存在xyz9", "绝对现卤(和达城店)"),
        ],
    )
    def test_rejects_garbage_keyword(self, keyword, candidate):
        """实测：高德对中文会分词，垃圾输入也会返回一串结果 —— 必须自己判掉。"""
        assert is_relevant(keyword, candidate) is False

    def test_empty_inputs(self):
        assert is_relevant("", "西湖") is False
        assert is_relevant("西湖", "") is False


class TestCategoryFilters:
    """typecode 过滤：typecode 可能是 "a|b|c" 多码拼接（实测）。"""

    def test_attraction_excludes_food_and_lodging(self):
        assert _is_attraction(make_attraction("餐馆", typecode="050202")) is False
        assert _is_attraction(make_attraction("酒店", typecode="100105")) is False

    def test_attraction_keeps_scenery_and_museum_and_shopping(self):
        assert _is_attraction(make_attraction("西湖", typecode="110202")) is True
        assert _is_attraction(make_attraction("博物馆", typecode="140100")) is True
        # 购物类不该被排掉：它可能是"逛街"这一站
        assert _is_attraction(make_attraction("商业街", typecode="060101")) is True

    def test_attraction_multi_code_uses_any_rule(self):
        """★ 实测踩过的坑：用 all()（要求全中）会让美容美发店漏进候选池。"""
        # 050106 = 餐饮服务、071100 = 生活服务 → 有任何一个属于排除类就该排掉
        assert _is_attraction(make_attraction("美容美发", typecode="050106|071100")) is False
        # 购物 + 服装 + 商业街：都不在排除名单里 → 保留
        assert _is_attraction(make_attraction("广场", typecode="060101|061101|061000")) is True

    def test_attraction_keeps_unknown_typecode(self):
        """没有分类码时保留：宁可多留一条，也不静默丢数据。"""
        assert _is_attraction(make_attraction("无名", typecode=None)) is True

    def test_hotel_uses_whitelist(self):
        assert _is_hotel(make_attraction("旅馆", typecode="100200")) is True
        assert _is_hotel(make_attraction("景点", typecode="110202")) is False
        # 酒店 POI 常带附加类目，只要有 10 开头就算住宿
        assert _is_hotel(make_attraction("酒店", typecode="100105|050000")) is True


class TestGeoHelpers:
    def test_haversine_matches_known_distance(self):
        """西湖 → 湖滨公园，实测约 4~5 km（直线）。"""
        west_lake = Location(longitude=120.121358, latitude=30.222692)
        lakeside = Location(longitude=120.155, latitude=30.250)
        distance = haversine_km(west_lake, lakeside)
        assert 3.0 < distance < 6.0

    def test_haversine_zero_for_same_point(self):
        point = Location(longitude=120.0, latitude=30.0)
        assert haversine_km(point, point) == pytest.approx(0.0, abs=1e-9)

    def test_trip_dates(self):
        assert _trip_dates("2026-10-10", 3) == ["2026-10-10", "2026-10-11", "2026-10-12"]

    def test_trip_dates_bad_input(self):
        assert _trip_dates("不是日期", 3) == []
