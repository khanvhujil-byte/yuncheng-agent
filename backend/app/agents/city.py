"""城市解析与可行性预检节点 validate_city（不进 LLM）。

为什么这个节点单独存在（参考项目没有）：

    1. 高德天气接口的 city 参数必须是 **adcode**，不是城市名 ——
       「拿到 adcode」是所有下游节点的前置依赖，必须有个地方先做掉。
    2. 它是图内「花钱之前」的关口，与 HTTP 入口共用本地输入校验规则。
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

已实现（模块 2b）：must_visit 解析成 POI 锚点（required=True），并写入 must_visit_resolved。
fixed_hotel 在本节点按名称、住宿类目、地址和城市距离解析；无法消歧时要求补充信息。
resolve_hotel 对已解析的指定酒店直接锁定，不允许模型另选酒店。
"""

import logging

from ..models.schemas import Attraction
from ..models.state import TripState
from ..services import java_bridge
from ..services.map_gateway import AmapError, AmapUnreachable, geocode, search_poi
from ..services.request_validation import MAX_SPOTS_PER_DAY as MAX_SPOTS_PER_DAY
from ..services.request_validation import validate_request
from ..services.tools import is_relevant, _is_attraction, _is_hotel, _hotel_of, haversine_km

logger = logging.getLogger(__name__)

FORECAST_WINDOW_DAYS = 4  # 高德 extensions=all 的预报窗口（实测：正好 4 天）
MAX_MUST_VISIT = 10  # 每个必去景点 = 1 次高德搜索调用。上限防「30 天行程塞 90 个景点」


def _is_relevant(keyword: str, candidate: str) -> bool:
    """已搬到 services/tools.py（v0.3：工具/服务层统一持有这类判定）。

    留这个薄封装是为了不散落调用点；实现在 tools.is_relevant，
    注意它**只能用在按名字解析具体地点的场景**，不要用在类目关键词搜索上。
    """
    return is_relevant(keyword, candidate)


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

    # 日期、晚数和完整必去列表容量，与缓存查询前使用同一套本地规则。
    invalid = validate_request(req)
    if invalid is not None:
        return fail(*invalid)

    must_visit = [s.strip() for s in (req.must_visit or []) if s and s.strip()]

    # ---- 预检 4：预报窗口（不拦；**用户可见的警告由 collect_weather 统一给出**）----
    # 为什么不在这里写 warning：travel_days > 4 必然导致天气缺天，而 weather_agent
    # 会给出**精确到日期**的警告。两处都写，用户会看到两条几乎重复的话。
    # 原则：每条 warning 只留一个来源，而且信息要具体。
    if travel_days > FORECAST_WINDOW_DAYS:
        logger.info(
            "预检提示 | 行程 %d 天超出预报窗口 %d 天，天气会缺天（详细警告由 collect_weather 给出）",
            travel_days,
            FORECAST_WINDOW_DAYS,
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
        return fail(exc.code, f"地理编码不可达：{exc}")

    if center is None or not adcode:
        return fail("CITY_NOT_FOUND", f"地理编码查不到 {req.city!r}")
    # ---- 解析 must_visit（硬约束的锚点）----
    # 物理容量和查询上限已经在 validate_request 中校验，不能截断用户硬约束。

    resolved: list[Attraction] = []
    for name in must_visit:
        try:
            hits = search_poi(name, req.city, offset=5)
        except AmapError as exc:
            # TODO(1) 的答案：配置类错误（Key 无效 / 配额满）**透传原码，绝不伪装成「景点搜不到」**。
            # 说"景点不存在"会把用户引到错误方向 —— 他会去改景点名，而问题其实在 Key 或配额。
            return fail(exc.code, f"解析必去景点 {name!r} 时高德报错：{exc.detail}")
        except AmapUnreachable as exc:
            # TODO(2) 的答案：网络问题同理，报 ATTRACTION_NOT_FOUND 是撒谎。
            # 而且预检没做完，不能带着「没验证过的硬约束」继续往下跑。
            return fail(exc.code, f"解析必去景点 {name!r} 时不可达：{exc}")

        if not hits:
            # 真正的 0 条（实测：只有纯拉丁乱码这类才会 0 条）
            return fail("ATTRACTION_NOT_FOUND", f"在城市 {req.city!r} 内搜不到必去景点 {name!r}")

        # 取第一条**相关的**结果，而不是第一条 —— 高德会把「绝对呼吸」这种
        # 只沾一点边的结果排在前面（见 _is_relevant 的说明）。
        matches = [h for h in hits if _is_attraction(h) and _is_relevant(name, h.name)]
        exact = [h for h in matches if h.name == name]
        matches = list({h.poi_id or h.name: h for h in (exact or matches)}.values())
        if len(matches) > 1:
            return fail("ATTRACTION_AMBIGUOUS", f"必去景点 {name!r} 命中多个地点，请提供完整名称")
        chosen = matches[0] if matches else None
        if chosen is None:
            # 有返回、但没一条真的像用户说的那个地方 → 按"搜不到"处理。
            # 绝不用第一条硬凑：它是硬约束，静默换成别的地方比报错危险得多。
            sample = "、".join(h.name for h in hits[:3])
            return fail(
                "ATTRACTION_NOT_FOUND",
                f"必去景点 {name!r} 没有匹配结果（高德返回的前几条是：{sample}）",
            )

        # 打标 required=True —— schemas.py 里已预留，下游和前端据此显示「必去」
        chosen.required = True  # Pydantic v2 允许直接改字段
        resolved.append(chosen)

        # 透明度：解析改名必须写 warning，否则用户以为系统去的是他说的那个地方
        if chosen.name != name:
            detail = chosen.address or "无地址"
            warnings.append(f"必去景点「{name}」已解析为「{chosen.name}」（{detail}）")

    hotel_anchor = None
    if req.fixed_hotel:
        try:
            hits = search_poi(req.fixed_hotel, req.city, offset=20)
        except AmapError as exc:
            return fail(exc.code, "解析指定酒店失败")
        except AmapUnreachable as exc:
            return fail(exc.code, "解析指定酒店服务不可用")
        matches = [h for h in hits if _is_hotel(h) and h.location and _is_relevant(req.fixed_hotel, h.name)]
        if req.fixed_hotel_address:
            address = req.fixed_hotel_address.strip()
            matches = [h for h in matches if address in (h.address or "") or (h.address and h.address in address)]
        exact = [h for h in matches if h.name == req.fixed_hotel]
        matches = list({h.poi_id or (h.name, h.address): h for h in (exact or matches)}.values())
        if not matches:
            return fail("HOTEL_NOT_FOUND", "指定酒店未找到，请补充完整名称和地址")
        if len(matches) != 1:
            return fail("HOTEL_AMBIGUOUS", "指定酒店存在重名分店，请补充地址")
        if haversine_km(center, matches[0].location) > 100:
            return fail("CITY_MISMATCH", "指定酒店距离目的地市中心超过 100 公里，请确认地点")
        hotel_anchor = _hotel_of(matches[0], nights)

    from ..services.regional_stays import resolve_request_anchors
    try:
        stay_locks, trip_endpoints = resolve_request_anchors(req)
    except (AmapError, AmapUnreachable) as exc:
        return fail(exc.code, "解析按日酒店或到离地点失败：" + str(exc))
    except ValueError as exc:
        return fail('CLARIFICATION_REQUIRED', str(exc))

    logger.info(
        "城市解析成功 | city=%s adcode=%s center=%s,%s | 天数=%d 晚数=%d",
        req.city,
        adcode,
        center.longitude,
        center.latitude,
        travel_days,
        nights,
    )

    # 热度反哺：读 Java 社区"现在热门什么"。**失败不影响预检**：
    # 这是加分项，不是必需项 —— 读不到就标 degraded，生成照常进行。
    hot = java_bridge.fetch_hot()

    return {
        "adcode": adcode,
        "city_center": center,
        "error_code": None,
        "errors": [],
        "warnings": warnings,
        "must_visit_resolved": resolved,  # 下游 attraction_agent 会把它注入候选池
        "hotel_anchor": hotel_anchor,
        "stay_locks": stay_locks, "trip_endpoints": trip_endpoints,
        "community_hot": hot,
        # 让"有没有拿到热度"这件事在响应里可见（与 sources 的既有约定一致：
        # 数据从哪来、有没有降级，一律如实标出）
        "sources": {"community_hot": "java" if hot else "degraded"},
    }
