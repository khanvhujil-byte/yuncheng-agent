"""Agent 1 / 4 · 景点搜集专家（collect_attractions 节点）—— v0.3 薄壳。

本节点只做四件事，**真正的活全在 services/tools.py 的工具里**：

    1. 建收集箱，把**必去景点锚点**先放进候选池（硬约束优先，且不能被后来的覆盖）
    2. 起一轮 ReAct 循环，把"搜什么关键词"交给 LLM 决定
    3. 从收集箱取出结构化候选池（原始数据从不经过模型的手）
    4. 如实汇总 warnings / errors / sources / tool_calls

为什么锚点要先放进去：偏好标签搜不到"灵隐寺"，但它必须出现在候选池里 ——
硬约束不能依赖搜索的运气。放在前面还有个副作用：同一个 poi_id 后来又被搜到时，
先入池的那条（带 required=True）会胜出，标记不会被覆盖掉。
"""

import logging

from ..models.schemas import Attraction
from ..models.state import TripState
from ..services.tools import ToolContext
from .prompts import ATTRACTION_AGENT_PROMPT
from .react_loop import run_agent

logger = logging.getLogger(__name__)

TARGET_PER_DAY = 3  # 每天备几个候选（编排每天实际用 2~3 个）
MAX_STEPS = 5  # 这个 Agent 的步数上限：标签数 + 收尾，5 步足够


def _user_prompt(state: TripState) -> str:
    req = state["request"]
    labels = "、".join(req.preferences) or "（用户未勾选，请用「景点」作为关键词）"
    anchors = [a.name for a in (state.get("must_visit_resolved") or [])]
    return (
        f"城市：{req.city}\n"
        f"行程天数：{state['travel_days']}\n"
        f"偏好标签：{labels}\n"
        f"用户必去景点（已在候选池里，不用再搜）：{'、'.join(anchors) if anchors else '（无）'}\n"
        f"目标候选条数：{state['travel_days'] * TARGET_PER_DAY} 条"
    )


def collect_attractions(state: TripState) -> dict:
    req = state["request"]
    anchors = list(state.get("must_visit_resolved") or [])

    ctx = ToolContext(
        city=req.city,
        adcode=state.get("adcode"),
        start_date=req.start_date,
        travel_days=state["travel_days"],
        nights=state["nights"],
    )
    for anchor in anchors:
        ctx.attractions[anchor.poi_id or anchor.name] = anchor

    result = run_agent("attractions", ATTRACTION_AGENT_PROMPT, _user_prompt(state), ctx, max_steps=MAX_STEPS)

    spots = list(ctx.attractions.values())
    target = state["travel_days"] * TARGET_PER_DAY
    warnings: list[str] = []
    if len(spots) < target:
        warnings.append(
            f"景点候选只有 {len(spots)} 个，少于目标 {target} 个"
            f"（{state['travel_days']} 天 × {TARGET_PER_DAY}）；编排会按实际数量安排，不凑数。"
        )
    if result.stop_reason == "max_steps":
        warnings.append("景点助手达到步数上限，已用当前候选继续。")
    if not result.ok:
        warnings.append("景点助手调用模型失败，仅返回已获取的数据。")

    logger.info(
        "景点采集完成 | 步数 %d | 工具调用 %d 次 | 候选 %d 条（锚点 %d）",
        result.steps,
        len(ctx.calls),
        len(spots),
        len(anchors),
    )
    return {
        "attractions": spots,
        "warnings": warnings,
        "errors": list(ctx.errors),
        "sources": {"attractions": "amap" if result.ok else "degraded"},
        "tool_calls": ctx.calls,
    }
