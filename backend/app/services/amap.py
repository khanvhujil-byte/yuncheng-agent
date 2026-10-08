"""高德开放平台 Web 服务 REST 适配器（Day 3 实现）。

需求文档 8.5。铁律（CC-2 / 需求文档 6.3）：**原始 JSON 一进门就翻译成 Pydantic，原文立刻丢弃**，
绝不流进任何节点的上下文。一条 POI 原始约 1.5KB、50+ 字段，
裁完只剩 12 个字段、约 300 字节 —— 这是"翻译"之外的第二件正事。

本文件只做四件事：
    1. 翻译   高德方言 → Pydantic（"lng,lat" 字符串、photos 对象数组、分号分类）
    2. 裁剪   50+ 字段 → 只留项目要用的
    3. 安检   infocode → 项目错误码；配置类错误「拦住」，网络类错误「降级」
    4. 兜底   高德用空数组 [] 表示"没有"，不是 null 也不是 ""，必须统一清洗

不做什么：不做去重、不做 typecode 过滤、不做多标签循环（那是 attraction_agent 的业务规则）；
不做重试（V0 失败即降级）；不打印 Key（config.py 已把 httpx 日志压到 WARNING）。

使用的接口（全部在 restapi.amap.com，公共信封 {status, info, infocode, count}）：
    GET /v3/geocode/geo           城市/地址 → location("lng,lat") + adcode     【已实现】
    GET /v3/place/text            POI 关键字搜索（offset/page 分页）            【已实现】
    GET /v3/weather/weatherInfo   天气（city 必须是 adcode）                    【已实现】
    GET /v3/distance              距离测量（一组起终点，type=1 驾车 / 3 步行）   【已实现】
    GET /v3/place/detail          POI 详情                                      【V0 不用】

实测记录（2026-10-08 探针，见 backend/amap_probe.txt）：
    geocode  : location="120.209903,30.246566" adcode="330100" level="市"；district/street 为 []
    place    : count="100"（总匹配数）但 pois[] 只有 offset 条；biz_ext.cost=[]；photos[].url
    weather  : forecasts[0].casts[] 正好 4 天；daytemp 是纯数字字符串 "26"
    distance : results[0].distance="6731"（米，字符串）duration="2002"（秒）

三条硬约束（需求文档 8.5）：
    1. 天气接口的 city 参数必须是 adcode，不是城市名。
    2. extensions=all 只返回「今天起 4 天」的预报，第 5 天起必须留空（不许编造）。
    3. extensions=base 返回的是实况 lives[]，不是预报 → is_forecast=False（V0 未使用）。

关于价格（需求文档 FR-05 / FR-07）：
    POI 的 biz_ext.cost 覆盖不稳定（实测为 []），住宿价格高德基本不提供。
    缺失时一律由模型估算，并标 source="llm_estimated"。

Day 6 可选优化：给「城市→adcode」「城市+关键词→POI」加内存缓存（TTL 1 小时）。
"""

import logging
from typing import Any, Optional

import httpx

from ..config import get_settings
from ..models.schemas import Attraction, Location, WeatherInfo

logger = logging.getLogger(__name__)

BASE_URL = "https://restapi.amap.com/v3"
GEOCODE_URL = f"{BASE_URL}/geocode/geo"
POI_URL = f"{BASE_URL}/place/text"
WEATHER_URL = f"{BASE_URL}/weather/weatherInfo"
DISTANCE_URL = f"{BASE_URL}/distance"

# 高德 infocode → 项目错误码（需求文档 8.4）。
# 这里列的是「确定性错误」（配置/配额/参数），必须拦住，不让它静默降级。
INFOCODE_TO_ERROR: dict[str, str] = {
    "10001": "AMAP_KEY_INVALID",  # Key 不正确或过期
    "10002": "AMAP_KEY_INVALID",  # 服务未开通 / 无权限
    "10009": "AMAP_KEY_INVALID",  # Key 与绑定平台类型不符（用成 JS API Key 就报它）
    "10003": "AMAP_QUOTA_EXCEEDED",  # 日访问量超限
    "10004": "AMAP_QUOTA_EXCEEDED",  # 单位时间访问过于频繁
    "30001": "INVALID_REQUEST",  # 引擎返回数据错误（实测：中文参数被编码破坏时出现）
}


class AmapError(RuntimeError):
    """高德明确回了错误码 —— **该拦住**（配置错、配额满、参数错）。

    为什么要和下面的 AmapUnreachable 分开：
        配置错了却"降级继续"，会产出一份看起来正常、实际全是假数据的行程，
        比直接报错危险得多。这个区分在 config.py 的 probe_amap 里已经用了一次
        （invalid vs unreachable），这里扩展到四个接口。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"[{code}] {detail}")
        self.code = code  # 项目错误码，如 AMAP_KEY_INVALID
        self.detail = detail


class AmapUnreachable(RuntimeError):
    """网络问题 / 超时 / 响应不是 JSON —— 无法判断 Key 是否可用，**该降级**。

    调用方（各 agent）捕到它应当写 warnings + 返回空列表，让主链路继续。
    """


def _s(value: Any) -> Optional[str]:
    """高德的「空」有三种写法：''、[]、None → 统一成 None。

    实测证据（杭州 geocode / place）：
        district / township / street / number = []
        pois[0].biz_ext.cost = []
        pois[0].distance = []
    不统一清洗，下游 float([]) / str([]) 就会崩，或者字段变成字面量 "[]"。
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        return None
    return str(value)


def _loc(text: Any) -> Optional[Location]:
    """高德的 '120.209903,30.246566' → Location。拿不到坐标就返回 None。

    坐标是「能不能用」的判定依据：没有坐标的 POI 在地图上画不出来，
    直接丢弃比填 (0,0) 好 —— 填 (0,0) 会在地图上跑到非洲西边的海里去。
    """
    raw = _s(text)
    if not raw or "," not in raw:
        return None
    lng, lat = raw.split(",", 1)
    try:
        return Location(longitude=float(lng), latitude=float(lat))
    except ValueError:
        return None


def _get(url: str, params: dict) -> dict:
    """统一取数：拼 Key、超时、HTTP 检查、信封检查。**只有这里拼 Key。**"""
    settings = get_settings()
    try:
        resp = httpx.get(
            url,
            params={"key": settings.amap_api_key, **params},
            timeout=settings.amap_timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError 覆盖超时/连接失败/4xx/5xx；ValueError 覆盖响应不是 JSON
        raise AmapUnreachable(f"{type(exc).__name__}: {exc}") from exc

    # 高德的坑：Key 无效时 HTTP 状态码也是 200，只有 infocode 不对。
    # 所以不能只看 resp.status_code。
    infocode = str(payload.get("infocode", ""))
    if infocode != "10000":
        info = str(payload.get("info", ""))
        raise AmapError(
            INFOCODE_TO_ERROR.get(infocode, "AMAP_ERROR"),
            f"infocode={infocode} info={info}",
        )
    return payload


# ================================================================ 四个接口


def geocode(address: str, city: Optional[str] = None) -> tuple[Optional[Location], Optional[str]]:
    """城市/地址 → (中心坐标, adcode)。

    实测：address="杭州" → location="120.209903,30.246566", adcode="330100", level="市"
    返回 (None, None) 表示高德没给出结果（不是错误，是"查不到"）。
    """
    params: dict[str, Any] = {"address": address}
    if city:
        params["city"] = city
    payload = _get(GEOCODE_URL, params)

    rows = payload.get("geocodes") or []
    if not rows:
        return None, None
    row = rows[0]
    return _loc(row.get("location")), _s(row.get("adcode"))


def search_poi(keywords: str, city: str, offset: int = 20, page: int = 1) -> list[Attraction]:
    """关键词搜 POI → list[Attraction]。

    实测：keywords="西湖", city="杭州" → count="100"（总匹配数），pois[] 只给 offset 条。
    所以「拿够候选」要靠调用方翻页或换关键词，不是靠这个函数（那是 attraction_agent 的事）。

    没有坐标的结果会被**直接丢弃**，因为下游地图和距离计算都需要坐标。
    """
    payload = _get(
        POI_URL,
        {
            "keywords": keywords,
            "city": city,
            "citylimit": "true",  # 限制在城市内，否则搜「西湖」会搜出外地的同名景点
            "offset": offset,
            "page": page,
            "extensions": "all",  # 少了它就没有 biz_ext / photos
        },
    )

    result: list[Attraction] = []
    for poi in payload.get("pois") or []:
        location = _loc(poi.get("location"))
        if location is None:
            continue

        biz_ext = poi.get("biz_ext") or {}
        rating = _s(biz_ext.get("rating"))
        type_path = _s(poi.get("type")) or ""
        photos = [
            url
            for photo in (poi.get("photos") or [])
            if (url := _s(photo.get("url")))
        ]

        result.append(
            Attraction(
                name=_s(poi.get("name")) or "",
                address=_s(poi.get("address")) or "",
                location=location,
                # "风景名胜;风景名胜;国家级景点" → 取最后一级 "国家级景点"
                category=type_path.split(";")[-1] or "景点",
                rating=float(rating) if rating else None,
                photos=photos,
                image_url=photos[0] if photos else None,
                poi_id=_s(poi.get("id")),
                # 门票价：高德 biz_ext.cost 实测为空 []，所以这里恒为 0。
                # 0 有两种含义（真免费 / 高德没给），由 sources 区分，不能自己猜。
                ticket_price=0,
            )
        )
    return result


def get_weather(adcode: str) -> list[WeatherInfo]:
    """天气预报（extensions=all）→ list[WeatherInfo]。

    实测：**只返回今天起 4 天**（casts 长度恒为 4）。
    所以「出行日期超出窗口」是必然会发生的情况，调用方必须留空 + 标 beyond_window，
    绝不能编造 —— 这是诚实性实验（编造率）要测的那一类。
    """
    payload = _get(WEATHER_URL, {"city": adcode, "extensions": "all"})

    forecasts = payload.get("forecasts") or []
    if not forecasts:
        return []
    casts = forecasts[0].get("casts") or []

    return [
        WeatherInfo(
            date=_s(cast.get("date")) or "",
            day_weather=_s(cast.get("dayweather")) or "",
            night_weather=_s(cast.get("nightweather")) or "",
            day_temp=_s(cast.get("daytemp")) or 0,  # 实测是纯数字字符串 "26"
            night_temp=_s(cast.get("nighttemp")) or 0,
            wind_direction=_s(cast.get("daywind")) or "",
            wind_power=_s(cast.get("daypower")) or "",
            is_forecast=True,  # extensions=all 给的是预报
        )
        for cast in casts
    ]


def measure_distance(origin: Location, dest: Location, mode: int = 1) -> Optional[float]:
    """两地距离（米）。实测：distance="6731"（字符串）、duration="2002"（秒）。

    mode: 1=驾车，3=步行（高德 v3/distance 的 type 参数）。

    **注意配额**：这个接口一次只支持一组起终点。
    N 家候选酒店 × M 个行程景点 = N×M 次调用，
    所以调用方（resolve_hotel）必须先筛 Top-K 候选，并按坐标对缓存结果。
    """
    payload = _get(
        DISTANCE_URL,
        {
            "origins": f"{origin.longitude},{origin.latitude}",
            "destination": f"{dest.longitude},{dest.latitude}",
            "type": str(mode),
        },
    )

    rows = payload.get("results") or []
    if not rows:
        return None
    distance = _s(rows[0].get("distance"))
    return float(distance) if distance else None
