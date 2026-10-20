"""唯一的默认 LLM 编排节点：联合选择景点、餐厅和酒店。

上游提供真实候选、用户要求和天气；模型只输出候选 ID 和选择理由。
绑定候选事实后执行结构、身份、必去点、餐次与多样性校验。
后续代码计算真实路线、舒适度与费用，具体失败原因限次回馈本节点。
旧完整对象输出契约只为已有运行日志恢复保留。
"""

import inspect
import json
import logging
from datetime import date, timedelta
from typing import Any, Optional

from pydantic import ValidationError

from ..config import get_settings
from ..models.schemas import Attraction, ConstraintReport, DayPlan, WeatherInfo
from ..models.state import TripState
from ..services.java_bridge import format_hot_hint
from ..services.llm import LlmError, LlmUnreachable, chat_json
from ..services.itinerary_experience import city_characteristics
from ..services.schedule_repair import dedupe_attractions, preflight_plan
from .prompts import LEGACY_PLANNER_AGENT_PROMPT, PLANNER_AGENT_PROMPT

logger = logging.getLogger(__name__)

def _chat_json(prompt: str, user: str, *, extra_user: str = "", max_tokens: int | None = None):
    """Call the LLM client, forwarding max_tokens only when the callable supports it.

    Tests and adapters replace ``chat_json`` with small fakes that only accept
    ``(system, user, extra_user=...)``; production passes an explicit output
    token budget so long itineraries are not truncated into invalid JSON.
    """
    try:
        params = inspect.signature(chat_json).parameters
        accepts = "max_tokens" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return chat_json(prompt, user, extra_user=extra_user, max_tokens=max_tokens)
    return chat_json(prompt, user, extra_user=extra_user)

MAX_ATTEMPTS = 2  # 与 graph.py 的 route_after_validate 封顶保持一致（尝试次数）
MAX_SPOTS_PER_DAY = 4  # 每天景点硬上限。prompt 里要的是 2~3 个，超过 4 个说明排得太满（实测会排到 5 个）


# ================================================================ prompt 组装


def _format_attractions(spots: list[Attraction]) -> str:
    """候选景点池 → 紧凑清单。只给模型需要的字段，不加任何多余的字。"""
    lines = []
    for index, spot in enumerate(spots, start=1):
        location = spot.location
        lines.append(
            f"{index}. {spot.name} | poi_id={spot.poi_id or '-'} | "
            f"坐标={location.longitude},{location.latitude} | "
            f"分类={spot.category or '-'} | 建议时长={spot.visit_duration}分钟 | "
            f"门票={spot.ticket_price if spot.ticket_price is not None else '未知'}元（{spot.ticket_price_status}） | "
            f"地址={spot.address or '-'}"
            + (" | 【用户必去】" if spot.required else "")
        )
    return "\n".join(lines) if lines else "（无候选景点）"


def _format_weather(items: list[WeatherInfo]) -> str:
    """天气 → 逐日一行。超出窗口的天明确写「无预报」，避免模型自己脑补。"""
    if not items:
        return "（无天气数据）"
    lines = []
    for item in items:
        if item.beyond_window:
            lines.append(f"{item.date}: 超出预报窗口，天气未知（不要编造）")
        else:
            lines.append(
                f"{item.date}: {item.day_weather}/{item.night_weather} "
                f"{item.day_temp}~{item.night_temp}℃ {item.wind_direction}{item.wind_power}"
            )
    return "\n".join(lines)


def _select_candidates(candidates: list[Attraction], travel_days: int, goals=None, weather=None) -> tuple[list[Attraction], int]:
    """上下文压缩：只把最有用的候选送进 prompt（对应 learn-claude-code 的 s08）。

    为什么需要：实测一次 3 天请求的候选池有 41 条，**全塞进 prompt 会白烧掉一大截输入 token**，
    而编排每天只用 2~3 个 —— 清单再长也用不完。
    怎么选（顺序即优先级）：
        ① **必去锚点永远在内**（硬约束绝不能被"省钱"裁掉，否则必去景点会静默消失）
        ② 其余按高德评分降序（无评分按 0），同分时游览时长更长的优先
    上限 = max(prompt_max_candidates, 天数 × 4, 锚点数)：
        「够用」优先于「省 token」—— 天数多时必须给够，否则编排无米下锅。

    返回 (选中的候选, 候选池总数)；总数用于在 prompt 里**如实说明筛过**，
    让模型知道清单是子集（否则它可能以为"就这么多景点"）。
    """
    total = len(candidates)
    settings = get_settings()
    if not settings.prompt_compact or total == 0:
        return candidates, total

    from ..services.day_goals import matches
    from ..services.itinerary_experience import severe_weather
    need_indoor = any(severe_weather(w) for w in (weather or []))
    anchors = [item for item in candidates if item.required or any(matches(item, goal) for goal in (goals or []))
               or (need_indoor and any(word in item.name for word in ('博物馆', '美术馆', '科技馆', '水族馆', '海洋馆')))]
    limit = max(settings.prompt_max_candidates, travel_days * 4, len(anchors))
    rest = sorted(
        (item for item in candidates if item not in anchors),
        key=lambda item: (item.rating or 0.0, item.visit_duration),
        reverse=True,
    )
    selected = anchors + rest[: max(limit - len(anchors), 0)]
    logger.info("编排 prompt 压缩 | 候选池 %d → 送模型 %d（锚点 %d 必留）", total, len(selected), len(anchors))
    return selected, total


def _build_user_prompt(state: TripState) -> str:
    req = state["request"]
    anchors = list(state.get("must_visit_resolved") or [])
    must_visit = [a.name for a in anchors]
    from ..services.day_goals import effective_goals
    selected, total = _select_candidates(list(state.get("attractions") or []), state["travel_days"], effective_goals(req), state.get('weather'))

    pool_note = (
        f"候选池共 {total} 个，已按评分筛出 {len(selected)} 个供你编排"
        if len(selected) < total
        else f"候选池共 {total} 个"
    )

    hot_hint = format_hot_hint(state.get("community_hot"))
    from ..services.requirements import context
    hotel = state.get("hotel")
    hotel = hotel.model_dump() if hasattr(hotel, "model_dump") else hotel
    prior_hotel = (f"\n【已有住宿锚点】{hotel['name']} | 坐标={hotel.get('location')}；"
                   "重排优先围绕它分片，是否可复用由系统实测，不要自行改酒店。\n") if hotel else ""

    if state.get('unified_planning'):
        from ..services.unified_prompt import build_prompt
        return build_prompt(state, selected, total)
    return (
        f"【行程要求】\n"
        f"目的地：{req.city}\n"
        f"开始日期：{req.start_date}\n"
        f"结束日期：{state['end_date']}\n"
        f"天数 travel_days：{state['travel_days']}\n"
        f"晚数 nights：{state['nights']}（住宿由系统决定，你只需在每天填 accommodation 类型）\n"
        f"交通方式：{req.transportation}\n"
        f"住宿偏好：{req.accommodation}\n"
        f"偏好标签：{'、'.join(req.preferences) or '（未指定）'}\n"
        f"额外要求：{req.free_text_input or '（无）'}\n"
        + context(req, "food", "attractions", "hotel", "transport", "pace", "weather", "budget")
        + "\n美食优先时先从真实候选选择三餐restaurant_id，以用餐点作为动线锚点安排景点；"
          "citywide取消就近偏好，但仍需满足实际通勤和每日时间限制。\n"
        f"用户必去景点 must_visit：{'、'.join(must_visit) if must_visit else '（无）'}\n"
        f"总预算上限：{req.budget_limit if req.budget_limit is not None else '未限制'} 元（按一位出行者口径，包含门票、三餐、住宿晚数与市内交通）\n"
        f"\n【先验时间配额】全日上限{req.requirements.pace.max_day_minutes}分钟；"
        "普通行程建议先把每天游玩控制在360分钟以内（120分钟景点通常2~3个），"
        "其余时间必须留给三餐135分钟、交通和休息。这个配额是初排启发式，"
        "不是实际路线报价；不要为凑景点数用满四个，也不能缩短候选建议时长。\n"
        + prior_hotel
        + f"\n【城市特点（静态编辑指南，非实时事实）】\n{' '.join(city_characteristics(req.city))}\n"
        f"\n【真实餐饮候选；只可选择这些 POI，不可编造餐厅】\n"
        + "\n".join(f"{r.name} | restaurant_id={r.poi_id} | 人均参考={r.avg_cost if r.avg_cost is not None else '未知'} | 榜单={r.ranking_name or '无已核验榜单'} | 坐标={r.location.longitude},{r.location.latitude}" for r in list(state.get('restaurants') or [])[:30])
        + "\n"
        f"\n【候选景点清单】（{pool_note}；每天只能从这里面挑）\n"
        f"{_format_attractions(selected)}\n"
        f"\n【天气预报】\n"
        f"{_format_weather(list(state.get('weather') or []))}\n"
        # 热度反哺：来自 Java 社区的真实统计。没有就整段不加（不给模型空信息）
        + (f"\n{hot_hint}\n" if hot_hint else "")
    )


# ================================================================ 两层校验


def _validate_structure(days: list[DayPlan], start_date: str, travel_days: int, request=None) -> Optional[str]:
    """第 1 层：结构校验。通过返回 None，不通过返回**具体**的错误说明（用于回喂）。"""
    if len(days) != travel_days:
        return f"days 数量是 {len(days)}，但要求正好 {travel_days} 天。"

    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return None  # 日期本身有问题，交给预检管，不在这里重复报

    for offset, day in enumerate(days):
        expect = (start + timedelta(days=offset)).isoformat()
        if day.date != expect:
            return f"第 {offset + 1} 天（day_index={offset}）的 date 应是 {expect}，实际是 {day.date}。"
        if day.day_index != offset:
            return f"第 {offset + 1} 天的 day_index 应是 {offset}，实际是 {day.day_index}。"
        from ..services.day_window import policy
        window = policy(request, expect) if request else None
        if not day.attractions and not (window and window['allow_empty_attractions']):
            return f"{day.date} 这一天没有安排任何景点。"
        if len(day.attractions) > MAX_SPOTS_PER_DAY:
            # 实测模型会把一天排到 5 个景点。这是「硬约束配校验代码」的又一例：
            # prompt 里写了 2~3 个，遵守率不够，必须由第 1 层校验顶住。
            return (
                f"{day.date} 安排了 {len(day.attractions)} 个景点，超过每天上限 "
                f"{MAX_SPOTS_PER_DAY} 个。请重新分配到其他天（每天 2~3 个）。"
            )
        if not day.meals and not (window and not window['meal_types']):
            return f"{day.date} 这一天没有安排三餐。"
    return None


def _validate_constraints(
    days: list[DayPlan],
    anchors: list[Attraction],
    candidates: list[Attraction],
) -> tuple[ConstraintReport, Optional[str]]:
    """第 2 层：业务约束校验。返回（对账报告, 需要重试时的错误说明）。

    两个约束：
        ① 用户必去的景点必须真的出现在 days[].attractions 里
        ② 每天的景点必须来自候选池 —— **防编造**（模型很爱编"著名景点"）
    """
    placed_ids = {a.poi_id for day in days for a in day.attractions if a.poi_id}
    placed_names = {a.name for day in days for a in day.attractions}

    # ① 必去景点对账（用**已解析的锚点**匹配，不是用户原话：
    #    用户说「西湖」，POI 实际叫「杭州西湖风景名胜区」）
    satisfied, unsatisfied, reasons = [], [], {}
    for anchor in anchors:
        hit = (anchor.poi_id and anchor.poi_id in placed_ids) or anchor.name in placed_names
        if hit:
            satisfied.append(anchor.name)
        else:
            unsatisfied.append(anchor.name)
            reasons[anchor.name] = "编排未能把它安排进任何一天"

    # ② 编造检查
    allowed_ids = {a.poi_id for a in candidates if a.poi_id}
    allowed_names = {a.name for a in candidates}
    invented = [
        a.name
        for day in days
        for a in day.attractions
        if (a.poi_id not in allowed_ids if a.poi_id else a.name not in allowed_names)
    ]

    report = ConstraintReport(
        required_attractions=[a.name for a in anchors],
        satisfied=satisfied,
        unsatisfied=unsatisfied,
        reasons=reasons,
        hotel_anchor=None,
        hotel_source="agent",
        feasible=True,
    )

    if invented:
        unique = sorted(set(invented))
        return report, f"以下景点不在候选清单里（属于编造，禁止）：{'、'.join(unique)}。"
    if unsatisfied:
        return report, f"用户必去的景点没有排进行程：{'、'.join(unsatisfied)}。请把它们安排进某一天。"
    return report, None


# ================================================================ 主节点


def _retry(attempts: int, error: str, warnings: list[str]) -> dict:
    """需要重试：把具体错误写进 validate_error，条件边据此把流程送回本节点。"""
    logger.warning("编排校验未通过（第 %d 次）| %s", attempts, error)
    return {"retries": attempts, "validate_error": error, "warnings": warnings}


def _enrich(days: list[DayPlan], candidates: list[Attraction]) -> int:
    """用候选池回填「不该让模型复述」的字段：图片、poi_id、评分、类目、地址。

    为什么必须回填（前端验证时发现的真问题）：
        候选池里的景点本来带着高德图片（photos[].url），但 prompt 只让模型给
        名字/坐标/时长/描述 —— 图片 URL 这种长字符串让它复述，
        等于白烧 token，而且它可能抄错或干脆省略（实测：行程里的景点首图全丢）。
        所以分工是：**模型负责"点出哪些景点"，具体字段从候选池按 poi_id/名字补回来**。
        这和"天气不让 compose_plan 复述"是同一条原则：外部数据不进模型的手。

    返回回填成功的景点数（用于日志/实验）。
    """
    index: dict[str, Attraction] = {}
    for spot in candidates:
        index[spot.name] = spot
        if spot.poi_id:
            index[spot.poi_id] = spot

    filled = 0
    for day in days:
        for item in day.attractions:
            source = (index.get(item.poi_id) if item.poi_id else None) or index.get(item.name)
            if source is None:
                continue
            filled += 1
            item.poi_id = item.poi_id or source.poi_id
            item.typecode = source.typecode
            item.category = source.category
            # ★ 坐标用候选池的**规范化坐标覆盖**模型输出（真 bug 的修法，不是防御性代码）。
            #   原先这里只补图片/评分/poi_id，**坐标沿用模型给的值**。而下一个节点
            #   verify_plan 判定"一天地理跨度是否超过 25km"用的就是这些坐标 ——
            #   于是可能出现：模型点名正确、坐标写错，地理复核建立在一个错误坐标上，
            #   要么漏报真实的长距离奔波，要么报出一个不存在的问题。
            #   原则与"天气不让模型复述"完全一致：**地理事实来自数据快照，模型只负责点哪个景点**。
            item.location = source.location
            # ★ 门票价同样用池子里的值覆盖（含"未知"这个状态）：
            #   prompt 里教模型输出 ticket_price=0，而池子里其实是 unknown ——
            #   不覆盖的话，模型那句 0 会被当成"免费"，正好把"未知"洗成"不要钱"。
            item.ticket_price = source.ticket_price
            item.ticket_price_status = source.ticket_price_status
            item.ticket_price_source = source.ticket_price_source
            item.ticket_price_evidence_url = source.ticket_price_evidence_url
            item.ticket_price_note = source.ticket_price_note
            item.admission_policy = source.admission_policy
            # 建议时长是数据源/规则事实，不能靠缩短模型时长绕过舒适性复核。
            item.visit_duration = max(30, source.visit_duration)
            item.image_url = item.image_url or source.image_url
            item.photos = item.photos or list(source.photos)
            item.required = source.required
            item.retrieval_scope = source.retrieval_scope
            # ↑ 「必去」标记也在这里回填。它是我们自己的锚点解析结果（city.py 设的
            #   required=True），不该由模型决定 —— 实测模型输出里没有这个字段，
            #   不补的话前端不会给用户打「必去」标。
            if item.rating is None:
                item.rating = source.rating
            if not item.address:
                item.address = source.address
            if not item.category or item.category == "景点":
                item.category = source.category or item.category
            if not item.description:
                item.description = source.description
    return filled


def _empty_plan(state: TripState) -> dict:
    """彻底排不出来时的「部分结果」骨架：字段齐全但 days 为空。

    为什么给骨架而不是 None：下游 patch_budget 要读 plan；给 None 会在那里炸。
    「字段齐全 + days 为空 + 明确 warning」比 500 好。
    """
    req = state["request"]
    return {
        "city": req.city,
        "start_date": req.start_date,
        "end_date": state["end_date"],
        "days": [],
        "overall_suggestions": "",
    }


def compose_plan(state: TripState) -> dict:
    from ..services.planning_limits import available
    if not available(state):
        return {'validate_error': state.get('validate_error') or '本次编排已达到调用、时间或 token 上限，保留草稿。'}
    req = state["request"]
    attempts = state.get("retries", 0) + 1
    candidates = list(state.get("attractions") or [])
    anchors = list(state.get("must_visit_resolved") or [])

    # ---- 前置：没有候选景点就别浪费一次 LLM 调用 ----
    if not candidates:
        logger.warning("编排跳过 | 候选景点为空")
        return {
            "plan": _empty_plan(state),
            "constraint_report": ConstraintReport(
                required_attractions=[a.name for a in anchors],
                satisfied=[],
                unsatisfied=[a.name for a in anchors],
                reasons={a.name: "景点候选为空，无法编排" for a in anchors},
                feasible=False,
            ),
            "retries": attempts,
            "validate_error": None,
            "warnings": ["没有可用的景点候选，未能生成行程。"],
            "errors": ["compose_plan: attractions 为空，跳过 LLM 调用"],
        }

    # ---- 组装：system 是规则，user 是数据；重试时再附一条「你上次错在哪」----
    previous_error = state.get("validate_error")
    retry_note = (
        f"你上一次的输出没通过校验，具体问题：\n{previous_error}\n请只输出修正后的完整 JSON。"
        if previous_error
        else ""
    )

    # 长行程的 JSON 明显更长：给足输出 token，否则会被截断成非法 JSON。
    max_tokens = min(8192, max(2048, 1000 + int(state["travel_days"]) * (360 if state.get('accommodation_version', 0) >= 11 else 300)))

    def call_planner(extra: str):
        from ..services.planning_limits import available
        counter = state.setdefault('_planner_call_counter', {'count': 0})
        if not available({**state, 'planning_calls': int(state.get('planning_calls') or 0) + counter['count']}):
            raise LlmError('PLANNING_LIMIT', '已达到本次编排调用、时间或token上限，停止额外格式修复。')
        counter['count'] += 1
        prompt = PLANNER_AGENT_PROMPT if state.get('unified_planning') else LEGACY_PLANNER_AGENT_PROMPT
        if state.get('unified_planning') and state.get('accommodation_version', 0) < 11:
            prompt += '\n本任务为旧契约恢复，仅输出hotel_id而非stays，整程一家酒店；无住宿时hotel_id=null。'
        return _chat_json(prompt, _build_user_prompt(state), extra_user=extra, max_tokens=max_tokens)

    def parse_json(text_raw: str):
        from ..services.planner_json import loads
        return loads(text_raw)

    # ---- 调模型（错误分两类：确定性错误 / 网络类 —— 和 amap.py 同一套理念）----
    try:
        raw, usage = call_planner(retry_note)
    except LlmError as exc:
        logger.error("LLM 返回错误 | %s", exc)
        # 不写 plan：保留上一轮已生成的有效行程（若有）。写空 plan 会把旧行程
        # 顶掉，随后的 enrich/budget 便只能交付"行程未生成"。
        return {
            "retries": attempts,
            "validate_error": state.get('validate_error') or f"模型服务错误 [{exc.code}]：{exc.detail}",
            "warnings": [f"模型服务不可用（{exc.code}），未能生成行程。"],
            "errors": [f"{exc.code}: {exc.detail}"],
        }
    except LlmUnreachable as exc:
        logger.error("LLM 不可达 | %s", exc)
        return {
            "retries": attempts,
            "validate_error": state.get('validate_error') or "模型服务连接失败，编排未完成。",
            "warnings": ["模型服务连接失败，未能生成行程。"],
            "errors": [f"UPSTREAM_TIMEOUT: {exc}"],
        }

    logger.info("编排第 %d 次调用完成 | 输出 %d 字符", attempts, len(raw))

    # ---- 解析 JSON（模型偶尔仍会裹 markdown 代码块或漏逗号）----
    try:
        payload: dict[str, Any] = parse_json(raw)
    except json.JSONDecodeError as exc:
        if not get_settings().planner_format_retry_enabled:
            return {**_retry(attempts, f"输出不是合法 JSON：{exc}", []), "usage": usage}
        # 格式错误不是"语义上排不出来"：在同一节点内做一次格式修复重试，
        # 不消耗本轮的语义重试名额。实测这是长行程失败的最主要原因之一。
        logger.warning("编排输出不是合法 JSON，执行同节点格式修复重试 | %s", exc)
        repair = ((retry_note + "\n") if retry_note else "") + (
            f"你上一次的输出不是合法 JSON（{exc}）。只输出一个 JSON 对象："
            "不要 markdown 代码块、不要注释、不要省略逗号，所有字符串用双引号。")
        try:
            raw2, usage2 = call_planner(repair)
        except (LlmError, LlmUnreachable) as exc2:
            logger.error("格式修复重试失败 | %s", exc2)
            return {**_retry(attempts, f"输出不是合法 JSON：{exc}", []), "usage": usage}
        usage = {key: int(usage.get(key, 0)) + int(usage2.get(key, 0))
                 for key in set(usage) | set(usage2)}
        try:
            payload = parse_json(raw2)
        except json.JSONDecodeError as exc2:
            return {**_retry(attempts, f"输出不是合法 JSON：{exc2}", []), "usage": usage}
    if isinstance(payload, dict) and payload.get('data_requests') and state.get('accommodation_version', 0) >= 11:
        from ..services.planning_data import fulfill
        try:
            update = fulfill(payload['data_requests'], state)
        except (ValueError, TypeError, KeyError) as exc:
            return {**_retry(attempts, f'补采请求无效：{exc}', []), 'usage': usage}
        return {**update, **_retry(attempts, '补采已按运行配额处理。请依据更新后的候选和planning_data_requests结果生成完整行程；不足时明确冲突，不要重复补采。', update.get('warnings', [])), 'usage': usage}
    if isinstance(payload, dict) and 'meal_patches' in payload:
        from ..services.planner_patch import expand
        try:
            payload = expand(payload, state)
        except (ValueError, TypeError, KeyError) as exc:
            return {**_retry(attempts, f'餐次修复无效：{exc}', []), 'usage':usage}
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        return {**_retry(attempts, "输出必须是含 days 数组的 JSON 对象。", []), "usage": usage}

    chosen_hotel = None
    if state.get('unified_planning'):
        from ..services.unified_planning import bind_choices
        try:
            payload, chosen_hotel = bind_choices(payload, state)
            from ..services.intercity import effective_request
            req = effective_request(req, payload)
        except (ValueError, TypeError, KeyError) as exc:
            return {**_retry(attempts, f"统一编排选择无效：{exc}。内部planner_draft已保留，可用meal_patches仅修正错误餐次，再完整复核。", []),
                    "usage": usage, 'planner_draft': payload if state.get('accommodation_version', 0) >= 11 else None}
    # ---- 第 1 层：结构校验 ----
    try:
        days = [DayPlan.model_validate(item) for item in (payload.get("days") or [])]
    except ValidationError as exc:
        return {**_retry(attempts, f"字段结构不合法：{exc.errors()}", []), "usage": usage}

    structure_error = _validate_structure(days, req.start_date, state["travel_days"], req if state.get('accommodation_version', 0) >= 11 else None)
    if structure_error:
        return {**_retry(attempts, structure_error, []), "usage": usage}

    if req.requirements.food.priority == "food_first" and state.get("experience_enabled"):
        identifiers = {r.poi_id for r in state.get("restaurants", [])}
        for day in days:
            if sorted(m.type for m in day.meals) != sorted(day.meal_types if day.meal_types is not None else ['breakfast', 'lunch', 'dinner']) or any(
                    not m.restaurant_id or m.restaurant_id not in identifiers for m in day.meals):
                return {**_retry(attempts, "美食优先时，必须先从真实餐饮候选指定早中晚三餐restaurant_id，再围绕它们安排景点。", []), "usage": usage}

    # ---- 第 2 层：约束校验 ----
    from ..services.schedule_repair import restore_required_anchors
    required_notes = restore_required_anchors(days, anchors, req,
                           chosen_hotel.model_dump() if chosen_hotel else None)
    report, constraint_error = _validate_constraints(days, anchors, candidates)

    if constraint_error and attempts < MAX_ATTEMPTS:
        return {**_retry(attempts, constraint_error, []), "usage": usage}

    # ---- 通过，或重试用尽 → 落地（重试用尽时如实写 warning，不静默）----
    warnings: list[str] = list(required_notes)
    if constraint_error:
        warnings.append(f"行程已生成，但有一个没满足的要求：{constraint_error}")
    warnings.extend(
        f"未能安排必去景点「{name}」：{report.reasons.get(name, '未知原因')}"
        for name in report.unsatisfied
    )

    # 回填图片/poi_id/评分等「模型不该复述」的字段
    enriched = _enrich(days, candidates)
    # Deterministic dedupe: a model may repeat the same POI on two days. Repeating
    # it is never useful, and leaving it for verify_plan to reject would waste the
    # bounded retry budget (and can still end in a partial result). Keep the first
    # occurrence, never empty a day, and tell the user what changed.
    from ..services.requirements import excluded as _excluded
    replaceable = [c for c in candidates if not _excluded(c.name, req.requirements.attractions)]
    dedupe_notes = dedupe_attractions(days, replaceable)
    warnings.extend(dedupe_notes)
    from ..services.day_goals import audit_days
    goal_errors = audit_days({'days': [day.model_dump() for day in days]}, req)
    if goal_errors:
        constraint_error = '\n'.join(filter(None, [constraint_error, *goal_errors]))
        if attempts < MAX_ATTEMPTS:
            return {**_retry(attempts, constraint_error, warnings), 'usage': usage}

    plan = {
        **_empty_plan(state),
        "days": [day.model_dump() for day in days],
        "overall_suggestions": str(payload.get("overall_suggestions") or ""),
        'origin_city': req.origin_city, 'adult_count': req.adult_count, 'room_count': req.room_count,
        'intercity_legs': payload.get('intercity_legs', []),
        'transport_warnings': state.get('transport_warnings', []),
    }
    if payload.get('accommodation_version') == 11:
        from ..services.stays import attach_boundaries
        plan.update(accommodation_version=11, stays=payload['stays'],
                    regional_weather=state.get('regional_weather', []), hotel_nights=state['nights'])
        attach_boundaries(plan, req, endpoints=state.get('trip_endpoints'))
        plan['hotel'] = chosen_hotel.model_dump() if chosen_hotel else None
    choice_errors = []
    selection_update = {}
    if state.get('unified_planning'):
        from ..services.unified_planning import audit_choices
        choice_errors = audit_choices(plan, state, chosen_hotel)
        if choice_errors:
            from ..services.unified_planning import correction_context
            feedback = "\n".join(choice_errors) + correction_context(plan, state, chosen_hotel)
            if attempts < MAX_ATTEMPTS and not state.get('experience_enabled'):
                return {**_retry(attempts, feedback, []), "usage": usage}
            choice_errors = [feedback]
        warnings.extend(choice_errors)
        reason = str(payload.get('hotel_reason') or '编排 Agent 综合住宿位置、逐日动线和预算选择；实际交通由工具复核。')
        if chosen_hotel is not None and chosen_hotel.cost_source == 'static_estimate':
            warnings.append(f'酒店每晚{chosen_hotel.estimated_cost}元仅为预算预留，尚未获得实际房价。')
        selection_update = {'hotel': chosen_hotel, 'hotel_reason': reason,
                            'hotel_source': 'user' if req.fixed_hotel else 'agent',
                            'sources': {'hotel': 'user' if req.fixed_hotel else state.get('sources', {}).get('hotel', 'amap')}}
        report.hotel_anchor = chosen_hotel.name if chosen_hotel else None
        report.hotel_source = 'user' if req.fixed_hotel else 'agent'
    # Identity/required-stop validation and source facts have already passed.
    # Reduce an obviously overfull optional selection locally, before hotel
    # selection or paid route retrieval, instead of asking the model to repeat.
    plan, load_notes = preflight_plan(plan, {**state, 'request': req})
    warnings.extend(load_notes)
    logger.info(
        "编排完成 | %d 天 | 必去满足 %d/%d | 重试次数 %d | 回填字段 %d 个景点",
        len(days),
        len(report.satisfied),
        len(report.required_attractions),
        attempts - 1,
        enriched,
    )
    return {
        "plan": plan,
        "planner_draft": None,
        "constraint_report": report,
        "retries": attempts,
        # 重试耗尽只停止再生成，不能清除仍然成立的约束错误。
        "validate_error": constraint_error or ("\n".join(choice_errors) if choice_errors else None),
        **selection_update,
        "warnings": warnings,
        "usage": usage,
        'transport_candidates': state.get('transport_candidates', {}),
    }


def _usage_of(response: Any) -> dict[str, int]:
    """把 langchain 的 usage 元数据转成状态里的 usage 字段（供成本实验用）。"""
    meta = getattr(response, "usage_metadata", None) or {}
    return {
        "prompt_tokens": int(meta.get("input_tokens") or 0),
        "completion_tokens": int(meta.get("output_tokens") or 0),
        "llm_calls": 1,
    }
