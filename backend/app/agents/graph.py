
"""LangGraph 联合编排接线（契约 v9）。

城市预检 -> 景点/天气/餐饮/酒店四路采集 -> 上下文准备 -> 统一编排
-> 独立复核 -> 路线/门票/舒适度 -> 预算计算与复核。
格式修正每轮最多2次；联合体验与预算复核共享全任务调用/耗时/token上限。
超预算最多进入2轮重排，舒适度修复也受总调用上限约束。
resolve_hotel 仅为旧日志断点恢复保留，新生成不经过该节点。
"""

from langgraph.graph import END, START, StateGraph

import logging

from ..models.state import TripState
from .attraction_agent import collect_attractions
from .budget import patch_budget
from .city import validate_city
from .hotel_agent import resolve_hotel
from .hotel_candidates import collect_hotels
from ..services.unified_planning import prepare_planning_context
from .planner_agent import compose_plan
from .verify_budget import BUDGET_RETRY_LIMIT, prepare_budget_retry, verify_budget
from .verify_plan import verify_plan
from .weather_agent import collect_weather
from .food import collect_food
from ..services.itinerary_experience import enrich_node
from ..services.node_timing import measured

logger = logging.getLogger(__name__)


def route_after_city(state: TripState) -> str:
    """预检结果决定：继续采集，还是直接结束（400）。

    返回值必须是下面 mapping 里的键，大小写敏感。
    """
    if state.get("adcode"):
        return "ok"
    return "fail"


def route_after_city_weather(state: TripState) -> str:
    """与 `route_after_city` **同判据**，但必须是另一个函数对象。

    ★ 这是一个真踩到的坑：LangGraph 用**函数名**给分支命名，
      同一个节点上挂两条由同一个函数产生的条件边会直接报
      `ValueError: Branch with name 'route_after_city' already exists for node 'validate_city'`。
      两条并行的 fan-out 边需要两个不同名的路由函数 —— 逻辑共用，名字分开。
    """
    return route_after_city(state)


def route_after_city_food(state: TripState) -> str:
    return route_after_city(state)


def route_after_city_hotels(state: TripState) -> str:
    return route_after_city(state)


def validate_planning_city(state):
    return {**validate_city(state), 'planning_contract_version': 10}


def route_after_plan(state):
    if state.get('unified_planning') and state.get('experience_enabled') and state.get('composition_valid'):
        return 'next'
    if route_after_audit(state) == 'retry':
        return 'retry'
    return 'next' if state.get('unified_planning') else 'legacy_hotel'


def route_after_audit(state: TripState) -> str:
    """独立复核之后：还能重试就回 compose_plan，否则继续往下走。

    开关仍是 `validate_error`，它现在有**两个来源**：
        ① compose_plan 自己的两层校验（结构 / 必去景点约束）
        ② verify_plan 的独立复核（时间可行性 / 重复排期）
    两者写同一个字段，所以条件边只需要一条逻辑，而不用两个分支。
    封顶 2 次尝试（配合 compose_plan 里的 MAX_ATTEMPTS）。
    """
    from ..services.planning_limits import available
    if not available(state):
        return 'next'
    if state.get("validate_error") and state.get("retries", 0) < 2:
        return "retry"
    return "next"


def route_after_budget(state: TripState) -> str:
    """预算复核之后：还能重排就回 compose_plan，否则结束。

    判据刻意**只看计数器 + 结论**，不看 `validate_error` 有没有被清掉。
    体验/选择修复与预算重排**各用各的计数器**（repair_rounds / budget_retries）：
    共用一个会让体验修复吃光预算重排的名额。两者都封顶 2 轮。
    """
    from ..services.planning_limits import available
    if state.get('unified_planning') and not available(state):
        return 'next'
    if state.get('unified_planning') and state.get('experience_enabled') and not state.get('composition_valid', True):
        return 'next'
    if state.get('unified_planning') and state.get('experience_enabled') and state.get('validate_error') and not str(state['validate_error']).startswith('预算复核不通过'):
        # 体验/选择修复是重排，必须走有界入口并单独计数；否则既不重置
        # compose 的 MAX_ATTEMPTS，也会和预算重排共用名额。
        if int(state.get("repair_rounds") or 0) < BUDGET_RETRY_LIMIT:
            return 'repair'
        return 'next'
    if int(state.get("budget_retries") or 0) < BUDGET_RETRY_LIMIT:
        status = (state.get("budget_verdict") or {}).get("status")
        if status in ("known_exceeded", "estimated_exceeded"):
            return "retry"
    return "next"


def prepare_repair(state: TripState) -> dict:
    """进入体验/选择修复：单独计数并重置本轮编排尝试次数。"""
    used = int(state.get("repair_rounds") or 0) + 1
    logger.info("进入体验修复 | 第 %d/%d 次", used, BUDGET_RETRY_LIMIT)
    return {"repair_rounds": used, "retries": 0}


def route_after_experience(state):
    return 'next' if state.get('unified_planning') and state.get('experience_enabled') else route_after_audit(state)


def build_graph(start_at: str | None = None):
    g = StateGraph(TripState)

    # ---- 登记节点（名字 = 日志和图上显示的名字）----
    for name, function in (
        ("validate_city", validate_planning_city), ("collect_attractions", collect_attractions),
        ("collect_weather", collect_weather), ("collect_food", collect_food),
        ("collect_hotels", collect_hotels), ("prepare_planning_context", prepare_planning_context),
        ("compose_plan", compose_plan), ("verify_plan", verify_plan),
        ("resolve_hotel", resolve_hotel), ("enrich_experience", enrich_node),
        ("patch_budget", patch_budget), ("verify_budget", verify_budget),
        ("prepare_budget_retry", prepare_budget_retry), ("prepare_repair", prepare_repair),
    ):
        g.add_node(name, measured(name, function))

    # ---- 入口 ----
    # start_at：**断点续跑**用。默认从 validate_city 开始；
    # 续跑时把它设成「第一个没做的节点」，前面已完成的昂贵节点就不会重跑
    # （省掉的是 search 调用与整段 LLM 编排）。图的结构本身一行都不用改。
    g.add_edge(START, start_at or "validate_city")

    # ---- 预检分叉：ok 才走四路采集，fail 直接 END ----
    #
    # ★ 两条边**都必须走条件路由**（这是真 bug 的修法，不是风格问题）：
    #   原先景点那条是条件边、天气那条是**普通边** `g.add_edge("validate_city", "collect_weather")`。
    #   普通边一定会触发 —— 所以预检失败时，条件边把流程导向 END 的同时，
    #   天气那一路照样执行，并顺着 collect_weather → compose_plan → verify_plan →
    #   resolve_hotel 把整条下游链跑完（沿途节点各自"跳过"，但 resolve_hotel 会真的起 LLM 循环）。
    #
    #   实测证据：eval_gate 跑一个"必去景点搜不到"的任务，预检已经拦下了，
    #   日志里却出现 `Agent(hotels) 到达步数上限 5，强制结束` —— 白烧一次 LLM 调用与配额。
    #   这不只是浪费钱：**被拦下的请求不该产生任何语义上的下游动作**。
    g.add_conditional_edges(
        "validate_city",
        route_after_city,
        {"ok": "collect_attractions", "fail": END},
    )
    g.add_conditional_edges(
        "validate_city",
        route_after_city_weather,  # 同逻辑、不同函数名（分支名不能重复）
        {"ok": "collect_weather", "fail": END},
    )
    g.add_conditional_edges("validate_city", route_after_city_food, {"ok":"collect_food", "fail":END})

    # Four-way barrier: don't compose until every factual pool is ready.
    g.add_conditional_edges("validate_city", route_after_city_hotels, {"ok": "collect_hotels", "fail": END})
    g.add_edge(["collect_attractions", "collect_weather", "collect_food", "collect_hotels"], "prepare_planning_context")
    g.add_edge("prepare_planning_context", "compose_plan")

    # ---- 两层校验 + 独立复核的重试环 ----
    # compose_plan 自己校验通过之后，还要过一遍**独立评估者**（verify_plan，纯代码）：
    # 它审的是"这份行程自己站不站得住"（时间排不下 / 同一景点排两天），
    # 与 compose_plan 审的"用户要求满足了没"是两个视角 —— 生成者不能给自己打分。
    g.add_edge("compose_plan", "verify_plan")
    g.add_conditional_edges(
        "verify_plan",
        route_after_plan,
        {"retry": "compose_plan", "next": "enrich_experience", "legacy_hotel": "resolve_hotel"},
    )

    # Legacy journal recovery may still need the former hotel node.
    g.add_edge("resolve_hotel", "enrich_experience")
    g.add_conditional_edges("enrich_experience", route_after_experience, {"retry":"compose_plan", "next":"patch_budget"})
    # ★ 复核必须在汇总**之后**。原先的费用检查写在 verify_plan 里，而图顺序是
    #   verify_plan → resolve_hotel → patch_budget —— 检查发生时预算还没算出来，
    #   那条"单日占比"检查因此从来没触发过：它看起来在工作，实际永远不执行。
    #   这也是"代码里有名字有接口，但缺少严格完成条件"的典型。
    g.add_edge("patch_budget", "verify_budget")
    # 预算复核不通过时回炉重排**一次或两次**（上限见 verify_budget.BUDGET_RETRY_LIMIT）。
    # 判据用独立的 budget_retries，不能用 verify_plan 那个 retries（原因见那条注释）。
    g.add_conditional_edges(
        "verify_budget",
        route_after_budget,
        {"retry": "prepare_budget_retry", "repair": "prepare_repair", "next": END},
    )
    g.add_edge("prepare_budget_retry", "compose_plan")
    g.add_edge("prepare_repair", "compose_plan")

    # Two compose attempts × three budget rounds remain bounded, but the graph
    # now includes the food/time audit nodes and exceeds LangGraph's default25.
    return g.compile().with_config({"recursion_limit":80})


app = build_graph()   # 模块级编译，只跑一次
