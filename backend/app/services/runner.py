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
    "resolve_hotel": "选择住宿",
    "patch_budget": "计算预算",
}
NODE_ORDER: list[str] = list(NODE_LABELS)


@dataclass
class Job:
    thread_id: str
    request: TripRequest
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


def get_job(thread_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(thread_id)


def _emit(job: Job, event: dict[str, Any]) -> None:
    with _lock:
        job.events.append(event)


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


def can_resume_from(completed: list[str]) -> Optional[str]:
    """能从哪里续跑。

    V0 只在**两路采集都已完成**之后的线性段支持续跑：
        采集都完成        → 从 compose_plan 续跑（省掉搜索与两次 Agent 的 LLM 调用）
        compose_plan 完成 → 从 resolve_hotel 续跑（省掉最贵的编排调用）
        resolve_hotel 完成 → 从 patch_budget 续跑（省掉一次住宿 Agent）
    再往前的断点（城市都没解析完）就老实从头跑 —— 假装能续跑比不能续跑更糟。
    """
    if "patch_budget" in completed:
        return None  # 全跑完了，没有可续的
    if "resolve_hotel" in completed:
        return "patch_budget"
    if "compose_plan" in completed:
        return "resolve_hotel"
    if "collect_attractions" in completed and "collect_weather" in completed:
        return "compose_plan"
    return None
