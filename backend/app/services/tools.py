"""工具层：**所有确定性逻辑都住在这里**（v0.3 多智能体架构的地基）。

架构分工：
    Agent（LLM）→ 决策：调哪个工具、用什么关键词、够不够、要不要再来一次
    工具（代码）→ 执行：过滤、去重、窗口对齐、距离计算、选址数学

    为什么规则写在工具里，而不是写在 prompt 里：
        prompt 里的规则是"建议"，遵守率会掉；工具里的规则是代码，必然执行。
        这样既拿到多智能体的自主性，又不丢确定性。

两条硬性约定：
    1. **工具永远不抛异常**。出错就返回一句人话，让 Agent 决定下一步
       （它可能换个关键词再试）—— 对应需求文档 FR-03：工具失败不能崩掉整条请求。
    2. **工具返回给模型的只有摘要字符串**；原始结构化数据写进 ctx（收集箱），不进对话历史。
       理由：省 token / 不丢数据 / 数据不经过模型的手，就不可能被编造。
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional

from ..models.schemas import Attraction, Hotel, Location, WeatherInfo
from . import amap

logger = logging.getLogger(__name__)

# ---- 过滤与相关性（原来写在 attraction_agent 里，v0.3 搬进工具层）----
EXCLUDED_TYPECODE_PREFIXES = ("05", "10")  # 05=餐饮服务 10=住宿服务，不是景点
HOTEL_TYPECODE_PREFIX = "10"  # 住宿服务
RELEVANCE_THRESHOLD = 0.6  # 逐字重叠率阈值；高德对中文会分词，必须自己判相关性
MAX_PAGE_SIZE = 20  # 高德单次返回上限

# ---- 选址（配额保护）----
RANK_MAX_ATTRACTIONS = 8  # 参与排序的景点上限（行程实际每天 2~3 个）
RANK_MAX_HOTELS = 5  # 参与排序的酒店候选上限

# 住宿偏好 → 搜索关键词（高德对"经济型酒店"这种说法不一定有对应 POI）
HOTEL_KEYWORDS = {
    "经济型酒店": "快捷酒店",
    "舒适型酒店": "酒店",
    "豪华酒店": "五星级酒店",
    "民宿": "民宿",
}


# ================================================================ 收集箱


@dataclass
class ToolContext:
    """工具的"上下文 + 收集箱"。

    为什么要有它：工具既要能读到本次请求的上下文（城市/日期），
    又要把结构化结果放在**不经过模型**的地方。这一个对象同时承担两件事。
    """

    city: str
    adcode: Optional[str]
    start_date: str
    travel_days: int
    nights: int = 0

    # ---- 收集箱（结构化数据，不进对话）----
    attractions: dict[str, Attraction] = field(default_factory=dict)
    hotels: dict[str, Hotel] = field(default_factory=dict)
    weather: list[WeatherInfo] = field(default_factory=list)
    chosen_hotel: Optional[Hotel] = None
    chosen_reason: str = ""
    ranked: list[str] = field(default_factory=list)  # rank_hotels 给出的排序（名字，从优到劣）

    # ---- 可观测性与缓存 ----
    calls: list[dict[str, Any]] = field(default_factory=list)  # 每次工具调用一条记录（trace/实验用）
    distance_cache: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ================================================================ 纯函数小工具


def _codes(typecode: Optional[str]) -> list[str]:
    """typecode 可能是 'a|b|c' 多码拼接（实测），统一拆成列表。"""
    return [code for code in (typecode or "").split("|") if code]


def _is_attraction(poi: Attraction) -> bool:
    """排除餐饮与住宿。判定用 any()：只要有一个码是餐饮/住宿就排除。

    实测反例：用「美食」搜出的「新发现·甄选（美容美发店）」typecode 是
    "050106|071100"，宽松规则（要求全中）会把它漏进候选池。
    typecode 缺失时保留：宁可多留一条，也不静默丢数据。
    """
    codes = _codes(poi.typecode)
    if not codes:
        return True
    return not any(code[:2] in EXCLUDED_TYPECODE_PREFIXES for code in codes)


def _is_hotel(poi: Attraction) -> bool:
    """住宿类目判定用**白名单**（只留 typecode 以 10 开头的）。

    与景点相反：找酒店时我们明确只要"住宿服务"这一类，
    多码里只要有一个是住宿就算住宿（酒店 POI 常带"餐饮/会议"等附加类目）。
    """
    return any(code[:2] == HOTEL_TYPECODE_PREFIX for code in _codes(poi.typecode))


def is_relevant(keyword: str, candidate: str, threshold: float = RELEVANCE_THRESHOLD) -> bool:
    """判断高德返回的这条是否真的就是**用户说的那个具体地点**。

    ★ 适用范围（实测教训）：**只用在「按名字解析一个具体地点」的场景**
      （city.py 解析 must_visit / fixed_hotel）。
      **不要用在按类目关键词搜索的场景**：搜「自然风光」返回的
      「杭州西湖风景名胜区-湖滨公园」是完全正确的，但它与关键词的逐字重叠率只有
      0.25 —— 类目词和地名本来就不该按字面比。加上它会让偏好标签的搜索结果
      被 100% 误杀（实测：搜「自然风光」20 条全部被过滤）。

    为什么在没有更好信号时必须自己判：
        高德对中文会**分词**，几乎不返回 0 条 —— 搜「这个景点绝对不存在xyz9」
        它返回「丰盛九玺」「绝对呼吸」（只匹配到"绝对"两个字）。
        而且没有可用的匹配度字段（实测 pois[].match 恒为 "0"、importance 恒为空）。
    """
    word, name = keyword.strip(), candidate.strip()
    if not word or not name:
        return False
    if word in name or name in word:
        return True
    return len(set(word) & set(name)) / len(set(word)) >= threshold


def _trip_dates(start_date: str, travel_days: int) -> list[str]:
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range(travel_days)]


def haversine_km(a: Location, b: Location) -> float:
    """球面直线距离（公里）。本地算，**不消耗配额**。

    用途：给候选酒店排序；也给独立评估者（agents/verify_plan.py）算"一天的景点跨度"。
    真实驾车距离走 measure_distance 工具（要配额），所以策略是「排序用近似，结论用真实」。
    """
    radius = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a.latitude, a.longitude, b.latitude, b.longitude])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _find_in(pool: dict[str, Any], name: str) -> Optional[Any]:
    """按名字（模糊包含）或 key 在收集箱里找一条。找不到返回 None。"""
    if not name:
        return None
    if name in pool:
        return pool[name]
    for key, item in pool.items():
        if name in key or name in getattr(item, "name", ""):
            return item
    return None


def _hotel_of(poi: Attraction, nights: int) -> Hotel:
    """高德 POI → Hotel。价格类字段高德不提供，留空由模型估算。"""
    return Hotel(
        name=poi.name,
        address=poi.address,
        location=poi.location,
        rating=str(poi.rating) if poi.rating is not None else "",
        type=poi.category or "",
        nights=nights,
        poi_id=poi.poi_id,
    )


# ================================================================ 工具实现


def _search_attractions(args: dict, ctx: ToolContext) -> str:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return "错误：keyword 不能为空。请给出搜索关键词（建议用用户的偏好标签）。"
    limit = min(int(args.get("limit") or MAX_PAGE_SIZE), MAX_PAGE_SIZE)

    try:
        hits = amap.search_poi(keyword, ctx.city, offset=limit, page=1)
    except amap.AmapError as exc:
        ctx.errors.append(f"{exc.code}: 搜索「{keyword}」失败（{exc.detail}）")
        return f"搜索失败（{exc.code}）：{exc.detail}。这是配置/配额问题，重试无用。"
    except amap.AmapUnreachable as exc:
        ctx.errors.append(f"UPSTREAM_TIMEOUT: 搜索「{keyword}」失败（{exc}）")
        return f"搜索失败（网络不可达）：{exc}。可以再试一次。"

    added = dropped = 0
    for poi in hits:
        # 这里**不做相关性判定**：关键词是偏好标签（自然风光/博物馆）而不是地名，
        # 按字面比对会误杀正确结果（见 is_relevant 的说明）。类目过滤到此为止。
        if not _is_attraction(poi):
            dropped += 1
            continue
        key = poi.poi_id or poi.name
        if key in ctx.attractions:
            continue
        ctx.attractions[key] = poi
        added += 1

    sample = "、".join(poi.name for poi in hits[:3]) or "无"
    return (
        f"关键词「{keyword}」：返回 {len(hits)} 条，采纳 {added} 条，"
        f"过滤 {dropped} 条（餐饮/住宿类目）。"
        f"累计候选 {len(ctx.attractions)} 条。返回的前几条：{sample}。"
    )


def _get_weather(args: dict, ctx: ToolContext) -> str:
    if not ctx.adcode:
        return "错误：缺少城市编码（adcode），无法查询天气。"
    try:
        forecast = amap.get_weather(ctx.adcode)
    except amap.AmapError as exc:
        ctx.errors.append(f"{exc.code}: 天气查询失败（{exc.detail}）")
        return f"天气查询失败（{exc.code}）：{exc.detail}。"
    except amap.AmapUnreachable as exc:
        ctx.errors.append(f"UPSTREAM_TIMEOUT: 天气查询失败（{exc}）")
        return f"天气查询失败（网络不可达）：{exc}。可以再试一次。"

    days = _trip_dates(ctx.start_date, ctx.travel_days)
    by_date = {item.date: item for item in forecast}
    # 对齐：行程的每一天都要有一个位置；缺的用占位项标记，绝不编造
    ctx.weather = [by_date.get(day) or WeatherInfo(date=day, is_forecast=False, beyond_window=True) for day in days]

    covered = sum(1 for item in ctx.weather if not item.beyond_window)
    line = "；".join(
        f"{item.date} {item.day_weather} {item.day_temp}/{item.night_temp}℃"
        for item in ctx.weather
        if not item.beyond_window
    )
    missing = [item.date for item in ctx.weather if item.beyond_window]
    text = f"预报覆盖 {covered}/{len(days)} 天（高德只提供今天起 4 天）。{line}"
    if missing:
        text += f"。以下日期**没有预报**，必须留空、不许编造：{'、'.join(missing)}"
    return text


def _search_hotels(args: dict, ctx: ToolContext) -> str:
    keyword = str(args.get("keyword") or "").strip() or "酒店"
    keyword = HOTEL_KEYWORDS.get(keyword, keyword)

    try:
        hits = amap.search_poi(keyword, ctx.city, offset=MAX_PAGE_SIZE, page=1)
    except amap.AmapError as exc:
        ctx.errors.append(f"{exc.code}: 搜索酒店失败（{exc.detail}）")
        return f"搜索酒店失败（{exc.code}）：{exc.detail}。"
    except amap.AmapUnreachable as exc:
        ctx.errors.append(f"UPSTREAM_TIMEOUT: 搜索酒店失败（{exc}）")
        return f"搜索酒店失败（网络不可达）：{exc}。"

    added = 0
    for poi in hits:
        if not _is_hotel(poi) or poi.location is None:
            continue
        key = poi.poi_id or poi.name
        if key in ctx.hotels:
            continue
        ctx.hotels[key] = _hotel_of(poi, ctx.nights)
        added += 1

    sample = "、".join(h.name for h in list(ctx.hotels.values())[:3]) or "无"
    return (
        f"关键词「{keyword}」：返回 {len(hits)} 条，采纳住宿类 {added} 条，"
        f"累计候选 {len(ctx.hotels)} 家。前几家：{sample}。"
    )


def _measure_distance(args: dict, ctx: ToolContext) -> str:
    hotel = _find_in(ctx.hotels, str(args.get("hotel") or ""))
    attraction = _find_in(ctx.attractions, str(args.get("attraction") or ""))
    if hotel is None or attraction is None:
        return "错误：没在候选里找到这家酒店或这个景点。请先用 search_hotels / search_attractions 取回候选。"
    if hotel.location is None or attraction.location is None:
        return "错误：其中一方缺少坐标，无法测距。"

    cache_key = f"{hotel.name}->{attraction.name}"
    if cache_key in ctx.distance_cache:
        meters = ctx.distance_cache[cache_key]
    else:
        try:
            result = amap.measure_distance(hotel.location, attraction.location)
        except amap.AmapError as exc:
            return f"测距失败（{exc.code}）：{exc.detail}。"
        except amap.AmapUnreachable as exc:
            return f"测距失败（网络不可达）：{exc}。"
        if result is None:
            return "测距失败：高德没有返回距离。"
        meters = result
        ctx.distance_cache[cache_key] = meters

    return f"{hotel.name} → {attraction.name}：{meters / 1000:.1f} 公里（驾车，高德实测）。"


def _rank_hotels(args: dict, ctx: ToolContext) -> str:
    """加权选址：cost(h) = Σ 距离(h, 景点i) × visit_duration(i) / Σ visit_duration(i)。

    权重用「建议游览时长」——用户在一个景点待得越久，酒店离它近就越值钱。
    排序用**球面直线距离**（本地算，0 配额）；结论建议再用 measure_distance 核一次。
    """
    if not ctx.hotels:
        return "错误：还没有酒店候选，先调 search_hotels。"
    if not ctx.attractions:
        return "错误：还没有景点候选，先调 search_attractions。"

    attractions = list(ctx.attractions.values())[:RANK_MAX_ATTRACTIONS]
    weighted = [a for a in attractions if a.location is not None]
    if not weighted:
        return "错误：候选景点都缺坐标，无法排序。"
    total_weight = sum(max(a.visit_duration, 1) for a in weighted)

    ranking: list[tuple[float, str, Hotel]] = []
    for hotel in list(ctx.hotels.values())[:RANK_MAX_HOTELS]:
        if hotel.location is None:
            continue
        cost = sum(
            haversine_km(hotel.location, a.location) * max(a.visit_duration, 1) for a in weighted
        ) / total_weight
        ranking.append((cost, hotel.name, hotel))

    if not ranking:
        return "错误：候选酒店都缺坐标，无法排序。"
    ranking.sort(key=lambda item: item[0])

    top_n = max(1, min(int(args.get("top_n") or 3), len(ranking)))
    lines = [f"{index}. {name}（加权平均距离 {cost:.1f} km）" for index, (cost, name, _) in enumerate(ranking[:top_n], 1)]
    # 排序结果留在收集箱：Agent 若没给出最终选择，节点可以拿第一名兜底
    ctx.ranked = [name for _, name, _ in ranking]
    ctx.distance_cache["__rank_best__"] = ranking[0][0]
    logger.info("酒店排序完成 | 候选 %d 家 / 景点 %d 个 | 最优 %s", len(ranking), len(weighted), ranking[0][1])
    return "加权距离排序（越小越好，已按建议游览时长加权）：" + "；".join(lines) + "。可用 measure_distance 对心仪的酒店实测驾车距离。"


def _choose_hotel(args: dict, ctx: ToolContext) -> str:
    """把 Agent 的决定**落到收集箱**，而不是让它写在自然语言里再被解析。"""
    hotel = _find_in(ctx.hotels, str(args.get("hotel") or ""))
    if hotel is None:
        return "错误：没在候选里找到这家酒店。请先用 search_hotels 取回候选，再选。"
    try:
        price = int(args.get("price_per_night") or 0)
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        return "错误：price_per_night 必须是正整数（元/晚）。这是估算值，系统会标注为「预估」。"
    if ctx.nights <= 0:
        return "错误：本次行程晚数为 0，不需要住宿。请直接结束。"

    hotel.estimated_cost = price
    ctx.chosen_hotel = hotel
    ctx.chosen_reason = str(args.get("reason") or "")
    return (
        f"已选定：{hotel.name}，{price} 元/晚 × {ctx.nights} 晚 = {price * ctx.nights} 元。"
        f"请结束本轮，不要再调用工具。"
    )


# ================================================================ 注册表


TOOLS: dict[str, dict[str, Any]] = {
    "search_attractions": {
        "description": (
            "按关键词搜索景点候选。工具内部已做去重、餐饮/住宿过滤、相关性校验，"
            "返回的是统计摘要（原始数据不会占你的上下文）。"
            "建议把用户的每个偏好标签各搜一次，关键词用中文景点词（如 自然风光、博物馆、古镇）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，建议用用户的偏好标签"},
                "limit": {"type": "integer", "description": "返回条数，默认 20，上限 20"},
            },
            "required": ["keyword"],
        },
        "handler": _search_attractions,
        "roles": ("attractions",),
    },
    "get_weather": {
        "description": (
            "查询目的地天气，并自动与行程日期对齐。超出高德预报窗口（今天起 4 天）的日期"
            "会被标记为空，**绝不允许编造**。无需参数。"
        ),
        "parameters": {"type": "object", "properties": {}},
        "handler": _get_weather,
        "roles": ("weather",),
    },
    "search_hotels": {
        "description": (
            "按关键词搜索住宿候选。内部只保留高德「住宿服务」类目的 POI，并做去重。"
            "关键词建议用用户的住宿偏好（经济型酒店/舒适型酒店/豪华酒店/民宿）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "住宿关键词；不给则默认搜「酒店」"},
            },
        },
        "handler": _search_hotels,
        "roles": ("hotels",),
    },
    "rank_hotels": {
        "description": (
            "对已搜到的酒店候选做加权距离排序：权重是各景点的建议游览时长，"
            "距离用坐标计算（不消耗配额）。返回前 N 名及加权平均距离。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"top_n": {"type": "integer", "description": "返回前几名，默认 3"}},
        },
        "handler": _rank_hotels,
        "roles": ("hotels",),
    },
    "measure_distance": {
        "description": "实测某家酒店到某个景点的驾车距离（高德距离接口，消耗配额，有缓存）。",
        "parameters": {
            "type": "object",
            "properties": {
                "hotel": {"type": "string", "description": "酒店名（需已在候选里）"},
                "attraction": {"type": "string", "description": "景点名（需已在候选里）"},
            },
            "required": ["hotel", "attraction"],
        },
        "handler": _measure_distance,
        "roles": ("hotels",),
    },
    "choose_hotel": {
        "description": (
            "做出最终决定并结束：选定一家酒店，并给出预估每晚价格。"
            "调用它之后请直接结束，不要再调用其他工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hotel": {"type": "string", "description": "选定的酒店名（需在候选里）"},
                "price_per_night": {"type": "integer", "description": "预估每晚价格（元）"},
                "reason": {"type": "string", "description": "为什么选它（一句话）"},
            },
            "required": ["hotel", "price_per_night"],
        },
        "handler": _choose_hotel,
        "roles": ("hotels",),
    },
}


def schemas_for(role: str) -> list[dict[str, Any]]:
    """给某个 Agent 角色可用的工具清单（OpenAI/DeepSeek 的 tools 格式）。"""
    return [
        {"type": "function", "function": {"name": name, "description": spec["description"], "parameters": spec["parameters"]}}
        for name, spec in TOOLS.items()
        if role in spec["roles"]
    ]


def execute(role: str, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    """执行一次工具调用。**永不抛异常** —— 失败也返回一句人话给模型。"""
    spec = TOOLS.get(name)
    if spec is None:
        return f"错误：没有名为 {name} 的工具。"
    if role not in spec["roles"]:
        return f"错误：{role} 这个角色无权使用 {name}。"
    try:
        result = spec["handler"](args or {}, ctx)
    except Exception as exc:  # noqa: BLE001 —— 工具绝不能让整条请求崩
        logger.exception("工具执行异常 | role=%s tool=%s", role, name)
        ctx.errors.append(f"TOOL_ERROR: {name} 执行异常（{type(exc).__name__}: {exc}）")
        return f"工具执行异常（{type(exc).__name__}）：{exc}。请不要重复调用同样的参数。"
    logger.info("工具调用 | role=%s tool=%s args=%s -> %s", role, name, args, result[:80])
    return result
