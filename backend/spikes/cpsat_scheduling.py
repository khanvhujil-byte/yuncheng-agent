# -*- coding: utf-8 -*-
"""离线 CP-SAT 排程 spike（独立运行，不接入主图）。

用途
----
在真实接入主图之前，先用**离线、可复跑**的方式验证三件事：

1. 排期问题能不能被显式建模成约束满足问题，并给出明确的
   FEASIBLE / INFEASIBLE / UNKNOWN 语义；
2. 不可行时，能不能报出"是哪些约束导致无解"（充分子集，不是最小冲突集）；
3. 预算里 known / estimated / unknown 三种口径能不能分开表达，
   尤其是 unknown 绝不当 0 通过。

两臂对照（读同一份 tasks.json、同一套约束、同一个独立打分器）
-------------------------------------------------------------
- 臂 A「固定排期基线」：确定性贪心顺序填充，不调用任何求解器；
- 臂 B「CP-SAT」：位置槽位建模 + 时间量累积 + 开放窗 + 预算。

这两个词是本 spike 的固定叫法。这里的约束是**结构化输入**（直接来自
tasks.json），不是模型抽取的，所以"约束抽取正确性"不在本测试范围内。

命令行
------
    python cpsat_scheduling.py --timeout 5 --reps 3

退出码：0 = 脚本正常跑完（任务结论是 FEASIBLE 还是 INFEASIBLE 都不影响退出码）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 依赖导入：ortools 是唯一外部依赖。缺失时给出人话提示，避免一堆红色堆栈把
# 真正的原因（没装包）埋掉。
# --------------------------------------------------------------------------
try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - 只在环境缺包时触发
    sys.stderr.write(
        "[FATAL] 缺少 ortools。请用本仓库的虚拟环境安装：\n"
        "    .\\.venv\\Scripts\\python.exe -m pip install ortools\n"
    )
    raise SystemExit(2)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TASKS_PATH = os.path.join(SCRIPT_DIR, "tasks.json")
DEFAULT_CSV_PATH = os.path.join(SCRIPT_DIR, "results.csv")
DEFAULT_SUMMARY_PATH = os.path.join(SCRIPT_DIR, "summary.md")

# 结论成立的范围声明。出现在终端输出、CSV 每一行、summary.md 与 README 里，
# 保证任何一份证据单独拿出去都带着前提。
GUARANTEE_SCOPE = (
    "结论只在给定候选池、给定天/日时间窗、haversine 估算通勤"
    "（{speed} km/h 直线折算）与估算/未知票价口径下成立；"
    "换候选池、换时间窗、换通勤估算或换票价数据，结论都可能改变。"
)


# ==========================================================================
# 小工具
# ==========================================================================
def hhmm_to_min(text: str) -> int:
    """把 'HH:MM' 转成从 00:00 起的分钟数。"""
    hours, minutes = text.strip().split(":")
    return int(hours) * 60 + int(minutes)


def min_to_hhmm(total: int) -> str:
    """把分钟数转回 'HH:MM'（用于输出，方便人看）。"""
    total = int(total)
    return f"{total // 60:02d}:{total % 60:02d}"


def haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点大圆距离（km）。这是**估算**，不是路网距离。"""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * radius * math.asin(math.sqrt(a))


def travel_minutes(
    poi_a: Dict[str, Any], poi_b: Dict[str, Any], speed_kmh: float, minimum: int = 1
) -> int:
    """估算单程通勤分钟数：haversine 直线距离按地面速度折算，四舍五入，保底 1 分钟。"""
    a = poi_a["location"]
    b = poi_b["location"]
    km = haversine_km(a["lng"], a["lat"], b["lng"], b["lat"])
    return max(minimum, int(round(km / speed_kmh * 60.0)))


def build_travel_matrix(
    pool_ids: Sequence[str], pois: Dict[str, Dict[str, Any]], speed_kmh: float
) -> Dict[Tuple[str, str], int]:
    """候选池内两两（含自己到自己）的估算通勤分钟数。"""
    matrix: Dict[Tuple[str, str], int] = {}
    for i in pool_ids:
        for j in pool_ids:
            if i == j:
                matrix[(i, j)] = 0
            else:
                matrix[(i, j)] = travel_minutes(pois[i], pois[j], speed_kmh)
    return matrix


def write_utf8(path: str, text: str) -> None:
    """统一用无 BOM 的 UTF-8 写文件，避免 Windows 默认编码把中文写坏。"""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def configure_stdout() -> None:
    """把 stdout 调到 UTF-8，避免中文在控制台因编码问题崩掉。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ==========================================================================
# 任务数据加载
# ==========================================================================
def load_dataset(path: str) -> Dict[str, Any]:
    """读 tasks.json 并做基本自检：POI 不重名、候选池不引用不存在的 POI。"""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    pois = {item["poi_id"]: item for item in payload["pois"]}
    if len(pois) != len(payload["pois"]):
        raise ValueError("tasks.json 里存在重复的 poi_id")

    for task in payload["tasks"]:
        for poi_id in task["candidate_pool"]:
            if poi_id not in pois:
                raise ValueError(f"{task['task_id']} 的候选池引用了不存在的 POI: {poi_id}")
        for poi_id in task["must_visit"]:
            if poi_id not in task["candidate_pool"]:
                raise ValueError(f"{task['task_id']} 的 must_visit 不在候选池内: {poi_id}")
    return payload


# ==========================================================================
# 两臂共用的东西：任务实例（Instances）
# ==========================================================================
def build_instance(task: Dict[str, Any], pois: Dict[str, Dict[str, Any]], default_speed: float) -> Dict[str, Any]:
    """把任务声明 + POI 注册表实例化成求解/打分都要用的扁平结构。

    关键点：这个函数是**两臂唯一的数据入口**，所以两臂看到的候选池、约束、
    时间矩阵完全一致，不可能出现"一边改了数据另一边没改"。
    """
    pool_ids = list(task["candidate_pool"])
    model_cfg = task.get("constraint_model", {})
    speed = float(model_cfg.get("travel_speed_kmh", default_speed))

    travel = model_cfg.get("travel_matrix")
    if travel:
        matrix: Dict[Tuple[str, str], int] = {}
        for i in pool_ids:
            for j in pool_ids:
                if i == j:
                    matrix[(i, j)] = 0
                else:
                    matrix[(i, j)] = int(travel[i][j])
    else:
        matrix = build_travel_matrix(pool_ids, pois, speed)

    day_start = hhmm_to_min(task["day_window"]["start"])
    day_end = hhmm_to_min(task["day_window"]["end"])
    if day_end <= day_start:
        raise ValueError(f"{task['task_id']} 的 day_window 不是有效区间")

    open_windows: Dict[str, Optional[Tuple[int, int]]] = {}
    for poi_id in pool_ids:
        raw = pois[poi_id].get("open_window")
        open_windows[poi_id] = None if not raw else (hhmm_to_min(raw["start"]), hhmm_to_min(raw["end"]))

    visits = {p: int(pois[p]["visit_minutes"]) for p in pool_ids}
    prices = {p: pois[p].get("ticket_price") for p in pool_ids}
    price_status = {p: pois[p].get("price_status") for p in pool_ids}

    # 任务级覆盖：允许某个任务改写它自己候选池里 POI 的字段（例如把同一个净慈寺
    # 的开放窗调早/调晚 10 分钟，用来做"刚好卡住 vs 差 5 分钟"的对照）。
    # 覆盖只作用于这一个任务，不影响共享注册表里的其他任务。
    for poi_id, patch in (task.get("overrides") or {}).items():
        if poi_id not in pool_ids:
            raise ValueError(f"{task['task_id']} 的 overrides 引用了不在候选池内的 POI: {poi_id}")
        if "visit_minutes" in patch:
            visits[poi_id] = int(patch["visit_minutes"])
        if "ticket_price" in patch:
            prices[poi_id] = patch["ticket_price"]
        if "price_status" in patch:
            price_status[poi_id] = patch["price_status"]
        if "open_window" in patch:
            raw_window = patch["open_window"]
            open_windows[poi_id] = (
                None
                if not raw_window
                else (hhmm_to_min(raw_window["start"]), hhmm_to_min(raw_window["end"]))
            )

    return {
        "task_id": task["task_id"],
        "description": task.get("description", ""),
        "expect": task.get("expect", {}),
        "days": int(task["days"]),
        "day_start": day_start,
        "day_end": day_end,
        "must_visit": list(task["must_visit"]),
        "max_pois_per_day": int(task["max_pois_per_day"]),
        "budget_limit": task.get("budget_limit"),
        "pool_ids": pool_ids,
        "visits": visits,
        "prices": prices,
        "price_status": price_status,
        "names": {p: pois[p]["name"] for p in pool_ids},
        "open_windows": open_windows,
        "travel": matrix,
        "travel_speed_kmh": speed,
        "overrides": task.get("overrides") or {},
    }


# ==========================================================================
# 独立的打分器（两臂共用）
# --------------------------------------------------------------------------
# 打分器不看求解器的内部状态，只看"排出来的行程"。它自己重新算时间线、
# 自己重新算预算状态 —— 所以求解器的实现错误有可能被它抓出来。
# ==========================================================================
class Judge:
    """对一条排好的行程做独立复核。返回 (是否全过, 明细列表)。"""

    def __init__(self, inst: Dict[str, Any]) -> None:
        self.inst = inst

    def evaluate(self, schedule: Optional[Dict[str, Any]]) -> Tuple[bool, List[Dict[str, str]], Dict[str, Any]]:
        inst = self.inst
        checks: List[Dict[str, str]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

        if schedule is None:
            add("有行程可评", False, "该臂没有产出行程（INFEASIBLE / UNKNOWN 时正常）")
            return False, checks, {}

        days = schedule.get("days", [])
        n_days = inst["days"]

        # --- 维度 1：天数是否对得上 ---
        add("天数一致", len(days) == n_days, f"排期天数 {len(days)}，任务要求 {n_days}")

        # --- 维度 2：是否重复安排 ---
        seen: List[str] = [item["poi_id"] for day in days for item in day.get("items", [])]
        dupes = sorted({p for p in seen if seen.count(p) > 1})
        add("无重复安排", not dupes, "重复 POI: " + (", ".join(dupes) if dupes else "无"))

        # --- 维度 3：POI 是否都在候选池内 ---
        outside = sorted({p for p in seen if p not in inst["pool_ids"]})
        add("POI 在候选池内", not outside, "越界 POI: " + (", ".join(outside) if outside else "无"))

        # --- 维度 4：must_visit 是否全部在内 ---
        missing = [p for p in inst["must_visit"] if p not in seen]
        add("must_visit 全覆盖", not missing, "缺失必去: " + (", ".join(missing) if missing else "无"))

        # --- 维度 5：每天数量上限 ---
        cap_ok = all(len(day.get("items", [])) <= inst["max_pois_per_day"] for day in days)
        worst = max((len(day.get("items", [])) for day in days), default=0)
        add(
            "每天数量上限",
            cap_ok,
            f"单日最多 {worst} 个，上限 {inst['max_pois_per_day']}",
        )

        # --- 维度 6：时间线（窗口 + 通勤 + 开放窗），全部由打分器自己从零算 ---
        timeline_ok = True
        timeline_notes: List[str] = []
        for day in days:
            cursor = inst["day_start"]
            prev = None
            for item in day.get("items", []):
                poi_id = item["poi_id"]
                if prev is not None:
                    cursor += inst["travel"][(prev, poi_id)]
                arrive = cursor
                start = max(arrive, hhmm_to_min(item["start"]))
                end = start + inst["visits"][poi_id]

                if hhmm_to_min(item["start"]) < arrive:
                    timeline_ok = False
                    timeline_notes.append(f"{poi_id} 声称 {item['start']} 开始，但通勤到不了（最早 {min_to_hhmm(arrive)}）")
                if end > inst["day_end"]:
                    timeline_ok = False
                    timeline_notes.append(f"{poi_id} 结束 {min_to_hhmm(end)} 超出日窗口 {min_to_hhmm(inst['day_end'])}")

                window = inst["open_windows"].get(poi_id)
                if window is not None:
                    if start < window[0] or end > window[1]:
                        timeline_ok = False
                        timeline_notes.append(
                            f"{poi_id} 安排 {min_to_hhmm(start)}-{min_to_hhmm(end)} 不在开放窗 "
                            f"{min_to_hhmm(window[0])}-{min_to_hhmm(window[1])} 内"
                        )
                cursor = end
                prev = poi_id
        add("时间线满足（窗口/通勤/开放窗）", timeline_ok, "; ".join(timeline_notes) if timeline_notes else "全部满足")

        # --- 维度 7：预算状态自洽（打分器自己重算一遍，不复用求解器的分类函数） ---
        judge_budget = classify_budget_from_costs(
            known=schedule.get("known_total", 0),
            estimated=schedule.get("estimated_total", 0),
            unknown_ids=[p for p in seen if inst["prices"].get(p) is None],
            limit=inst["budget_limit"],
        )
        claimed = schedule.get("budget_status")
        add(
            "预算状态自洽",
            claimed == judge_budget["status"],
            f"行程声称 {claimed}，打分器重算 {judge_budget['status']}",
        )
        # 未知项绝不能按 0 计入已知/估算总价
        unknown_leaked = any(inst["prices"].get(p) is None for p in seen) and not judge_budget["has_unknown"]
        add("未知项未被当作 0", not unknown_leaked, "存在未知票价且已被识别" if not unknown_leaked else "未知项被静默忽略")
        # 已知费用超上限是"确定超支"，不该被当成一个可接受的行程
        over_known = (
            inst["budget_limit"] is not None and schedule.get("known_total", 0) > inst["budget_limit"]
        )
        add(
            "已知费用未超上限",
            not over_known,
            "已知合计在预算内" if not over_known else f"已知合计 {schedule.get('known_total')} 已超 {inst['budget_limit']}",
        )

        passed = all(item["result"] == "PASS" for item in checks)
        return passed, checks, judge_budget


def classify_budget_from_costs(
    known: float,
    estimated: float,
    unknown_ids: Sequence[str],
    limit: Optional[float],
) -> Dict[str, Any]:
    """预算状态分类（打分器侧独立实现，规则与 README 里写的一致）。

    known_exceeded      已知费用就已超上限（无论未知项如何都成立）
    estimated_exceeded  加上估算费用后超上限（不能据此断言实际不可行）
    unverified          存在影响判断的未知项（绝不能按 0 元通过）
    within              当前数据与估算口径内未超限（附依据）
    none                用户没给上限
    """
    has_unknown = len(unknown_ids) > 0
    total = float(known) + float(estimated)
    if limit is None:
        status, scope = "none", "未给出预算上限，无法判断是否超支。"
    elif float(known) > float(limit):
        status, scope = "known_exceeded", "已知票价合计已经超过上限；未知项只会让总额更高，结论不依赖未知项。"
    elif total > float(limit):
        status, scope = "estimated_exceeded", "已知+估算合计超过上限；估算值不是实测值，不能据此断言实际不可行。"
    elif has_unknown:
        status, scope = "unverified", "当前口径未超限，但存在价格未知的 POI；未知项按未知处理，未按 0 元计入。"
    else:
        status, scope = "within", "所有票价均为 known/estimated，合计未超上限；结论依赖 estimated 值的准确性。"
    return {
        "status": status,
        "known_total": float(known),
        "estimated_total": float(estimated),
        "total": total,
        "limit": limit,
        "has_unknown": has_unknown,
        "unknown_ids": list(unknown_ids),
        "scope": scope,
    }


# ==========================================================================
# 臂 A：固定排期基线（确定性贪心，不调用求解器）
# ==========================================================================
def baseline_greedy(inst: Dict[str, Any], judge: Judge) -> Dict[str, Any]:
    """按固定顺序往每天塞：必去优先（保持声明顺序），其余按候选池顺序。

    规则（固定且可复现，不含任何随机数）：
    1. 逐天填充，每天从 day_window.start 开始，按上面的顺序尝试候选；
    2. 放得下就放（同时满足日窗口、POI 开放窗、每天数量上限、预算硬约束）；
    3. 放不下就跳过，继续试下一个候选；当天结束时进入下一天；
    4. 必去项一个都没落下才算成功，否则判定 INFEASIBLE 并列出被跳过的必去项。

    这条臂不做回溯、不做交换，所以它给出的 FEASIBLE 只是"贪心找到了排法"，
    不代表"按这个模型无解"。INFEASIBLE 也只是"贪心按这套规则排不出来"。
    """
    started = time.perf_counter()
    days: List[Dict[str, Any]] = []
    scheduled: List[str] = []
    skipped: List[Dict[str, str]] = []
    known_total = 0.0
    estimated_total = 0.0

    must_set = set(inst["must_visit"])
    ordered = [p for p in inst["pool_ids"] if p in must_set] + [p for p in inst["pool_ids"] if p not in must_set]

    for day_index in range(inst["days"]):
        cursor = inst["day_start"]
        prev: Optional[str] = None
        items: List[Dict[str, Any]] = []
        for poi_id in ordered:
            if poi_id in scheduled:
                continue
            if len(items) >= inst["max_pois_per_day"]:
                break
            window = inst["open_windows"][poi_id]
            start = cursor + (0 if prev is None else inst["travel"][(prev, poi_id)])
            if window is not None:
                start = max(start, window[0])
            end = start + inst["visits"][poi_id]
            # 日窗口 / 开放窗
            if end > inst["day_end"]:
                skipped.append({"poi_id": poi_id, "reason": "日窗口放不下"})
                continue
            if window is not None and end > window[1]:
                skipped.append({"poi_id": poi_id, "reason": "开放时间窗放不下"})
                continue
            # 注意：基线**不拿预算当硬条件**，它只做"能不能排下"的可行性判断。
            # 预算作为"排完之后的结论"由 classify_budget_from_costs 输出。
            # 这样两臂在预算口径上是对齐的，差异只体现在"排得出/排不出"。
            price = inst["prices"][poi_id]
            known_total += price if (price is not None and inst["price_status"][poi_id] == "known") else 0
            estimated_total += (
                price if (price is not None and inst["price_status"][poi_id] != "known") else 0
            )
            items.append(
                {
                    "poi_id": poi_id,
                    "name": inst["names"][poi_id],
                    "arrive": min_to_hhmm(cursor + (0 if prev is None else inst["travel"][(prev, poi_id)])),
                    "start": min_to_hhmm(start),
                    "end": min_to_hhmm(end),
                    "visit_minutes": inst["visits"][poi_id],
                    "ticket_price": price,
                    "price_status": inst["price_status"][poi_id],
                }
            )
            scheduled.append(poi_id)
            cursor = end
            prev = poi_id
        days.append({"day_index": day_index + 1, "items": items})

    missing_must = [p for p in inst["must_visit"] if p not in scheduled]
    if missing_must:
        verdict = "INFEASIBLE"
        detail = "贪心按固定顺序排不出必去项: " + ", ".join(missing_must)
        schedule = None
        passed, checks, _ = judge.evaluate(None)
    else:
        verdict = "FEASIBLE"
        detail = "贪心顺序填充成功"
        unknown_ids = [p for p in scheduled if inst["prices"].get(p) is None]
        budget = classify_budget_from_costs(known_total, estimated_total, unknown_ids, inst["budget_limit"])
        schedule = {
            "days": days,
            "known_total": known_total,
            "estimated_total": estimated_total,
            "budget_status": budget["status"],
        }
        passed, checks, _ = judge.evaluate(schedule)
        missing_must = []

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "arm": "fixed_baseline",
        "verdict": verdict,
        "detail": detail,
        "schedule": schedule,
        "judge_passed": passed,
        "judge_checks": checks,
        "missing_must_visit": missing_must,
        "skipped": skipped,
        "solve_seconds": elapsed_ms / 1000.0,
        "solver_status": "not_applicable",
        "optimization_status": "not_applicable",
        "optimization_proven": False,
        "assumption_subset": [],
        "minimized_subset": [],
        "hard_assumption_subset": [],
        "sufficiency_verified": None,
        "relaxation_status": "",
        "relaxation_note": "",
        "domain_infeasible_proof": None,
        "unknown_price_bound": None,
        "solver_stats": {},
    }


# ==========================================================================
# 臂 B：CP-SAT
# ==========================================================================
def _unknown_price_bound(inst: Dict[str, Any]) -> Dict[str, Any]:
    """给未知票价算一个"有依据的保守上界"。

    规则：取同一个任务实例有效候选集中**最小的正价格**（known 或 estimated）
    作为单个未知项的上界。

    为什么这个界是保守的（不会低估）：候选池里两条已知/估算价格 P1 <= P2（P1 是最小正价格），
    剩下都是未知项。若真实总价 > 上限，则必然存在一项超过它的已知/估算价格
    （否则总价 = sum(已知) + sum(未知) < P1 + P1 = 2*P1 <= P1 + P2，矛盾）。
    所以用"未知项全部按 P1 计"得到的总额不会低估真实总额。
    """
    positives = [
        float(inst["prices"][p])
        for p in inst["pool_ids"]
        if inst["prices"][p] is not None and float(inst["prices"][p]) > 0
    ]
    per_unknown = min(positives) if positives else 0.0
    return {
        "per_unknown_upper_bound": per_unknown,
        "basis": "取同任务候选池中最小的正票价作为单个未知项上界",
        "known_estimated_total": 0.0,  # 由调用方填充
        "upper_bound_total": 0.0,
    }


def build_model(inst: Dict[str, Any], register_assumptions: bool = True) -> Dict[str, Any]:
    """构建 CP-SAT 模型（求解与诊断共用同一个建模函数）。

    建模要点
    --------
    - 位置槽位：每天 K 个槽位（K = 候选池大小），x[poi][day][slot] 表示该 POI
      放在该天该槽位；
    - 时间用累积量 cum[day][slot] 表示"第 slot 个槽位开始时刻"，槽位之间的
      通勤用 AddElement 查表得到（所以通勤时间进入模型，不是事后加的）；
    - 必去覆盖、每个 POI 只排一次、槽位互斥、时间线这些"结构约束"始终是硬约束；
    - 每天数量上限 / POI 开放时间窗 / 日窗口收起时刻 / 预算上限这四族约束，
      **只**由假设字面量控制。正常求解时统一 Add(lit == 1) 让它们全部生效；
      诊断时只放开其中若干条，就能真正看清"是哪一族导致无解"。
    - 返回值里带 literals（字面量）与 literal_names，供诊断/自查使用。
    """
    pool = inst["pool_ids"]
    n_pool = len(pool)
    days = inst["days"]
    visits = inst["visits"]
    windows = inst["open_windows"]
    travel = inst["travel"]
    cap = inst["max_pois_per_day"]
    day_start, day_end = inst["day_start"], inst["day_end"]
    limit = inst["budget_limit"]
    price = inst["prices"]
    pstatus = inst["price_status"]

    # 先做一次领域级下界检查：这类不可行不需要求解器就能证明，
    # 而且能直接告诉用户"是哪条计数型约束卡住了"。
    domain_proof = None
    required_min_slots = len(inst["must_visit"])
    if required_min_slots > days * cap:
        domain_proof = (
            f"必去项 {required_min_slots} 个 > 天数 {days} × 每天上限 {cap} = {days * cap}，"
            "纯计数即可证明无解，无需进入搜索。"
        )

    model = cp_model.CpModel()
    assign: Dict[Tuple[str, int, int], Any] = {}
    for poi_id in pool:
        for d in range(days):
            for s in range(n_pool):
                assign[(poi_id, d, s)] = model.NewBoolVar(f"x_{poi_id}_{d}_{s}")

    # 每个 POI 最多出现一次（整个行程内）
    for poi_id in pool:
        model.Add(sum(assign[(poi_id, d, s)] for d in range(days) for s in range(n_pool)) <= 1)

    # 每个槽位最多一个 POI
    for d in range(days):
        for s in range(n_pool):
            model.Add(sum(assign[(poi_id, d, s)] for poi_id in pool) <= 1)

    # 必去项必须恰好安排一次（硬约束：这就是"约束满足"本身）
    for poi_id in inst["must_visit"]:
        model.Add(sum(assign[(poi_id, d, s)] for d in range(days) for s in range(n_pool)) == 1)

    # 累积时间量：cum[d][s] = 第 s 个槽位的开始时刻
    horizon = day_end + 1
    cum: Dict[Tuple[int, int], Any] = {}
    for d in range(days):
        for s in range(n_pool):
            cum[(d, s)] = model.NewIntVar(day_start, horizon, f"t_{d}_{s}")

    # 槽位顺序：后一个槽位必须在前一个之后（空槽位时间不前进）
    for d in range(days):
        for s in range(1, n_pool):
            model.Add(cum[(d, s)] >= cum[(d, s - 1)])

    # used[d][s] = 该槽位是否有人；并强制"已用槽位必须是前缀"。
    # 这条前缀约束把"空槽位"挤到当天末尾，避免求解器把空位插在中间
    # 从而让某些槽位的时间关系变得难以解读。
    used: Dict[Tuple[int, int], Any] = {}
    for d in range(days):
        for s in range(n_pool):
            used[(d, s)] = model.NewBoolVar(f"used_{d}_{s}")
            model.Add(used[(d, s)] == sum(assign[(poi_id, d, s)] for poi_id in pool))
    for d in range(days):
        for s in range(1, n_pool):
            model.Add(used[(d, s)] <= used[(d, s - 1)])

    assumption_names: Dict[int, str] = {}
    assumption_literals: List[Any] = []

    def register_assumption(name: str) -> Any:
        """创建一个"假设字面量"，并用它来控制某条约束。

        两件事必须同时做，少一件诊断就废掉：
        1. 用 OnlyEnforceIf 把约束挂在这个字面量上（字面量为真时约束生效）；
        2. 调 AddAssumption 把它登记为**假设**。
        只做第 1 步时，字面量默认取假 = 约束不存在；而只要它被登记成假设，
        求解器又会强制它为真 —— 所以正常求解时必须在最后统一 Add(lit == 1)，
        让语义回到"所有约束都生效"。
        """
        literal = model.NewBoolVar(f"asm::{name}")
        if register_assumptions:
            # AddAssumption 会让求解器"假定该字面量为真"。诊断时必须关掉这一步，
            # 否则想把某条约束置零就会和"假定为真"直接矛盾，解一定不可行。
            model.AddAssumption(literal)
        assumption_names[literal.Index()] = name
        assumption_literals.append(literal)
        return literal

    # 每天的数量上限：约束**只**由假设字面量控制（不加"硬约束副本"）。
    # 为什么不用副本：副本会让"放开假设"失去意义 —— 放开后约束照样生效，
    # 模型永远解不开，诊断就没法自查。正确做法是既让字面量管约束，
    # 又在正常求解时把字面量强制为真（见下面的 force_assumptions）。
    for d in range(days):
        literal = register_assumption(f"每天 POI 数量上限(第 {d + 1} 天) <= {cap}")
        model.Add(sum(assign[(poi_id, d, s)] for poi_id in pool for s in range(n_pool)) <= cap).OnlyEnforceIf(
            literal
        )

    # 槽位 -> POI 索引的线性表达式（用槽位变量求和，得到"该槽位是谁"的 0/1 指示）
    index_of = {poi_id: idx for idx, poi_id in enumerate(pool)}
    slot_expr: Dict[Tuple[int, int], Any] = {}
    for d in range(days):
        for s in range(n_pool):
            slot_expr[(d, s)] = sum(
                index_of[poi_id] * assign[(poi_id, d, s)] for poi_id in pool
            )

    # AddElement 要求索引表达式落在表的定义域内。空槽位时 slot_expr 会是 0，
    # 组合索引为 0，在表内；但变量域本身必须先收窄，否则模型会被判为无效。
    for d in range(days):
        for s in range(n_pool):
            model.Add(slot_expr[(d, s)] <= n_pool - 1)
            if s > 0:
                model.Add(slot_expr[(d, s - 1)] * n_pool + slot_expr[(d, s)] <= n_pool * n_pool - 1)

    # 通勤：从"上一个槽位"到"当前槽位"的估算分钟数，用 AddElement 查表
    travel_table: List[int] = [0] * (n_pool * n_pool)
    for a_id in pool:
        for b_id in pool:
            travel_table[index_of[a_id] * n_pool + index_of[b_id]] = travel[(a_id, b_id)]

    travel_used: Dict[Tuple[int, int], Any] = {}
    for d in range(days):
        for s in range(n_pool):
            if s == 0:
                continue
            arrived_at = model.NewIntVar(0, horizon, f"arr_{d}_{s}")
            model.AddElement(
                slot_expr[(d, s - 1)] * n_pool + slot_expr[(d, s)], travel_table, arrived_at
            )
            # 关键：后一个槽位的开始时刻 >= 前一个槽位的开始 + 前一个槽位的**游览时长** + 通勤。
            # 漏掉"前一个槽位的游览时长"会让两次游览时间重叠（一个景点还没逛完就出发去下一个），
            # 独立打分器就是靠这条把这种错误抓出来的。
            prev_visit = sum(visits[p] * assign[(p, d, s - 1)] for p in pool)
            model.Add(cum[(d, s)] >= cum[(d, s - 1)] + prev_visit + arrived_at)
            travel_used[(d, s)] = arrived_at

    # 每个槽位里的 POI，其 [start, end] 必须落在日窗口内（硬约束）；
    # 同时算一个"当天的收工时刻"用于目标函数里的 makespan。
    # 空槽位的时间不前进（cum 单调 + 空位挤到末尾），所以空槽位的 end 等于前一天收工时刻，
    # 不会污染 makespan；而 AddMaxEquality 在整天没人时给出 day_start，也不影响目标。
    end_of_slot: Dict[Tuple[int, int], Any] = {}
    for d in range(days):
        for s in range(n_pool):
            end_time = model.NewIntVar(0, horizon, f"end_{d}_{s}")
            model.Add(
                end_time == cum[(d, s)] + sum(visits[p] * assign[(p, d, s)] for p in pool)
            ).OnlyEnforceIf(used[(d, s)])
            end_of_slot[(d, s)] = end_time

    # 每个 POI 的开放时间窗：一个字面量管该 POI 的全部槽位，且约束只由它控制。
    for poi_id in pool:
        window = windows[poi_id]
        if window is None:
            continue
        window_literal = register_assumption(
            f"{inst['names'][poi_id]} 的开放时间窗 {min_to_hhmm(window[0])}-{min_to_hhmm(window[1])}"
        )
        for d in range(days):
            for s in range(n_pool):
                hard_var = assign[(poi_id, d, s)]
                model.Add(cum[(d, s)] >= window[0]).OnlyEnforceIf([hard_var, window_literal])
                model.Add(
                    cum[(d, s)] + visits[poi_id] <= window[1]
                ).OnlyEnforceIf([hard_var, window_literal])

    # 日窗口收起时刻：同样只由假设字面量控制（每天一个字面量，避免字面量爆炸）
    day_last_end: Dict[int, Any] = {}
    for d in range(days):
        day_end_literal = register_assumption(f"第 {d + 1} 天结束不得晚于 {min_to_hhmm(day_end)}")
        for s in range(n_pool):
            model.Add(end_of_slot[(d, s)] <= day_end).OnlyEnforceIf([used[(d, s)], day_end_literal])
        last_end = model.NewIntVar(day_start, horizon, f"last_end_{d}")
        model.AddMaxEquality(last_end, [end_of_slot[(d, s)] for s in range(n_pool)])
        day_last_end[d] = last_end

    # 预算**不进硬约束**，只做"尽量少花"的目标偏好。
    # 理由：预算上限是按 known + estimated 估出来的，estimated 不是实测值。
    # 如果把它写成硬约束，会出现两种坏结果：
    #   a) 求解器直接把可选 POI 丢掉来"满足预算"，于是 estimated_exceeded 永远不出现；
    #   b) 把"估算超支"当成"不可行"，等于用估算数据否决一个真实可行的行程。
    # 所以这里只把花销放进目标函数（known 优先于 estimated），排期本身不受预算否决；
    # 预算是否超限由 classify_budget_from_costs 在结果里单独表达。
    # 价格先乘 100 转成整数（分），因为 CP-SAT 的线性表达式不接受浮点系数。
    price_scale = 100
    known_expr: List[Any] = []
    estimated_expr: List[Any] = []
    for poi_id in pool:
        value = price[poi_id]
        if value is None:
            continue
        term = int(round(float(value) * price_scale)) * sum(
            assign[(poi_id, d, s)] for d in range(days) for s in range(n_pool)
        )
        if pstatus[poi_id] == "known":
            known_expr.append(term)
        else:
            estimated_expr.append(term)

    known_spend = sum(known_expr) if known_expr else 0
    estimated_spend = sum(estimated_expr) if estimated_expr else 0

    # 只有"已知费用"超限才当硬约束拒绝（这是确定的超支，无论未知项如何都成立），
    # 口径与固定排期基线一致；estimated 超限不否决排期（见上面注释）。
    if limit is not None:
        literal = register_assumption(f"已知费用不超过预算上限 {limit}")
        model.Add(known_spend <= int(round(float(limit) * price_scale))).OnlyEnforceIf(literal)

    # 目标分五层，用系数做出**字典序**。这是本 spike 实测踩得最深的一个坑，
    # 记录在这里，接入主图时不要再踩：
    #   第一版把"收工早/少走路"的权重设得比"多排一处"大，结果求解器为了早点收工，
    #   把可选 POI 全部丢掉，甚至把两个 POI 拆到两天（各占一天 makespan 反而更小）。
    # 教训：这里最小化的是"收工时刻"这种**累积量**，"多塞一个"会让当天收工时刻
    # 单调变大，所以"多排一处"的权重必须直接压过一整天的收工时间变化。
    # 每层系数都要 > 下一层的最大可能影响：
    #   1) 排上的 POI 数（最高）：上界 n_pool*days；
    #   2) 已知花销（known）：已知钱是确定的，尽量别超；
    #   3) 估算花销（estimated）：单位系数最小，只做偏好；
    #   4) 总通勤时间；5) 收工最早（makespan）仅作最后平局打破。
    count_weight = (n_pool * days + 1) * (day_end + 1)
    known_spend_weight = 100
    estimated_spend_weight = 1
    travel_weight = n_pool * days + 1
    makespan_weight = 1
    objective: List[Any] = [
        count_weight * sum(assign[(poi_id, d, s)] for poi_id in pool for d in range(days) for s in range(n_pool))
    ]
    objective.append(-known_spend_weight * known_spend)
    objective.append(-estimated_spend_weight * estimated_spend)
    for d in range(days):
        for s in range(1, n_pool):
            objective.append(-travel_weight * travel_used[(d, s)])
    objective.append(-makespan_weight * sum(day_last_end[d] for d in range(days)))
    model.Maximize(sum(objective))

    # 注意：这里**不**把假设字面量固定为真。正常求解时由 solve_cpsat 统一 Add(lit==1)，
    # 诊断/自查时由 _solve_probe 按需决定每条约束要不要生效。
    # （早先把强制为真写在建模函数里，结果诊断再置零就自相矛盾，踩过这个坑。）
    return {
        "model": model,
        "assign": assign,
        "used": used,
        "cum": cum,
        "end_of_slot": end_of_slot,
        "travel_used": travel_used,
        "day_last_end": day_last_end,
        "assumption_literals": assumption_literals,
        "assumption_names": assumption_names,
        "literal_names": [assumption_names[lit.Index()] for lit in assumption_literals],
        "domain_proof": domain_proof,
        "travel_table": travel_table,
        "horizon": horizon,
    }


def _solve_probe(inst: Dict[str, Any], kept_names: Sequence[str], timeout_seconds: float) -> str:
    """诊断专用：只让 kept_names 里的约束生效（其余约束全部放开），返回求解状态名。

    实现方式：重新建一次模型，然后把每个假设字面量固定成
    "在 kept_names 里 = 1，否则 = 0"。这样：
    - 字面量为 1 -> OnlyEnforceIf 生效，约束在；
    - 字面量为 0 -> 约束失效，等价于"放开"。
    注意假设字面量只通过 OnlyEnforceIf 控制约束，没有第二份硬约束副本，
    所以这一步是真正的放开，不会出现"放开了约束还在"的假象。
    """
    kept = set(kept_names)
    # register_assumptions=False：诊断模型不登记假设，只靠"把字面量固定成 0/1"来开关约束。
    built = build_model(inst, register_assumptions=False)
    model = built["model"]
    for literal, name in zip(built["assumption_literals"], built["literal_names"]):
        model.Add(literal == (1 if name in kept else 0))
    probe = cp_model.CpSolver()
    probe.parameters.max_time_in_seconds = float(timeout_seconds)
    probe.parameters.num_search_workers = 8
    probe.parameters.random_seed = 42
    return probe.StatusName(probe.Solve(model))


def solve_cpsat(
    inst: Dict[str, Any],
    timeout_seconds: float,
    judge: Judge,
    diagnostic: bool = True,
) -> Dict[str, Any]:
    """构建模型、求解，并在不可行时给出"充分子集"诊断（自带可核查性检查）。

    注意：本函数只从 build_model 的返回值里取它真正要用的东西。
    travel_table / travel_used / day_last_end / windows / day_start / day_end 这些
    都**只在 build_model 内部**参与建模或目标函数（通勤表在 AddElement 里用，
    travel_used 与 day_last_end 在目标函数里用），在建模函数外面用不到，
    所以这里不再解包 —— 它们不是"该用没用"，只是不该在这里出现。
    """
    started = time.perf_counter()
    built = build_model(inst)
    model = built["model"]
    assign = built["assign"]
    used = built["used"]
    cum = built["cum"]
    end_of_slot = built["end_of_slot"]
    assumption_literals = built["assumption_literals"]
    assumption_names = built["assumption_names"]
    domain_proof = built["domain_proof"]
    # 正常求解：所有业务约束都必须生效，所以把全部假设字面量固定为真。
    for literal in assumption_literals:
        model.Add(literal == 1)

    pool = inst["pool_ids"]
    n_pool = len(pool)
    days = inst["days"]
    visits = inst["visits"]
    limit = inst["budget_limit"]
    price = inst["prices"]
    pstatus = inst["price_status"]

    # 模型自检：Validate() 不通过说明建模本身有错（而不是"无解"）。
    # 这里不抛异常，而是当作 UNKNOWN 处理并如实报出来 —— 绝不能把建模错误
    # 伪装成 INFEASIBLE。
    validation_error = None
    try:
        model.Validate()
    except Exception as exc:  # pragma: no cover - 只在建模出错时触发
        validation_error = f"{type(exc).__name__}: {exc}"

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(timeout_seconds)
    solver.parameters.num_search_workers = 8
    # 固定随机种子：CP-SAT 本身多线程仍可能有细微差异，但固定种子能把
    # "重复跑结果不一样"的情况压到最小，避免把噪声当成结论。
    solver.parameters.random_seed = 42
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)

    if os.environ.get("CPSAT_SPIKE_DEBUG") == "1":
        # 临时调试开关：把真实模型和关键变量值打到 stderr，便于对照独立打分器。
        # 默认关闭，不影响正常输出与 CSV。
        import sys as _sys

        _sys.stderr.write(f"[debug] task={inst['task_id']} status={solver.StatusName(status)}\n")
        for _d in range(days):
            _assign_str = ", ".join(
                f"{p}@{_s}" for p in pool for _s in range(n_pool) if solver.Value(assign[(p, _d, _s)]) == 1
            )
            _sys.stderr.write(f"[debug] day{_d} assign: {_assign_str}\n")
        for _d in range(days):
            for _s in range(n_pool):
                try:
                    _sys.stderr.write(
                        f"[debug] day{_d} slot{_s} used={solver.Value(used[(_d, _s)])} "
                        f"cum={solver.Value(cum[(_d, _s)])} end={solver.Value(end_of_slot[(_d, _s)])}\n"
                    )
                except Exception:
                    continue
    status_name = solver.StatusName(status)
    wall = float(solver.WallTime())

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        verdict = "FEASIBLE"
    elif status == cp_model.INFEASIBLE:
        verdict = "INFEASIBLE"
    else:
        # UNKNOWN / MODEL_INVALID：搜索没完成或模型无效 —— 绝不能当成无解
        verdict = "UNKNOWN"
    if validation_error:
        # 建模自检没过时，任何判定都不可信，一律降级为 UNKNOWN 并如实说明
        verdict = "UNKNOWN"
        status_name = f"MODEL_INVALID({validation_error})"

    # ---------------- 组装行程（仅 FEASIBLE 时） ----------------
    schedule = None
    unknown_bound = None
    if verdict == "FEASIBLE":
        day_payload: List[Dict[str, Any]] = []
        scheduled_ids: List[str] = []
        for d in range(days):
            items: List[Dict[str, Any]] = []
            for s in range(n_pool):
                for poi_id in pool:
                    if solver.Value(assign[(poi_id, d, s)]) == 1:
                        start = int(solver.Value(cum[(d, s)]))
                        end = start + visits[poi_id]
                        items.append(
                            {
                                "poi_id": poi_id,
                                "name": inst["names"][poi_id],
                                "slot": s,
                                "start": min_to_hhmm(start),
                                "end": min_to_hhmm(end),
                                "visit_minutes": visits[poi_id],
                                "ticket_price": price[poi_id],
                                "price_status": pstatus[poi_id],
                            }
                        )
                        scheduled_ids.append(poi_id)
            items.sort(key=lambda item: hhmm_to_min(item["start"]))
            day_payload.append({"day_index": d + 1, "items": items})

        known_total = sum(
            float(price[p]) for p in scheduled_ids if price[p] is not None and pstatus[p] == "known"
        )
        estimated_total = sum(
            float(price[p]) for p in scheduled_ids if price[p] is not None and pstatus[p] != "known"
        )
        unknown_scheduled = [p for p in scheduled_ids if price[p] is None]
        budget = classify_budget_from_costs(known_total, estimated_total, unknown_scheduled, limit)

        unknown_bound = _unknown_price_bound(inst)
        unknown_bound["known_estimated_total"] = known_total + estimated_total
        unknown_bound["upper_bound_total"] = (
            known_total + estimated_total + unknown_bound["per_unknown_upper_bound"] * len(unknown_scheduled)
        )
        unknown_bound["unknown_count"] = len(unknown_scheduled)
        unknown_bound["unknown_ids"] = unknown_scheduled
        unknown_bound["status"] = "estimated_upper_bound"

        schedule = {
            "days": day_payload,
            "known_total": known_total,
            "estimated_total": estimated_total,
            "budget_status": budget["status"],
            "budget_detail": budget,
            "unknown_price_bound": unknown_bound,
        }

    # ---------------- 不可行诊断（充分子集，不是最小冲突集） ----------------
    # 口径：报告的是**充分子集** —— 让这一组约束同时生效会无解；里面可能有冗余。
    # 两点可核查证据：
    #   1) 把全部软约束都放开（只留结构约束），看是否仍然无解 -> 判断是不是结构性无解；
    #   2) 只保留报告出来的这一组、放开其余，实测是否真的无解 -> 证明"充分"这两个字。
    assumption_subset: List[str] = []
    minimized_subset: List[str] = []
    hard_subset: List[str] = []
    sufficiency_verified: Optional[bool] = None
    relaxation_status = ""
    relaxation_note = ""
    if diagnostic and verdict == "INFEASIBLE":
        subset_indices = [int(i) for i in solver.SufficientAssumptionsForInfeasibility()]
        seen_names: set = set()
        for i in subset_indices:
            name = assumption_names.get(i, f"未登记字面量#{i}")
            if name in seen_names:
                continue
            seen_names.add(name)
            assumption_subset.append(name)

        # 第 1 步：全部软约束放开，只留结构约束
        structural_only = _solve_probe(inst, kept_names=[], timeout_seconds=timeout_seconds)
        structural_infeasible = structural_only not in ("OPTIMAL", "FEASIBLE")
        if structural_infeasible:
            hard_subset = [
                "结构约束本身即可证明无解（必去覆盖 / 每个 POI 只排一次 / 槽位互斥 / 通勤时间线）"
            ]

        # 第 2 步：只保留报告出来的这一组，实测是否无解
        if assumption_subset:
            kept = [name for name in assumption_subset]
            relaxation_status = _solve_probe(inst, kept_names=kept, timeout_seconds=timeout_seconds)
            # 只留这一组仍无解 => 这一组"足以"解释无解，充分性成立
            sufficiency_verified = relaxation_status not in ("OPTIMAL", "FEASIBLE")
            relaxation_note = "只保留这一组约束时实测无解，充分性成立（组内可能有冗余）"

            # 第 3 步：逐条试着去掉，把明显冗余的约束减掉，让报告能读
            if sufficiency_verified:
                reduced = list(assumption_subset)
                changed = True
                while changed and len(reduced) > 1:
                    changed = False
                    for name in list(reduced):
                        trial = [x for x in reduced if x != name]
                        if not trial:
                            continue
                        status_trial = _solve_probe(inst, kept_names=trial, timeout_seconds=timeout_seconds)
                        if status_trial not in ("OPTIMAL", "FEASIBLE"):
                            reduced = trial
                            changed = True
                minimized_subset = reduced
        else:
            # 求解器没给出可解释子集：逐条试探，找出单独就能造成无解的那些
            singles = []
            for name in assumption_names.values():
                probe_status = _solve_probe(inst, kept_names=[name], timeout_seconds=timeout_seconds)
                if probe_status not in ("OPTIMAL", "FEASIBLE"):
                    singles.append(name)
            if singles:
                assumption_subset = singles
                minimized_subset = singles
                sufficiency_verified = True
                relaxation_note = "求解器未给出解释子集，改为逐条试探：以上单条约束各自即可导致无解"
            elif not structural_infeasible:
                hard_subset = ["未能量化（求解器未返回可解释的假设子集，逐条试探也没找到单条即可致无解的约束）"]

    passed, checks, _ = judge.evaluate(schedule)
    elapsed = time.perf_counter() - started
    return {
        "arm": "cpsat",
        "verdict": verdict,
        "detail": (
            f"求解器状态 {status_name}"
            + ("（达到时限，搜索未完成）" if verdict == "UNKNOWN" else "")
        ),
        "schedule": schedule,
        "judge_passed": passed,
        "judge_checks": checks,
        "missing_must_visit": [] if schedule is None else [p for p in inst["must_visit"] if p not in {
            item["poi_id"] for day in schedule["days"] for item in day["items"]
        }],
        "skipped": [],
        "solve_seconds": elapsed,
        "solver_status": status_name,
        "optimization_status": "proven_optimal" if status == cp_model.OPTIMAL else ("feasible_not_proven" if status == cp_model.FEASIBLE else "not_applicable"),
        "optimization_proven": status == cp_model.OPTIMAL,
        "assumption_subset": assumption_subset,
        "minimized_subset": minimized_subset,
        "hard_assumption_subset": hard_subset,
        "sufficiency_verified": sufficiency_verified,
        "relaxation_status": relaxation_status,
        "relaxation_note": relaxation_note,
        "domain_infeasible_proof": domain_proof,
        "unknown_price_bound": unknown_bound,
        "solver_stats": {
            "wall_time": wall,
            "num_booleans": int(solver.NumBooleans()),
            "num_conflicts": int(solver.NumConflicts()),
            "num_branches": int(solver.NumBranches()),
        },
    }


# ==========================================================================
# 跑一个任务的两臂
# ==========================================================================
def run_task(
    inst: Dict[str, Any],
    timeout_seconds: float,
    reps: int,
    diagnostic: bool,
) -> List[Dict[str, Any]]:
    """同一任务：基线跑 reps 次，CP-SAT 跑 reps 次（除了 --reps，其余全同）。"""
    judge = Judge(inst)
    rows: List[Dict[str, Any]] = []
    for rep in range(1, reps + 1):
        base = baseline_greedy(inst, judge)
        base["rep"] = rep
        rows.append(base)
        # 每次都新建 Solve：不复用上一次的模型/求解器状态，避免缓存影响计时
        solve = solve_cpsat(inst, timeout_seconds, judge, diagnostic=diagnostic)
        solve["rep"] = rep
        rows.append(solve)
    return rows


# ==========================================================================
# 输出：CSV / summary.md / 终端
# ==========================================================================
CSV_COLUMNS = [
    "task_id",
    "arm",
    "rep",
    "verdict",
    "solver_status",
    "optimization_proven",
    "judge_passed",
    "judge_failed_checks",
    "solve_seconds",
    "scheduled_count",
    "must_visit_count",
    "missing_must_visit",
    "budget_status",
    "known_total",
    "estimated_total",
    "budget_limit",
    "unknown_price_bound",
    "assumption_subset",
    "minimized_subset",
    "sufficiency_verified",
    "relaxation_status",
    "hard_assumption_subset",
    "domain_infeasible_proof",
    "travel_time_status",
    "timeout_seconds",
    "guarantee_scope",
]


def flatten_rows(inst: Dict[str, Any], rows: List[Dict[str, Any]], timeout_seconds: float) -> List[Dict[str, Any]]:
    """把一次运行的结果压成 CSV 行（原始数字，不做美化）。"""
    flat: List[Dict[str, Any]] = []
    for row in rows:
        schedule = row.get("schedule")
        failed = [item["check"] for item in row.get("judge_checks", []) if item["result"] == "FAIL"]
        scheduled_count = (
            sum(len(day["items"]) for day in schedule["days"]) if schedule is not None else ""
        )
        bound = row.get("unknown_price_bound") or {}
        flat.append(
            {
                "task_id": inst["task_id"],
                "arm": row["arm"],
                "rep": row["rep"],
                "verdict": row["verdict"],
                "solver_status": row["solver_status"],
                "optimization_proven": row["optimization_proven"],
                "judge_passed": row["judge_passed"],
                "judge_failed_checks": " | ".join(failed),
                "solve_seconds": f"{row['solve_seconds']:.4f}",
                "scheduled_count": scheduled_count,
                "must_visit_count": len(inst["must_visit"]),
                "missing_must_visit": " | ".join(row["missing_must_visit"]),
                "budget_status": (schedule or {}).get("budget_status", ""),
                "known_total": (schedule or {}).get("known_total", ""),
                "estimated_total": (schedule or {}).get("estimated_total", ""),
                "budget_limit": "" if inst["budget_limit"] is None else inst["budget_limit"],
                "unknown_price_bound": "" if not bound else bound.get("upper_bound_total", ""),
                "assumption_subset": " | ".join(row["assumption_subset"]),
                "minimized_subset": " | ".join(row["minimized_subset"]),
                "sufficiency_verified": row["sufficiency_verified"],
                "relaxation_status": row["relaxation_status"],
                "hard_assumption_subset": " | ".join(row["hard_assumption_subset"]),
                "domain_infeasible_proof": row["domain_infeasible_proof"] or "",
                "travel_time_status": "estimated",
                "timeout_seconds": timeout_seconds,
                "guarantee_scope": GUARANTEE_SCOPE.format(speed=inst["travel_speed_kmh"]),
            }
        )
    return flat


def write_csv(path: str, flat_rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(flat_rows)


def build_summary_markdown(
    dataset: Dict[str, Any],
    insts: List[Dict[str, Any]],
    results: Dict[str, List[Dict[str, Any]]],
    timeout_seconds: float,
    reps: int,
) -> str:
    """生成可核查的 summary.md：原始数字为主，结论集中在最后。"""
    lines: List[str] = []
    lines.append("# CP-SAT 排程 spike 运行摘要")
    lines.append("")
    lines.append("本文件由 `cpsat_scheduling.py` 自动生成，内容全部是当次运行的原始输出。")
    lines.append("")
    lines.append(f"- 命令行参数：`--timeout {timeout_seconds} --reps {reps}`")
    lines.append(f"- ortools 版本：{_ortools_version()}")
    lines.append(f"- Python：{sys.version.split()[0]}")
    lines.append(f"- {GUARANTEE_SCOPE.format(speed=dataset.get('constraint_model', {}).get('travel_speed_kmh', '?'))}")
    lines.append("")

    # 状态汇总
    for arm in ("fixed_baseline", "cpsat"):
        counters: Dict[str, List[str]] = {}
        for inst in insts:
            verdict = results[inst["task_id"]][0 if arm == "fixed_baseline" else 1]["verdict"]
            counters.setdefault(verdict, []).append(inst["task_id"])
        arm_name = "固定排期基线" if arm == "fixed_baseline" else "CP-SAT"
        lines.append(f"## 臂 {arm_name} 的判定分布")
        lines.append("")
        lines.append("| 判定 | 任务数 | 任务 |")
        lines.append("|---|---|---|")
        for verdict in ("FEASIBLE", "INFEASIBLE", "UNKNOWN"):
            tasks = counters.get(verdict, [])
            lines.append(f"| {verdict} | {len(tasks)} | {', '.join(tasks) if tasks else '-'} |")
        lines.append("")

    # 逐任务明细
    lines.append("## 逐任务明细（取第 1 次重复）")
    lines.append("")
    lines.append("| 任务 | 基线判定 | 基线打分 | CP-SAT 判定 | 求解状态 | 打分 | 求解耗时(s) | 预算状态 | 与 expect 是否一致 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for inst in insts:
        base, solve = results[inst["task_id"]][0], results[inst["task_id"]][1]
        expect = inst["expect"].get("verdict", "-")
        agree = "一致" if solve["verdict"] == expect else f"不一致（expect {expect}）"
        schedule = solve.get("schedule") or {}
        lines.append(
            "| {task} | {bv} | {bp} | {sv} | {ss} | {sp} | {sec:.3f} | {bs} | {agree} |".format(
                task=inst["task_id"],
                bv=base["verdict"],
                bp="PASS" if base["judge_passed"] else "FAIL",
                sv=solve["verdict"],
                ss=solve["solver_status"],
                sp="PASS" if solve["judge_passed"] else "FAIL",
                sec=solve["solve_seconds"],
                bs=schedule.get("budget_status", "-"),
                agree=agree,
            )
        )
    lines.append("")

    # 不可行诊断
    lines.append("## 不可行诊断（充分子集，不是最小冲突集）")
    lines.append("")
    for inst in insts:
        solve = results[inst["task_id"]][1]
        if solve["verdict"] == "INFEASIBLE":
            subset = solve["assumption_subset"] or solve["hard_assumption_subset"]
            lines.append(f"- **{inst['task_id']}**：{'; '.join(subset) if subset else '未能量化'}")
            if solve["minimized_subset"] and solve["minimized_subset"] != solve["assumption_subset"]:
                lines.append(f"  - 精简后：{'; '.join(solve['minimized_subset'])}")
            if solve["sufficiency_verified"] is not None:
                mark = "充分性已实测证实" if solve["sufficiency_verified"] else "充分性未通过实测，结论不可信"
                lines.append(
                    f"  - 可核查性：只保留该组约束 -> {solve['relaxation_status']}（{mark}）"
                )
            if solve["domain_infeasible_proof"]:
                lines.append(f"  - 领域下界证明：{solve['domain_infeasible_proof']}")
    lines.append("")
    lines.append("> 口径说明：上面列出的是**充分子集** —— 让这些约束同时生效会导致无解，")
    lines.append("> 组内可能有冗余（`精简后` 一栏是逐条剔除得到的较小集合，仍不保证是全局最小）。")
    lines.append("> **不声称是最小冲突集/不可约不可行子集（IIS）**。")
    lines.append("")

    # 预算口径
    lines.append("## 预算口径")
    lines.append("")
    lines.append("| 任务 | known | estimated | 未知项上界估计 | 上限 | 预算状态 |")
    lines.append("|---|---|---|---|---|---|")
    for inst in insts:
        solve = results[inst["task_id"]][1]
        schedule = solve.get("schedule") or {}
        bound = solve.get("unknown_price_bound") or {}
        if not schedule:
            lines.append(f"| {inst['task_id']} | - | - | - | {inst['budget_limit']} | 无行程可判（{solve['verdict']}） |")
            continue
        lines.append(
            "| {task} | {k} | {e} | {u} | {l} | {s} |".format(
                task=inst["task_id"],
                k=schedule.get("known_total"),
                e=schedule.get("estimated_total"),
                u=bound.get("upper_bound_total", "-"),
                l=inst["budget_limit"],
                s=schedule.get("budget_status"),
            )
        )
    lines.append("")
    lines.append("## 不能下的结论")
    lines.append("")
    lines.append("- 本 spike 不调用任何模型，约束是结构化输入，因此**约束抽取正确性不在测试范围内**。")
    lines.append("- 与固定排期基线的差异只能说明：在这 10 个任务、这套估算数据下的差异；样本很小，不能外推。")
    lines.append("- `estimated_exceeded` 不能读成「实际不可行」；`unknown` 也不等于「超支」，只等于「无法核实」。")
    lines.append("- 通勤是 haversine 直线估算，不是真实路网时间。")
    lines.append("")
    return "\n".join(lines)


def _ortools_version() -> str:
    try:
        import ortools  # noqa: PLC0415

        return getattr(ortools, "__version__", "unknown")
    except Exception:
        return "unknown"


# ==========================================================================
# 入口
# ==========================================================================
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线 CP-SAT 排程 spike：固定排期基线 vs CP-SAT，共用一份结构化约束输入。"
    )
    parser.add_argument("--tasks", default=DEFAULT_TASKS_PATH, help="tasks.json 路径")
    parser.add_argument("--out-csv", default=DEFAULT_CSV_PATH, help="results.csv 输出路径")
    parser.add_argument("--out-summary", default=DEFAULT_SUMMARY_PATH, help="summary.md 输出路径")
    parser.add_argument("--timeout", type=float, default=5.0, help="单个 CP-SAT 求解的时限（秒）")
    parser.add_argument("--reps", type=int, default=3, help="每个任务重复跑几次（看稳定性）")
    parser.add_argument("--only", default=None, help="只跑指定 task_id（逗号分隔）")
    parser.add_argument("--no-diagnostic", action="store_true", help="跳过不可行诊断，只看判定")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_stdout()
    args = parse_args(argv)
    dataset = load_dataset(args.tasks)

    default_speed = float(dataset.get("constraint_model", {}).get("travel_speed_kmh", 18.0))
    pois = {item["poi_id"]: item for item in dataset["pois"]}
    selected = [task for task in dataset["tasks"] if not args.only or task["task_id"] in set(args.only.split(","))]
    if not selected:
        sys.stderr.write("[FATAL] 没有匹配的任务，检查 --only\n")
        return 2

    insts = [build_instance(task, pois, default_speed) for task in selected]

    print("=" * 78)
    print("离线 CP-SAT 排程 spike")
    print(f"ortools {_ortools_version()} / Python {sys.version.split()[0]}")
    print(f"任务数 {len(insts)} / 每任务重复 {args.reps} 次 / 单次求解时限 {args.timeout}s")
    print(GUARANTEE_SCOPE.format(speed=default_speed))
    print("=" * 78)

    results: Dict[str, List[Dict[str, Any]]] = {}
    for inst in insts:
        rows = run_task(inst, args.timeout, max(1, args.reps), diagnostic=not args.no_diagnostic)
        results[inst["task_id"]] = rows
        base, solve = rows[0], rows[1]
        print("")
        print(f"[{inst['task_id']}] {inst['description']}")
        print(
            "  固定排期基线 : {v:<10} 打分 {p:<4} 耗时 {s:.3f}s".format(
                v=base["verdict"], p="PASS" if base["judge_passed"] else "FAIL", s=base["solve_seconds"]
            )
        )
        print(
            "  CP-SAT      : {v:<10} 打分 {p:<4} 耗时 {s:.3f}s  ({d})".format(
                v=solve["verdict"],
                p="PASS" if solve["judge_passed"] else "FAIL",
                s=solve["solve_seconds"],
                d=solve["detail"],
            )
        )
        schedule = solve.get("schedule") or {}
        if schedule:
            print(
                "  预算        : status={s} known={k} estimated={e} limit={l}".format(
                    s=schedule.get("budget_status"),
                    k=schedule.get("known_total"),
                    e=schedule.get("estimated_total"),
                    l=inst["budget_limit"],
                )
            )
            bound = schedule.get("unknown_price_bound") or {}
            if bound.get("unknown_count"):
                print(
                    "  未知票价    : {n} 项未核实，保守上界估计总额 {u}（依据：{b}）".format(
                        n=bound["unknown_count"], u=bound["upper_bound_total"], b=bound["basis"]
                    )
                )
        if solve["verdict"] == "INFEASIBLE":
            subset = solve["assumption_subset"] or solve["hard_assumption_subset"]
            print(f"  不可行诊断  : {'; '.join(subset) if subset else '未能量化'}")
            if solve["minimized_subset"] and solve["minimized_subset"] != solve["assumption_subset"]:
                print(f"  精简后      : {'; '.join(solve['minimized_subset'])}")
            if solve["sufficiency_verified"] is not None:
                mark = "充分性已实测证实" if solve["sufficiency_verified"] else "充分性未通过实测，结论不可信"
                print(f"  诊断可核查  : 只保留该组约束 -> {solve['relaxation_status']}（{mark}）")
            if solve["domain_infeasible_proof"]:
                print(f"  领域下界    : {solve['domain_infeasible_proof']}")
        # 重复跑的一致性：必须**按臂分别**比较。
        # 两臂判定不同是设计的一部分（基线不是求解器），把它当成"重复不一致"是假警报；
        # 假警报比没有检查更糟 —— 真出问题时没人会信这个警告。
        unstable_arms: List[str] = []
        for arm_key, arm_label in (("fixed_baseline", "固定排期基线"), ("cpsat", "CP-SAT")):
            arm_verdicts = [row["verdict"] for row in rows if row["arm"] == arm_key]
            distinct = sorted(set(arm_verdicts))
            if len(distinct) == 1:
                print(f"  {arm_label} 重复 {args.reps} 次判定一致: {distinct[0]}")
            else:
                unstable_arms.append(f"{arm_label}={distinct}")
        if unstable_arms:
            print(f"  警告：同一臂内重复 {args.reps} 次判定不稳定 -> {'; '.join(unstable_arms)}")
        # 臂间差异单独说明（这是设计差异，不是稳定性问题）
        base_verdict = base["verdict"]
        solve_verdict = solve["verdict"]
        if base_verdict != solve_verdict:
            print(f"  臂间差异    : 固定排期基线={base_verdict} / CP-SAT={solve_verdict}（设计差异，非重复不稳定）")

    flat_rows: List[Dict[str, Any]] = []
    for inst in insts:
        flat_rows.extend(flatten_rows(inst, results[inst["task_id"]], args.timeout))
    write_csv(args.out_csv, flat_rows)
    write_utf8(
        args.out_summary,
        build_summary_markdown(dataset, insts, results, args.timeout, max(1, args.reps)),
    )

    print("")
    print("=" * 78)
    print(f"results.csv  -> {args.out_csv}  ({len(flat_rows)} 行)")
    print(f"summary.md   -> {args.out_summary}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
