"""四个 Agent 的 system prompt 集中管理。

Day 3 你要写的内容：把下面四个常量填完整（草案见 docs/开发计划.md 第 3 章）。

    ATTRACTION_AGENT_PROMPT   景点搜集专家
    WEATHER_AGENT_PROMPT      天气查询专家
    HOTEL_AGENT_PROMPT        酒店推荐专家
    PLANNER_AGENT_PROMPT      行程编排专家

设计要点（与参考项目的关键区别）：
    1. 参考项目的 Prompt 里塞满了"[TOOL_CALL:amap_maps_text_search:keywords=..,city=..]"
       这种字符串约定，还反复叮嘱"格式必须完全正确,包括方括号和冒号"。
       本项目用 LangGraph / LangChain 原生 tool calling，参数由 JSON Schema 约束，
       **Prompt 里不需要也不应该出现工具调用格式说明**。
    2. 参考项目的 Prompt 里没写"不要编造"，结果降级时产出"杭州景点1"这种假名字。
       本项目的 Prompt 必须显式要求"取不到就如实说，不要编造"。
    3. 四个 Prompt 都要写清"输入是什么、输出是什么形状"，不要靠模型猜。
"""
