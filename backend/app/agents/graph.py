"""LangGraph 状态图装配（需求文档 5.3）。

Day 1 你要写：串行版骨架（六个节点依次串起来，先跑占位数据）。
Day 4 你要改：并行 + 降级边 + 重试环。

图的形状：

    START
      → validate_city
      → (条件边) 解析成功 → fan-out 三路并行
                 解析失败 → END（返回 CITY_NOT_FOUND）

    三路并行：
        collect_attractions
        collect_weather
        collect_hotels

    三路都汇入：
        compose_plan
      → (条件边) 校验通过      → patch_budget → END（success=true）
                 校验失败未超次 → 回 compose_plan 重试
                 校验失败已超次 → END（success=false + warnings）

要用到的 API：
    StateGraph(TripState)
    add_node("validate_city", validate_city)
    add_edge(START, "validate_city")
    add_conditional_edges("validate_city", route_after_city, {...})
    add_edge("collect_attractions", "compose_plan")   # 三路各连一条
    compile(checkpointer=...)                          # Day 7 才加

两个实现要点：
    1. 不需要写 join 节点。LangGraph 的 superstep 语义：
       从同一个节点分出的三条边会并行执行，
       下游节点会自动等三条边全部完成后才跑。
    2. 每条边都要有名字。尤其是从采集节点到 compose_plan 的降级边，
       起个能看懂的名字（如 "degraded"），否则图打印出来分不清哪条是哪条。

注意：graph 要在模块级编译一次（compile()），不要每次请求都重新编译。
"""
