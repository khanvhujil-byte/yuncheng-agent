"""Measure each node where it runs, including parallel collectors and repeats."""
from functools import wraps
from time import perf_counter


def measured(name, function):
    @wraps(function)
    def call(state):
        started = perf_counter()
        if name == 'compose_plan':
            from time import time
            state = {**state, 'planning_started_at': state.get('planning_started_at') or time(),
                     '_planner_call_counter': {'count': 0}}
        update = dict(function(state) or {})
        if name == 'compose_plan':
            update.update(planning_started_at=state['planning_started_at'],
                          composition_valid=bool((update.get('plan') or {}).get('days')),
                          planning_calls=int(state.get('planning_calls') or 0)+max(
                              1, state['_planner_call_counter']['count'],
                              int((update.get('usage') or {}).get('llm_calls') or 0)))
        entry = {"node": name, "step": len(state.get("trace") or []) + 1,
                 "elapsed_ms": round((perf_counter() - started) * 1000),
                 "detail": "节点执行耗时；并行节点不能直接求和作为全程耗时。"}
        update["trace"] = [*(update.get("trace") or []), entry]
        return update
    return call
