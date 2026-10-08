"""预算汇总节点 patch_budget（纯代码，不进 LLM）。

Day 4 你要写的函数：

    def patch_budget(state: TripState) -> dict:
        '''
        输入：state["plan"]（已通过 Pydantic 校验的 TripPlan）
        输出：{"plan": <填好 budget 与 day_total_cost 的 TripPlan>}

        计算：
            total_attractions    = 各天 Attraction.ticket_price 之和
            total_hotels         = 各天 Hotel.estimated_cost 之和
            total_meals          = 各天三餐 Meal.estimated_cost 之和
            total_transportation = 按交通方式与天数估算
            total                = 四项之和
            DayPlan.day_total_cost = 该天四项之和
        '''

为什么单独拆一个节点，而不是让 Agent 4 一起算（需求文档 FR-07）：
    加法是确定性的，模型做加法会出错，而且这种错误很难被发现——
    预算卡上写着「总计 2060」，你不会真的去逐项加一遍。
    凡是能确定性完成的计算，都不要交给模型。

边界处理：
    任一项缺失按 0 计入，同时在 warnings 里写「含缺失项，实际费用可能更高」。
"""
def patch_budget(state) -> dict:
    """占位版（Day 4 换成真实加总）。"""
    return {"plan": {**state["plan"], "budget": {"total": 0}}}