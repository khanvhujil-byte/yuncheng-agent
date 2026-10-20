"""行程规划路由（HTTP 层）。

路由只处理 HTTP、后台任务、进度查询与恢复。
预检、缓存、初始状态和最终交付组装统一位于 services/planning.py。

路径：router 前缀 "/trip" + main.py 里叠的 "/api" = POST /api/trip/plan

表单与对话共用规划服务，不各自维护一套成功判据。

注意：函数用 def 而不是 async def —— graph.invoke() 是同步阻塞调用，
写成 async def 会堵住 FastAPI 的事件循环，其他请求全部排队。

私人会话运行的结果和 journal 由用户归属检查保护。
"""

import json
import hashlib
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ...agents.graph import app as trip_graph
from ...agents.graph import build_graph
from ...models.schemas import TripPlanResponse, TripRequest
from ...services import runner, store
from ...services.state_codec import StateRestoreError
from ...services.identity import authorize_run, optional_user
from ..security import rate_limit, require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/trip",
    tags=["旅行规划"],
    # 鉴权与限流挂在**路由级**而不是逐个端点：漏掉一个端点就等于漏掉一个口子，
    # 挂在 router 上，以后新增端点会自动继承。两者默认关闭（见 security.py 的说明）。
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)

from ...services import planning
from ...services.planning import (  # compatibility for callers of previous boundary helpers
    _initial_state, _finalize, _cached_response, _reject_invalid_request,
    _STATE_RESTORE_MESSAGE,
)

@router.post("/plan", response_model=TripPlanResponse, summary="生成行程")
def plan_trip(req: TripRequest, user_id: str | None = Depends(optional_user)) -> TripPlanResponse:
    """同步阻塞接口。

    函数签名用 def 而不是 async def：graph.invoke() 是同步调用（真实版本要跑几十秒），
    写成 async def 会堵住 FastAPI 的事件循环，其他请求全部排队。
    用 def 时 FastAPI 会自动把它丢进线程池。

    v0.4 起该接口有**两条路径**（对应 learn-claude-code 的 s09 Memory / s10 持久化）：
        ① 行程库里已有同形状的需求 → 平移日期 + 重取天气，秒级返回（**0 次 LLM 调用**）
        ② 没有 → 跑完整图（约 40 秒 / 11 次 LLM 调用），成功后写入行程库
    两条路径都会往 run_log 记一行，让缓存效果可以被 SQL 查出来。
    """
    owner = user_id if isinstance(user_id, str) else None
    run_id = uuid4().hex
    if owner:
        store.save_run(run_id, req)
        with store._connect() as db:
            db.execute("INSERT INTO owned_run VALUES(?,?,?)", (run_id, owner, ""))
    response = planning.plan(req, graph=trip_graph, run_id=run_id, cache_scope=owner)
    if owner:
        runner.register_done_job(run_id, req, response.model_dump())
    return response


def _sse(payload: dict) -> str:
    """SSE 的一帧：`data: <json>\\n\\n`。

    json 里绝不能出现裸换行，否则会把一帧劈成两帧 —— 所以统一走 json.dumps。
    `default=str` 是兜底：万一某个字段是 Pydantic 对象，序列化成字符串也比整帧丢掉强。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


@router.post("/plan/async", summary="提交后台生成任务（立即返回 thread_id）")
def plan_async(req: TripRequest, user_id: str | None = Depends(optional_user)) -> dict:
    """提交即返回，进度走 SSE 推送。

    为什么要异步：一次规划约 40 秒，同步接口把浏览器连接占满，
    用户在整个过程里看不到任何进展 —— 这是当前体验最差的一处。
    """
    thread_id = uuid4().hex
    fingerprint = store.fingerprint_of(req)
    owner = user_id if isinstance(user_id, str) else None
    if owner:
        fingerprint = hashlib.sha256((owner + ":" + fingerprint).encode()).hexdigest()
        store.save_run(thread_id, req)
        with store._connect() as db:
            db.execute("INSERT INTO owned_run VALUES(?,?,?)", (thread_id, owner, ""))

    invalid = _reject_invalid_request(req, thread_id, fingerprint, 0)
    if invalid is not None:
        runner.register_done_job(thread_id, req, invalid.model_dump())
        return {
            "success": False, "thread_id": thread_id,
            "message": invalid.message, "error_code": invalid.error_code,
        }

    # 命中行程库就不必起任务：登记一个「已完成」的任务，让 SSE 同一套流程立刻把结果推出去。
    # success 必须和复核结论一致：命中一条复核不通过的历史行程时不能先报成功。
    hit = store.find_itinerary(fingerprint)
    if hit is not None and req.intercity_mode == 'none':
        response = _cached_response(hit, req, thread_id, fingerprint, 0)
        runner.register_done_job(thread_id, req, response.model_dump())
        return {
            "success": response.success,
            "thread_id": thread_id,
            "message": "命中历史行程：" + (response.message or "可直接取结果"),
            "cached": True,
            "error_code": response.error_code,
        }

    started = time.perf_counter()

    def _finish(final: dict) -> TripPlanResponse:
        return _finalize(
            final, req, thread_id, fingerprint, int((time.perf_counter() - started) * 1000)
        )

    runner.start_job(thread_id, req, _initial_state(req), _finish)
    return {"success": True, "thread_id": thread_id, "message": "已开始生成"}


@router.get("/plan/stream/{thread_id}", summary="SSE：推送节点进度与最终结果", dependencies=[Depends(authorize_run)])
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


@router.get("/plan/result/{thread_id}", summary="取任务结果（轮询兜底）", dependencies=[Depends(authorize_run)])
def plan_result(thread_id: str) -> dict:
    """SSE 断线时的兜底：前端可以按 thread_id 直接取结果。"""
    job = runner.get_job(thread_id)
    if job is None:
        return {"success": False, "message": "找不到这个任务（后端可能重启过）", "status": "error", "error_code": "THREAD_GONE"}
    if job.status == "error":
        return {"success": False, "message": job.error or "任务执行失败，可检查断点后继续", "status": "error", "error_code": "GENERATION_FAILED"}
    if job.status != "done":
        return {"success": False, "message": "仍在生成中", "status": job.status, "elapsed_ms": job.elapsed_ms}
    return job.result or {"success": False, "message": "任务没有产出结果"}


@router.get("/journal/{thread_id}", summary="查看某次任务的节点 journal", dependencies=[Depends(authorize_run)])
def get_journal(thread_id: str) -> dict:
    """看这次任务完成了哪些节点、能不能续跑 —— 断点续跑的入口。"""
    records = store.load_journal(thread_id)
    completed = [record["node"] for record in records]
    # 和真正的续跑走**同一套判断**（含"有没有未解决的校验错误"），
    # 否则这个调试接口会报出一个与真实续跑起点不一致的值 —— 调试信息撒谎比没有更糟。
    result = {
        "thread_id": thread_id,
        "completed_nodes": completed,
        "can_resume_from": None,
        "nodes": [
            {"node": r["node"], "seq": r["seq"], "elapsed_ms": r["elapsed_ms"]} for r in records
        ],
    }
    try:
        state, _ = runner.rebuild_state({}, thread_id)
    except runner.LegacyJournalError:
        return {**result, "error_code": "LEGACY_JOURNAL_UNSAFE",
                "message": "旧版日志可能缺失重试历史，无法安全续跑，请重新提交。"}
    except StateRestoreError as exc:
        logger.warning("journal 状态恢复失败 | %s | %s", thread_id[:8], exc)
        return {**result, "error_code": "STATE_RESTORE_FAILED", "message": _STATE_RESTORE_MESSAGE}
    job = runner.get_job(thread_id)
    if job is None or job.status != "done":
        result["can_resume_from"] = runner.can_resume_from(completed, state)
    return result


@router.post("/plan/resume/{thread_id}", response_model=TripPlanResponse, summary="从断点续跑", dependencies=[Depends(authorize_run)])
def resume_plan(thread_id: str) -> TripPlanResponse:
    """从最后一次持久提交后继续；同一进程内禁止并发续跑。"""
    try:
        with runner.execution_guard(thread_id):
            return _resume_plan_locked(thread_id)
    except runner.JobAlreadyRunning:
        return TripPlanResponse(success=False, error_code="JOB_ALREADY_RUNNING",
                                message="任务正在执行，请等待完成后再操作。", thread_id=thread_id)


def _resume_plan_locked(thread_id: str) -> TripPlanResponse:
    job = runner.get_job(thread_id)
    if job is not None and job.status == "done" and job.result is not None:
        return TripPlanResponse.model_validate(job.result)
    record = store.load_run(thread_id)
    if record is None:
        return TripPlanResponse(
            success=False, message="找不到这次任务的原始请求，无法续跑。", thread_id=thread_id
        )
    req = TripRequest.model_validate(record["request"])
    fingerprint = store.fingerprint_of(req)
    with store._connect() as db:
        owner = db.execute("SELECT user_id FROM owned_run WHERE run_id=?", (thread_id,)).fetchone()
    if owner:
        fingerprint = hashlib.sha256((owner["user_id"] + ":" + fingerprint).encode()).hexdigest()
    started = time.perf_counter()

    try:
        state, completed = runner.rebuild_state(_initial_state(req), thread_id)
    except runner.LegacyJournalError:
        return TripPlanResponse(success=False, error_code="LEGACY_JOURNAL_UNSAFE",
                                message="旧版日志可能缺失重试历史，无法安全续跑，请重新提交。",
                                thread_id=thread_id)
    except StateRestoreError as exc:
        logger.warning("续跑状态恢复失败 | %s | %s", thread_id[:8], exc)
        return TripPlanResponse(
            success=False, message=_STATE_RESTORE_MESSAGE,
            error_code="STATE_RESTORE_FAILED", thread_id=thread_id,
        )
    start_at = runner.can_resume_from(completed, state)
    if start_at is None:
        return TripPlanResponse(
            success=False,
            message="已完成的步骤不足以续跑（或这次任务已经跑完），请重新提交。",
            thread_id=thread_id,
        )

    logger.info("断点续跑 | %s | 已完成 %s | 从 %s 继续", thread_id[:8], completed, start_at)
    job = runner.continue_job(
        thread_id, req, state,
        lambda final: _finalize(
            final, req, thread_id, fingerprint, int((time.perf_counter() - started) * 1000)
        ),
        None if start_at == runner.FINALIZE_ONLY else build_graph(start_at=start_at),
    )
    if job.status == "done" and job.result is not None:
        return TripPlanResponse.model_validate(job.result)
    return TripPlanResponse(success=False, error_code="RESUME_FAILED",
                            message="续跑执行失败，请查看任务日志后再次续跑。", thread_id=thread_id)


@router.get("/stats", summary="行程库与运行的统计（缓存效果）")
def trip_stats() -> dict:
    """一眼看出缓存有没有真的在省事：命中率、命中/未命中各自的平均耗时、累计 token。

    这是"可观测性"的最小落地 —— 没有它，"我加了缓存"只是一句话；
    有了它，"命中一次省下多少秒、多少 token"是查得出来的。
    """
    return store.stats()
