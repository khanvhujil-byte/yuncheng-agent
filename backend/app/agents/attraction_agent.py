"""Agent 1 / 4 · 景点搜集专家（collect_attractions 节点）。

Day 3 你要写的函数：

    def collect_attractions(state: TripState) -> dict:
        '''
        输入（从 State 读）：
            request.city、request.preferences、request.travel_days

        输出（写回 State）：
            成功 → {"attractions": [Attraction, ...]}
            失败 → {"attractions": [], "errors": [...], "warnings": [...]}

        返回条数目标：>= travel_days * 3
        '''

绑定工具：
    search_poi（高德 POI 关键字搜索，见 app/services/amap.py）

必须做对的三件事：
    1. 每个偏好标签各查一次再合并。
       参考项目只取 preferences[0]（keywords = request.preferences[0]），
       用户选了「自然风光 + 美食」就只搜了自然风光。这是缺陷，不要照抄。
    2. 按 poi_id 去重，不要按 name。
       高德会返回「西湖」和「西湖风景区」这类近重复项。
    3. 过滤掉餐饮与住宿类的结果（按 typecode 判断）。
       用「美食」当关键词搜，会搜出一堆餐馆，这些不是景点。

降级要求：
    工具调用失败不要抛异常向上冒。写 errors + warnings，返回空列表，
    让 compose_plan 照常执行（需求文档 FR-03 异常分支）。
"""
