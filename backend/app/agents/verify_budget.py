"""预算独立复核（v0.7 新增）—— **放在预算汇总之后**，结论参与最终成功与缓存准入。

## 为什么必须是一个独立节点，而不是塞进 patch_budget

原先的费用检查写在 `verify_plan` 里（"单日费用占比 > 60%"），而图顺序是
    compose_plan → verify_plan → resolve_hotel → patch_budget
也就是说**检查发生时预算还没算出来**（`plan.budget` 是 None），那条检查从来没触发过。
它一直躺在代码里、看起来在工作、实际永远不触发 —— 这比没有检查更危险。

现在的顺序是
    ... → resolve_hotel → patch_budget（算钱）→ verify_budget（复核钱）
复核只做一件事：**把汇总结果与用户给的上限比对，给出四种结论之一**，并且说清依据。

## 四种结论（用户明确要求的表达方式）

| limit_status | 含义 | 能不能算"满足预算" |
|---|---|---|
| `none` | 用户没给上限 | 不做预算约束 |
| `within` | 在当前数据与估算口径内未超限 | 能（依据写在 limit_basis）|
| `known_exceeded` | **已知**费用就已超限 | 不能，这是硬违反 |
| `estimated_exceeded` | 加入**估算**后超限 | 不能，但**这不等于实际不可行** |
| `unverified` | 存在**影响判断的未知项** | 不能确认 —— **未知绝不按零元通过** |

## 保证范围（写进结论里，不许含糊）

"within" 只在**当前数据快照 + 当前估算口径**下成立：门票价我们没有真实来源，
酒店价与餐饮是估算，所以它证明的是"按这套口径算没超"，不是"实际不会超"。
"""

import logging

from ..models.state import TripState

logger = logging.getLogger(__name__)

# 单日费用占比的提示阈值。它**只是提示**，与用户总预算无关 ——
# 一天就花掉大头可能是合理的（比如那天有张贵票），所以不参与 limit_status 的判定。
SINGLE_DAY_SHARE_HINT = 0.6

# 预算超限最多触发几次**重排**。为什么值得重排：估算超限通常是"选了一批偏贵的景点"，
# 换更省的组合就能救回来。为什么必须封顶：不能因为预算卡住就让请求反复重排（成本 + 延迟）。
BUDGET_RETRY_LIMIT = 2


def verify_budget(state: TripState) -> dict:
    """复核预算汇总，产出 `budget_verdict`。"""
    plan = state.get("plan") or {}
    budget = plan.get("budget") or {}
    if not budget:
        # 行程为空时 patch_budget 已经把 budget 置 None，这里没什么可审的
        return {}

    request = state.get("request")
    # ★ 上限以**请求**为准而不是 budget 里存的：缓存复用时 budget 是历史快照，
    #   而这次请求的上限可能不同（指纹里含 budget_limit，所以正常不会，但别依赖它）。
    limit = getattr(request, "budget_limit", None)
    if limit is None:
        limit = budget.get("limit")

    known = int(budget.get("known_total") or 0)
    estimated = int(budget.get("estimated_total") or 0)
    unknown_items = list(budget.get("unknown_items") or [])
    subtotal = known + estimated

    warnings: list[str] = []

    # ---- 判定顺序：先报证据最强的那个结论 ----
    # ⚠️ 先处理"旧版行程"：v0.7 之前的缓存只有 total，没有逐项三态。
    #    那种数据**无法复核预算** —— 不能因为算出来 subtotal=0 就说"未超限"。
    legacy = "items" not in budget and "known_total" not in budget

    if limit is None:
        status = "none"
        basis = (
            f"未设置预算上限。当前口径：已知 {known} 元 + 估算 {estimated} 元 = {subtotal} 元"
            + (f"，另有 {len(unknown_items)} 项金额未知。" if unknown_items else "。")
        )
    elif legacy:
        status = "unverified"
        basis = (
            "这条行程是用 v0.7 之前的旧口径生成并缓存的（只有总价、没有逐项三态），"
            f"因此无法复核它是否满足上限 {limit} 元。"
        )
    elif known > limit:
        status = "known_exceeded"
        basis = (
            f"已知费用 {known} 元已超过上限 {limit} 元（还没算估算项）。"
            "这是硬违反：即使用户接受所有估算偏差，也超出上限。"
        )
    elif subtotal > limit:
        status = "estimated_exceeded"
        basis = (
            f"已知 {known} 元 + 估算 {estimated} 元 = {subtotal} 元，超过上限 {limit} 元"
            f"（超出 {subtotal - limit} 元）。"
            "注意：估算项本身有误差，所以这不等于实际旅行不可行，而是"
            "「按当前口径很可能超」—— 需要换更省的选择或放宽上限。"
        )
    elif unknown_items:
        status = "unverified"
        shown = "、".join(unknown_items[:3])
        more = f" 等 {len(unknown_items)} 项" if len(unknown_items) > 3 else ""
        basis = (
            f"已知 {known} 元 + 估算 {estimated} 元 = {subtotal} 元，在上限 {limit} 元以内；"
            f"但 {shown}{more} 的金额未知 —— 无法确认是否满足预算。"
            "未知项不能按 0 元计入后宣称满足。"
        )
    else:
        status = "within"
        basis = (
            f"已知 {known} 元 + 估算 {estimated} 元 = {subtotal} 元，在上限 {limit} 元以内"
            f"（余 {limit - subtotal} 元），且没有金额未知的项。"
            "范围：该结论只在当前数据快照与估算口径下成立。"
        )

    if status == "unverified":
        warnings.append(f"预算无法确认：{basis}")
    elif status == "estimated_exceeded":
        warnings.append(f"预算可能超限（按估算）：{basis}")
    elif status == "known_exceeded":
        warnings.append(f"预算已超限：{basis}")

    # 单日占比：纯粹的提示，和总预算无关（阈值见 SINGLE_DAY_SHARE_HINT）
    days = plan.get("days") or []
    if len(days) > 1 and subtotal > 0:
        worst = max(days, key=lambda day: int(day.get("day_total_cost") or 0))
        share = int(worst.get("day_total_cost") or 0) / subtotal
        if share > SINGLE_DAY_SHARE_HINT:
            warnings.append(
                f"{worst.get('date')} 一天占了总花费的 {share * 100:.0f}%（{worst.get('day_total_cost')} 元），"
                "看看是不是某项估算偏高。"
            )

    verdict = {
        "status": status,
        "limit": limit,
        "known_total": known,
        "estimated_total": estimated,
        "subtotal": subtotal,
        "unknown_items": unknown_items,
        "basis": basis,
    }
    updated_budget = {**budget, "limit": limit, "limit_status": status, "limit_basis": basis}

    logger.info(
        "预算复核 | status=%s | 已知 %d + 估算 %d = %d | 上限 %s | 未知 %d 项",
        status, known, estimated, subtotal, limit, len(unknown_items),
    )
    result: dict = {"plan": {**plan, "budget": updated_budget}, "budget_verdict": verdict}

    # ---- 重排之后预算已经通过：清掉**自己上一轮**留下的那条错误 ----
    # 不清的话，最终 success 会被一条"已经解决了的"错误判成部分结果 ——
    # 那等于自己给自己挖坑。只清自己写的（按前缀识别），不碰 verify_plan 留下的。
    if status in ("within", "none", "unverified") and state.get("validate_error"):
        if str(state["validate_error"]).startswith("预算复核不通过"):
            result["validate_error"] = None
            logger.info("预算已通过，清掉上一轮的重排提示")

    # ---- 超预算时给一次**有界**的重排机会 ----
    # 复用项目既有的重试信号 `validate_error`（compose_plan 会把这条错误回喂给自己重排），
    # 但计数用**独立的 budget_retries** —— 理由写在 state.py 那条注释里：
    # compose_plan 成功时 `retries` 不动，复用它会死循环。
    if status in ("known_exceeded", "estimated_exceeded"):
        used = int(state.get("budget_retries") or 0)
        if used < BUDGET_RETRY_LIMIT:
            result["budget_retries"] = used + 1
            result["validate_error"] = (
                f"预算复核不通过（{status}）：{basis} "
                f"请在保持必去景点与已定酒店的前提下，改选更省的景点组合。"
            )
            result["errors"] = [f"verify_budget: {basis}"]
            logger.info("预算超限，回炉重排 | 第 %d/%d 次", used + 1, BUDGET_RETRY_LIMIT)
        else:
            # 重排次数用尽：不再写 validate_error（否则会误触发重排），
            # 由 budget_verdict 参与最终判定 —— `_budget_reasons` 会把它算成"不算成功"。
            logger.warning("预算超限且重排次数已用尽 | status=%s", status)
    if warnings:
        result["warnings"] = warnings
    return result
