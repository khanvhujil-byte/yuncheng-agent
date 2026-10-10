/**
 * 后端接口封装。
 *
 * 全前端只有这个文件跟后端说话。组件不直接用 axios，
 * 一是为了错误码映射统一做，二是为了把「进度回调」这个形状固定下来。
 */

import axios, { AxiosError } from 'axios'

import type { ApiErrorBody, TripPlanResponse, TripRequest } from '@/types/trip'

/**
 * 解析后的后端地址。
 *
 * 为什么单独抽一个常量：axios 有自己的 baseURL，而流式那条路走的是原生 fetch，
 * fetch 不认 axios 的 baseURL。两边各写一份的话，哪天换了端口就会出现
 * 「同步能用、流式连不上」这种最难查的偏差。所以地址只解析一次，两边共用。
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

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
  baseURL: API_BASE_URL,
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

/* ==================================================================
 * v0.4 流式进度 / 兜底轮询 / 断点续跑
 *
 * 这一组接口的形状按「后端契约」写死了，改动必须两边一起改：
 *   POST /api/trip/plan/async            -> 只回 thread_id，立刻返回
 *   GET  /api/trip/plan/stream/{id}      -> SSE，每个节点完成推一条
 *   GET  /api/trip/plan/result/{id}      -> 兜底轮询（还在跑时 success=false）
 *   GET  /api/trip/journal/{id}          -> 已完成的节点 + 能从哪继续
 *   POST /api/trip/plan/resume/{id}      -> 从断点继续，返回完整结果
 *
 * 注意这里刻意**没有**改 types/trip.ts：那三个缓存字段已经在 TripPlanResponse 里了，
 * 剩下这几个形状只服务于本文件的五个函数，放局部定义，
 * 免得把「前后端逐字对应」的契约文件掺进前端自己的中间态。
 * ================================================================== */

/** 后端固定的节点执行顺序。流式事件不带 running，前端只能靠它推断「谁在跑」 */
export const PIPELINE_NODES = [
  'validate_city',
  'collect_attractions',
  'collect_weather',
  'compose_plan',
  'resolve_hotel',
  'patch_budget',
] as const

/** 节点中文名。只在后端没给 label 时兜底用，正常情况下直接用后端的 label */
const NODE_LABELS: Record<string, string> = {
  validate_city: '解析城市',
  collect_attractions: '搜集景点',
  collect_weather: '查询天气',
  compose_plan: '编排行程',
  resolve_hotel: '选择住宿',
  patch_budget: '计算预算',
}

/** 一个节点完成事件（后端 label 直接透传，前端不做映射） */
export interface PlanNodeEvent {
  node: string
  label: string
  status: string
  elapsed_ms?: number
}

/** 断点续跑日志 */
export interface PlanJournal {
  thread_id: string
  completed_nodes: string[]
  can_resume_from: string | null
}

/** 流式回调。三种事件各自一个入口，组件不需要自己判断 type */
export interface StreamHandlers {
  onNode?: (event: PlanNodeEvent) => void
  /** 只在收到 done 时调用一次；流中途断掉会走 onError，不会走这里 */
  onDone?: (response: TripPlanResponse) => void
  onError?: (message: string) => void
}

/** 默认轮询间隔。太密会给已经满载的后端再压一层负担，2 秒足够 */
export const POLL_INTERVAL_MS = 2_000

/** 给「把裸 TripPlan 当作响应体」的接口补外层包装，顺便继承行程自带的缓存元信息 */
function wrapPlan(plan: TripPlanResponse['data']): TripPlanResponse {
  if (!plan) {
    return { success: false, message: '后端返回了空结果。' }
  }
  // 契约里 cached / cache_hit_count / cache_saved_at 正式挂在外层响应上，
  // 但 /result 与 /resume 直接返回裸 TripPlan（外层包装是我们补的），
  // 所以这里做一次兼容读取：行程对象上带就带出来，不带就是 undefined。
  // types/trip.ts 是冻结的契约文件，不允许为这个兼容改动它，故就地断言。
  const carried = plan as TripPlanResponse['data'] & {
    cached?: boolean
    cache_hit_count?: number
    cache_saved_at?: string | null
  }
  return {
    success: true,
    message: '',
    data: plan,
    cached: carried.cached ?? false,
    cache_hit_count: carried.cache_hit_count ?? 0,
    cache_saved_at: carried.cache_saved_at ?? null,
  }
}

/** 给原生 fetch 补一个超时。fetch 自己不带超时，卡住的请求会一直占着兜底链 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * 异步提交：让后端在后台开工，立刻拿到 thread_id。
 *
 * 为什么不用原来的同步 /plan：同步请求把「生成」和「等待」粘在一条 HTTP 连接上，
 * 41 秒里前端拿不到任何中间状态，用户只能盯着转圈。
 * 这里先拿 id，再用 SSE 订阅进度，等待过程才有内容可看。
 *
 * 任何非 200 / 缺 thread_id 都抛 ApiError，让调用方**静默降级**回同步接口
 * （后端还没上线这条路时，老路径必须照常可用）。
 */
export async function startPlan(request: TripRequest): Promise<{ thread_id: string }> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/trip/plan/async`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    })
  } catch {
    // 连不上后端：归到「异步不可用」，由调用方决定降级还是报错，
    // 这里不直接抛 NETWORK_ERROR，否则会绕过降级路径
    throw new ApiError('异步接口连不上。', 'ASYNC_UNAVAILABLE')
  }

  if (!response.ok) {
    throw new ApiError(`异步接口返回 ${response.status}。`, 'ASYNC_UNAVAILABLE')
  }

  const body = (await response.json()) as {
    success?: boolean
    thread_id?: string
    message?: string
  }

  if (!body?.thread_id) {
    throw new ApiError(body?.message || '后端没有返回 thread_id。', 'ASYNC_UNAVAILABLE')
  }

  return { thread_id: body.thread_id }
}

/**
 * 订阅进度（SSE）。
 *
 * 为什么不用浏览器的 EventSource：
 *   1. EventSource 只支持 GET，没法带请求体和自定义头（后端将来加鉴权就死）；
 *   2. 它自带重连，断了会无限重试，而我们要的是「断了就让调用方去兜底轮询」；
 *   3. 断流只能靠 eventSource.close()，拿不到「读完了」这个时机。
 * 所以这里用 fetch + ReadableStream 自己按 SSE 协议分帧。
 *
 * SSE 的解析说白了就两条规则：
 *   - 一帧以空行（\n\n）结束；
 *   - 帧里以 `data: ` 开头的行才是载荷，多行 data 用 \n 拼起来。
 * 服务端可能把一帧切成好几次 chunk 发过来，所以必须留 leftover 缓冲，
 * 不能假设「一次 read = 一帧」。
 *
 * 返回值是 cancel 函数：组件卸载时必须调它，否则连接会挂到浏览器关掉为止。
 */
export function streamPlan(thread_id: string, handlers: StreamHandlers): () => void {
  const controller = new AbortController()
  let cancelled = false
  let finished = false

  function finishError(message: string): void {
    if (finished) return
    finished = true
    handlers.onError?.(message)
  }

  void (async () => {
    let response: Response
    try {
      response = await fetch(`${API_BASE_URL}/api/trip/plan/stream/${thread_id}`, {
        method: 'GET',
        headers: { Accept: 'text/event-stream' },
        signal: controller.signal,
      })
    } catch (error) {
      if (cancelled || controller.signal.aborted) return
      finishError(error instanceof Error ? error.message : '流式连接失败。')
      return
    }

    if (!response.ok) {
      finishError(`流式接口返回 ${response.status}。`)
      return
    }
    if (!response.body) {
      // 极老的浏览器没有 body 流；这种情况交给调用方降级
      finishError('当前浏览器不支持流式读取响应。')
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    // leftover：上一轮 read 结尾那段「还不完整的帧」，必须留到下一轮补全
    let leftover = ''

    /** 处理一帧。返回 true = 这一帧宣告结束（done / error），后面不用再读了 */
    function processFrame(frame: string): boolean {
      // 一帧里可能有多行 data（SSE 允许多行拼接），这里全部拼起来当一份 JSON
      const dataLines = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
      if (!dataLines.length) return false

      const raw = dataLines.join('\n')
      if (!raw) return false

      let payload: {
        type?: string
        node?: string
        label?: string
        status?: string
        elapsed_ms?: number
        message?: string
        result?: TripPlanResponse
      }
      try {
        payload = JSON.parse(raw)
      } catch {
        // 单帧 JSON 坏了不该拖垮整条流：跳过它，等后面的 done 事件兜住结果
        return false
      }

      if (payload.type === 'node') {
        if (!payload.node) return false
        handlers.onNode?.({
          node: payload.node,
          // 契约说 label 用后端给的，只有后端漏了才回退到本地表
          label: payload.label || NODE_LABELS[payload.node] || payload.node,
          status: payload.status || 'done',
          elapsed_ms: payload.elapsed_ms,
        })
        return false
      }

      if (payload.type === 'done') {
        finished = true
        // done 事件自带完整结果，省掉一次兜底轮询
        handlers.onDone?.(payload.result ? payload.result : wrapPlan(null))
        return true
      }

      if (payload.type === 'error') {
        finished = true
        handlers.onError?.(payload.message || '生成过程中出错。')
        return true
      }

      return false
    }

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        leftover += decoder.decode(value, { stream: true })

        // 统一换行：后端按 \n\n 分帧，但代理/网关可能改写成 \r\n\r\n
        leftover = leftover.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

        let splitAt = leftover.indexOf('\n\n')
        while (splitAt !== -1) {
          const frame = leftover.slice(0, splitAt)
          leftover = leftover.slice(splitAt + 2)
          if (processFrame(frame)) {
            await reader.cancel().catch(() => undefined)
            return
          }
          splitAt = leftover.indexOf('\n\n')
        }
      }

      // 流正常结束（服务端关了连接）时，把最后一帧补处理掉：
      // 有的实现最后一条事件不带结尾空行
      const tail = leftover.replace(/\s+$/, '')
      if (tail && processFrame(tail)) return

      // 没等到 done 就断了：不算成功。由调用方转向兜底轮询，
      // 因为结果可能其实已经生成好、只是推送通道断了
      finishError('进度通道提前关闭，未收到完成事件。')
    } catch (error) {
      if (cancelled || controller.signal.aborted) return
      finishError(error instanceof Error ? error.message : '读取进度时出错。')
    } finally {
      reader.releaseLock()
    }
  })()

  return () => {
    cancelled = true
    controller.abort()
  }
}

/**
 * 兜底轮询：直接问「结果好了没」。
 *
 * 为什么必须有它：SSE 通道可能被代理掐断、可能在后端重启时断掉，
 * 但生成任务还在跑。这时候再发一次同步请求等于让后端白算一遍，
 * 所以宁可轮询这条便宜的路。
 *
 * 用原生 fetch 而不是 axios：这里需要「先声明 Accept 再自己算超时」的写法，
 * 而且轮询是高频调用，少一层 axios 适配更可控。
 *
 * 返回 null 表示「还在生成中」（契约里的 {"success": false, "message": "仍在生成中"}）。
 *
 * timeoutMs 由调用方按「整条兜底链还能花多久」预算着给：
 * 单个请求卡住时它必须能把这一次砍掉，否则一次卡死就吃掉整个等待窗口。
 */
export async function getPlanResult(
  thread_id: string,
  timeoutMs = 15_000,
): Promise<TripPlanResponse | null> {
  let response: Response
  try {
    response = await fetchWithTimeout(
      `${API_BASE_URL}/api/trip/plan/result/${thread_id}`,
      { method: 'GET', headers: { Accept: 'application/json' } },
      timeoutMs,
    )
  } catch {
    // 网络抖动 / 超时：不当成致命错误，返回 null 让调用方再等一轮
    return null
  }

  if (!response.ok) {
    // 404 说明这个 thread 已经不在后端的记忆里了，再等也没用
    if (response.status === 404 || response.status === 410) {
      throw new ApiError('这次生成的会话已经不存在了。', 'THREAD_GONE')
    }
    // 5xx 可能是后端忙，还有救
    return null
  }

  let data: TripPlanResponse
  try {
    data = (await response.json()) as TripPlanResponse
  } catch {
    return null
  }

  if (!data?.success || !data.data) return null
  // 外层响应和 data 里都可能带缓存元信息，两处都认，外层优先
  const wrapped = wrapPlan(data.data)
  return {
    ...wrapped,
    thread_id: data.thread_id ?? thread_id,
    cached: data.cached ?? wrapped.cached,
    cache_hit_count: data.cache_hit_count ?? wrapped.cache_hit_count,
    cache_saved_at: data.cache_saved_at ?? wrapped.cache_saved_at ?? null,
  }
}

/** 读断点日志：告诉我「已经做完哪些节点、能从哪个节点接着跑」 */
export async function getJournal(thread_id: string): Promise<PlanJournal> {
  try {
    const { data } = await http.get<PlanJournal>(`/api/trip/journal/${thread_id}`, {
      timeout: 8_000,
    })
    return {
      thread_id: data?.thread_id ?? thread_id,
      completed_nodes: data?.completed_nodes ?? [],
      can_resume_from: data?.can_resume_from ?? null,
    }
  } catch (error) {
    throw toApiError(error)
  }
}

/**
 * 从断点续跑。
 *
 * 它比重新提交便宜得多：已完成的节点（最贵的是三个采集 Agent）不会重跑，
 * 所以界面上的按钮文案要把这件事说清楚，用户才敢点。
 */
export async function resumePlan(thread_id: string): Promise<TripPlanResponse> {
  let response: Response
  try {
    // 续跑要跑编排 + 住宿 + 预算，仍然是几十秒级的活儿，所以超时给足
    response = await fetchWithTimeout(
      `${API_BASE_URL}/api/trip/plan/resume/${thread_id}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' } },
      180_000,
    )
  } catch {
    throw new ApiError(ERROR_MESSAGES.NETWORK_ERROR, 'NETWORK_ERROR')
  }

  if (!response.ok) {
    let message = `续跑失败（HTTP ${response.status}）。`
    let code: string | undefined
    try {
      const body = (await response.json()) as ApiErrorBody
      code = body?.error_code ?? undefined
      if (code && ERROR_MESSAGES[code]) message = ERROR_MESSAGES[code]
      else if (body?.message) message = body.message
    } catch {
      // 响应体不是 JSON（网关错误页），就用上面那句兜底
    }
    throw new ApiError(message, code)
  }

  const data = (await response.json()) as TripPlanResponse
  const plan = data?.data ?? null
  if (!plan) {
    throw new ApiError(data?.message || '续跑没有返回行程数据。', data?.error_code ?? undefined)
  }
  return wrapPlan(plan)
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
