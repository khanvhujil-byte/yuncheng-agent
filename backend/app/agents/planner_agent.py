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

from ..models.schemas import Attraction, ConstraintReport, DayPlan, WeatherInfo
from ..models.state import TripState
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


def _build_user_prompt(state: TripState) -> str:
    req = state["request"]
    anchors = list(state.get("must_visit_resolved") or [])
    must_visit = [a.name for a in anchors]

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
        f"\n【候选景点清单】（每天只能从这里面挑）\n"
        f"{_format_attractions(list(state.get('attractions') or []))}\n"
        f"\n【天气预报】\n"
        f"{_format_weather(list(state.get('weather') or []))}\n"
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

    plan = {
        **_empty_plan(state),
        "days": [day.model_dump() for day in days],
        "overall_suggestions": str(payload.get("overall_suggestions") or ""),
    }
    logger.info(
        "编排完成 | %d 天 | 必去满足 %d/%d | 重试次数 %d",
        len(days),
        len(report.satisfied),
        len(report.required_attractions),
        attempts - 1,
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
