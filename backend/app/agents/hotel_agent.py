"""Agent 3 / 4 · 酒店推荐专家（collect_hotels 节点）。

Day 3 你要写的函数：

    def collect_hotels(state: TripState) -> dict:
        '''
        输入（从 State 读）：
            request.city、request.accommodation、request.preferences

        输出（写回 State）：
            成功 → {"hotels": [Hotel, ...]}
            失败 → {"hotels": [], "errors": [...], "warnings": [...]}

        返回条数目标：>= 3
        '''

绑定工具：
    search_poi（高德 POI 关键字搜索）
    measure_distance（高德距离测量）

必须做对的三件事：
    1. 关键词要从 accommodation 映射，不能直接把「经济型酒店」丢给高德：
           经济型酒店 → 快捷酒店
           舒适型酒店 → 精品酒店 / 商务酒店
           豪华酒店   → 五星级酒店
           民宿       → 民宿
    2. 只保留 typecode 属于住宿服务类的结果，过滤掉景点与餐饮。
    3. price_range 与 estimated_cost 是模型估算，不是真实房价。
       高德 POI 不提供房价。这两个字段最终会被标成 source="llm_estimated"，
       前端会显示「预估」字样。不要在 Prompt 里让模型声称「查到了价格」。

distance 字段：
    用 measure_distance 真实计算，不要让模型猜。
    因为本节点与 collect_attractions 是并行的，拿不到当天景点坐标，
    所以先用 city_center 近似（需求文档 5.5 已接受的取舍）。
"""
