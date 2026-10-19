"""LangGraph 状态契约，包含候选事实、联合选择、审计与恢复信息。

attractions 保存候选景点，plan.days 保存已编排行程；restaurants/hotels
保存餐厅和酒店候选。统一编排节点同时产出 plan 和最终 hotel。
天气、坐标、建议时长、价格证据和总额由工具/代码提供。
列表、sources 和 usage 的 reducer 支持四路并行采集并累计整个运行的成本。
planning_contract_version 区分新运行和旧恢复日志；失败原因与查询上限跨重排保留。
"""

import operator
from typing import Annotated, Dict, List, Optional, TypedDict

from .schemas import (
    Attraction,
    ConstraintReport,
    DataSource,
    Hotel,
    Location,
    Restaurant,
    TripPlan,
    TripRequest,
    WeatherInfo,
)


def _merge_sources(left: Dict[str, DataSource], right: Dict[str, DataSource]) -> Dict[str, DataSource]:
    """sources 的 reducer：把各节点写的 key 合并成一个字典。

    为什么要它（实测教训）：景点与天气是**并行**节点，各自都想写 sources 里自己那一格。
    没有 reducer 时，LangGraph 判定「同一个 step 往同一个 key 写了两次」并直接抛
    InvalidUpdateError —— 不是"后面的覆盖前面的"，是崩。

    为什么不用 operator.add：那是给列表追加用的，对 dict 会 TypeError。

    上一版这里写的是「不带 reducer，靠纪律让各节点只写自己的 key」——**那是错的**，
    已用实验推翻（两个并行节点各写一个 key → InvalidUpdateError）。
    """
    return {**(left or {}), **(right or {})}


def _merge_usage(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
    """usage 的 reducer：**把各 Agent 的 token 累加**，而不是覆盖。

    为什么必须是"累加"而不是默认覆盖：
        ① 一次请求有 4 个 Agent 各自调模型，覆盖的结果只剩最后一个节点的用量；
        ② 景点与天气是**并行**节点，不带 reducer 会直接抛 InvalidUpdateError。
    为什么和 sources 的 reducer 不一样：sources 是合并字典（各写各的 key），
    usage 是累加数字（同一个 key 反复累加）—— 两个 reducer 各自的语义不同，别混用。
    """
    total = dict(left or {})
    for key, value in (right or {}).items():
        total[key] = total.get(key, 0) + int(value)
    return total


class TraceEntry(TypedDict):
    """一个节点的执行记录。V0 先留空列表，V1 起填充（对应 NFR-2 可观测性）。"""

    node: str  # 节点名，如 collect_attractions
    step: int  # 步号。week1 的教训：日志里没有步号，就无法判断哪些调用属于同一轮
    elapsed_ms: int  # 该节点耗时（毫秒）
    detail: str  # 一句话结果说明，如「取到 9 条候选，其中 3 条标记为估算」


class TripState(TypedDict):
    """贯穿整张图的状态。

    三条刻意为之的约定，都是为了后面不返工：

    1) errors / warnings / trace 必须写成 Annotated[..., operator.add]。
       v0.2 里并行的是两路（景点、天气），它们会同时往这几个字段写。不声明 reducer，
       LangGraph 会判定为「并发写同一个 key」并抛 InvalidUpdateError。
       V0 阶段若先写串行图、看起来用不上，也照旧声明着 —— 零成本，改并行时不用回头改这个文件。

    2) 这里故意没有 messages 字段。
       消息历史只活在每个节点的局部变量里，节点只把结构化结果写回 State。
       加上它，上下文会按「Agent 数 × 轮数」膨胀；而且等发现要拆掉时，
       会牵动所有节点的代码。

    3) 行程骨架（travel_days / nights / end_date）在这里**执行一次**，
       之后所有节点都读 State 里的值，不再各自去解释 request —— 避免出现
       「天气按 3 天查、预算按 5 天算」这种各说各话。
    """

    # ---- 输入（只读，所有节点都能看）----
    request: TripRequest  # 用户填的原始请求

    # ---- validate_city 产出 ----
    city_center: Optional[Location]  # 城市中心坐标；解析失败为 None，此时直接返回 400 CITY_NOT_FOUND
    adcode: Optional[str]  # 行政区划编码，如 "330100"。天气接口只认 adcode，不认「杭州」两个字

    # ---- 行程骨架（v0.2：在 validate_city 里一次算好，后续节点只读）----
    travel_days: int  # = request.travel_days；它是所有约束的「分母」（容量 = 天数 × 每天上限）
    nights: int  # = request.nights 或 travel_days - 1。**住几晚才决定住宿费**，这是晚数的唯一真相源
    # ★ 教训（真实事故）：曾经有个 `hotel_nights` 字段没在这里声明，而 resolve_hotel 往它写值。
    #   LangGraph 对**未声明的键直接丢弃** —— 于是 patch_budget 读到 0 晚，
    #   住宿费算成 260 × 0 = 0。请求不报错、日志不告警，只有金额是错的。
    #   所以：① 写进 state 的每个键都必须声明；② 能不加字段就不加（晚数只留这一个）。
    end_date: str  # = start_date + travel_days - 1，由代码派生，不让用户直接给

    # ---- 锚点解析产物（v0.2，硬约束）----
    must_visit_resolved: List[Attraction]  # 用户必去景点解析后的 POI，**要注入候选池**（偏好搜不到也要有）
    hotel_anchor: Optional[Hotel]  # 用户已定酒店的解析结果；None 表示由系统选址
    hotel_source: Optional[str]
    hotel_reason: str
    # ↑ 住宿来源。取值：user（用户指定）/ llm_estimated（系统选址，价格是模型估的）/ degraded（没选出来）。
    #   不要写窄成 Literal["user","agent"] —— resolve_hotel 会写 llm_estimated，写窄了就对不上。

    # ---- 三路并行采集的产出（彼此看不见对方）----
    attractions: List[Attraction]  # Agent 1：候选景点池（目标 >= travel_days*3，含 must_visit_resolved），不是排期结果
    weather: List[WeatherInfo]  # Agent 2：只覆盖预报窗口（最多 4 天），超出部分 beyond_window=True 且留空
    restaurants: List[Restaurant]
    food_coverage_queries: List[str]  # 当前图运行内跨重排共享的片区补充检索上限
    food_coverage_elapsed_seconds: float
    food_coverage_blocked_code: Optional[str]
    planning_contract_version: int
    unified_planning: bool  # planner owns attractions, meals and hotel IDs
    ticket_lookup_queries: List[str]
    ticket_lookup_elapsed_seconds: float
    ticket_lookup_results: Dict[str, object]
    ticket_estimates: Dict[str, object]
    ticket_estimate_attempted: bool
    planning_started_at: float
    planning_calls: int
    composition_valid: bool
    feasibility: Dict[str, object]
    experience_enabled: bool
    route_cache: Dict[str, object]
    route_query_count: int
    route_blocked_code: Optional[str]  # 仅当前运行：无效Key/配额耗尽后停止后续补边请求

    # ---- resolve_hotel 产出（v0.2：后置，因为要看到真实行程）----
    hotels: List[Hotel]  # 候选池：选址用，也可给前端当「备选」展示
    hotel_candidate_key: str  # 城市/日期/住宿需求签名；复用候选后仍按新景点重排
    hotel: Optional[Hotel]  # 最终住宿：**行程级一家**（不是每天一个）

    # ---- compose_plan 产出 ----
    plan: Optional[TripPlan]  # Agent 4：最终计划。也是 TripState 里唯一对外暴露的计划字段
    retries: int  # 本轮编排的尝试次数（成功也计数），最多 2 次；进入预算重排时重置为 0
    constraint_report: Optional[ConstraintReport]  # v0.2：硬约束对账结果，会透传到 TripPlan
    validate_error: Optional[str]
    # ↑ 上一轮编排校验失败的具体原因。一个字段两用：
    #   ① 回喂给模型（下一轮 prompt 末尾附上「你哪里错了」）
    #   ② 驱动条件边（非空 = 需要重试）—— 因为它俩本来就是同一件事
    tool_calls: Annotated[List[Dict[str, object]], operator.add]
    # ↑ v0.3：每个 Agent 的每次工具调用记一条（角色 / 工具名 / 参数 / 结果摘要）。
    #   两个用途：① trace 与失败排查；② 对照实验里量化「Agent 化到底多花了多少次调用」

    # ---- 降级与诚实性（并行节点共用的「意见箱」）----
    error_code: Optional[str]
    # ↑ v0.2：预检失败的**机器可读原因**（CITY_NOT_FOUND / CONSTRAINT_INFEASIBLE /
    #   ATTRACTION_NOT_FOUND / HOTEL_NOT_FOUND / CITY_MISMATCH / DATE_RANGE_MISMATCH）。
    #   由 validate_city 写入，路由据此填 TripPlanResponse.error_code。
    #   注意它**不带 reducer**：它是单值，多路并发写会冲突 —— 只有预检节点写它。
    errors: Annotated[List[str], operator.add]  # 技术性失败记录，给日志排查用
    warnings: Annotated[List[str], operator.add]  # 给用户看的降级说明，会透传到 TripPlan.warnings
    sources: Annotated[Dict[str, DataSource], _merge_sources]
    # ↑ 字段级来源标记，key 约定与 TripPlan.sources 一致。
    #   **必须带合并 reducer**（见 _merge_sources 的说明）：不带就在并行写时直接崩。
    #   有了它，各节点可以只返回自己那几个 key，框架负责合起来。

    # ---- 跨服务联动：来自 Java 社区的热度数据（热度反哺）----
    community_hot: Optional[Dict[str, object]]
    # ↑ validate_city 从 Java 的 GET /community/hot 读回来的"社区在热什么"，
    #   编排节点会把它作为**参考**写进 prompt（用户明确偏好永远优先）。
    #   Java 不可用时是 None —— 生成照常，只在 sources 里标 degraded，绝不因此失败。
    #   ★ 这个键必须在 TypedDict 里声明：未声明的键会被 LangGraph **静默丢弃**
    #     （我们已经被这个坑咬过一次：hotel_nights 丢失导致住宿费算成 0）。

    # ---- 预算复核结论（v0.7：预算从"事后估算"变成"规划约束"）----
    budget_verdict: Optional[Dict[str, object]]
    # ↑ 由 `agents/verify_budget.py` 在 patch_budget **之后**写：
    #   {status, limit, known_total, estimated_total, unknown_items, basis}。
    #   为什么单独一个键而不是塞回 plan.budget：结论要参与**最终 success 与缓存准入**的判断，
    #   而 plan 是给用户看的产物、会被序列化进行程库；结论属于"过程判定"，分开更清楚。
    #   ★ 同样必须声明 —— 未声明的键会被 LangGraph 静默丢弃。

    budget_retries: int
    # ↑ 已进入的预算重排轮数，初始为 0；只有 prepare_budget_retry 递增，最多 2 轮。
    #   retries 管理每轮编排的校验尝试；usage 仍累计整个请求的调用量和 token。
    repair_rounds: int
    # ↑ 已进入的体验/选择修复轮数（与 budget_retries 分开计数）。
    #   共用一个计数器会让"舒适度修复"吃光预算重排的名额，导致预算超限一次都排不了。

    # ---- 可观测性与上下文计量 ----
    trace: Annotated[List[TraceEntry], operator.add]  # 每完成一个节点追加一条
    usage: Annotated[Dict[str, int], _merge_usage]
    # ↑ 整条请求的 token 与调用次数（**累加**，见 _merge_usage）。
    #   它是「Agent 化的代价」这个对照实验的核心指标：不累加就量不出总成本。
