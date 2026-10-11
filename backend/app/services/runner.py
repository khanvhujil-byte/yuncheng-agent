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

线程安全：`_jobs` 是本进程内存里的字典（单机单用户够用），
跨进程/多副本要靠 Redis 之类 —— 这一点在注释里写明，不要假装它是分布式的。
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models.schemas import TripRequest
from . import store

logger = logging.getLogger(__name__)

# 节点的中文名（前端直接显示，不要在前端再映射一遍）
NODE_LABELS: dict[str, str] = {
    "validate_city": "解析城市",
    "collect_attractions": "搜集景点",
    "collect_weather": "查询天气",
    "compose_plan": "编排行程",
    "verify_plan": "独立复核",
    "resolve_hotel": "选择住宿",
    "patch_budget": "计算预算",
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


def _persist(job: Job) -> None:
    """把任务状态落库。

    这是 12-Factor F12（agent 应该是无状态 reducer）的落点：
    内存里的 `_jobs` 只是**缓存**，真正的状态在 SQLite 里 —— 所以多副本部署或进程重启后，
    `/result` 与 `/stream` 依然能用。否则"用户在 A 副本提交、拉流打到 B 副本"就直接找不到任务。
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
    if job is not None:
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
    job = Job(thread_id=thread_id, request=req)
    with _lock:
        _jobs[thread_id] = job
    store.save_run(thread_id, req)
    threading.Thread(
        target=_run, args=(job, initial_state, finalize), name=f"trip-{thread_id[:8]}", daemon=True
    ).start()
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


def _run(job: Job, initial_state: dict[str, Any], finalize: Callable[[dict[str, Any]], Any]) -> None:
    """在工作线程里跑图：逐节点推进度 + 写 journal，最后组装响应。"""
    from ..agents.graph import app as trip_graph  # 延迟导入：避免与 api 层的导入顺序纠缠

    latest: dict[str, Any] = dict(initial_state)
    seq = 0
    try:
        for mode, chunk in trip_graph.stream(initial_state, stream_mode=["updates", "values"]):
            if mode == "values":
                latest = chunk  # 完整 state：最终组装用它，省掉自己合并 reducer
                continue

            # mode == "updates"：chunk = {节点名: 该节点写回的部分}
            for node, update in (chunk or {}).items():
                seq += 1
                elapsed = job.elapsed_ms
                _emit(job, {
                    "type": "node", "node": node,
                    "label": NODE_LABELS.get(node, node),
                    "status": "done", "elapsed_ms": elapsed,
                })
                if isinstance(update, dict):
                    # journal 会落盘 → 必须能 JSON 序列化；Pydantic 对象由 store 的 default 兜底
                    store.journal_node(job.thread_id, node, seq, update, elapsed)
                logger.info("后台任务节点完成 | %s | %s | %d ms", job.thread_id[:8], node, elapsed)

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
    """用 journal 重建状态。返回 (状态, 已完成节点列表)。"""
    state = dict(initial_state)
    completed: list[str] = []
    for record in store.load_journal(thread_id):
        state = merge_update(state, record["update"])
        completed.append(record["node"])
    return state, completed


def can_resume_from(completed: list[str], state: Optional[dict[str, Any]] = None) -> Optional[str]:
    """能从哪里续跑。

    V0 只在**两路采集都已完成**之后的线性段支持续跑：
        采集都完成            → 从 compose_plan 续跑（省掉搜索与两次 Agent 的 LLM 调用）
        编排完成 + 复核完成    → 从 resolve_hotel 续跑（省掉最贵的编排调用）
        resolve_hotel 完成    → 从 patch_budget 续跑（省掉一次住宿 Agent）
    再往前的断点（城市都没解析完）就老实从头跑 —— 假装能续跑比不能续跑更糟。

    ★ 光看"节点出现过"是不够的（这是一个真 bug）：重试会让**同一个节点出现多次**，
      于是"compose_plan 出现过"既可能是
        ① 编排完成、独立复核也做过、等着选酒店
        ② 复核判了不合格、正等着重排（retries 还没用完）
        ③ 编排完成但复核还没跑（在 verify_plan 之前崩的）
      旧实现一律返回 resolve_hotel，于是 ② 会丢掉那次重试、③ 会**整个绕过独立复核**。
      判据必须结合状态：有未解决的 `validate_error` 就说明还等着重排。

    @param state 重建后的状态（用来判断"有没有未解决的校验错误"）。
                 不传时退回旧的保守行为：只要没法确认复核做过，就不续跑。
    """
    if "patch_budget" in completed:
        return None  # 全跑完了，没有可续的
    if "resolve_hotel" in completed:
        return "patch_budget"

    if "compose_plan" in completed:
        has_unresolved_error = bool((state or {}).get("validate_error"))
        if has_unresolved_error:
            return "compose_plan"  # 复核要求重试，那次重排还没做 —— 不能跳过
        if "verify_plan" in completed:
            return "resolve_hotel"
        # 编排完成但独立复核没跑过：从这里续会绕过复核，宁可不续
        logger.info("不续跑：compose_plan 已完成但 verify_plan 没跑过，续跑会绕过独立复核")
        return None

    if "collect_attractions" in completed and "collect_weather" in completed:
        return "compose_plan"
    return None
