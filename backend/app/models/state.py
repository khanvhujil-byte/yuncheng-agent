"""LangGraph 图状态 TripState。  —— v0.2（2026-10-07 契约冻结）

位置：models/ 层，与 schemas.py 并列；只被 agents/ 和 api/ 读，自己只 import schemas。
作用：定义「在图上流转的那份数据」。

与 TripPlan 的区别（最容易混的一处）：
    TripPlan   给外部看，只含用户要的信息。
    TripState  给内部用，含过程数据（adcode、retries、trace、usage）。
    两者的交集有两个字段：TripState.plan 和 TripState.hotel。

v0.2 的图形状（方案 D：酒店后置 + 锚点分叉）：

    validate_city                       无 LLM：城市 → adcode + 坐标；顺手做可行性预检
      ├ 解析失败 → END 400 CITY_NOT_FOUND
      ├ 预检不通过 → END 400 CONSTRAINT_INFEASIBLE（**花钱之前就拦下**）
      └ 成功 → 两路并行
             collect_attractions  ∥  collect_weather
                         ↓ 两路全部完成
             compose_plan            只排景点 + 餐饮，**不含酒店**
                         ↓ 两层校验
                第 1 层 Pydantic 结构校验
                第 2 层 约束满足性校验（must_visit 是否真的排进去了）
                    ├ 不通过且 retries < 2 → 回 compose_plan（把具体缺失项回喂）
                    └ retries = 2 → 继续，但写进 ConstraintReport.unsatisfied
                         ↓
             resolve_hotel           分叉：用户已定 → 解析锚点；未定 → 加权选址
                         ↓
             patch_budget            住宿费 = 单价 × nights（不是 × 天数）
                         ↓
                      END

为什么酒店必须**后置**：选址要基于「实际会去哪些景点」。景点候选池有 travel_days×3 个，
其中一半不会去；用候选池选址等于把不去的地方也算进距离里。
"""

import operator
from typing import Annotated, Dict, List, Literal, Optional, TypedDict

from .schemas import (
    Attraction,
    ConstraintReport,
    DataSource,
    Hotel,
    Location,
    TripPlan,
    TripRequest,
    WeatherInfo,
)


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
    nights: int  # = request.nights 或 travel_days - 1。**住几晚才决定住宿费**
    end_date: str  # = start_date + travel_days - 1，由代码派生，不让用户直接给

    # ---- 锚点解析产物（v0.2，硬约束）----
    must_visit_resolved: List[Attraction]  # 用户必去景点解析后的 POI，**要注入候选池**（偏好搜不到也要有）
    hotel_anchor: Optional[Hotel]  # 用户已定酒店的解析结果；None 表示由系统选址
    hotel_source: Literal["user", "agent"]  # 住宿来源，前端据此显示「你指定的」还是「推荐」

    # ---- 两路并行采集的产出（彼此看不见对方）----
    attractions: List[Attraction]  # Agent 1：候选景点池（目标 >= travel_days*3，含 must_visit_resolved），不是排期结果
    weather: List[WeatherInfo]  # Agent 2：只覆盖预报窗口（最多 4 天），超出部分 beyond_window=True 且留空

    # ---- resolve_hotel 产出（v0.2：后置，因为要看到真实行程）----
    hotels: List[Hotel]  # 候选池：选址用，也可给前端当「备选」展示
    hotel: Optional[Hotel]  # 最终住宿：**行程级一家**（不是每天一个）

    # ---- compose_plan 产出 ----
    plan: Optional[TripPlan]  # Agent 4：最终计划。也是 TripState 里唯一对外暴露的计划字段
    retries: int  # 校验失败次数（结构校验 + 约束校验共用）：达到 2 次就走「部分结果」分支，防无限重试
    constraint_report: Optional[ConstraintReport]  # v0.2：硬约束对账结果，会透传到 TripPlan

    # ---- 降级与诚实性（并行节点共用的「意见箱」）----
    errors: Annotated[List[str], operator.add]  # 技术性失败记录，给日志排查用
    warnings: Annotated[List[str], operator.add]  # 给用户看的降级说明，会透传到 TripPlan.warnings
    sources: Dict[str, DataSource]
    # ↑ 字段级来源标记，key 约定与 TripPlan.sources 一致。
    #   注意这个字段「不带」reducer：它是 dict，多路并发写同一个 key 会冲突。
    #   所以纪律是：各节点只写自己那几个 key（景点写 attractions、天气写 weather、
    #   resolve_hotel 写 hotel），key 互不重叠。改并行时必须守住这条纪律，否则会从「写冲突」这里炸。

    # ---- 可观测性与上下文计量 ----
    trace: Annotated[List[TraceEntry], operator.add]  # 每完成一个节点追加一条
    usage: Dict[str, int]  # {"prompt_tokens": .., "completion_tokens": .., "llm_calls": ..}
