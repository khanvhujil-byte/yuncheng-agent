"""Agent 2 / 4 · 天气查询专家（collect_weather 节点，**不进 LLM**）。

输入（从 State 读）：adcode（validate_city 产出，**必须用它，不能用城市名**）、
                    request.start_date、travel_days
输出（写回 State）：{"weather": [WeatherInfo, ...], "warnings": [...], "sources": {...}}

为什么这个节点不用 LLM：
    它本质是「adcode + 日期 → 天气数组」的**确定性搬运**。
    参考项目为它配了一个 SimpleAgent 加一大段「必须使用工具」的 prompt，
    属于不必要的智能体化 —— 白花 token、白等 1~3 秒，还多一个出错的地方。

必须做对的三件事：

    1. **city 参数传 adcode**。高德天气接口不接受城市名；传「杭州」会返回空或报错。
    2. **把 4 天窗口和行程天数对齐**（本节点的真正难点）。
       高德 extensions=all 只返回「今天起 4 天」。行程 6 天时后 2 天没有预报 ——
       必须留空 + 标记，**绝不编造**。这是诚实性实验的数据源头。
    3. **两个「没有天气」的原因要分开**，不能混成一个：
         超出预报窗口 → 等长列表 + beyond_window=True（日期在、数据空）
         接口调用失败 → 空列表 + sources="degraded" + warning
       混在一起，前端就没法区分「预报不到」和「接口挂了」。

温度清洗：高德返回的 daytemp 可能带单位，WeatherInfo 的 validator 已经负责清洗，这里不管。
"""

import logging
from datetime import date, timedelta

from ..models.schemas import WeatherInfo
from ..models.state import TripState
from ..services.amap import AmapError, AmapUnreachable, get_weather

logger = logging.getLogger(__name__)


def _trip_dates(start_date: str, travel_days: int) -> list[str]:
    """行程的每一天（YYYY-MM-DD 列表）。解析不了返回空列表，由调用方降级。"""
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range(travel_days)]


def _blank(day: str) -> WeatherInfo:
    """超出预报窗口的**占位项**：日期留着，天气字段全空。

    为什么占位而不是「干脆不输出这一天」：
        前端是按天气列表渲染格子的。6 天行程只给 4 个格子，用户会以为天气功能坏了；
        给 6 个格子、其中 2 个明确标 beyond_window=True，才是「如实说明缺在哪」。
    """
    return WeatherInfo(date=day, is_forecast=False, beyond_window=True)


def collect_weather(state: TripState) -> dict:
    adcode = state.get("adcode")
    travel_days = state["travel_days"]
    days = _trip_dates(state["request"].start_date, travel_days)

    # ---- 前置缺失：连问题都问不出去 ----
    if not adcode or not days:
        logger.warning("天气采集跳过 | adcode=%r | 行程日期=%r", adcode, days)
        return {
            "weather": [],
            "warnings": ["天气数据未获取：缺少城市编码，或出行日期无法解析。"],
            "sources": {"weather": "degraded"},
        }

    # ---- 真实调用（要网络 + 配额）----
    try:
        forecast = get_weather(adcode)
    except AmapError as exc:
        logger.warning("天气采集失败 | %s", exc)
        return {
            "weather": [],
            "errors": [f"{exc.code}: 天气查询失败（{exc.detail}）"],
            "warnings": [f"天气数据获取失败（{exc.code}），本次行程不含天气信息。"],
            "sources": {"weather": "degraded"},
        }
    except AmapUnreachable as exc:
        logger.warning("天气采集不可达 | %s", exc)
        return {
            "weather": [],
            "errors": [f"UPSTREAM_TIMEOUT: 天气查询失败（{exc}）"],
            "warnings": ["天气数据获取失败（网络不可达），本次行程不含天气信息。"],
            "sources": {"weather": "degraded"},
        }

    # ---- 按日期对齐：行程的每一天都要有一个位置 ----
    by_date = {item.date: item for item in forecast}
    result = [by_date.get(day) or _blank(day) for day in days]
    missing = [item.date for item in result if item.beyond_window]

    # ---- 如实报告缺在哪（这里只写「实测缺口」，不写「预计会缺」）----
    warnings: list[str] = []
    if missing:
        if len(missing) == len(days):
            warnings.append(
                f"高德天气预报只覆盖今天起 4 天，本次行程的 {len(days)} 天全部在窗口外，"
                f"因此不提供天气数据（已留空，不编造）。"
            )
        else:
            warnings.append(
                f"以下日期超出高德预报窗口（今天起 4 天），已留空而不是编造："
                f"{'、'.join(missing)}。"
            )

    logger.info(
        "天气采集完成 | 行程 %d 天 | 预报命中 %d 天 | 超窗口 %d 天",
        len(days),
        len(days) - len(missing),
        len(missing),
    )

    return {
        "weather": result,
        "warnings": warnings,
        "sources": {"weather": "amap"},
    }
