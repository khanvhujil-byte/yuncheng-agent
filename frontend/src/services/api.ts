/**
 * 后端接口封装。
 *
 * 全前端只有这个文件跟后端说话。组件不直接用 axios，
 * 一是为了错误码映射统一做，二是为了把「进度回调」这个形状固定下来。
 */

import axios, { AxiosError } from 'axios'

import type { ApiErrorBody, TripPlanResponse, TripRequest } from '@/types/trip'

/** 后端错误码 → 给人看的中文提示 */
const ERROR_MESSAGES: Record<string, string> = {
  CITY_NOT_FOUND: '没找到这个城市。填城市名即可，例如「杭州」。',
  INVALID_REQUEST: '填写的内容有问题，请检查表单里的必填项。',
  AMAP_KEY_INVALID:
    '高德 Key 无效。请检查后端 backend/.env 里的 AMAP_API_KEY，必须是「Web 服务」类型的 Key。',
  AMAP_QUOTA_EXCEEDED: '高德接口今日调用量已用完，请明天再试。',
  UPSTREAM_TIMEOUT: '外部服务响应超时，请重试。',
  LLM_ERROR: '模型服务异常，请重试。',
  // ---- v0.3 新增：模型服务自己的错误分类（services/llm.py）----
  LLM_AUTH_ERROR: '模型 Key 无效。请检查后端 backend/.env 里的 LLM_API_KEY。',
  LLM_QUOTA_EXCEEDED: '模型账户余额不足，请充值后重试。',
  LLM_RATE_LIMITED: '模型接口触发限流，请等几秒重试。',
  NETWORK_ERROR: '连不上后端。确认后端已启动（默认 http://127.0.0.1:8000）。',
  // ---- v0.2 新增：锚点与预检相关的错误码 ----
  DATE_RANGE_MISMATCH:
    '日期区间和天数对不上。例如选了 10 月 1 日到 5 日（5 天），天数却填 3 —— 请改到一致。',
  CONSTRAINT_INFEASIBLE:
    '这次的要求排不下。必去景点的数量超过了行程容量（每天最多 3 个景点），请减少必去景点或增加天数。',
  ATTRACTION_NOT_FOUND: '必去景点里有一个在城市内搜不到，请检查名字（或补上更完整的名称）。',
  ATTRACTION_AMBIGUOUS: '必去景点有多个同名结果（例如「西湖」和「西湖风景区」），请写得更具体。',
  HOTEL_NOT_FOUND: '你指定的酒店没搜到，建议补上地址或换一个名字。',
  HOTEL_AMBIGUOUS: '你指定的酒店重名了（例如「如家酒店」全国有很多家），请补上地址。',
  CITY_MISMATCH: '你指定的酒店和目的地城市相距太远，请确认酒店订在了这个城市。',
}

const FALLBACK_MESSAGE = '请求失败，请重试。'

/** 带错误码的异常，方便组件判断该不该显示「重试」按钮 */
export class ApiError extends Error {
  readonly code?: string

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000',
  timeout: 90_000,
  headers: { 'Content-Type': 'application/json' },
})

/**
 * 一个规划阶段的进度事件。
 *
 * V0 阶段只会收到「提交请求」和「生成完成」两条，因为后端是一次性返回的。
 * 到 V2 后端接上 SSE 之后，每完成一个节点就会推一条，
 * 组件消费事件的方式完全不用改——这是提前把接口形状定下来的意义。
 */
export interface PlanProgressEvent {
  /** 后端节点名，如 collect_attractions */
  node: string
  /** 给人看的中文名 */
  label: string
  status: 'running' | 'done' | 'degraded' | 'failed'
  elapsed_ms?: number
}

export interface PlanCallbacks {
  onProgress?: (event: PlanProgressEvent) => void
  onComplete?: (response: TripPlanResponse) => void
}

/**
 * 提交一次旅行规划请求。
 *
 * 用法（组件侧）：
 *   await planTrip(form, {
 *     onProgress: (e) => progressList.value.push(e),
 *     onComplete: (r) => { plan.value = r.data ?? null },
 *   })
 */
export async function planTrip(
  request: TripRequest,
  callbacks: PlanCallbacks = {},
): Promise<TripPlanResponse> {
  callbacks.onProgress?.({
    node: 'request',
    label: '提交请求',
    status: 'running',
  })

  try {
    const { data } = await http.post<TripPlanResponse>('/api/trip/plan', request)
    callbacks.onProgress?.({
      node: 'request',
      label: '生成完成',
      status: 'done',
    })
    callbacks.onComplete?.(data)
    return data
  } catch (error) {
    throw toApiError(error)
  }
}

/** 探测后端是否在线。用于页面加载时给出「后端没起」的明确提示。 */
export async function checkHealth(): Promise<boolean> {
  try {
    await http.get('/health', { timeout: 3_000 })
    return true
  } catch {
    return false
  }
}

function toApiError(error: unknown): ApiError {
  if (!axios.isAxiosError(error)) {
    return new ApiError(FALLBACK_MESSAGE)
  }

  const axiosError = error as AxiosError<ApiErrorBody>
  const errorCode = axiosError.response?.data?.error_code

  if (errorCode && ERROR_MESSAGES[errorCode]) {
    return new ApiError(ERROR_MESSAGES[errorCode], errorCode)
  }
  if (axiosError.code === 'ECONNABORTED') {
    return new ApiError(
      '请求超时（超过 90 秒）。后端可能还在跑，稍后可以到后端日志里看进度。',
      'UPSTREAM_TIMEOUT',
    )
  }
  if (!axiosError.response) {
    return new ApiError(ERROR_MESSAGES.NETWORK_ERROR, 'NETWORK_ERROR')
  }

  const backendMessage = axiosError.response.data?.message
  return new ApiError(backendMessage || FALLBACK_MESSAGE, errorCode)
}
