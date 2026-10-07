"""LLM 客户端构造。

Day 3 待实现：
    from langchain_openai import ChatOpenAI
    自己构造，显式传参：
        ChatOpenAI(
            model=settings.LLM_MODEL_ID,
            base_url=settings.LLM_BASE_URL,     # https://api.deepseek.com
            api_key=settings.LLM_API_KEY,
            timeout=60,
        )

为什么不用 hello-agents 的 HelloAgentsLLM（需求文档「坑 2」）：
    它的 _auto_detect_provider() 只要发现系统环境里 OPENAI_API_KEY 非空，
    就直接判定 provider="openai"，完全忽略 .env 里的 DeepSeek 配置，导致 401。
    本项目不复用该库，从根上绕开。

Day 4 待实现（结构化输出）：
    编排节点需要模型返回可被 Pydantic 校验的 TripPlan。
    优先尝试 response_format={"type": "json_object"}；
    若 DeepSeek 不支持 JSON Schema 严格模式（风险 R-6），
    就走「json_object + Pydantic 校验 + 把具体字段错误回喂重试（最多 2 次）」。
"""
