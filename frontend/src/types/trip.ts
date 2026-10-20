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
 *
 * ★ v0.7（2026-10-11 同步）的两处变化 —— 都是「别把不知道说成不要钱」：
 *   1. 费用从「事后一个总价」变成「逐项三态」：新增 CostItem / CostStatus / CostSource，
 *      Budget 新增 items / known_total / estimated_total / unknown_items。
 *      不变式：状态为 unknown 的费用项 amount 必须是 null，且**不计入 total**（0 才是「真免费」）。
 *   2. 预算从「事后估算」变成「规划约束」：TripRequest 新增 budget_limit，
 *      Budget 新增 limit / limit_status / limit_basis；Attraction.ticket_price 改成可空 + 带三态。
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
 *   java           来自 Java 社区服务的数据（v0.6 新增）：热度反哺。
 *                  ★ 后端 schemas.py 的 DataSource 是 Literal，没登记的值会直接 500，
 *                    所以这里必须同步加上 —— 两份文件是同一份契约的两种语言表达。
 */
export type DataSource = 'amap' | 'llm_estimated' | 'degraded' | 'user' | 'java' | 'catalog' | 'catalog+amap' | 'catalog_partial' | 'flyai'

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

/**
 * 费用项的置信度（v0.7 新增）。三态是**每一项费用自己的属性**，不是整张卡一个标记：
 *   known      有确切来源的真实金额（高德门票、真免费、用户已定酒店）
 *   estimated  模型或固定规则估出来的金额（餐饮、交通、酒店价）
 *   unknown    金额未知 —— 界面上必须说「待查」，**绝不能显示成 0**
 *              （0 的含义是「真免费」，把未知写成 0 等于按零元帮用户通过了预算）
 */
export type CostStatus = 'known' | 'estimated' | 'unknown'

/**
 * 费用的来源。和 DataSource 分开是因为语义不同：
 *   static_estimate 是我们自己的固定表（比如交通费按每人每天 40 元），既不是高德也不是模型。
 */
export type CostSource = 'amap' | 'imported' | 'llm_estimated' | 'static_estimate' | 'venue_policy' | 'unknown'

/**
 * 预算结论（v0.7 新增）。只有五种值，各自的可信范围不同 —— 界面文案必须对应上：
 *   none                用户没设上限，不做预算约束
 *   within              在当前数据与估算口径内未超限（依据见 limit_basis）
 *   known_exceeded      **已知**费用就已超限 —— 硬违反，估算偏差解释不了
 *   estimated_exceeded  加入估算后超限 —— **不等于实际不可行**，不许写成「无解」
 *   unverified          有金额未知的项 —— **无法确认**，未知不能按 0 元算作已满足
 */
export type LimitStatus = 'none' | 'within' | 'known_exceeded' | 'estimated_exceeded' | 'unverified'

/** 请求：一次规划需要填的全部参数 */
export interface ModuleNeeds {
  instructions?: string
  unverified?: string[]
}
export interface PlaceNeeds extends ModuleNeeds {
  keywords?: string[]
  excluded_names?: string[]
}
export interface Requirements {
  food?: PlaceNeeds & { priority?: 'nearby' | 'food_first'; travel_scope?: 'nearby' | 'citywide'; max_cost_per_meal?: number | null }
  attractions?: PlaceNeeds & { max_per_day?: number; day_goals?: { theme: string; days: number; scope?: 'city' | 'surrounding'; day_indices?: number[] }[] }
  hotel?: PlaceNeeds & { max_price_per_night?: number | null; stay_policy?: 'auto' | 'prefer_same' | 'must_same'; luggage_policy?: 'carry' | 'storage_required' }
  transport?: ModuleNeeds & { max_leg_minutes?: number | null; allow_estimated_routes?: boolean }
  pace?: ModuleNeeds & { max_day_minutes?: number; max_activity_minutes?: number | null }
  weather?: ModuleNeeds & { avoid_rain_outdoors?: boolean }
  budget?: ModuleNeeds
}

export interface StayLock { check_in: string; hotel_name: string; address?: string; city?: string }
export interface OvernightStay { check_in: string; check_out: string; hotel: Hotel; locked: boolean; reason: string }
export interface RoutePoint { name: string; location?: Location | null; poi_id?: string | null }

export interface TripRequest {
  origin_city?: string
  intercity_mode?: 'none' | 'train' | 'flight' | 'auto'
  return_trip?: boolean
  adult_count?: number
  room_count?: number
  stay_locks?: StayLock[]
  arrival_address?: string | null
  departure_address?: string | null
  arrival_time?: string | null
  departure_time?: string | null
  requirements?: Requirements
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
  /**
   * 总预算上限（元，v0.7 新增）。不填（undefined / 空输入）= 不做预算约束。
   *
   * 它是**约束**不是「期望值」：后端算完预算会拿汇总结果与它比对，
   * 把结论写进 `Budget.limit_status` 并给出依据（`limit_basis`）。
   * 所以界面上填了它，就必须把「到底在不在预算内」显示出来。
   */
  budget_limit?: number
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
  retrieval_scope?: 'city' | 'surrounding'
  image_url?: string | null
  /**
   * 门票价（元）。★ v0.7 起**可空**，两种「没有数字」必须分开：
   *   null      未知 —— 高德 biz_ext.cost 实测恒为空，所以绝大多数景点门票是这一类
   *   0         真免费
   * 后端已保证：未知的价格不进 total，而是列进 `Budget.unknown_items`。
   */
  ticket_price?: number | null
  /** 门票价的置信度（v0.7）。status === 'unknown' 时 ticket_price 必为 null */
  ticket_price_status?: CostStatus
  ticket_price_source?: CostSource
  ticket_price_evidence_url?: string | null
  ticket_price_note?: string
  admission_policy?: 'unknown' | 'ordinary_access' | 'consumption'
  /** v0.2：是否来自用户的 must_visit（界面上给「必去」打标） */
  required?: boolean
}

/** 酒店（v0.2：行程级一家，不是每天一个） */
export interface Hotel {
  provider_id?: string | null
  provider_url?: string | null
  platform_quotes?: Record<string, unknown>[]
  price_note?: string
  poi_id?: string | null
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
  cost_status?: CostStatus
  cost_source?: CostSource
  price_evidence_url?: string | null
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
  estimated_cost?: number | null
  restaurant_id?: string | null
  cost_status?: CostStatus
  cost_source?: CostSource
  cost_note?: string
  source?: string
  ranking_name?: string | null
  ranking_rank?: number | null
  ranking_url?: string | null
  evidence_url?: string | null
}

export interface RouteLeg {
  from_name: string
  to_name: string
  from_id?: string | null
  to_id?: string | null
  mode: string
  duration_minutes: number
  distance_meters?: number | null
  status: CostStatus
  source: string
  note: string
}
export interface ScheduleItem {
  kind: 'meal' | 'attraction' | 'travel' | 'rest'
  meal_type?: 'breakfast' | 'lunch' | 'dinner' | null
  name: string
  start_time: string
  end_time: string
  duration_minutes: number
  reference_id?: string | null
  status: string
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
  dated_boundaries?: boolean
  meal_types?: Array<'breakfast' | 'lunch' | 'dinner'> | null
  route_start?: RoutePoint | null
  route_end?: RoutePoint | null
  stay_check_in?: string | null
  hotel_change?: boolean
  transfer_minutes?: number
  route_boundary_note?: string
  route_legs?: RouteLeg[]
  schedule?: ScheduleItem[]
  visit_minutes?: number
  travel_minutes?: number
  rest_minutes?: number
  total_minutes?: number
  comfort_warnings?: string[]
}

/**
 * 一条费用项（v0.7 新增）—— 三态落在**每一条**费用上，不是只给整张卡打一个标记。
 *
 * 为什么必须逐项：只知道「总价里混着未知」回答不了用户真正的问题
 * （「到底是哪一项不确定」），逐项记下来界面才能指着某个景点说「这张门票没查到」。
 *
 * ★ 不变式：`status === 'unknown'` 时 `amount` 必为 `null`。
 *   后端有断言守着这条；前端渲染时也要靠它决定显示「待查」还是显示金额。
 */
export interface CostItem {
  category: 'attraction' | 'meal' | 'hotel' | 'transportation'
  name: string
  /** 第几天。行程级开销（住宿 / 交通）为 null */
  day_index?: number | null
  /** 金额（元）。未知项为 **null**，不是 0 */
  amount: number | null
  status: CostStatus
  source: CostSource
  note?: string
}

/** 预算汇总 */
export interface Budget {
  ticket_reserve_amount?: number | null
  total_attractions?: number
  /** v0.2：= 每晚预估单价 × nights，不是按天求和 */
  total_hotels?: number
  total_meals?: number
  total_transportation?: number
  /**
   * ★ v0.7 起它的含义收窄了：只是「已知 + 估算」之和（保留是为了平滑迁移），
   * **不含金额未知的项**。够不够看 `limit_status`，不要只看这个数就下结论。
   */
  total?: number

  // ---- v0.7：逐项三态 + 分类汇总 ----
  /** 逐条费用项。旧响应（v0.7 之前缓存）没有这个字段 */
  items?: CostItem[]
  /** status === known 的金额合计：有确切来源、可以核对的部分 */
  known_total?: number
  /** status === estimated 的金额合计：估出来的部分，有误差 */
  estimated_total?: number
  /** 金额未知的费用项**名字**列表。★ 它们没有被算进 total */
  unknown_items?: string[]

  // ---- v0.7：用户上限与预算结论（由后端独立复核节点给出）----
  /** 用户给的上限；null / undefined = 没给，不做预算约束 */
  limit?: number | null
  limit_status?: LimitStatus
  /** 结论的依据（人话，写清了是按哪些数、哪个口径算的）。界面必须显示出来 */
  limit_basis?: string
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
export interface IntercityLeg {
  offer_id: string
  direction: 'outbound' | 'return'
  mode: 'train' | 'flight'
  origin: string
  destination: string
  departure_at: string
  arrival_at: string
  departure_station: string
  arrival_station: string
  transport_no: string
  seat_class: string
  duration_minutes: number
  amount_per_adult: number | null
  price_status: CostStatus
  url?: string | null
  observed_at: string
  expires_at: string
  exit_buffer_minutes: number
  boarding_buffer_minutes: number
  local_endpoint?: RoutePoint | null
  price_note: string
}

export interface TripPlan {
  origin_city?: string
  adult_count?: number
  room_count?: number
  intercity_legs?: IntercityLeg[]
  transport_warnings?: string[]
  city_characteristics?: string[]
  hotel_reason?: string
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
  accommodation_version?: number
  stays?: OvernightStay[]
  stay_comparison?: { strategy?: string; hotel_changes?: number; travel_minutes?: number; transfer_minutes?: number; basis?: string }
  regional_weather?: { poi_id?: string; name: string; date: string; status: string; note: string; day_weather?: string; district_forecast?: { day_weather?: string; day_temp?: string | number; night_temp?: string | number } }[]
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

// ================================================================ 社区（Java 侧，v0.6）

/**
 * 排行榜里的一条行程。
 *
 * 注意这是 **Java 侧的数据结构**（`LeaderboardService` 组装），不是 Python 的 TripPlan ——
 * 榜上只需要展示用的轻字段，完整行程要点进详情才拉。
 * 所以字段是驼峰命名：Java 的 Jackson 默认驼峰，前端照着接，不做转换。
 */
export interface LeaderboardItem {
  itineraryId: number
  title: string
  city: string
  travelDays: number
  /** Java 发布时回调 Python 生成的简介（可能为空串） */
  aiSummary: string
  likeCount: number
  /** 热度分 = 点赞数 / (发布至今小时数 + 2) ^ 1.5，只在热榜里有 */
  hotScore?: number
  /** 只在最新榜里有 */
  createdAt?: string
}

/** 点赞接口的返回。注意：它是 HTTP 200 + `success` 标记业务结果，不是靠状态码。 */
export interface LikeResult {
  success: boolean
  /** false = 你之前已经点过（幂等，不算错误） */
  liked: boolean
  likeCount: number
  /** RATE_LIMITED = 点太快了；NOT_FOUND = 行程不存在或已下架 */
  errorCode?: string
  message: string
}

/** 一条热度统计项 */
export interface HotStat {
  key: string
  score: number
}

/** 三类热度（来自 Java 的 tb_hot_stat，由已发布行程累积） */
export interface HotStats {
  city?: HotStat[]
  preference?: HotStat[]
  attraction?: HotStat[]
}

/** 社区热度接口的返回；`available=false` 时说明 Java 侧不可用（前端要如实提示，不要编数据） */
export interface CommunityHotResponse {
  enabled: boolean
  available: boolean
  stats: HotStats
  message: string
}

/** 发布行程的结果 */
export interface PublishResult {
  success: boolean
  itinerary_id: number | null
  message: string
}
