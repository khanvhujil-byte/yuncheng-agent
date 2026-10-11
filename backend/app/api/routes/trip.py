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

import copy
import json
import logging
import time
from datetime import date, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ...agents.graph import app as trip_graph
from ...agents.graph import build_graph
from ...agents.verify_budget import verify_budget
from ...agents.verify_plan import verify_plan
from ...agents.weather_agent import weather_warnings
from ...models.schemas import TripPlanResponse, TripRequest
from ...services import amap, runner, store
from ...services.tools import ToolContext, execute
from ..security import rate_limit, require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/trip",
    tags=["旅行规划"],
    # 鉴权与限流挂在**路由级**而不是逐个端点：漏掉一个端点就等于漏掉一个口子，
    # 挂在 router 上，以后新增端点会自动继承。两者默认关闭（见 security.py 的说明）。
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)

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


def _degraded_sources(payload: dict) -> list[str]:
    """哪些数据源降级了。`success` 的判据之一是它，而不是「warnings 是否为空」。"""
    return [key for key, value in (payload.get("sources") or {}).items() if value == "degraded"]


def _budget_reasons(verdict: dict | None) -> list[str]:
    """预算复核结论里**不能算成功**的那几种（v0.7）。

    注意 `unverified`（存在未知项）**不在这里**：它是"我们没确认"，不是"确实超了"。
    它照样会作为 warning 告诉用户，也照样会阻止入库，但不把整份行程判成失败 ——
    否则只要有一个景点门票查不到，所有行程都会变成"部分结果"，那是在惩罚用户。
    """
    if not verdict:
        return []
    status = verdict.get("status")
    if status == "known_exceeded":
        return [f"预算已超限（已知费用已超）：{verdict.get('basis', '')}"]
    if status == "estimated_exceeded":
        return [f"按估算很可能超预算：{verdict.get('basis', '')}"]
    return []


def _cacheable_budget_status(verdict: dict | None) -> bool:
    """只有"没设上限"或"确认在上限内"的行程才允许进缓存。

    为什么 `unverified` 也不行：缓存是**给之后的人复用**的答案。
    把一份"预算没验证过"的行程存进去，等于把一次不确定性固化成默认答案。
    """
    return ((verdict or {}).get("status") or "none") in ("none", "within")


def _unresolved_reasons(final: dict, data: dict) -> list[str]:
    """这份计划**还没解决掉**的问题（用来判 success，也用来决定能不能入库）。

    ★ 这是一个真 bug 的修法。原先的判据是 `bool(days) and not degraded` ——
    只看"有没有数据"和"数据路有没有降级"，完全没看**校验结论**。于是：

        两层校验 / 独立复核 判定不合格（写了 validate_error），但 retries 用尽后
        流程会继续往下走（选酒店、算预算），最后这份不合格计划
        「有 days + 无降级」→ 被标成 success=True 并写进行程库。

    后果有两层：① 用户看到"成功"，而系统自己知道这份计划排不下；
    ② 更糟的是**它进了缓存**，之后每次命中都拿到这份不合格计划（把一次失败永久固化）。

    两个来源：
        validate_error          最后一次校验给出的具体原因（两层校验或独立复核写的）
        constraint_report.unsatisfied  硬约束（必去景点）未满足项
    """
    reasons: list[str] = []
    if final.get("validate_error"):
        reasons.append(str(final["validate_error"]))

    report = final.get("constraint_report") or data.get("constraint_report")
    unsatisfied: list = []
    if report is not None:
        # constraint_report 是 Pydantic 对象（来自图）或 dict（来自行程库反序列化），两种都要认
        unsatisfied = list(getattr(report, "unsatisfied", None) or (report.get("unsatisfied") if isinstance(report, dict) else None) or [])
    reasons.extend(str(item) for item in unsatisfied)
    return reasons


def _partial_message(degraded: list[str], unresolved: list[str], has_days: bool) -> str:
    """部分结果的说明。**必须说清是"哪一类"没达成**，不能只丢一句"部分结果"。"""
    parts: list[str] = []
    if not has_days:
        parts.append("行程未生成")
    if degraded:
        parts.append(f"{'、'.join(degraded)} 未取到完整数据")
    if unresolved:
        # 去掉校验原因自带的句号：否则拼出来是"……请去掉重复。，请见上方提示。"
        parts.append("未通过校验：" + "；".join(reason.rstrip("。") for reason in unresolved[:2]))
    return "已返回部分结果：" + "，".join(parts) + "，请见上方提示。"


def _finalize(
    final: dict,
    req: TripRequest,
    thread_id: str,
    fingerprint: str,
    elapsed_ms: int,
    *,
    cached: bool = False,
    cache_hit_count: int = 0,
    cache_saved_at: str | None = None,
) -> TripPlanResponse:
    """把图跑完之后的 state 组装成响应 —— **同步路径与后台路径共用这一个出口**。

    为什么必须共用：组装逻辑是最容易漏字段的地方（已经踩过 hotel、weather_info 两次）。
    两条路径各写一份，等于把这个坑复制成两份。

    success 的口径（需求文档 8.2）：success = 是否拿到**完整**行程。
    判据是「sources 里有没有 degraded」，而不是「warnings 是否为空」——
    warnings 里混着降级类与说明类（如「必去景点已解析为…」），后者不代表行程不完整。
    """
    warnings = list(final.get("warnings") or [])

    # 预检失败（validate_city 没拿到 adcode，图已经走到 END）
    # 错误码由 validate_city 写进 state，这里不硬编码 —— 否则
    # CONSTRAINT_INFEASIBLE / HOTEL_NOT_FOUND / CITY_MISMATCH 都会被报成 CITY_NOT_FOUND。
    if not final.get("adcode"):
        code = final.get("error_code") or "CITY_NOT_FOUND"
        store.log_run(thread_id=thread_id, fingerprint=fingerprint, cached=cached,
                      success=False, error_code=code, elapsed_ms=elapsed_ms)
        return TripPlanResponse(
            success=False,
            message=_PREFLIGHT_MESSAGES.get(code, "请求未通过可行性预检，请检查表单后重试。"),
            error_code=code,
            warnings=warnings,
            thread_id=thread_id,
            cached=cached,
        )

    data = _to_trip_plan(final, req)
    days = data.get("days") or []
    degraded = _degraded_sources(data)
    # ★ 成功 = 有行程 **且** 数据没降级 **且** 校验没有未解决项。
    #   第三个条件是补上的（见 _unresolved_reasons 的说明）：只看前两个会把
    #   "重试用尽的不合格计划"标成成功，并让它进缓存。
    unresolved = _unresolved_reasons(final, data) + _budget_reasons(final.get("budget_verdict"))
    ok = bool(days) and not degraded and not unresolved
    message = "" if ok else _partial_message(degraded, unresolved, bool(days))

    # 只有「完整、无降级、通过校验、且预算状态可确认」的行程才入库。
    # 后两条都不能省：降级的入库 = 把一次故障永久固化；
    # 不合格的入库 = 把一份"系统自己知道排不下"的计划变成之后所有人的默认答案；
    # 预算未验证的入库 = 把一次不确定性固化成默认答案。
    if ok and not cached and _cacheable_budget_status(final.get("budget_verdict")):
        store.save_itinerary(fingerprint, req, data)
        logger.info("行程已入库 | fingerprint=%s days=%d", fingerprint[:8], len(days))
    elif not ok or not _cacheable_budget_status(final.get("budget_verdict")):
        logger.info(
            "行程**不入库** | fingerprint=%s degraded=%s unresolved=%s budget=%s",
            fingerprint[:8], degraded, unresolved[:2],
            (final.get("budget_verdict") or {}).get("status"),
        )

    usage = final.get("usage") or {}
    store.log_run(
        thread_id=thread_id, fingerprint=fingerprint, cached=cached, success=ok,
        elapsed_ms=elapsed_ms,
        llm_calls=usage.get("llm_calls", 0),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        tool_calls=len(final.get("tool_calls") or []),
        plan_days=len(days),
        budget_total=int((data.get("budget") or {}).get("total") or 0),
    )
    return TripPlanResponse(
        success=ok,
        message=message,
        data=data,
        warnings=warnings,
        thread_id=thread_id,
        cached=cached,
        cache_hit_count=cache_hit_count,
        cache_saved_at=cache_saved_at,
    )


def _cached_response(
    hit: dict, req: TripRequest, thread_id: str, fingerprint: str, elapsed_ms: int
) -> TripPlanResponse:
    """命中行程库时的响应（同步路径与后台路径共用）。

    ★ 命中也要**重新复核**（不能因为"它当初合格"就当现在也合格）：
        - 行程库里的计划是当初某个日期的确通过的，但复用时会平移日期；
        - 更要紧的是：入库判据本身就是这一版才收紧的，库里可能存着
          "按老判据算合格、实际有未解决校验项"的历史数据。
      复核是纯代码（verify_plan，0 次 LLM），在这里跑几乎不花任何代价。
    """
    data = _reuse_with_new_dates(hit["plan"], req)
    days = data.get("days") or []
    degraded = _degraded_sources(data)

    audit = verify_plan({"plan": data, "retries": 0, "validate_error": None})
    unresolved: list[str] = []
    if audit.get("validate_error"):
        unresolved.append(str(audit["validate_error"]))
    unresolved.extend(_unresolved_reasons({}, data))
    # 预算也要重算一遍结论：复用的是历史行程，而这次请求的上限来自本次 request。
    # verify_budget 是纯代码（0 次 LLM），在这里跑没有任何成本。
    budget_verdict = verify_budget({"plan": data, "request": req}).get("budget_verdict")
    unresolved.extend(_budget_reasons(budget_verdict))

    ok = bool(days) and not degraded and not unresolved
    warnings = list(data.get("warnings") or [])
    warnings.extend(audit.get("warnings") or [])

    store.log_run(
        thread_id=thread_id, fingerprint=fingerprint, cached=True, success=ok,
        elapsed_ms=elapsed_ms, plan_days=len(days),
        budget_total=int((data.get("budget") or {}).get("total") or 0),
    )
    logger.info(
        "行程命中复用 | %s | %d ms（未花 LLM）| 复核%s",
        fingerprint[:12], elapsed_ms, "通过" if ok else f"发现问题：{unresolved[:1] or degraded}",
    )
    if ok:
        message = f"复用了一条已生成的行程（第 {hit['hit_count']} 次复用，日期已按本次请求平移）"
    else:
        message = "复用了一条历史行程，但" + _partial_message(degraded, unresolved, bool(days))
    return TripPlanResponse(
        success=ok,
        message=message,
        data=data,
        warnings=warnings,
        thread_id=thread_id,
        cached=True,
        cache_hit_count=hit["hit_count"],
        cache_saved_at=hit["created_at"],
    )


def _reuse_with_new_dates(plan: dict, req: TripRequest) -> dict:
    """把行程库里的行程**平移到本次请求的日期**，并按新日期重取天气。

    为什么不能直接把缓存的响应原样返回（这是本模块最重要的一个决定）：
        天气是**跟日期绑定的外部数据**。把 10 月 10 日的「晴 26 度」搬到 11 月，
        就是编造 —— 正是实验 B 里测的那种错误。
        所以命中路径也必须重取天气（0 token、2 次 HTTP），只是不再跑 LLM。

    可以复用的东西：景点、游览顺序、三餐、酒店、预算、约束对账 —— 它们与出发日期无关。
    """
    data = copy.deepcopy(plan)
    try:
        start = date.fromisoformat(req.start_date)
    except ValueError:
        return data  # 日期本身不合法，交给正常路径去报错

    days = data.get("days") or []
    for index, day in enumerate(days):
        day["date"] = (start + timedelta(days=index)).isoformat()
        day["day_index"] = index
    data["start_date"] = req.start_date
    data["end_date"] = _resolve_end_date(req)

    ctx = ToolContext(
        city=req.city,
        adcode=None,
        start_date=req.start_date,
        travel_days=len(days) or req.travel_days,
        nights=int(data.get("hotel_nights") or 0),
    )
    try:
        _, adcode = amap.geocode(req.city)
        ctx.adcode = adcode
    except Exception as exc:  # noqa: BLE001 —— 天气取不到不该让复用整体失败，按降级处理
        logger.warning("复用行程时地理编码失败 | %s", exc)
        adcode = None
    if adcode:
        execute("weather", "get_weather", {}, ctx)

    sources = dict(data.get("sources") or {})
    sources["weather"] = "amap" if ctx.weather else "degraded"
    data["sources"] = sources
    if ctx.weather:
        data["weather_info"] = [item.model_dump() for item in ctx.weather]

    # 警告：把旧的「预报窗口」那条换成本次实测的；其余（解析改名 / 门票未获取）保留
    kept = [w for w in (data.get("warnings") or []) if "预报窗口" not in w]
    kept += weather_warnings(ctx.weather)
    data["warnings"] = kept
    return data


@router.post("/plan", response_model=TripPlanResponse, summary="生成行程")
def plan_trip(req: TripRequest) -> TripPlanResponse:
    """同步阻塞接口。

    函数签名用 def 而不是 async def：graph.invoke() 是同步调用（真实版本要跑几十秒），
    写成 async def 会堵住 FastAPI 的事件循环，其他请求全部排队。
    用 def 时 FastAPI 会自动把它丢进线程池。

    v0.4 起该接口有**两条路径**（对应 learn-claude-code 的 s09 Memory / s10 持久化）：
        ① 行程库里已有同形状的需求 → 平移日期 + 重取天气，秒级返回（**0 次 LLM 调用**）
        ② 没有 → 跑完整图（约 40 秒 / 11 次 LLM 调用），成功后写入行程库
    两条路径都会往 run_log 记一行，让缓存效果可以被 SQL 查出来。
    """
    started = time.perf_counter()
    thread_id = uuid4().hex
    fingerprint = store.fingerprint_of(req)

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    # ---------------- 路径一：命中行程库 ----------------
    hit = store.find_itinerary(fingerprint)
    if hit is not None:
        return _cached_response(hit, req, thread_id, fingerprint, elapsed_ms())

    # ---------------- 路径二：完整生成 ----------------
    # 组装集中在 _finalize()，与后台任务路径共用同一个出口（见该函数的说明）
    final = trip_graph.invoke(_initial_state(req))
    return _finalize(final, req, thread_id, fingerprint, elapsed_ms())


def _sse(payload: dict) -> str:
    """SSE 的一帧：`data: <json>\\n\\n`。

    json 里绝不能出现裸换行，否则会把一帧劈成两帧 —— 所以统一走 json.dumps。
    `default=str` 是兜底：万一某个字段是 Pydantic 对象，序列化成字符串也比整帧丢掉强。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


@router.post("/plan/async", summary="提交后台生成任务（立即返回 thread_id）")
def plan_async(req: TripRequest) -> dict:
    """提交即返回，进度走 SSE 推送。

    为什么要异步：一次规划约 40 秒，同步接口把浏览器连接占满，
    用户在整个过程里看不到任何进展 —— 这是当前体验最差的一处。
    """
    thread_id = uuid4().hex
    fingerprint = store.fingerprint_of(req)

    # 命中行程库就不必起任务：登记一个「已完成」的任务，让 SSE 同一套流程立刻把结果推出去
    hit = store.find_itinerary(fingerprint)
    if hit is not None:
        response = _cached_response(hit, req, thread_id, fingerprint, 0)
        runner.register_done_job(thread_id, req, response.model_dump())
        return {"success": True, "thread_id": thread_id, "message": "命中历史行程，可直接取结果"}

    started = time.perf_counter()

    def _finish(final: dict) -> TripPlanResponse:
        return _finalize(
            final, req, thread_id, fingerprint, int((time.perf_counter() - started) * 1000)
        )

    runner.start_job(thread_id, req, _initial_state(req), _finish)
    return {"success": True, "thread_id": thread_id, "message": "已开始生成"}


@router.get("/plan/stream/{thread_id}", summary="SSE：推送节点进度与最终结果")
def plan_stream(thread_id: str) -> StreamingResponse:
    """服务端推送。每个节点完成推一条，最后推 done（带完整结果）或 error。

    用同步生成器：FastAPI 会把它放到线程池里跑，不会堵住事件循环。
    单机单用户项目没必要上 async 队列 —— 这一点如实写在注释里，不假装是分布式。
    """

    def generate():
        sent = 0
        while True:
            job = runner.get_job(thread_id)
            if job is None:
                yield _sse({"type": "error", "message": "找不到这个任务（后端可能重启过）"})
                return
            while sent < len(job.events):
                yield _sse(job.events[sent])
                sent += 1
            if job.status == "done":
                yield _sse({"type": "done", "result": job.result})
                return
            if job.status == "error":
                yield _sse({"type": "error", "message": job.error})
                return
            time.sleep(0.3)  # 轮询内存事件列表；前端断开时生成器会被回收

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/plan/result/{thread_id}", summary="取任务结果（轮询兜底）")
def plan_result(thread_id: str) -> dict:
    """SSE 断线时的兜底：前端可以按 thread_id 直接取结果。"""
    job = runner.get_job(thread_id)
    if job is None:
        return {"success": False, "message": "找不到这个任务（后端可能重启过）"}
    if job.status != "done":
        return {"success": False, "message": "仍在生成中", "status": job.status, "elapsed_ms": job.elapsed_ms}
    return job.result or {"success": False, "message": "任务没有产出结果"}


@router.get("/journal/{thread_id}", summary="查看某次任务的节点 journal")
def get_journal(thread_id: str) -> dict:
    """看这次任务完成了哪些节点、能不能续跑 —— 断点续跑的入口。"""
    records = store.load_journal(thread_id)
    completed = [record["node"] for record in records]
    # 和真正的续跑走**同一套判断**（含"有没有未解决的校验错误"），
    # 否则这个调试接口会报出一个与真实续跑起点不一致的值 —— 调试信息撒谎比没有更糟。
    state, _ = runner.rebuild_state({}, thread_id)
    return {
        "thread_id": thread_id,
        "completed_nodes": completed,
        "can_resume_from": runner.can_resume_from(completed, state),
        "nodes": [
            {"node": r["node"], "seq": r["seq"], "elapsed_ms": r["elapsed_ms"]} for r in records
        ],
    }


@router.post("/plan/resume/{thread_id}", response_model=TripPlanResponse, summary="从断点续跑")
def resume_plan(thread_id: str) -> TripPlanResponse:
    """从 journal 里已完成的位置之后继续，**不重跑已完成的昂贵节点**。

    为什么能省：图的结构没变，只是把入口从 validate_city 换成「第一个没做的节点」
    （`build_graph(start_at=...)`）。已完成的节点产出从 journal 里拼回 state。
    """
    record = store.load_run(thread_id)
    if record is None:
        return TripPlanResponse(
            success=False, message="找不到这次任务的原始请求，无法续跑。", thread_id=thread_id
        )
    req = TripRequest.model_validate(record["request"])
    fingerprint = store.fingerprint_of(req)
    started = time.perf_counter()

    state, completed = runner.rebuild_state(_initial_state(req), thread_id)
    # 把重建后的 state 一起传进去：判断"能不能续"必须知道**有没有未解决的校验错误**，
    # 只看"节点出现过"会把"复核要求重试"误判成"复核已通过"（真 bug）。
    start_at = runner.can_resume_from(completed, state)
    if start_at is None:
        return TripPlanResponse(
            success=False,
            message="已完成的步骤不足以续跑（或这次任务已经跑完），请重新提交。",
            thread_id=thread_id,
        )

    logger.info("断点续跑 | %s | 已完成 %s | 从 %s 继续", thread_id[:8], completed, start_at)
    resumed = build_graph(start_at=start_at).invoke(state)
    return _finalize(
        resumed, req, thread_id, fingerprint, int((time.perf_counter() - started) * 1000)
    )


@router.get("/stats", summary="行程库与运行的统计（缓存效果）")
def trip_stats() -> dict:
    """一眼看出缓存有没有真的在省事：命中率、命中/未命中各自的平均耗时、累计 token。

    这是"可观测性"的最小落地 —— 没有它，"我加了缓存"只是一句话；
    有了它，"命中一次省下多少秒、多少 token"是查得出来的。
    """
    return store.stats()