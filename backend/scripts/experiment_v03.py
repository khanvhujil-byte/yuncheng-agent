r"""v0.3 对照实验跑批脚本 —— 简历上那些数字从这里来。

为什么**在进程内跑图**而不是打 HTTP：
    响应体里只有对外的 TripPlan；而实验要的 usage（token / LLM 调用次数）、
    tool_calls（工具调用明细）、各节点写的 sources，全都只活在 graph 的**最终 state** 里。
    进程内 `graph.invoke()` 能一次拿到全部，不用为了跑实验去改对外的响应契约。

三个实验（对应 docs/开发计划.md 第 11 章）：

    A  Agent 化的代价     臂 A1「全 Agent」 vs 臂 A2「天气走纯工具」
    B  天数自变量         2 / 4 / 6 / 8 天 → 超出预报窗口的天有没有被如实留空
    C  用户指定非最优酒店  量化「用户订的酒店不是最优解」的代价

用法（在 backend 目录下）：
    .\.venv\Scripts\python.exe -u scripts\experiment_v03.py
    .\.venv\Scripts\python.exe -u scripts\experiment_v03.py --reps 2 --out experiments

产出（边跑边写，随时可读）：
    <out>/raw_runs.csv    每次运行的原始指标，一行一次
    <out>/summary.md      汇总表（可直接贴进交付报告）
"""

import argparse
import csv
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agents.graph import app as trip_graph  # noqa: E402
from app.api.routes.trip import _initial_state  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models.schemas import TripRequest  # noqa: E402

COLUMNS = [
    "experiment", "arm", "days", "rep", "elapsed_s", "success", "error_code",
    "llm_calls", "prompt_tokens", "completion_tokens", "tool_calls",
    "attractions", "plan_days", "required", "satisfied", "unsatisfied",
    "weather_days", "beyond_window", "fabricated_weather", "retries",
    "hotel", "hotel_weighted_km", "budget_total", "degraded_sources", "warning_count",
]


@dataclass
class Case:
    experiment: str
    arm: str
    travel_days: int = 3
    fixed_hotel: str | None = None
    preferences: list[str] = field(default_factory=lambda: ["自然风光", "博物馆"])
    must_visit: list[str] = field(default_factory=lambda: ["西湖"])


def _fabricated(final: dict) -> int:
    """编造天气的天数：超出预报窗口（beyond_window）却填了天气内容 —— 必须是 0。

    这是「诚实性实验」的核心判据：留空是正确，填了就是编造。
    """
    count = 0
    for item in final.get("weather") or []:
        if getattr(item, "beyond_window", False):
            if (item.day_weather or "").strip() or (item.night_weather or "").strip():
                count += 1
    return count


def _weighted_km(final: dict) -> float | None:
    """酒店到行程景点的加权平均直线距离（权重 = 建议游览时长）。

    只做**观测**，不参与任何决策 —— 用它比较不同臂的"选址质量"。
    """
    hotel = final.get("hotel")
    plan = final.get("plan") or {}
    if not hotel or not hotel.location:
        return None
    spots = [
        a for day in (plan.get("days") or [])
        for a in [x if isinstance(x, dict) else x.model_dump() for x in day.get("attractions") or []]
    ]
    coords = [(s["location"]["longitude"], s["location"]["latitude"], s.get("visit_duration") or 120)
              for s in spots if s.get("location")]
    if not coords:
        return None
    total_w = sum(c[2] for c in coords)

    def km(a_lng: float, a_lat: float, b_lng: float, b_lat: float) -> float:
        import math

        radius = 6371.0
        lat1, lon1, lat2, lon2 = map(math.radians, [a_lat, a_lng, b_lat, b_lng])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * radius * math.asin(math.sqrt(h))

    cost = sum(km(hotel.location.longitude, hotel.location.latitude, lng, lat) * w for lng, lat, w in coords)
    return round(cost / total_w, 2)


def run_one(case: Case, rep: int) -> dict:
    settings = get_settings()
    settings.weather_use_agent = case.arm != "A2_天气纯工具"

    start = date.today()
    req = TripRequest(
        city="杭州",
        start_date=start.isoformat(),
        travel_days=case.travel_days,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=case.preferences,
        must_visit=case.must_visit,
        fixed_hotel=case.fixed_hotel,
    )

    t0 = time.perf_counter()
    try:
        final = trip_graph.invoke(_initial_state(req))
        error = ""
    except Exception as exc:  # noqa: BLE001 —— 实验里任何异常都要记录，不能让整批中断
        final, error = {}, f"{type(exc).__name__}: {exc}"
    elapsed = round(time.perf_counter() - t0, 1)

    usage = final.get("usage") or {}
    report = final.get("constraint_report")
    plan = final.get("plan") or {}
    weather = final.get("weather") or []
    hotel = final.get("hotel")

    return {
        "experiment": case.experiment,
        "arm": case.arm,
        "days": case.travel_days,
        "rep": rep,
        "elapsed_s": elapsed,
        "success": bool(plan.get("days")) and not error,
        "error_code": error or (final.get("error_code") or ""),
        "llm_calls": usage.get("llm_calls", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "tool_calls": len(final.get("tool_calls") or []),
        "attractions": len(final.get("attractions") or []),
        "plan_days": len(plan.get("days") or []),
        "required": len(report.required_attractions) if report else 0,
        "satisfied": len(report.satisfied) if report else 0,
        "unsatisfied": len(report.unsatisfied) if report else 0,
        "weather_days": len(weather),
        "beyond_window": sum(1 for w in weather if getattr(w, "beyond_window", False)),
        "fabricated_weather": _fabricated(final),
        "retries": final.get("retries", 0),
        "hotel": (hotel.name if hotel else "")[:28],
        "hotel_weighted_km": _weighted_km(final),
        "budget_total": (plan.get("budget") or {}).get("total", 0) if plan else 0,
        "degraded_sources": ",".join(k for k, v in (final.get("sources") or {}).items() if v == "degraded"),
        "warning_count": len(final.get("warnings") or []),
    }


def build_cases(reps: int) -> list[Case]:
    cases: list[Case] = []
    # ---- 实验 A：Agent 化的代价（同一输入，只切换天气节点走不走 Agent）----
    for arm in ("A1_全Agent", "A2_天气纯工具"):
        for _ in range(reps):
            cases.append(Case(experiment="A_Agent化代价", arm=arm, travel_days=3))
    # ---- 实验 B：天数自变量 → 预报窗口诚实性（4 天是窗口边界）----
    for days in (2, 4, 6, 8):
        for _ in range(reps):
            cases.append(Case(experiment="B_天数诚实性", arm="B_全Agent", travel_days=days))
    # ---- 实验 C：用户指定一家「非最优」的酒店 ----
    for _ in range(reps):
        cases.append(
            Case(
                experiment="C_用户固定酒店",
                arm="C_用户指定非最优酒店",
                travel_days=3,
                fixed_hotel="布丁精选酒店(杭州武林广场西湖文化广场地铁站店)",
            )
        )
    return cases


def summarize(rows: list[dict]) -> str:
    lines = ["# v0.3 对照实验汇总", "", f"生成时间：{date.today().isoformat()}　共 {len(rows)} 次运行", ""]

    def group(key: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(str(row[key]), []).append(row)
        return out

    def med(rows_: list[dict], col: str) -> str:
        vals = [r[col] for r in rows_ if isinstance(r.get(col), (int, float))]
        return f"{statistics.median(vals):.1f}" if vals else "-"

    lines += ["## 实验 A · Agent 化的代价（3 天同输入，中位数）", ""]
    lines.append("| 臂 | 运行 | 端到端秒 | LLM 调用 | token(prompt+completion) | 工具调用 | 预算 |")
    lines.append("|---|---|---|---|---|---|---|")
    for arm, rows_ in sorted(group("arm").items()):
        if not arm.startswith("A"):
            continue
        tok = statistics.median([r["prompt_tokens"] + r["completion_tokens"] for r in rows_])
        lines.append(
            f"| {arm} | {len(rows_)} | {med(rows_, 'elapsed_s')} | {med(rows_, 'llm_calls')} | "
            f"{tok:.0f} | {med(rows_, 'tool_calls')} | {med(rows_, 'budget_total')} |"
        )

    lines += ["", "## 实验 B · 天数自变量与天气预报诚实性", ""]
    lines.append("| 天数 | 运行 | 天气条数 | 超窗口天数 | **编造天数** | 端到端秒 | 约束满足 |")
    lines.append("|---|---|---|---|---|---|---|")
    for days, rows_ in sorted(group("days").items(), key=lambda kv: int(kv[0])):
        if not rows_ or rows_[0]["experiment"] != "B_天数诚实性":
            continue
        sat = statistics.mean([r["satisfied"] / r["required"] for r in rows_ if r["required"]]) * 100
        lines.append(
            f"| {days} | {len(rows_)} | {med(rows_, 'weather_days')} | {med(rows_, 'beyond_window')} | "
            f"**{sum(r['fabricated_weather'] for r in rows_)}** | {med(rows_, 'elapsed_s')} | {sat:.0f}% |"
        )
    lines += ["", "> 「编造天数」= 标记为超出预报窗口、却仍然填了天气内容的天数。**正确实现必须恒为 0。**"]

    c_rows = [r for r in rows if r["experiment"] == "C_用户固定酒店"]
    if c_rows:
        lines += ["", "## 实验 C · 用户指定非最优酒店的代价", ""]
        lines.append("| 臂 | 运行 | 酒店→行程景点加权平均距离(km) | 预算 | 端到端秒 |")
        lines.append("|---|---|---|---|---|")
        lines.append(
            f"| C 用户指定 | {len(c_rows)} | {med(c_rows, 'hotel_weighted_km')} | "
            f"{med(c_rows, 'budget_total')} | {med(c_rows, 'elapsed_s')} |"
        )
        a1 = [r for r in rows if r["arm"] == "A1_全Agent" and r["days"] == 3]
        if a1:
            lines.append(
                f"| A1 系统选址（对照） | {len(a1)} | {med(a1, 'hotel_weighted_km')} | "
                f"{med(a1, 'budget_total')} | {med(a1, 'elapsed_s')} |"
            )

    ok = sum(1 for r in rows if r["success"])
    lines += [
        "", "## 整体", "",
        f"- 成功运行：{ok}/{len(rows)}",
        f"- 端到端耗时中位数：{med(rows, 'elapsed_s')} 秒",
        f"- 平均 LLM 调用次数：{statistics.mean([r['llm_calls'] for r in rows]):.1f}",
        f"- 平均工具调用次数：{statistics.mean([r['tool_calls'] for r in rows]):.1f}",
        f"- 平均 token：{statistics.mean([r['prompt_tokens'] + r['completion_tokens'] for r in rows]):.0f}",
        f"- **编造天气总天数：{sum(r['fabricated_weather'] for r in rows)}（应为 0）**",
        f"- 约束完全满足的运行：{sum(1 for r in rows if r['required'] and r['satisfied'] == r['required'])}/{len(rows)}",
        f"- 出现降级的运行：{sum(1 for r in rows if r['degraded_sources'])}/{len(rows)}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3, help="每个条件的重复次数")
    parser.add_argument("--out", default="experiments", help="输出目录（相对 backend）")
    args = parser.parse_args()

    out_dir = BACKEND / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw_runs.csv"
    cases = build_cases(args.reps)

    print(f"共 {len(cases)} 次运行，预计 {len(cases) * 40 // 60}~{len(cases) * 60 // 60} 分钟", flush=True)
    rows: list[dict] = []
    with raw_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for index, case in enumerate(cases, start=1):
            row = run_one(case, rep=(index - 1) % args.reps + 1)
            rows.append(row)
            writer.writerow(row)
            handle.flush()  # 边跑边落盘：中途被打断也能读到已完成的记录
            print(
                f"[{index}/{len(cases)}] {case.experiment} {case.arm} {case.travel_days}天 "
                f"| {row['elapsed_s']}s | LLM {row['llm_calls']} | token "
                f"{row['prompt_tokens'] + row['completion_tokens']} | 工具 {row['tool_calls']} | "
                f"编造 {row['fabricated_weather']} | 酒店 {row['hotel_weighted_km']}km",
                flush=True,
            )

    summary = summarize(rows)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary, flush=True)
    print(f"原始数据：{raw_path}", flush=True)


if __name__ == "__main__":
    main()
