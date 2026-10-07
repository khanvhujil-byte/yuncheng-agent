"""城市解析节点 validate_city（不进 LLM）。

Day 3 你要写的函数：

    def validate_city(state: TripState) -> dict:
        '''
        把 state["request"].city 解析成坐标与 adcode。

        返回（增量，不是完整 State；LangGraph 会合并回 State）：
            成功 → {"city_center": Location, "adcode": "330100"}
            失败 → {"errors": ["CITY_NOT_FOUND: 无法解析城市 <city>"], "city_center": None}

        实现：
            调 app.services.amap.geocode(address=city)
            取 geocodes[0].location（"lng,lat" 字符串，要切开转 float）
            取 geocodes[0].adcode
        '''

为什么这个节点单独存在（参考项目没有）：
    1. 高德天气接口的 city 参数必须是 adcode，不是城市名。
       所以「拿 adcode」是天气节点的前置依赖，必须有个地方先做掉。
    2. 城市解析失败应该尽早返回 400，而不是走完三路再失败。
"""
