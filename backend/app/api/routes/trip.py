"""行程规划路由（HTTP 层）。

职责只有三件事：造初始 state、跑图、把结果**组装**成 TripPlanResponse。
业务逻辑（搜景点 / 排行程 / 选住宿 / 算预算）全在 agents/ 里，这里一行都不写。

路径：router 前缀 "/trip" + main.py 里叠的 "/api" = POST /api/trip/plan

为什么要在这个文件里「组装」TripPlan（而不是直接 data = final["plan"]）：
    compose_plan 产出的 plan 是**那一刻的快照**，里面只有行程本体；
    之后 resolve_hotel 才算出住宿、校验层才产出 constraint_report、
    warnings/sources 更是多个节点共写的 reducer 字段。
    这些碎片来自五个地方，永远不可能都属于 plan，
    所以必须有一个组装点 —— 选在 HTTP 边界，就是本文件的 _to_trip_plan()。

注意：函数用 def 而不是 async def —— graph.invoke() 是同步阻塞调用，
写成 async def 会堵住 FastAPI 的事件循环，其他请求全部排队。

V1 待做：GET /api/trip/{thread_id}（需要 LangGraph Checkpointer）
"""

from datetime import date, timedelta
from uuid import uuid4

from fastapi import APIRouter

from ...agents.graph import app as trip_graph
from ...models.schemas import TripPlanResponse, TripRequest

router = APIRouter(prefix="/trip", tags=["旅行规划"])

# 预检失败的错误码 → 给人看的一句话（前端还会用自己的 ERROR_MESSAGES 再映射一层）
_PREFLIGHT_MESSAGES: dict[str, str] = {
    "CITY_NOT_FOUND": "城市解析失败，请检查城市名。",
    "DATE_RANGE_MISMATCH": "日期区间和天数对不上，请改到一致。",
    "CONSTRAINT_INFEASIBLE": "必去景点的数量超过了行程容量，请减少景点或增加天数。",
    "ATTRACTION_NOT_FOUND": "必去景点里有一个搜不到，请检查名字。",
    "ATTRACTION_AMBIGUOUS": "必去景点有多个同名结果，请写得更具体。",
    "HOTEL_NOT_FOUND": "你指定的酒店没搜到，建议补上地址。",
    "HOTEL_AMBIGUOUS": "你指定的酒店重名了，请补上地址。",
    "CITY_MISMATCH": "你指定的酒店与目的地城市相距太远。",
}


def _resolve_end_date(req: TripRequest) -> str:
    """结束日期 = start_date + travel_days - 1（v0.2：由代码派生，不让用户直接给）。

    用户提供了 end_date 就先用它（validate_city 会校验它和天数是否一致）；
    解析不出来就兜底成 start_date，避免把空串传给下游做日期运算。
    """
    if req.end_date:
        return req.end_date
    try:
        start = date.fromisoformat(req.start_date)
    except ValueError:
        return req.start_date
    return (start + timedelta(days=req.travel_days - 1)).isoformat()


def _initial_state(req: TripRequest) -> dict:
    """构造图的初始状态。

    注意：这里把「天数 / 晚数 / 日期」一次算好（v0.2 的单一真相源），
    后面所有节点只读 state，不再各自解释 request。
    """
    return {
        # 输入
        "request": req,
        # 行程骨架
        "travel_days": req.travel_days,
        "nights": req.nights if req.nights is not None else req.travel_days - 1,
        "end_date": _resolve_end_date(req),
        # 预检/采集产物（先给空值，节点会覆盖）
        "city_center": None,
        "adcode": None,
        "must_visit_resolved": [],
        "attractions": [],
        "weather": [],
        "hotels": [],
        "hotel": None,
        "hotel_anchor": None,
        "hotel_source": "agent",
        # 计划与校验
        "plan": None,
        "retries": 0,
        "constraint_report": None,
        "validate_error": None,
        "tool_calls": [],
        # 降级与可观测性（三个 reducer 字段必须初始化成 list）
        "error_code": None,
        "errors": [],
        "warnings": [],
        "trace": [],
        "sources": {},
        "usage": {},
    }


def _to_trip_plan(final: dict, req: TripRequest) -> dict:
    """把图的最终 state「组装」成对外的 TripPlan 形状。

    这是全项目唯一的组装点。TripPlan 的成员来自**五个地方**：
        city / start_date / end_date      ← 用户输入（不在 plan 里！plan 可能是空的）
        days / overall_suggestions / weather_info ← compose_plan
        budget                            ← patch_budget（塞回 plan）
        hotel / hotel_nights              ← resolve_hotel（留在 state，没进 plan）
        constraint_report                 ← 两层校验
        warnings / sources                ← 多个节点共写的 reducer 字段（在 state 顶层）

    为什么不能写成 data = final["plan"]：
        compose_plan 产出的 plan 是「那一刻的快照」，之后没有任何人回头改它；
        hotel 是在它之后才算出来的，warnings/sources 更是跨节点的 reducer 字段。
        这些碎片拼不到一起，所以必须在这里合。
    """
    plan = final.get("plan") or {}
    warnings = list(final.get("warnings") or [])
    return {
        # ① 先用 plan 的内容打底
        **plan,
        # ② 再用「权威来源」覆盖。TripPlan 的三个必填字段来自 request，不能让 plan 决定生死：
        #    如果 plan 为空（编排彻底失败），缺这三个字段会让 Pydantic 抛
        #    ValidationError → FastAPI 返回 500 —— 而这条路径正是「返回部分结果」，
        #    是你最想展示的降级能力，绝不能炸。
        "city": req.city,
        "start_date": req.start_date,
        "end_date": final.get("end_date") or req.start_date,
        # ③ 行程之外的碎片，统一在这里合进来
        #
        # weather_info 也在这里合，而不是让 compose_plan 复述：天气是外部数据，
        # 让模型把 4 天的天气抄一遍 = 白烧 token + 给幻觉留门（它可能"顺手"改个温度）。
        # 原则：外部数据在边界拼进响应，模型只负责它真正要生成的部分。
        "weather_info": final.get("weather") or [],
        "hotel": final.get("hotel"),
        "hotel_nights": final.get("nights", 0),
        "constraint_report": final.get("constraint_report"),
        "warnings": warnings,
        "sources": final.get("sources") or {},
    }


@router.post("/plan", response_model=TripPlanResponse, summary="生成行程")
def plan_trip(req: TripRequest) -> TripPlanResponse:
    """同步阻塞接口。

    函数签名用 def 而不是 async def：graph.invoke() 是同步调用（真实版本要跑几十秒），
    写成 async def 会堵住 FastAPI 的事件循环，其他请求全部排队。
    用 def 时 FastAPI 会自动把它丢进线程池。
    """
    thread_id = uuid4().hex
    final = trip_graph.invoke(_initial_state(req))

    warnings = list(final.get("warnings") or [])

    # 结局一：预检失败（validate_city 没拿到 adcode，图已经走到 END）
    # 错误码由 validate_city 写进 state，这里不再硬编码 —— 否则
    # CONSTRAINT_INFEASIBLE / HOTEL_NOT_FOUND / CITY_MISMATCH 都会被报成 CITY_NOT_FOUND。
    if not final.get("adcode"):
        code = final.get("error_code") or "CITY_NOT_FOUND"
        return TripPlanResponse(
            success=False,
            message=_PREFLIGHT_MESSAGES.get(code, "请求未通过可行性预检，请检查表单后重试。"),
            error_code=code,
            warnings=warnings,
            thread_id=thread_id,
        )

    data = _to_trip_plan(final, req)
    days = data.get("days") or []

    # 结局二：有行程 + 无降级 → 完整成功
    # 结局三：有行程 + 有降级 → **部分结果**（success=False 但 data 有内容，前端照常渲染）
    # 结局四：连行程都没有（plan 为空）→ 仍是 200 + success=False + data（days 为空），不抛 500
    #
    # success 的口径（需求文档 8.2）：success = 是否拿到**完整**行程。
    #
    # v0.3 修正判据：从「warnings 非空」改成「sources 里有没有 degraded」。
    #   原因：warnings 里混着两类东西 ——
    #     降级类（某一路数据没取到）   → 行程确实不完整
    #     说明类（「必去景点『西湖』已解析为『杭州西湖风景名胜区』」）→ 行程是完整的
    #   以前只要有任何一句话就判 success=False，导致一次正常成功的规划也报「部分结果」。
    #   而 sources 天生就是「哪一路降级了」的标记，用它判断才对。
    sources = data.get("sources") or {}
    degraded = [key for key, value in sources.items() if value == "degraded"]
    ok = bool(days) and not degraded
    message = "" if ok else f"已返回部分结果：{'、'.join(degraded) or '行程未生成'} 未取到完整数据，请见上方提示。"
    return TripPlanResponse(
        success=ok,
        message=message,
        data=data,
        warnings=warnings,
        thread_id=thread_id,
    )