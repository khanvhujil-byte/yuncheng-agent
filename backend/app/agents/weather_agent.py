"""Agent 2 / 4 · 天气查询专家（collect_weather 节点）。

Day 3 你要写的函数：

    def collect_weather(state: TripState) -> dict:
        '''
        输入（从 State 读）：
            adcode（由 validate_city 产出，必须用这个，不能用城市名）
            request.start_date、request.travel_days

        输出（写回 State）：
            成功 → {"weather": [WeatherInfo, ...]}
            失败 → {"weather": [], "errors": [...], "warnings": [...]}
        '''

绑定工具：
    get_weather（高德天气查询，见 app/services/amap.py）

必须做对的两件事：
    1. city 参数传 adcode。高德天气接口不接受城市名，传「杭州」会返回空或报错
    2. extensions=all 只返回「今天起 4 天」的预报。行程 6 天时只能填前 4 天，
       第 5~6 天留空并写 warning，绝不编造

温度清洗：
    高德返回的 daytemp 可能带单位，WeatherInfo 的 validator 已经负责清洗，
    这里不用管

可选实验（V1 之后再做）：把它改成不用 LLM 的纯工具节点，对比耗时与 token。
它本质是「adcode + 日期 → 天气数组」的确定性搬运。
"""
def collect_weather(state) -> dict:
    """占位版（Day 3 换成真实天气接口）。"""
    return {"weather": [
        {"date": "2026-10-01", "day_weather": "晴", "night_weather": "多云",
         "day_temp": 25, "night_temp": 18, "wind_direction": "南风", "wind_power": "1-3级"},
    ]}