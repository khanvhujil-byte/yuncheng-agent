"""Agent 2 / 4 · 天气查询专家（collect_weather 节点）—— v0.3 薄壳，**带开关**。

两条路径，同一个工具（`get_weather`）：

    weather_use_agent = True  （显式对照实验）
        ReAct 循环：LLM 决定调工具、看结果、汇报 —— 多花 1~2 次 LLM 调用

    weather_use_agent = False （默认）
        直接调用工具 —— 0 token，瞬时错误由工具运行时限次重试

**这个开关本身就是实验变量**：同一批请求跑两遍，就能量化出
「把一个确定性节点 Agent 化，代价是多少 token / 多少秒」，
而且两条路径的产物完全一样（都来自同一个工具），对比是干净的。

本节点不做什么：
    不做窗口对齐、不做占位标记 —— 那些都在工具内部（规则写在工具里，不写在 prompt 里）。
"""

import logging

from ..config import get_settings
from ..models.state import TripState
from ..services.tools import ToolContext, execute
from .prompts import WEATHER_AGENT_PROMPT
from .react_loop import run_agent

logger = logging.getLogger(__name__)

MAX_STEPS = 3  # 天气任务很短：调一次 + 汇报


def _user_prompt(state: TripState) -> str:
    req = state["request"]
    from ..services.requirements import context
    return (
        f"城市：{req.city}\n"
        f"行程日期：{req.start_date} 起共 {state['travel_days']} 天\n"
        f"请查询天气并汇报覆盖情况。"
        + context(req, "weather", "pace")
    )


def weather_warnings(weather: list) -> list[str]:
    """只有"实测缺口"才写警告（预检阶段不写，避免两条重复的话）。

    公开出来是因为**缓存命中路径也要用它**（trip.py 复用行程时会按新日期重取天气，
    需要重新生成这条警告）——同一个口径只留一处实现。
    """
    missing = [item.date for item in weather if getattr(item, "beyond_window", False)]
    if not missing:
        return []
    if len(missing) == len(weather):
        return [
            f"高德天气预报只覆盖今天起 4 天，本次行程的 {len(weather)} 天全部在窗口外，"
            f"因此不提供天气数据（已留空，不编造）。"
        ]
    return [f"以下日期超出高德预报窗口（今天起 4 天），已留空而不是编造：{'、'.join(missing)}。"]


def collect_weather(state: TripState) -> dict:
    req = state["request"]
    ctx = ToolContext(
        city=req.city,
        adcode=state.get("adcode"),
        start_date=req.start_date,
        travel_days=state["travel_days"],
        nights=state["nights"],
    )

    use_agent = get_settings().weather_use_agent
    steps = 0
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}
    if use_agent:
        result = run_agent("weather", WEATHER_AGENT_PROMPT, _user_prompt(state), ctx, max_steps=MAX_STEPS)
        steps = result.steps
        ok = result.ok
        usage = result.usage
    else:
        # 历史失败保留在 errors；当前结果是否成功取决于最后一次尝试。
        outcome = execute("weather", "get_weather", {}, ctx)
        ok = outcome.status == "ok"

    weather = ctx.weather
    warnings = weather_warnings(weather)
    if use_agent and not ok:
        warnings.append("天气助手调用模型失败，本次行程不含天气信息。")
    if not weather and not warnings:
        last_weather = next((call["outcome"] for call in reversed(ctx.calls)
                             if call["tool"] == "get_weather"), None)
        if last_weather and last_weather["status"] == "error":
            warnings.append("天气数据未获取：" + last_weather["message"])
        else:
            warnings.append("天气数据未获取：缺少城市编码，或出行日期无法解析。")

    logger.info(
        "天气采集完成 | 模式=%s | 步数 %d | 工具调用 %d 次 | 天气 %d 天",
        "agent" if use_agent else "tool",
        steps,
        len(ctx.calls),
        len(weather),
    )
    return {
        "weather": weather,
        "warnings": warnings,
        "errors": list(ctx.errors),
        "sources": {"weather": "amap" if weather else "degraded"},
        "tool_calls": ctx.calls,
        "usage": usage,
    }
