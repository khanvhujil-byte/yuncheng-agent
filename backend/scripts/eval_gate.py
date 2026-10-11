r"""评测集与回归门禁 —— 把"一次性实验"变成"可复跑的资产"。

和单元测试的分工（两者回答的问题完全不同）：
    单元测试（tests/）：**每次改动**有没有破坏已有行为。零 token，CI 每次提交都跑。
    本脚本（eval_gate）：**端到端质量**有没有退化。要花真实 token，按门禁模式选时机跑。

两种模式：
    --mode fast   只跑**预检类**用例（城市解析失败 / 容量超限 / 日期不一致 / 必去景点搜不到）。
                  这些用例**不调用大模型**，只花高德地理编码的钱，几百毫秒就出结果 ——
                  可以当作 PR 门禁，用来保证"该拦的还拦得住"。
    --mode full   预检类 + 生成类（要真跑完整图）。用于夜间/发版前，作为质量回归门禁。

为什么要有门禁而不是"跑完看一眼"：
    没有阈值和退出码，"评测"就只是个人肉仪式。这里对每个用例断言**可计算的性质**，
    不达标就 exit(1)，CI 才能真的挡住退化。

用法（在 backend 目录下）：
    .\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode fast
    .\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode full
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agents.graph import app as trip_graph  # noqa: E402
from app.api.routes.trip import _initial_state  # noqa: E402
from app.models.schemas import TripRequest  # noqa: E402


@dataclass
class Case:
    name: str
    kind: str  # preflight / generate
    request: dict
    expect: dict = field(default_factory=dict)

    def build(self) -> TripRequest:
        return TripRequest(**self.request)


def _offset(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def build_cases() -> list[Case]:
    """评测集：既有"必须被拦住"的负例，也有"必须跑出好行程"的正例。

    负例用**预检拦截**当判据（不花 LLM），正例用**可计算的性质**当判据
    （编造天数、约束满足、天数吻合），而不是靠人眼看行程好不好。
    """
    return [
        # ---------------- 预检类（不花 LLM）----------------
        Case(
            name="城市不存在应被拦",
            kind="preflight",
            request={"city": "XyzNotACity", "start_date": _offset(0), "travel_days": 3,
                     "transportation": "公共交通", "accommodation": "经济型酒店"},
            expect={"error_code": "CITY_NOT_FOUND", "adcode_is_none": True},
        ),
        Case(
            name="必去景点超过容量应被拦",
            kind="preflight",
            request={"city": "杭州", "start_date": _offset(0), "travel_days": 2,
                     "transportation": "公共交通", "accommodation": "经济型酒店",
                     "must_visit": [f"景点{i}" for i in range(12)]},
            expect={"error_code": "CONSTRAINT_INFEASIBLE"},
        ),
        Case(
            name="日期与天数不一致应被拦",
            kind="preflight",
            request={"city": "杭州", "start_date": _offset(0), "end_date": _offset(5),
                     "travel_days": 3, "transportation": "公共交通",
                     "accommodation": "经济型酒店"},
            expect={"error_code": "DATE_RANGE_MISMATCH"},
        ),
        Case(
            name="垃圾必去景点应被拦",
            kind="preflight",
            request={"city": "杭州", "start_date": _offset(0), "travel_days": 3,
                     "transportation": "公共交通", "accommodation": "经济型酒店",
                     "must_visit": ["这个景点绝对不存在xyz9"]},
            expect={"error_code": "ATTRACTION_NOT_FOUND"},
        ),
        # ---------------- 生成类（花真实 token）----------------
        Case(
            name="3 天行程应完整且满足硬约束",
            kind="generate",
            request={"city": "杭州", "start_date": _offset(0), "travel_days": 3,
                     "transportation": "公共交通", "accommodation": "经济型酒店",
                     "preferences": ["自然风光", "博物馆"], "must_visit": ["西湖"]},
            expect={"days": 3, "satisfied_all": True, "fabricated": 0,
                    "max_beyond_window": 0, "sources_ok": True},
        ),
        Case(
            name="6 天行程超窗口必须留空且不编造",
            kind="generate",
            request={"city": "杭州", "start_date": _offset(0), "travel_days": 6,
                     "transportation": "公共交通", "accommodation": "经济型酒店",
                     "preferences": ["自然风光"], "must_visit": ["西湖"]},
            expect={"days": 6, "satisfied_all": True, "fabricated": 0,
                    "min_beyond_window": 1, "sources_ok": True},
        ),
        Case(
            name="出发日期在预报窗口外也不能编造",
            kind="generate",
            request={"city": "杭州", "start_date": _offset(10), "travel_days": 3,
                     "transportation": "公共交通", "accommodation": "经济型酒店",
                     "preferences": ["自然风光"]},
            expect={"days": 3, "fabricated": 0, "beyond_window_all": True},
        ),
    ]


def _fabricated_weather(final: dict) -> int:
    """编造天数：标记为超出预报窗口、却仍然填了天气内容。**必须恒为 0**。"""
    return sum(
        1 for item in (final.get("weather") or [])
        if getattr(item, "beyond_window", False)
        and ((item.day_weather or "").strip() or (item.night_weather or "").strip())
    )


def run_case(case: Case) -> tuple[bool, list[str]]:
    """跑一个用例，返回 (是否通过, 失败原因列表)。"""
    request = case.build()
    final = trip_graph.invoke(_initial_state(request))
    reasons: list[str] = []
    expect = case.expect

    if case.kind == "preflight":
        actual = final.get("error_code")
        if expect.get("error_code") != actual:
            reasons.append(f"error_code 期望 {expect.get('error_code')}，实际 {actual}")
        if expect.get("adcode_is_none") and final.get("adcode") is not None:
            reasons.append("预检失败时不该写出 adcode")
        return (not reasons), reasons

    # 生成类：全部用**可计算的性质**判定，不靠人眼
    plan = final.get("plan") or {}
    days = plan.get("days") or []
    if not days:
        return False, [f"没有产出行程（error_code={final.get('error_code')}）"]

    if "days" in expect and len(days) != expect["days"]:
        reasons.append(f"天数期望 {expect['days']}，实际 {len(days)}")

    fabricated = _fabricated_weather(final)
    if "fabricated" in expect and fabricated != expect["fabricated"]:
        reasons.append(f"编造天气 {fabricated} 天（期望 {expect['fabricated']}）")

    beyond = sum(1 for item in (final.get("weather") or []) if getattr(item, "beyond_window", False))
    if "min_beyond_window" in expect and beyond < expect["min_beyond_window"]:
        reasons.append(f"超窗口天数 {beyond}，少于期望的 {expect['min_beyond_window']}")
    if "max_beyond_window" in expect and beyond > expect["max_beyond_window"]:
        reasons.append(f"超窗口天数 {beyond}，多于期望的 {expect['max_beyond_window']}")
    if expect.get("beyond_window_all") and (len(final.get("weather") or []) != beyond):
        reasons.append("出发日期全在窗口外时，天气应该全是占位项")

    report = final.get("constraint_report")
    if expect.get("satisfied_all"):
        if report is None:
            # ★ 原先 report 为 None 时直接跳过检查 —— 等于"没有报告就算通过"。
            #   评测自己的漏洞比被测代码的漏洞更危险：它会让失败看起来像成功。
            reasons.append("缺 constraint_report：无法证明硬约束被满足（不能算通过）")
        elif report.unsatisfied:
            reasons.append(f"硬约束未满足：{report.unsatisfied}")

    # ★ 最终校验结论必须为空。存在 validate_error 就说明"两层校验或独立复核"
    #   给出了未解决的失败原因 —— 这样的计划不能作为成功样例。
    #   （它曾经会被标成 success=True 并写进行程库，这是这套评测要守住的第一条不变量。）
    if expect.get("verified", True) and final.get("validate_error"):
        reasons.append(f"存在未解决的校验错误：{final.get('validate_error')}")

    if expect.get("sources_ok"):
        bad = [k for k, v in (final.get("sources") or {}).items() if v == "degraded"]
        if bad:
            reasons.append(f"出现降级：{bad}")

    return (not reasons), reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["fast", "full"], default="fast")
    args = parser.parse_args()

    cases = [c for c in build_cases() if args.mode == "full" or c.kind == "preflight"]
    print(f"评测模式：{args.mode}　用例数：{len(cases)}", flush=True)

    passed = 0
    failures: list[str] = []
    critical_failures: list[str] = []
    for case in cases:
        try:
            ok, reasons = run_case(case)
        except Exception as exc:  # noqa: BLE001 —— 用例异常算失败，不该中断整批
            ok, reasons = False, [f"异常 {type(exc).__name__}: {exc}"]
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case.name}" + ("" if ok else f"  <- {'; '.join(reasons)}"), flush=True)
        passed += int(ok)
        if not ok:
            failures.append(case.name)
            if case.critical:
                critical_failures.append(case.name)

    rate = passed / len(cases) if cases else 0
    print(f"\n通过 {passed}/{len(cases)}（{rate:.0%}）", flush=True)

    # ---- 关键用例：一票否决 ----
    # 为什么要在通过率之外单列：0.85 的阈值意味着"错 1 个也能过"，
    # 但编造天气、不合格计划被当成功这类是**安全性质**，不能靠平均分盖过去。
    if critical_failures:
        print("门禁不通过：关键用例失败 → " + "、".join(critical_failures), flush=True)
        return 1

    # ---- 门禁阈值：不达标就 exit(1)，CI 才能真正挡住退化 ----
    threshold = 1.0 if args.mode == "fast" else 0.85
    if rate < threshold:
        print(f"门禁不通过：通过率 {rate:.0%} 低于阈值 {threshold:.0%}", flush=True)
        print("失败用例：" + "、".join(failures), flush=True)
        return 1
    print(f"门禁通过（阈值 {threshold:.0%}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
