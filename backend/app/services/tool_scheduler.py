"""Execute one model tool batch with explicit ordering and isolated search workers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from itertools import groupby
from typing import Any

from .tools import TOOLS, ToolContext, execute, query_key
from .tool_result import ToolResult, next_action

PendingCall = tuple[dict[str, Any], str, dict[str, Any]]


def execute_batch(role: str, pending: list[PendingCall], ctx: ToolContext) -> list[ToolResult]:
    """Results match model call order; trace records actual scheduled execution order.

    Stages order deterministic work only. A decision using a fresh tool result
    must wait until the next model turn; reordering cannot invent its arguments.
    """
    results: dict[int, ToolResult] = {}
    allowed = {name for _, name, _ in pending if role in TOOLS.get(name, {}).get("roles", ())}

    def stage(index: int) -> int:
        return TOOLS.get(pending[index][1], {}).get("batch_stage", 0)

    def record(index: int, result: ToolResult, trace: dict | None = None) -> None:
        call, name, args = pending[index]
        results[index] = result
        if trace is None:
            trace = {"role": role, "tool": name, "args": args, "tool_call_id": call.get("id"),
                     "attempts": [], "attempt_count": 0, "stop_reason": "not_executed",
                     "next_action": next_action(result)}
            ctx.calls.append(trace)
        trace.update(result=result.message[:200], outcome=result.to_dict())

    def execute_one(index: int) -> None:
        call, name, args = pending[index]
        result = execute(role, name, args, ctx, tool_call_id=call.get("id"))
        record(index, result, ctx.calls[-1])

    for _, group in groupby(sorted(range(len(pending)), key=stage), key=stage):
        ready = []
        for index in group:
            _, name, _ = pending[index]
            spec = TOOLS.get(name, {})
            if role not in spec.get("roles", ()):
                execute_one(index)
                continue
            unseen = allowed.intersection(spec.get("observe_after", ()))
            if unseen:
                text = (f"错误：{name} 需要先查看本轮 {', '.join(sorted(unseen))} 的结果。"
                        "请根据实际结果，在下一轮单独提交后续调用；不要猜测酒店名、排名或距离。")
                ctx.errors.append(f"TOOL_DEPENDENCY: {name} 等待前置结果被模型读取")
                record(index, ToolResult.failure(text, "TOOL_DEPENDENCY"))
            elif name == "choose_hotel" and sum(n == name for _, n, _ in pending) > 1:
                text = "错误：本轮提交了多次 choose_hotel，无法确定最终决定；请下一轮只选择一家。"
                ctx.errors.append("TOOL_CONFLICT: 同一轮多次选择酒店")
                record(index, ToolResult.failure(text, "TOOL_CONFLICT"))
            else:
                ready.append(index)

        unique = {query_key(role, pending[i][1], pending[i][2], ctx) for i in ready}
        if len(ready) > 1 and len(unique) == len(ready) and all(
            TOOLS[pending[i][1]].get("parallel_search") for i in ready
        ):
            # Workers never write the shared collection. Merge by submitted order
            # so duplicate POIs cannot race or overwrite required anchors.
            contexts = [deepcopy(ctx) for _ in ready]
            error_count = len(ctx.errors)
            with ThreadPoolExecutor(max_workers=min(4, len(ready))) as pool:
                futures = [pool.submit(execute, role, pending[i][1], pending[i][2], local,
                                       tool_call_id=pending[i][0].get("id"))
                           for i, local in zip(ready, contexts)]
                outcomes = [future.result() for future in futures]
            for position, local in enumerate(contexts):
                hotel_count = len(ctx.hotels)
                pool = ctx.attractions if pending[ready[position]][1] == "search_attractions" else ctx.hotels
                previous_count = len(pool)
                for key, item in local.attractions.items():
                    ctx.attractions.setdefault(key, item)
                for key, item in local.hotels.items():
                    ctx.hotels.setdefault(key, item)
                if len(ctx.hotels) != hotel_count:
                    ctx.ranked = []
                    ctx.distance_cache.pop("__rank_best__", None)
                ctx.errors.extend(local.errors[error_count:])
                ctx.exhausted_queries.update(local.exhausted_queries)
                result = outcomes[position]
                if result.status == "ok":
                    added = len(pool) - previous_count
                    stats = {**result.stats, "added_count": added,
                             "duplicate_count": result.stats["duplicate_count"] + result.stats["added_count"] - added}
                    outcomes[position] = replace(result, stats=stats)
            for index, result, local in zip(ready, outcomes, contexts):
                if result.status == "ok":
                    pool = ctx.attractions if pending[index][1] == "search_attractions" else ctx.hotels
                    result = replace(result, stats={**result.stats, "candidate_count": len(pool)},
                                     message=result.message + f" 合并本轮搜索后：景点候选 {len(ctx.attractions)} 条，"
                                     f"酒店候选 {len(ctx.hotels)} 家。")
                trace = local.calls[-1]
                ctx.calls.append(trace)
                record(index, result, trace)
        else:
            for index in ready:
                execute_one(index)

    return [results[i] for i in range(len(pending))]
