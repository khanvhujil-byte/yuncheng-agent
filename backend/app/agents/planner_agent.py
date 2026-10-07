"""Agent 4 / 4 · 行程编排专家（compose_plan 节点）。

Day 4 你要写的函数：

    def compose_plan(state: TripState) -> dict:
        '''
        输入（从 State 读，全是结构化字段，不是上一段自然语言）：
            request、attractions[]、weather[]、hotels[]

        输出（写回 State）：
            成功     → {"plan": TripPlan, "retries": 0}
            校验失败 → {"retries": retries + 1}   # 回到自己重试
            超次     → {"plan": None, "warnings": [...], "errors": [...]}
        '''

绑定工具：
    无。这是纯生成节点。

这个节点是整个项目的技术难点，集中在三件事上：

    1. Prompt 由代码生成，不是字符串拼接自由文本（CC-3）。
       把三路数据格式化成紧凑的表格/清单。
       参考项目是把 attraction_response 整段塞给下一个 agent 的。

    2. 结构化输出 + Pydantic 校验 + 错误回喂重试。
       优先试 response_format={"type":"json_object"}。
       校验失败时，把「具体哪个字段错了」回喂给模型重试，最多 2 次
       （用 state["retries"] 计数）。

    3. 硬性约束要写进 Prompt（草案见 docs/开发计划.md 3.5）：
       - 每天 2~3 个景点，必须来自给定候选列表，不得编造
       - 每天 1 个酒店，必须来自给定候选列表，不得编造
       - 每天必须含早/中/晚三餐
       - 日期从 start_date 起连续，不得有缺口
       - 只输出 JSON，不要 markdown 代码块、不要解释文字

注意：budget 不在这里算，交给 budget.py（代码算，不让模型做加法）。
"""
