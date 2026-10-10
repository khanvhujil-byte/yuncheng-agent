r"""v0.5 实验：上下文压缩的代价/收益 + 逐节点耗时分布。

两个实验：
    D  上下文压缩 A/B —— 同一批需求，只切 `PROMPT_COMPACT`
       指标：token、约束满足率、行程规模（天数/景点数/预算）是否变化
       ★ 关键：省 token 不能以"行程变差"为代价，所以必须同时看质量列
    E  逐节点耗时分布 —— 同一需求跑 N 次，给出每个节点的 p50/p95
       目的：找出真正的瓶颈节点（同步接口下只能看到总耗时，看不出是谁慢）

为什么在进程内跑图：`usage`（token/调用次数）与逐节点时延只活在 state 与 stream 里，
HTTP 响应里没有它们。进程内 `stream()` 能同时拿到两者，还绕开了行程库缓存。

用法（在 backend 目录下）：
    .\.venv\Scripts\python.exe -u scripts\experiment_v05.py --reps 3 --timing-runs 10
产出：
    backend/experiments/raw_v05.csv
    backend/experiments/summary_v05.md
"""

import argparse
import csv
import statistics
import sys
import time
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agents.graph import app as trip_graph  # noqa: E402
from app.api.routes.trip import _initial_state  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models.schemas import TripRequest  # noqa: E402

NODE_KEYS = [
    "validate_city", "collect_weather", "collect_attractions",
    "compose_plan", "verify_plan", "resolve_hotel", "patch_budget",
]
COLUMNS = (
    ["experiment", "arm", "rep", "elapsed_s", "success", "llm_calls", "tokens",
     "tool_calls", "required", "satisfied", "plan_days", "attractions", "budget", "warnings"]
    + [f"ms_{node}" for node in NODE_KEYS]
)


def run_once(arm: str, rep: int, experiment: str) -> dict:
    """跑一次并记录逐节点耗时。

    逐节点耗时怎么来的：用 `stream(stream_mode=["updates","values"])`，
    每收到一个节点完成事件就打一次时间戳，相邻两次的差就是"这段时间里图往前走了多少"。
    注意：两路采集是**并行**的，它们的耗时数字会相互重叠，看的时候要当成"这两步合计"。
    """
    req = TripRequest(
        city="杭州",
        start_date=date.today().isoformat(),
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光", "博物馆", "历史文化"],
        must_visit=["西湖"],
    )
    state = _initial_state(req)
    marks: dict[str, float] = {}
    last = time.perf_counter()
    started = last
    latest: dict = {}

    try:
        for mode, chunk in trip_graph.stream(state, stream_mode=["updates", "values"]):
            now = time.perf_counter()
            if mode == "values":
                latest = chunk
                continue
            for node in (chunk or {}).keys():
                marks[node] = now - last
                last = now
    except Exception as exc:  # noqa: BLE001 —— 实验里任何异常都要记录，不能中断整批
        return {"experiment": experiment, "arm": arm, "rep": rep,
                "elapsed_s": round(time.perf_counter() - started, 1), "success": False,
                "error": f"{type(exc).__name__}: {exc}"[:80]}

    latest = latest or {}
    plan = latest.get("plan") or {}
    report = latest.get("constraint_report")
    usage = latest.get("usage") or {}
    days = plan.get("days") or []
    row = {
        "experiment": experiment,
        "arm": arm,
        "rep": rep,
        "elapsed_s": round(time.perf_counter() - started, 1),
        "success": bool(days),
        "llm_calls": usage.get("llm_calls", 0),
        "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
        "tool_calls": len(latest.get("tool_calls") or []),
        "required": len(report.required_attractions) if report else 0,
        "satisfied": len(report.satisfied) if report else 0,
        "plan_days": len(days),
        "attractions": sum(len(d.get("attractions") or []) for d in days),
        "budget": (plan.get("budget") or {}).get("total", 0) if plan else 0,
        "warnings": len(latest.get("warnings") or []),
    }
    for node in NODE_KEYS:
        row[f"ms_{node}"] = round(marks.get(node, 0) * 1000)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3, help="压缩 A/B 每个臂的重复次数")
    parser.add_argument("--timing-runs", type=int, default=10, help="逐节点耗时的采样次数")
    parser.add_argument("--out", default="experiments")
    args = parser.parse_args()

    out_dir = BACKEND / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    total = args.reps * 2 + args.timing_runs
    print(f"共 {total} 次运行，预计 {total * 35 // 60}~{total * 60 // 60} 分钟", flush=True)

    with (out_dir / "raw_v05.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS + ["error"])
        writer.writeheader()

        def record(row: dict) -> None:
            rows.append(row)
            writer.writerow({k: row.get(k, "") for k in COLUMNS + ["error"]})
            handle.flush()
            print(
                f"[{len(rows)}/{total}] {row['experiment']} {row['arm']} #{row['rep']} | "
                f"{row['elapsed_s']}s | LLM {row['llm_calls']} | token {row['tokens']} | "
                f"景点 {row.get('attractions')} | 约束 {row.get('satisfied')}/{row.get('required')} | "
                f"verify {(row.get('ms_verify_plan') or 0)/1000:.1f}s | "
                f"hotel {(row.get('ms_resolve_hotel') or 0)/1000:.1f}s",
                flush=True,
            )

        # ---- 实验 D：上下文压缩 A/B ----
        for arm, compact in (("D1_压缩关闭", False), ("D2_压缩开启", True)):
            get_settings().prompt_compact = compact
            for rep in range(1, args.reps + 1):
                record(run_once(arm, rep, "D_上下文压缩"))

        # ---- 实验 E：逐节点耗时分布（用默认配置，压缩开启）----
        get_settings().prompt_compact = True
        for rep in range(1, args.timing_runs + 1):
            record(run_once("E_逐节点耗时", rep, "E_耗时分布"))

    # ---------------- 汇总 ----------------
    lines = ["# v0.5 实验汇总", "", f"生成时间：{date.today().isoformat()}　共 {len(rows)} 次运行", ""]

    def median(rows_: list[dict], key: str) -> str:
        vals = [r[key] for r in rows_ if isinstance(r.get(key), (int, float))]
        return f"{statistics.median(vals):.0f}" if vals else "-"

    def percentile(rows_: list[dict], key: str, pct: float) -> str:
        vals = sorted(r[key] for r in rows_ if isinstance(r.get(key), (int, float)))
        if not vals:
            return "-"
        index = min(int(len(vals) * pct), len(vals) - 1)
        return f"{vals[index]:.0f}"

    lines += ["## 实验 D · 上下文压缩：省了 token，行程有没有变差？", "",
              "| 臂 | 运行 | token(中位) | LLM 调用 | 端到端秒 | 天数 | 景点数 | 预算 | **约束满足** |",
              "|---|---|---|---|---|---|---|---|---|"]
    for arm in ("D1_压缩关闭", "D2_压缩开启"):
        rows_ = [r for r in rows if r["arm"] == arm]
        if not rows_:
            continue
        sat = statistics.mean(
            (r["satisfied"] / r["required"]) for r in rows_ if r["required"]
        ) * 100
        lines.append(
            f"| {arm} | {len(rows_)} | {median(rows_, 'tokens')} | {median(rows_, 'llm_calls')} | "
            f"{median(rows_, 'elapsed_s')} | {median(rows_, 'plan_days')} | "
            f"{median(rows_, 'attractions')} | {median(rows_, 'budget')} | **{sat:.0f}%** |"
        )
    d1 = [r for r in rows if r["arm"] == "D1_压缩关闭"]
    d2 = [r for r in rows if r["arm"] == "D2_压缩开启"]
    if d1 and d2:
        t1 = statistics.median([r["tokens"] for r in d1])
        t2 = statistics.median([r["tokens"] for r in d2])
        cut = (1 - t2 / t1) * 100 if t1 else 0
        lines += ["", f"> 中位 token：{t1:.0f} → {t2:.0f}（**省 {cut:.0f}%**）。"
                      f"关键看右侧的约束满足率与行程规模：**省 token 不能以行程变差为代价**。"]

    e_rows = [r for r in rows if r["experiment"] == "E_耗时分布"]
    if e_rows:
        lines += ["", "## 实验 E · 逐节点耗时分布（p50 / p95，毫秒）", "",
                  "| 节点 | p50 | p95 | 占总耗时 |", "|---|---|---|---|"]
        total_p50 = sum(statistics.median([r.get(f"ms_{n}", 0) for r in e_rows]) for n in NODE_KEYS)
        for node in NODE_KEYS:
            p50 = statistics.median([r.get(f"ms_{node}", 0) for r in e_rows])
            if p50 <= 0 and all(r.get(f"ms_{node}", 0) == 0 for r in e_rows):
                continue
            share = p50 / total_p50 * 100 if total_p50 else 0
            lines.append(
                f"| {node} | {p50:.0f} | {percentile(e_rows, f'ms_{node}', 0.95)} | {share:.0f}% |"
            )
        lines += ["", "> 两路采集是**并行**执行的，它们的数字会相互重叠，应当合并看作「采集阶段」。", ""]

    ok = sum(1 for r in rows if r.get("success"))
    lines += ["## 整体", "",
              f"- 成功运行：{ok}/{len(rows)}",
              f"- 端到端中位数：{median(rows, 'elapsed_s')} 秒",
              f"- 中位 token：{median(rows, 'tokens')}",
              f"- 约束完全满足：{sum(1 for r in rows if r.get('required') and r.get('satisfied') == r.get('required'))}/{len(rows)}"]

    (out_dir / "summary_v05.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines), flush=True)
    print(f"\n原始数据：{out_dir / 'raw_v05.csv'}", flush=True)


if __name__ == "__main__":
    main()
