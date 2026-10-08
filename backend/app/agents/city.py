"""城市解析与可行性预检节点 validate_city（不进 LLM）。

为什么这个节点单独存在（参考项目没有）：

    1. 高德天气接口的 city 参数必须是 **adcode**，不是城市名 ——
       「拿到 adcode」是所有下游节点的前置依赖，必须有个地方先做掉。
    2. 它是全项目**唯一「花钱之前」的关口**：调 LLM 之前先把不可行的请求拦掉。
       跑 20 秒、烧完 token 才报「必去景点装不下」，是最差的体验。
    3. v0.2 的单一真相源：travel_days / nights / end_date 在边界（trip.py）算好，
       本节点**只校验、不重新发明** —— 否则又会出现「各处自己解释 request」。

预检清单（需求文档 FR-05c / 12.5）：

    | 检查项            | 判定                                        | 失败错误码              | 拦不拦 |
    |-------------------|---------------------------------------------|-------------------------|--------|
    | 日期 ↔ 天数一致    | end_date == start_date + days - 1           | DATE_RANGE_MISMATCH     | 拦     |
    | 晚数范围          | 0 <= nights <= travel_days                  | INVALID_REQUEST         | 拦     |
    | 容量              | len(must_visit) <= travel_days * 3          | CONSTRAINT_INFEASIBLE   | 拦     |
    | 城市可解析         | geocode(city) 拿到 adcode + 坐标            | CITY_NOT_FOUND          | 拦     |
    | 高德确定性错误      | infocode != 10000（Key/配额/参数）           | AMAP_*                  | 拦     |
    | 预报窗口          | travel_days > 4                             | ——                      | 不拦，写 warning |

    拦住 = 不写 adcode，图的条件边直接走到 END，路由把 error_code 透给前端。

下一步（模块 2b）在这里补：must_visit / fixed_hotel 的锚点解析与城市一致性校验
（ATTRACTION_NOT_FOUND / ATTRACTION_AMBIGUOUS / HOTEL_NOT_FOUND / HOTEL_AMBIGUOUS / CITY_MISMATCH）。
"""

import logging
from datetime import date, timedelta
from typing import Optional

from ..models.state import TripState
from ..services.amap import AmapError, AmapUnreachable, geocode

logger = logging.getLogger(__name__)

MAX_SPOTS_PER_DAY = 3  # 每天景点上限。容量 = 天数 × 它（与 planner 的 prompt 约束同源）
FORECAST_WINDOW_DAYS = 4  # 高德 extensions=all 的预报窗口（实测：正好 4 天）


def _parse_date(text: Optional[str]) -> Optional[date]:
    """'2026-10-01' → date；解析不出来返回 None（不抛异常，由调用方决定怎么处理）。"""
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def validate_city(state: TripState) -> dict:
    """把城市名解析成坐标 + adcode，并做完所有「不花 LLM 钱」的可行性检查。"""
    req = state["request"]
    travel_days = state["travel_days"]  # 边界已经算好，这里只校验
    nights = state["nights"]
    warnings: list[str] = []

    def fail(code: str, detail: str) -> dict:
        """预检失败：**不写 adcode** —— 路由据此判定「没通过预检」并把 error_code 透出去。"""
        logger.warning("预检不通过 | %s | %s", code, detail)
        return {
            "adcode": None,
            "city_center": None,
            "error_code": code,
            "errors": [f"{code}: {detail}"],
            "warnings": warnings,
        }

    # ---- 预检 1：日期区间 ↔ 天数一致（v0.2 的单一真相源）----
    start = _parse_date(req.start_date)
    if start is None:
        return fail("DATE_RANGE_MISMATCH", f"start_date 不是合法日期：{req.start_date!r}")
    expect_end = (start + timedelta(days=travel_days - 1)).isoformat()
    if req.end_date and req.end_date != expect_end:
        return fail(
            "DATE_RANGE_MISMATCH",
            f"end_date={req.end_date} 与 start_date+travel_days-1={expect_end} 不一致",
        )

    # ---- 预检 2：晚数范围 ----
    if not 0 <= nights <= travel_days:
        return fail("INVALID_REQUEST", f"nights={nights} 越界（应在 0~{travel_days}）")

    # ---- 预检 3：容量（必去景点装不下就别花钱）----
    must_visit = [s.strip() for s in (req.must_visit or []) if s and s.strip()]
    capacity = travel_days * MAX_SPOTS_PER_DAY
    if len(must_visit) > capacity:
        return fail(
            "CONSTRAINT_INFEASIBLE",
            f"必去 {len(must_visit)} 个，但 {travel_days} 天最多安排 {capacity} 个",
        )

    # ---- 预检 4：预报窗口（不拦，但提前承诺降级 —— 不能等跑到一半才说）----
    if travel_days > FORECAST_WINDOW_DAYS:
        warnings.append(
            f"行程 {travel_days} 天，超出高德天气预报窗口（{FORECAST_WINDOW_DAYS} 天）；"
            f"第 {FORECAST_WINDOW_DAYS + 1} 天起没有天气预报，系统会留空而不是编造。"
        )

    # ---- 真实地理编码（这一步要网络 + 花配额）----
    try:
        center, adcode = geocode(req.city)
    except AmapError as exc:
        # 确定性错误（Key 无效 / 配额满 / 参数错）：拦住，不降级 —— 降级会产出一份假行程
        #
        # 语境映射（实测发现）：高德把「地址解析不出来」也归到 30001 ENGINE_RESPONSE_DATA_ERROR，
        # 跟「参数被编码破坏」是同一个码。但在**本节点这个语境**里，它等价于「这个城市名查不到」，
        # 所以映射成 CITY_NOT_FOUND，让前端提示「请检查城市名」而不是「参数有问题」。
        if exc.code == "INVALID_REQUEST":
            return fail("CITY_NOT_FOUND", f"地理编码无法解析 {req.city!r}（高德 {exc.detail}）")
        return fail(exc.code, exc.detail)
    except AmapUnreachable as exc:
        # 网络错误：也拦住。没有 adcode，天气那一路必废；且此时无法判断 Key 是否可用
        return fail("UPSTREAM_TIMEOUT", f"地理编码不可达：{exc}")

    if center is None or not adcode:
        return fail("CITY_NOT_FOUND", f"地理编码查不到 {req.city!r}")

    logger.info(
        "城市解析成功 | city=%s adcode=%s center=%s,%s | 天数=%d 晚数=%d",
        req.city,
        adcode,
        center.longitude,
        center.latitude,
        travel_days,
        nights,
    )

    return {
        "adcode": adcode,
        "city_center": center,
        "error_code": None,
        "errors": [],
        "warnings": warnings,
        # ---- 模块 2b 在这里补：把 must_visit / fixed_hotel 解析成锚点 ----
        "must_visit_resolved": [],
        "hotel_anchor": None,
    }
