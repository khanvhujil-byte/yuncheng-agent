/**
 * 与后端 backend/app/models/schemas.py 一一对应的类型定义。
 *
 * 改动本文件的任何字段，都必须同步改 schemas.py，反之亦然。
 * 这两份文件是同一份契约的两种语言表达。这里刻意保留 snake_case 不做驼峰转换，
 * 目的就是让两边的字段名逐字对应，对着看的时候不用做脑内翻译。
 */

/**
 * 数据来源标记。
 *
 * 这是本项目相对参考项目新增的东西，用来区分「真数据」和「模型估算的数据」：
 *   amap           来自高德接口的真实数据
 *   llm_estimated  模型估算（高德不提供的价格类字段走这里）
 *   degraded       该路数据获取失败，已降级
 */
export type DataSource = 'amap' | 'llm_estimated' | 'degraded'

/**
 * sources 字段的 key 约定（后端按这些 key 填，前端按这些 key 查）。
 *
 *   attractions              景点列表整体
 *   weather                  天气列表整体
 *   hotels                   酒店列表整体
 *   attraction.ticket_price  门票价（高德 biz_ext.cost 覆盖不稳定，多数是估算）
 *   hotel.price_range        酒店价格区间（几乎必然是估算）
 *   hotel.estimated_cost     酒店每晚费用（几乎必然是估算）
 *   meal.estimated_cost      餐饮费用（必然是估算）
 *   plan                     整份行程本身（由编排 Agent 生成）
 */
export type SourceMap = Partial<Record<string, DataSource>>

/** 请求：一次规划需要填的全部参数 */
export interface TripRequest {
  /** 目的地城市，如「杭州」 */
  city: string
  /** 开始日期 YYYY-MM-DD */
  start_date: string
  /** 结束日期 YYYY-MM-DD */
  end_date: string
  /** 旅行天数，1~30 */
  travel_days: number
  /** 交通方式：公共交通 / 自驾 / 步行 / 混合 */
  transportation: string
  /** 住宿偏好：经济型酒店 / 舒适型酒店 / 豪华酒店 / 民宿 */
  accommodation: string
  /** 旅行偏好标签 */
  preferences: string[]
  /** 额外要求，自由文本 */
  free_text_input?: string
}

/** 经纬度 */
export interface Location {
  longitude: number
  latitude: number
}

/** 景点 */
export interface Attraction {
  name: string
  address: string
  location: Location
  /** 建议游览时间（分钟） */
  visit_duration: number
  description: string
  category?: string | null
  rating?: number | null
  /** 图片 URL 列表，来自高德 POI 自带的 photos */
  photos?: string[]
  poi_id?: string | null
  image_url?: string | null
  /** 门票价（元）。高德有就填真值，否则是模型估算 */
  ticket_price?: number
}

/** 酒店 */
export interface Hotel {
  name: string
  address?: string
  location?: Location | null
  /** 价格区间。高德不提供房价，恒为模型估算 */
  price_range?: string
  rating?: string
  /** 距市中心/景点距离的说明 */
  distance?: string
  type?: string
  /** 预估费用（元/晚），模型估算 */
  estimated_cost?: number
}

/** 一餐 */
export interface Meal {
  /** breakfast / lunch / dinner / snack */
  type: string
  name: string
  address?: string | null
  location?: Location | null
  description?: string | null
  estimated_cost?: number
}

/** 某一天的天气 */
export interface WeatherInfo {
  /** YYYY-MM-DD */
  date: string
  day_weather?: string
  night_weather?: string
  day_temp?: number | string
  night_temp?: number | string
  wind_direction?: string
  wind_power?: string
  /**
   * true = 预报，false = 实况。
   * 高德 extensions=all 只给今天起 4 天的预报，出行日期已过时才会返回实况。
   */
  is_forecast?: boolean
}

/** 一天的行程 */
export interface DayPlan {
  /** YYYY-MM-DD */
  date: string
  /** 第几天，从 0 开始 */
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel | null
  attractions: Attraction[]
  meals: Meal[]
  /** 当日小计费用（元），由后端代码加总 */
  day_total_cost?: number
}

/** 预算汇总 */
export interface Budget {
  total_attractions?: number
  total_hotels?: number
  total_meals?: number
  total_transportation?: number
  /** 总计。由后端代码计算，禁止模型生成 */
  total?: number
}

/** 最终的旅行计划 */
export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget | null
  /** 降级与估算的说明文字。有内容就必须在界面上显示出来 */
  warnings?: string[]
  /** 字段级的数据来源标记 */
  sources?: SourceMap
}

/** 后端统一响应包装。真实数据在 data 字段下，不在第一层。 */
export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan | null
  warnings?: string[]
  /**
   * 会话 ID。
   *
   * V0 阶段后端会返回它，但前端暂时用不上；到 V2 接入 LangGraph
   * Checkpointer 之后，同一次会话的后续请求都带上它，就能实现
   * 「在原有行程上改一天」而不是从零重算。
   */
  thread_id?: string
}

/** 后端返回的错误体 */
export interface ApiErrorBody {
  success?: boolean
  message?: string
  error_code?: string
}
