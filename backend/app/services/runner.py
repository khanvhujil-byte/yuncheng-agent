"""后台生成任务：把一次规划从「阻塞 40 秒」变成「提交即返回 + 进度推送 + 可续跑」。

对应 learn-claude-code 的三章：
    s10 Task System      —— 任务与产出落盘（journal 表）
    s11 Background Tasks —— 慢操作丢后台线程，完成后通知；这里用「事件列表 + SSE」推送
    s16 Workflow Runtime —— 每个节点的产出写 journal，失败后可从断点续跑

为什么用 `graph.stream(stream_mode=["updates", "values"])` 两种模式：
    "updates" 给出**每个节点刚产出什么** —— 用来推进度、写 journal；
    "values"  给出**这一步之后的完整 state** —— 用来最终组装响应。
    两种一起用，既不用自己实现 reducer 合并（LangGraph 帮我们合好了），
    也不用为了拿进度去改图的结构。这是"不改图就能拿到节点级进度"的关键。

内存保存本进程事件；共享本地 SQLite 的进程通过可续租租约和写事务 token 校验互斥。
这不支持跨主机 SQLite 部署，生产模板仍采用单 worker。
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models.schemas import TripRequest
from . import store
from .state_codec import StateRestoreError, restore_state_types

logger = logging.getLogger(__name__)

# 节点的中文名（前端直接显示，不要在前端再映射一遍）
NODE_LABELS: dict[str, str] = {
    "validate_city": "解析城市",
    "collect_attractions": "搜集景点",
    "collect_weather": "查询天气",
    "collect_food": "搜集本地美食",
    "collect_hotels": "搜集住宿候选",
    "prepare_planning_context": "准备联合编排数据",
    "compose_plan": "统一编排景点、餐饮和住宿",
    "verify_plan": "独立复核",
    "resolve_hotel": "选择住宿",
    "enrich_experience": "计算路程和舒适度",
    "patch_budget": "计算预算",
    "verify_budget": "预算复核",
    "prepare_budget_retry": "准备预算重排",
    "prepare_repair": "准备体验修复",
}
NODE_ORDER: list[str] = list(NODE_LABELS)


@dataclass
class Job:
    thread_id: str
    request: Optional[TripRequest] = None
    status: str = "running"  # running / done / error
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None  # TripPlanResponse 的 dict 形式
    error: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    finished_at: Optional[float] = None

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at if self.finished_at is not None else time.perf_counter()
        return int((end - self.started_at) * 1000)


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_active_runs: set[str] = set()
_leases: dict = {}
FINALIZE_ONLY = "__finalize__"


class JobAlreadyRunning(RuntimeError):
    """同一数据库上的其他执行者已持有任务租约。"""


class LegacyJournalError(StateRestoreError):
    """覆盖式历史日志无法证明完整执行历史。"""


def _claim_execution(thread_id: str) -> None:
    with _lock:
        if thread_id in _active_runs:
            raise JobAlreadyRunning(thread_id)
        from .execution_lease import Lease, Busy
        try:
            _leases[thread_id] = Lease('run:' + thread_id).acquire()
        except Busy as exc:
            raise JobAlreadyRunning(thread_id) from exc
        _active_runs.add(thread_id)


def _release_execution(thread_id: str) -> None:
    with _lock:
        _active_runs.discard(thread_id)
        lease = _leases.pop(thread_id, None)
    if lease:
        lease.release()


@contextmanager
def execution_guard(thread_id: str):
    """持有共享数据库租约，并将其 token 传递到关键写事务。"""
    _claim_execution(thread_id)
    from .execution_lease import current_lease
    token = current_lease.set(_leases[thread_id])
    try:
        yield
    finally:
        current_lease.reset(token)
        _release_execution(thread_id)


def _persist(job: Job) -> None:
    """把任务状态落库。

    SQLite 支持进程重启后的查询；旧租约持有者不能覆盖新执行者结果。
    落盘失败不能影响任务本身，所以这里吞掉异常、只记日志。
    """
    try:
        # started_at 存 epoch 秒（不是进程内的 perf_counter），重启后算耗时才有意义
        epoch_start = time.time() - job.elapsed_ms / 1000
        store.save_job(
            job.thread_id,
            status=job.status,
            events=job.events,
            result=job.result,
            error=job.error,
            started_at=epoch_start,
            finished_at=(epoch_start + job.elapsed_ms / 1000) if job.finished_at else None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("任务状态落盘失败 | %s", job.thread_id[:8])


def get_job(thread_id: str) -> Optional[Job]:
    """先查内存（快），再查库（跨进程 / 重启后的兜底）。"""
    with _lock:
        job = _jobs.get(thread_id)
    if job is not None and thread_id in _active_runs:
        return job

    stored = store.load_job(thread_id)
    if stored is None:
        return None

    job = Job(thread_id=thread_id, status=stored["status"])
    job.events = list(stored["events"])
    job.result = stored["result"]
    job.error = stored["error"]
    # 从 epoch 秒反推 perf_counter 起点：让重建出来的任务也能报出合理耗时
    elapsed = (stored["finished_at"] or time.time()) - (stored["started_at"] or time.time())
    job.started_at = time.perf_counter() - elapsed
    job.finished_at = time.perf_counter() if stored["finished_at"] else None
    logger.info("任务状态从库里恢复 | %s | status=%s", thread_id[:8], job.status)
    with _lock:
        _jobs[thread_id] = job
    return job


def _emit(job: Job, event: dict[str, Any]) -> None:
    with _lock:
        job.events.append(event)
    _persist(job)  # 每个事件都落盘：SSE 断线重连后要能把已发生的事件重放出来


def start_job(
    thread_id: str,
    req: TripRequest,
    initial_state: dict[str, Any],
    finalize: Callable[[dict[str, Any]], Any],
) -> Job:
    """建任务并起后台线程。

    `initial_state` / `finalize` 由 HTTP 层传进来，而不是在这里 import ——
    否则 runner 要反向依赖 api 层，形成循环导入（真实踩过的坑）。
    """
    _claim_execution(thread_id)
    job = Job(thread_id=thread_id, request=req)
    try:
        store.save_run(thread_id, req)
        with _lock:
            _jobs[thread_id] = job
        _persist(job)
        threading.Thread(
            target=_background_run, args=(job, initial_state, finalize),
            name=f"trip-{thread_id[:8]}", daemon=True,
        ).start()
    except BaseException:
        _release_execution(thread_id)
        raise
    return job


def register_done_job(thread_id: str, req: TripRequest, result: dict[str, Any]) -> Job:
    """登记一个「已经有结果」的任务。

    缓存命中时用：这样 SSE 那条路径不用分叉 —— 前端只认「开流 → 收事件」这一种流程，
    命中时它照样能立刻收到 done 事件。
    """
    job = Job(thread_id=thread_id, request=req, status="done", result=result)
    job.finished_at = time.perf_counter()
    with _lock:
        _jobs[thread_id] = job
    _persist(job)
    return job


def _background_run(job: Job, initial_state: dict[str, Any], finalize: Callable[[dict[str, Any]], Any]) -> None:
    try:
        from ..agents.graph import app as trip_graph
        _execute(job, initial_state, finalize, trip_graph)
    finally:
        _release_execution(job.thread_id)


def continue_job(
    thread_id: str, req: TripRequest, state: dict[str, Any],
    finalize: Callable[[dict[str, Any]], Any], trip_graph: Any,
) -> Job:
    """调用方先持有 execution_guard；与首次运行共用日志与任务完成逻辑。"""
    job = get_job(thread_id) or Job(thread_id=thread_id)
    job.request = req
    job.status = "running"
    job.error = ""
    job.result = None
    job.finished_at = None
    # 以持久日志重建事件，补齐“日志提交后、发事件前”中断的情况。
    records = store.load_journal(thread_id)
    job.events = [_node_event(r["node"], r["seq"], r["elapsed_ms"]) for r in records]
    with _lock:
        _jobs[thread_id] = job
    _persist(job)
    _execute(job, state, finalize, trip_graph, seq=max((r["seq"] for r in records), default=0))
    return job


def _node_event(node: str, seq: int, elapsed: int, duration_ms: int | None = None) -> dict[str, Any]:
    event = {"type": "node", "node": node, "seq": seq,
             "label": NODE_LABELS.get(node, node), "status": "done", "elapsed_ms": elapsed}
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    return event


def _execute(
    job: Job, initial_state: dict[str, Any], finalize: Callable[[dict[str, Any]], Any],
    trip_graph: Any, seq: int = 0,
) -> None:
    """首次与续跑统一执行；None 表示最终节点已提交，只需组装响应。"""

    latest: dict[str, Any] = dict(initial_state)
    from .execution_lease import current_lease
    token = current_lease.set(_leases.get(job.thread_id) or current_lease.get())
    try:
        stream = trip_graph.stream(initial_state, stream_mode=["updates", "values"]) if trip_graph is not None else ()
        for mode, chunk in stream:
            if mode == "values":
                latest = chunk  # 完整 state：最终组装用它，省掉自己合并 reducer
                continue

            # mode == "updates"：chunk = {节点名: 该节点写回的部分}
            for node, update in (chunk or {}).items():
                if job.thread_id in _leases:
                    _leases[job.thread_id].check()
                seq += 1
                elapsed = job.elapsed_ms
                # 先提交结果，再通知“完成”。空产出节点也必须留下执行位置。
                store.journal_node(job.thread_id, node, seq, update or {}, elapsed)
                timing = next((t for t in reversed((update or {}).get("trace") or []) if t.get("node") == node), {})
                duration = timing.get("elapsed_ms")
                _emit(job, _node_event(node, seq, elapsed, duration))
                logger.info("后台任务节点完成 | %s | %s | 单节点 %s ms | 全程 %d ms",
                            job.thread_id[:8], node, duration if duration is not None else "unknown", elapsed)

        if job.thread_id in _leases:
            _leases[job.thread_id].check()
        response = finalize(latest)
        job.result = response.model_dump() if hasattr(response, "model_dump") else response
        job.status = "done"
        logger.info("后台任务完成 | %s | %.1f 秒", job.thread_id[:8], job.elapsed_ms / 1000)
    except Exception as exc:  # noqa: BLE001 —— 后台线程里绝不能把异常漏出去
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        logger.exception("后台任务失败 | %s", job.thread_id[:8])
    finally:
        job.finished_at = time.perf_counter()
        _persist(job)
        current_lease.reset(token)


# ================================================================ 断点续跑


def merge_update(state: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """把 journal 里的节点产出合并回 state（用来重建断点前的状态）。

    为什么必须自己写：reducer 只在**图内**由 LangGraph 应用。续跑是从图外面
    拿着历史碎片拼一个 state 再喂进图，所以这里要复刻一遍 reducers 的语义 ——
    列表追加（errors/warnings/tool_calls/trace）、字典合并（sources）、数字累加（usage）、其余覆盖。
    这三个特例必须和图里 state.py 的定义保持一致，改一边要改另一边。
    """
    merged = dict(state)
    for key, value in (update or {}).items():
        if key in ("errors", "warnings", "tool_calls", "trace"):
            merged[key] = list(merged.get(key) or []) + list(value or [])
        elif key == "sources":
            merged[key] = {**(merged.get(key) or {}), **(value or {})}
        elif key == "usage":
            total = dict(merged.get(key) or {})
            for k, v in (value or {}).items():
                total[k] = total.get(k, 0) + int(v)
            merged[key] = total
        else:
            merged[key] = value
    return merged


def rebuild_state(initial_state: dict[str, Any], thread_id: str) -> tuple[dict[str, Any], list[str]]:
    """按 reducer 重建状态，再恢复领域对象。返回 (状态, 已完成节点列表)。"""
    run = store.load_run(thread_id)
    if run and run["journal_version"] != 2:
        raise LegacyJournalError("旧版日志可能丢失重试记录，不能安全恢复")
    state = dict(initial_state)
    completed: list[str] = []
    for record in store.load_journal(thread_id):
        state = merge_update(state, record["update"])
        completed.append(record["node"])
    return restore_state_types(state), completed


def can_resume_from(completed: list[str], state: Optional[dict[str, Any]] = None) -> Optional[str]:
    """根据最后一次提交与当前状态，沿真实图路由恢复；采集未汇齐时不续。

    同名节点可以出现多次。旧复核不证明新行程已复核，旧预算也不证明新一轮已结束。
    FINALIZE_ONLY 表示最后的预算复核已提交，但响应可能尚未持久化。
    """
    from ..agents.graph import route_after_plan, route_after_experience, route_after_budget

    if not completed:
        return None
    last = completed[-1]
    if last == "compose_plan":
        return "verify_plan"
    if last == "verify_plan":
        return {'retry':'compose_plan','next':'enrich_experience','legacy_hotel':'resolve_hotel'}[route_after_plan(state or {})]
    if last == "resolve_hotel":
        return "enrich_experience"
    if last == "enrich_experience":
        return "compose_plan" if route_after_experience(state or {}) == "retry" else "patch_budget"
    if last == "patch_budget":
        return "verify_budget"
    if last == "verify_budget":
        return {'retry':'prepare_budget_retry','repair':'prepare_repair','next':FINALIZE_ONLY}[route_after_budget(state or {})]
    if last in ("prepare_budget_retry", "prepare_repair"):
        return "compose_plan"
    if last == "prepare_planning_context":
        return "compose_plan"
    if last in ("collect_attractions", "collect_weather", "collect_food", "collect_hotels"):
        collectors = {"collect_attractions", "collect_weather"}
        if (state or {}).get("experience_enabled"):
            collectors.add("collect_food")
        if (state or {}).get('planning_contract_version', 0) >= 9 or "collect_hotels" in completed:
            collectors.add("collect_hotels")
        if collectors.issubset(completed):
            return "prepare_planning_context" if "collect_hotels" in collectors else "compose_plan"
    return None
