/**
 * 与后端 backend/app/models/schemas.py 一一对应的类型定义。  —— v0.2（2026-10-08 同步）
 *
 * 改动本文件的任何字段，都必须同步改 schemas.py，反之亦然。
 * 这两份文件是同一份契约的两种语言表达。这里刻意保留 snake_case 不做驼峰转换，
 * 目的就是让两边的字段名逐字对应，对着看的时候不用做脑内翻译。
 *
 * v0.2 相对上一版的四处变化（对应 schemas.py 的同一批改动）：
 *   1. 天数 / 晚数 / 日期：end_date 变可选（后端会校验一致性），新增 nights
 *   2. 住宿是「行程级」：删掉 DayPlan.hotel，改成 TripPlan.hotel + hotel_nights
 *   3. 锚点：新增 must_visit（必去景点）、fixed_hotel（已定酒店）
 *   4. 约束对账：新增 ConstraintReport
 */

/**
 * 数据来源标记。
 *
 * 这是本项目相对参考项目新增的东西，用来区分「真数据」和「模型估算的数据」：
 *   amap           来自高德接口的真实数据
 *   llm_estimated  模型估算（高德不提供的价格类字段走这里）
 *   degraded       该路数据获取失败，已降级
 *   user           用户自己指定的锚点（v0.2 新增）—— 既不是高德实查也不是模型估算，
 *                  界面上要显示「你指定的」，不能混进「预估」里
 */
export type DataSource = 'amap' | 'llm_estimated' | 'degraded' | 'user'

/**
 * sources 字段的 key 约定（后端按这些 key 填，前端按这些 key 查）。
 *
 *   attractions              景点列表整体
 *   weather                  天气列表整体
 *   hotels                   酒店候选池
 *   hotel                    最终住宿（可能取 "user"，表示用户指定）
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
  /** 旅行天数，1~30。**权威字段**：容量、晚数、日期都由它派生 */
  travel_days: number
  /**
   * 结束日期 YYYY-MM-DD（v0.2 改为可选）。
   * 提供则后端会校验 `end_date === start_date + travel_days - 1`，
   * 不一致直接 422 DATE_RANGE_MISMATCH —— 这是为了避免
   * 「日期说 5 天、天数说 3 天」这种各节点各算各的 bug。
   */
  end_date?: string
  /**
   * 住几晚（v0.2 新增）。不填 = travel_days - 1。
   * 住几晚才决定住宿费：3 天行程通常住 2 晚（最后一晚返程），
   * 但「红眼航班到、红眼航班走」确实住 3 晚，所以允许覆盖。
   */
  nights?: number
  /** 交通方式：公共交通 / 自驾 / 步行 / 混合 */
  transportation: string
  /** 住宿偏好：经济型酒店 / 舒适型酒店 / 豪华酒店 / 民宿 */
  accommodation: string
  /** 旅行偏好标签（软约束，可以让位） */
  preferences: string[]
  /** 额外要求，自由文本 */
  free_text_input?: string
  /** 必须去的景点（硬约束）：要注入候选池，且必须真的排进行程 */
  must_visit?: string[]
  /**
   * 用户已订好的酒店名（硬约束）。
   * 后端必须结合 city 一起解析（「如家酒店」全国几千家），
   * 解析不到 → HOTEL_NOT_FOUND；重名 → HOTEL_AMBIGUOUS。
   */
  fixed_hotel?: string | null
  /** 可选，用于消除酒店重名歧义 */
  fixed_hotel_address?: string | null
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
  /** 建议游览时间（分钟）。v0.2 起它还是住宿选址的权重 */
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
  /** v0.2：是否来自用户的 must_visit（界面上给「必去」打标） */
  required?: boolean
}

/** 酒店（v0.2：行程级一家，不是每天一个） */
export interface Hotel {
  name: string
  address?: string
  location?: Location | null
  /** 价格区间。高德不提供房价，恒为模型估算 */
  price_range?: string
  rating?: string
  /** v0.2 语义：到行程中各景点的加权距离说明，不是「距市中心」的近似 */
  distance?: string
  type?: string
  /** 预估费用（元/晚），模型估算 */
  estimated_cost?: number
  /** v0.2：住几晚。住宿费 = estimated_cost × nights */
  nights?: number
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
  /** v0.2：true 表示这一天超出预报窗口（天数 > 4 时必然出现）。超窗口必须留空，不许编造 */
  beyond_window?: boolean
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
  // v0.2 删除了 hotel 字段：住宿是行程级的（见 TripPlan.hotel），不是天级的。
  attractions: Attraction[]
  meals: Meal[]
  /** 当日小计费用（元），由后端代码加总 */
  day_total_cost?: number
}

/** 预算汇总 */
export interface Budget {
  total_attractions?: number
  /** v0.2：= 每晚预估单价 × nights，不是按天求和 */
  total_hotels?: number
  total_meals?: number
  total_transportation?: number
  /** 总计。由后端代码计算，禁止模型生成 */
  total?: number
}

/** 硬约束对账报告（v0.2 新增） */
export interface ConstraintReport {
  /** 用户要求必去的景点 */
  required_attractions?: string[]
  /** 确实排进行程的 */
  satisfied?: string[]
  /** 没排进去的 */
  unsatisfied?: string[]
  /** key = 景点名，value = 原因（容量超限 / 距指定酒店过远 / 搜索不到 …） */
  reasons?: Record<string, string>
  /** 用户指定的酒店名；null 表示由系统选址 */
  hotel_anchor?: string | null
  /** user = 用户指定；agent = 系统推荐 */
  hotel_source?: 'user' | 'agent'
  /** 可行性预检结论。false = 在花钱调模型之前就被判定不可行 */
  feasible?: boolean
}

/** 最终的旅行计划 */
export interface TripPlan {
  city: string
  start_date: string
  /** v0.2：由后端派生 = start_date + travel_days - 1 */
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget | null
  /** 全程住宿（行程级一家）。v0.2 新增 */
  hotel?: Hotel | null
  /** 住几晚 */
  hotel_nights?: number
  /** V1 预留：跨区域行程分段住宿。V0 恒为空数组 */
  hotel_segments?: Hotel[]
  /** 硬约束对账报告 */
  constraint_report?: ConstraintReport | null
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
  /** v0.2：便于前端按错误码分支（CITY_NOT_FOUND / CONSTRAINT_INFEASIBLE …） */
  error_code?: string | null
  warnings?: string[]
  /**
   * 会话 ID。
   *
   * V0 阶段后端会返回它，但前端暂时用不上；到 V2 接入 LangGraph
   * Checkpointer 之后，同一次会话的后续请求都带上它，就能实现
   * 「在原有行程上改一天」而不是从零重算。
   */
  thread_id?: string
  /**
   * v0.4：本次响应是否来自**行程库**（缓存命中）。
   * 命中时不会消耗任何 LLM 调用（40 秒 → 1 秒内）；但天气仍会按新日期重取，
   * 因为日期与天气绑定，复用旧天气就是编造。
   */
  cached?: boolean
  /** 这条行程被复用了几次 —— 缓存价值的直接证据 */
  cache_hit_count?: number
  /** 这条行程最初生成的时间 */
  cache_saved_at?: string | null
}

/** 后端返回的错误体 */
export interface ApiErrorBody {
  success?: boolean
  message?: string
  error_code?: string
}
