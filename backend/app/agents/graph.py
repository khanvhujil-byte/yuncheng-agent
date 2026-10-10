
"""LangGraph 状态图装配（v0.2：两路并行 + 酒店后置）。

本文件只有接线，没有业务逻辑。
编译在模块级做一次（app = build_graph()），不要每次请求都编译。
"""

from langgraph.graph import END, START, StateGraph

from ..models.state import TripState
from .attraction_agent import collect_attractions
from .budget import patch_budget
from .city import validate_city
from .hotel_agent import resolve_hotel
from .planner_agent import compose_plan
from .verify_plan import verify_plan
from .weather_agent import collect_weather


def route_after_city(state: TripState) -> str:
    """预检结果决定：继续采集，还是直接结束（400）。

    返回值必须是下面 mapping 里的键，大小写敏感。
    """
    if state.get("adcode"):
        return "ok"
    return "fail"


def route_after_audit(state: TripState) -> str:
    """独立复核之后：还能重试就回 compose_plan，否则继续往下走。

    开关仍是 `validate_error`，它现在有**两个来源**：
        ① compose_plan 自己的两层校验（结构 / 必去景点约束）
        ② verify_plan 的独立复核（时间可行性 / 重复排期）
    两者写同一个字段，所以条件边只需要一条逻辑，而不用两个分支。
    封顶 2 次尝试（配合 compose_plan 里的 MAX_ATTEMPTS）。
    """
    if state.get("validate_error") and state.get("retries", 0) < 2:
        return "retry"
    return "next"


def build_graph(start_at: str | None = None):
    g = StateGraph(TripState)

    # ---- 登记节点（名字 = 日志和图上显示的名字）----
    g.add_node("validate_city", validate_city)
    g.add_node("collect_attractions", collect_attractions)
    g.add_node("collect_weather", collect_weather)
    g.add_node("compose_plan", compose_plan)
    g.add_node("verify_plan", verify_plan)
    g.add_node("resolve_hotel", resolve_hotel)
    g.add_node("patch_budget", patch_budget)

    # ---- 入口 ----
    # start_at：**断点续跑**用。默认从 validate_city 开始；
    # 续跑时把它设成「第一个没做的节点」，前面已完成的昂贵节点就不会重跑
    # （省掉的是 search 调用与整段 LLM 编排）。图的结构本身一行都不用改。
    g.add_edge(START, start_at or "validate_city")

    # ---- 预检分叉：ok 走采集，fail 直接 END（不花 LLM 钱）----
    g.add_conditional_edges(
        "validate_city",
        route_after_city,
        {"ok": "collect_attractions", "fail": END},
    )

    # ---- 两路 fan-out：validate_city 同时分出第二条边 ----
    g.add_edge("validate_city", "collect_weather")

    # ---- 汇聚：两条边都指向 compose_plan，它会自动等两路都完成 ----
    g.add_edge("collect_attractions", "compose_plan")
    g.add_edge("collect_weather", "compose_plan")

    # ---- 两层校验 + 独立复核的重试环 ----
    # compose_plan 自己校验通过之后，还要过一遍**独立评估者**（verify_plan，纯代码）：
    # 它审的是"这份行程自己站不站得住"（时间排不下 / 同一景点排两天），
    # 与 compose_plan 审的"用户要求满足了没"是两个视角 —— 生成者不能给自己打分。
    g.add_edge("compose_plan", "verify_plan")
    g.add_conditional_edges(
        "verify_plan",
        route_after_audit,
        {"retry": "compose_plan", "next": "resolve_hotel"},
    )

    # ---- v0.2：酒店后置，然后算钱 ----
    g.add_edge("resolve_hotel", "patch_budget")
    g.add_edge("patch_budget", END)

    return g.compile()


app = build_graph()   # 模块级编译，只跑一次