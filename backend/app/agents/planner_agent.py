"""Agent 4 / 4 · 行程编排专家（compose_plan 节点）—— **本项目唯一的 LLM 节点**。

它在架构里的位置：两路采集完成之后、住宿求解之前。

    上游：collect_attractions（候选景点池）、collect_weather（天气）
    下游：resolve_hotel（住宿）、patch_budget（预算）
    控制：本节点写 validate_error，图上的条件边 route_after_validate 据此决定
          「回自己重试」还是「继续往下走」

为什么只有它必须用 LLM：
    其它节点的答案都能算出来（查数据、过滤、去重、选址、加法），
    而「把哪些景点排进第几天、怎么让动线合理」是**语义排程**，没有唯一正解。

它的技术难点（也是本模块的全部价值）在三件事：

    1. **Prompt 由代码生成**：把候选景点池/天气格式化成紧凑清单，从 user 消息给，
       而不是把上一段自然语言整段塞进去（参考项目的做法）。
    2. **结构化输出 + 两层校验**：
       第 1 层 结构校验 —— JSON 能否解析、字段类型、天数是否等于 travel_days、日期是否连续
       第 2 层 约束校验 —— 必去景点是否真的排进去了、有没有编造候选池之外的景点
    3. **错误回喂重试**：把「具体哪里错」写进 state["validate_error"]，
       图上的条件边把流程送回本节点，下一轮把这段错误原样附在消息末尾。
       validate_error 既是「回喂内容」也是「重试开关」，因为它俩本来就是同一件事。

不做什么：
    不算预算（budget.py）、不定住宿（resolve_hotel）、不调工具（纯生成）。
"""

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
from .prompts import PLANNER_AGENT_PROMPT

logger = logging.getLogger(__name__)

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


def _select_candidates(candidates: list[Attraction], travel_days: int) -> tuple[list[Attraction], int]:
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

    anchors = [item for item in candidates if item.required]
    limit = max(settings.prompt_max_candidates, travel_days * 4, len(anchors))
    rest = sorted(
        (item for item in candidates if not item.required),
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
    selected, total = _select_candidates(list(state.get("attractions") or []), state["travel_days"])

    pool_note = (
        f"候选池共 {total} 个，已按评分筛出 {len(selected)} 个供你编排"
        if len(selected) < total
        else f"候选池共 {total} 个"
    )

    hot_hint = format_hot_hint(state.get("community_hot"))

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
        f"用户必去景点 must_visit：{'、'.join(must_visit) if must_visit else '（无）'}\n"
        f"\n【候选景点清单】（{pool_note}；每天只能从这里面挑）\n"
        f"{_format_attractions(selected)}\n"
        f"\n【天气预报】\n"
        f"{_format_weather(list(state.get('weather') or []))}\n"
        # 热度反哺：来自 Java 社区的真实统计。没有就整段不加（不给模型空信息）
        + (f"\n{hot_hint}\n" if hot_hint else "")
    )


# ================================================================ 两层校验


def _validate_structure(days: list[DayPlan], start_date: str, travel_days: int) -> Optional[str]:
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
        if not day.attractions:
            return f"{day.date} 这一天没有安排任何景点。"
        if len(day.attractions) > MAX_SPOTS_PER_DAY:
            # 实测模型会把一天排到 5 个景点。这是「硬约束配校验代码」的又一例：
            # prompt 里写了 2~3 个，遵守率不够，必须由第 1 层校验顶住。
            return (
                f"{day.date} 安排了 {len(day.attractions)} 个景点，超过每天上限 "
                f"{MAX_SPOTS_PER_DAY} 个。请重新分配到其他天（每天 2~3 个）。"
            )
        if not day.meals:
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
        if (a.poi_id not in allowed_ids) and (a.name not in allowed_names)
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
            item.typecode = item.typecode or source.typecode
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
            item.image_url = item.image_url or source.image_url
            item.photos = item.photos or list(source.photos)
            item.required = item.required or source.required
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

    # ---- 调模型（错误分两类：确定性错误 / 网络类 —— 和 amap.py 同一套理念）----
    try:
        raw, usage = chat_json(PLANNER_AGENT_PROMPT, _build_user_prompt(state), extra_user=retry_note)
    except LlmError as exc:
        logger.error("LLM 返回错误 | %s", exc)
        return {
            "plan": _empty_plan(state),
            "retries": attempts,
            "validate_error": None,
            "warnings": [f"模型服务不可用（{exc.code}），未能生成行程。"],
            "errors": [f"{exc.code}: {exc.detail}"],
        }
    except LlmUnreachable as exc:
        logger.error("LLM 不可达 | %s", exc)
        return {
            "plan": _empty_plan(state),
            "retries": attempts,
            "validate_error": None,
            "warnings": ["模型服务连接失败，未能生成行程。"],
            "errors": [f"UPSTREAM_TIMEOUT: {exc}"],
        }

    logger.info("编排第 %d 次调用完成 | 输出 %d 字符", attempts, len(raw))

    # ---- 解析 JSON（模型偶尔仍会裹 markdown 代码块）----
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        return {**_retry(attempts, f"输出不是合法 JSON：{exc}", []), "usage": usage}

    # ---- 第 1 层：结构校验 ----
    try:
        days = [DayPlan.model_validate(item) for item in (payload.get("days") or [])]
    except ValidationError as exc:
        return {**_retry(attempts, f"字段结构不合法：{exc.errors()}", []), "usage": usage}

    structure_error = _validate_structure(days, req.start_date, state["travel_days"])
    if structure_error:
        return {**_retry(attempts, structure_error, []), "usage": usage}

    # ---- 第 2 层：约束校验 ----
    report, constraint_error = _validate_constraints(days, anchors, candidates)

    if constraint_error and attempts < MAX_ATTEMPTS:
        return {**_retry(attempts, constraint_error, []), "usage": usage}

    # ---- 通过，或重试用尽 → 落地（重试用尽时如实写 warning，不静默）----
    warnings: list[str] = []
    if constraint_error:
        warnings.append(f"行程已生成，但有一个没满足的要求：{constraint_error}")
    warnings.extend(
        f"未能安排必去景点「{name}」：{report.reasons.get(name, '未知原因')}"
        for name in report.unsatisfied
    )

    # 回填图片/poi_id/评分等「模型不该复述」的字段
    enriched = _enrich(days, candidates)

    plan = {
        **_empty_plan(state),
        "days": [day.model_dump() for day in days],
        "overall_suggestions": str(payload.get("overall_suggestions") or ""),
    }
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
        "constraint_report": report,
        "retries": attempts,
        "validate_error": None,
        "warnings": warnings,
        "usage": usage,
    }


def _usage_of(response: Any) -> dict[str, int]:
    """把 langchain 的 usage 元数据转成状态里的 usage 字段（供成本实验用）。"""
    meta = getattr(response, "usage_metadata", None) or {}
    return {
        "prompt_tokens": int(meta.get("input_tokens") or 0),
        "completion_tokens": int(meta.get("output_tokens") or 0),
        "llm_calls": 1,
    }
