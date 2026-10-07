"""高德开放平台 Web 服务 REST 适配器。

需求文档 8.5。Day 3 待实现。

使用的接口（全部在 restapi.amap.com，公共信封 {status, info, infocode, count}）：
    GET /v3/geocode/geo           城市/地址 → location("lng,lat") + adcode
    GET /v3/place/text            POI 关键字搜索（offset / page 分页，
                                  extensions=all 才返回 biz_ext 与 photos）
    GET /v3/place/detail          POI 详情
    GET /v3/weather/weatherInfo   天气（city 必须是 adcode，extensions=all）
    GET /v3/distance              距离测量（origins → destination，type=1 驾车 / 0 直线）

本文件的职责（也就是 CC-2「工具结果截断与摘要」的落地点）：
    把高德的原始 JSON 立刻映射成 Pydantic 模型，原始响应不进入任何上下文。
    POI 每条含 photos 数组，20 条能到几十 KB，直接塞进 Prompt 就是自找麻烦。

三条硬约束（需求文档 8.5）：
    1. 天气接口的 city 参数必须是 adcode，不是城市名。
    2. extensions=all 只返回「今天起 4 天」的预报，第 5 天起必须留空。
    3. extensions=base 返回的是实况 lives[]，不是预报 → is_forecast=False。

关于价格（需求文档 FR-05 / FR-07）：
    POI 的 biz_ext.cost 覆盖不稳定，住宿价格高德基本不提供。
    缺失时一律由模型估算，并标 source="llm_estimated"。

错误映射（需求文档 8.4）：
    infocode == "10000"          成功
    INVALID_USER_KEY        →  AMAP_KEY_INVALID
    日调用量超限            →  AMAP_QUOTA_EXCEEDED
    网络超时                →  UPSTREAM_TIMEOUT

Day 6 可选优化：给「城市→adcode」「城市+关键词→POI」加内存缓存（TTL 1 小时）。
"""
