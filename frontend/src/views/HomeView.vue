<script setup lang="ts">
/**
 * 行程规划首页：左侧表单，右侧结果。
 *
 * 状态机：idle → loading → success / error
 *
 * v0.4 起提交有三条路，按「信息量从多到少」依次尝试：
 *   1. 异步提交 + SSE 订阅（能逐节点看到进度，用户知道卡在哪一步）
 *   2. 兜底轮询 /api/trip/plan/result（推送通道断了，但任务还在跑）
 *   3. 同步 planTrip（后端还没上线异步接口时的老路径）
 * 三条路的区别只体现在「等待期间看到什么」；拿到结果之后的渲染完全共用。
 * 关键约束：1 和 2 失败必须静默降级，绝不能让用户看到比以前更差的体验。
 */
import dayjs, { type Dayjs } from 'dayjs'
import {
  Compass,
  History,
  LoaderCircle,
  RotateCcw,
  Search,
  TrendingUp,
  TriangleAlert,
} from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import DayTimeline from '@/components/DayTimeline.vue'
import TripSummary from '@/components/TripSummary.vue'
import {
  ApiError,
  PIPELINE_NODES,
  POLL_INTERVAL_MS,
  checkHealth,
  getJournal,
  getPlanResult,
  planTrip,
  resumePlan,
  startPlan,
  streamPlan,
  type PlanNodeEvent,
  type PlanProgressEvent,
} from '@/services/api'
import type { TripPlan, TripPlanResponse, TripRequest } from '@/types/trip'

const TRANSPORT_OPTIONS = ['公共交通', '自驾', '步行', '混合']
const ACCOMMODATION_OPTIONS = ['经济型酒店', '舒适型酒店', '豪华酒店', '民宿']
const PREFERENCE_OPTIONS = [
  '自然风光',
  '历史文化',
  '美食',
  '亲子',
  '购物',
  '夜生活',
  '博物馆',
  '摄影',
]

type Status = 'idle' | 'loading' | 'success' | 'error'

/** 推送给界面的进度行状态。waiting = 还没轮到，running = 前端推断出的「正在跑」 */
type NodeState = 'waiting' | 'running' | 'done' | 'degraded' | 'failed'

interface ProgressItem {
  key: string
  label: string
  state: NodeState
  elapsedMs?: number
  detail?: string
}

/** 兜底轮询的最长等待时间。超过就不再等，回退到同步请求 */
const POLL_DEADLINE_MS = 90_000

const form = reactive<TripRequest>({
  city: '杭州',
  start_date: dayjs().add(1, 'day').format('YYYY-MM-DD'),
  end_date: dayjs().add(3, 'day').format('YYYY-MM-DD'),
  travel_days: 3,
  // v0.2：留空 = 按「天数 - 1」算（3 天 2 晚，最后一晚返程）
  nights: undefined,
  transportation: '公共交通',
  accommodation: '经济型酒店',
  preferences: ['自然风光'],
  // v0.2 锚点（硬约束）
  must_visit: [],
  fixed_hotel: '',
  free_text_input: '',
})

const rangeValue = ref<[Dayjs, Dayjs] | undefined>([
  dayjs(form.start_date),
  dayjs(form.end_date),
])

const status = ref<Status>('idle')
const progress = ref<PlanProgressEvent[]>([])
/** 流式节点事件；空数组 = 走的是同步老路径，界面用 progress 兜底 */
const nodeEvents = ref<PlanNodeEvent[]>([])
const elapsedMs = ref(0)
const errorMessage = ref('')
const errorCode = ref<string | undefined>(undefined)
const plan = ref<TripPlan | null>(null)
const warnings = ref<string[]>([])
const threadId = ref<string | undefined>(undefined)
const backendOnline = ref<boolean | null>(null)
const cityMissing = ref(false)
/** 当前等待阶段的说明文字（流式 / 轮询 / 同步 三种文案不同） */
const loadingHint = ref('')
/**
 * 本次**提交时**用的偏好标签快照。
 * 为什么不能直接用表单的 form.preferences：用户拿到结果后可能回去改表单，
 * 而"发布到社区"要上报的是当初生成这份行程时用的偏好（否则社区热度会被没生成过的偏好污染）。
 */
const submittedPreferences = ref<string[]>([])
/** 断点续跑：后端说能从哪个节点接着跑；null = 没有可续的断点 */
const canResumeFrom = ref<string | null>(null)
const resuming = ref(false)
/** 缓存徽标用的三个值。直接在 template 里读 plan/cached 也行，但显式传更清楚 */
const cacheInfo = ref<{ cached: boolean; count: number; savedAt: string | null }>({
  cached: false,
  count: 0,
  savedAt: null,
})

const elapsedText = computed(() => (elapsedMs.value / 1000).toFixed(1))
const canRetry = computed(() => errorCode.value !== 'CITY_NOT_FOUND')
const showResume = computed(() => Boolean(canResumeFrom.value) && !resuming.value)

/** 节点中文名兜底表。正常情况用后端给的 label，这里只防后端漏字段 */
const NODE_LABELS: Record<string, string> = {
  validate_city: '解析城市',
  collect_attractions: '搜集景点',
  collect_weather: '查询天气',
  compose_plan: '编排行程',
  resolve_hotel: '选择住宿',
  patch_budget: '计算预算',
}

/**
 * 进度列表。
 *
 * 后端只在每个节点「完成」时推一条，从不推 running，所以「当前在跑哪个节点」
 * 只能由前端按固定顺序推断：第一个还没到的节点就是进行中。
 * 两个采集分支是并行的，所以它们的到达顺序可能互换 —— 这没关系，
 * 推断只依赖「谁还没到」，不依赖到达次序。
 *
 * 没有任何节点事件时（同步老路径），回退到 progress 里的两条事件。
 */
const progressItems = computed<ProgressItem[]>(() => {
  if (!nodeEvents.value.length) {
    return progress.value.map((event, index) => ({
      key: `${event.node}-${index}`,
      label: event.label,
      state: normalizeState(event.status),
      elapsedMs: event.elapsed_ms,
    }))
  }

  const arrived = new Map<string, PlanNodeEvent>(
    nodeEvents.value.map((event) => [event.node, event]),
  )
  // 先把已知节点按固定顺序排一遍，保住「进度条往前走」的观感
  const ordered: Array<[string, PlanNodeEvent | undefined]> = PIPELINE_NODES.map((node) => [
    node,
    arrived.get(node),
  ])
  // 后端将来新增节点时，事件顺序原样追加在末尾，不会被丢掉
  const knownNodes: readonly string[] = PIPELINE_NODES
  nodeEvents.value.forEach((event) => {
    if (!knownNodes.includes(event.node)) {
      ordered.push([event.node, event])
    }
  })

  const items: ProgressItem[] = []
  let runningAssigned = false
  for (const [node, event] of ordered) {
    if (event) {
      items.push({
        key: node,
        label: event.label,
        state: normalizeState(event.status),
        elapsedMs: event.elapsed_ms,
      })
      continue
    }
    // 第一个还没到的节点 = 正在跑；它后面的都还是等待中
    if (!runningAssigned) {
      runningAssigned = true
      items.push({ key: node, label: NODE_LABELS[node] ?? node, state: 'running' })
    } else {
      items.push({ key: node, label: NODE_LABELS[node] ?? node, state: 'waiting' })
    }
  }
  return items
})

/** 后端给的 status 是裸字符串，收窄成界面认识的那几个值，认不出的一律当「已完成」 */
function normalizeState(raw: string | undefined): NodeState {
  if (raw === 'running' || raw === 'waiting' || raw === 'degraded' || raw === 'failed') {
    return raw
  }
  return 'done'
}

let timer: number | undefined
/** 卸载时要调它，否则 SSE 连接会一直挂着 */
let cancelStream: (() => void) | undefined

function startTimer(): void {
  const startedAt = Date.now()
  elapsedMs.value = 0
  timer = window.setInterval(() => {
    elapsedMs.value = Date.now() - startedAt
  }, 100)
}

function stopTimer(): void {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

onMounted(async () => {
  backendOnline.value = await checkHealth()
})

onBeforeUnmount(() => {
  stopTimer()
  // 组件没了还留着连接 = 内存泄漏 + 后端白推
  cancelStream?.()
})

/** 用户改日期区间：反算天数 */
function onRangeChange(value: unknown): void {
  const picked = value as [Dayjs, Dayjs] | undefined
  if (!picked?.[0] || !picked?.[1]) return
  form.start_date = picked[0].format('YYYY-MM-DD')
  form.end_date = picked[1].format('YYYY-MM-DD')
  form.travel_days = picked[1].diff(picked[0], 'day') + 1
}

/** 用户改天数：顺推结束日期，并回写日期选择器 */
function onDaysChange(value: number | string | null): void {
  const days = Number(value)
  if (!days || days < 1 || !form.start_date) return
  form.travel_days = days
  const end = dayjs(form.start_date).add(days - 1, 'day')
  form.end_date = end.format('YYYY-MM-DD')
  rangeValue.value = [dayjs(form.start_date), end]
}

function validate(): boolean {
  cityMissing.value = !form.city.trim()
  if (cityMissing.value) {
    status.value = 'error'
    errorMessage.value = '请先填写目的地城市。'
    errorCode.value = 'INVALID_REQUEST'
    return false
  }
  return true
}

/** 表单 → 请求体。v0.2 锚点：空值一律清成 undefined，别把空字符串送给后端 */
function buildRequest(): TripRequest {
  return {
    ...form,
    city: form.city.trim(),
    preferences: [...form.preferences],
    must_visit: (form.must_visit ?? []).map((item) => item.trim()).filter(Boolean),
    fixed_hotel: form.fixed_hotel?.trim() ? form.fixed_hotel.trim() : undefined,
    nights: form.nights ?? undefined,
  }
}

/** 把一份响应铺到界面上。三条路径拿到结果后都走这里，渲染逻辑只有一份 */
function applyResponse(response: TripPlanResponse): void {
  warnings.value = response.warnings ?? []
  threadId.value = response.thread_id ? response.thread_id : threadId.value
  plan.value = response.data ?? null
  backendOnline.value = true
  cacheInfo.value = {
    cached: response.cached === true,
    count: response.cache_hit_count ?? 0,
    savedAt: response.cache_saved_at ?? null,
  }

  if (response.data) {
    status.value = 'success'
    // success=false 但有 data，说明是「部分结果」，必须把原因也当警示显示
    if (!response.success && response.message) {
      warnings.value = [response.message, ...warnings.value]
    }
  } else {
    status.value = 'error'
    errorMessage.value = response.message || '生成失败，请重试。'
  }
}

/**
 * 查一次断点日志，决定要不要显示「继续」按钮。
 *
 * 为什么失败时静默：这只是个附加功能，查不到日志不该把「生成失败」的界面
 * 变成「报错套报错」。用户至少还能点重试。
 */
async function refreshJournal(): Promise<void> {
  const id = threadId.value
  if (!id) return
  try {
    const journal = await getJournal(id)
    canResumeFrom.value = journal.can_resume_from
  } catch {
    canResumeFrom.value = null
  }
}

/** 路径 2：异步提交成功、但推送通道没接上时，改用轮询等结果 */
async function pollUntilReady(id: string): Promise<TripPlanResponse | null> {
  const deadline = Date.now() + POLL_DEADLINE_MS
  while (Date.now() < deadline) {
    await sleep(POLL_INTERVAL_MS)
    // 每轮都按「剩余预算」重新算超时，避免一次卡死的请求吃掉整个等待窗口
    const remaining = Math.max(deadline - Date.now(), 1_000)
    const result = await getPlanResult(id, Math.min(remaining, 15_000))
    if (result) return result
  }
  return null
}

/**
 * 路径 1：异步提交 + 流式订阅。
 *
 * 返回值刻意做成三态：
 *   'done'       —— 拿到完整结果
 *   'poll'       —— 提交成功但流断了，交给调用方轮询（不能重算，会白花 LLM 钱）
 *   'unavailable'—— 异步接口本身不可用，交给调用方降级到同步接口
 */
async function runStreaming(request: TripRequest): Promise<'done' | 'poll' | 'unavailable'> {
  let id: string
  try {
    const started = await startPlan(request)
    id = started.thread_id
    threadId.value = id
  } catch {
    // 后端没有 /async（404）、连不上、或返回体不对 —— 都归为「这条路没铺好」
    return 'unavailable'
  }

  loadingHint.value = '正在订阅进度推送，节点完成后会立刻显示在这里。'

  return new Promise((resolve) => {
    cancelStream = streamPlan(id, {
      onNode: (event) => {
        // 同一个节点理论上只推一次，去重是为了防后端重发导致列表出现两行
        if (!nodeEvents.value.some((item) => item.node === event.node)) {
          nodeEvents.value.push(event)
        }
      },
      onDone: (response) => {
        applyResponse(response)
        resolve('done')
      },
      onError: () => {
        // 推送断了不等于任务失败了：结果可能已经好了，只是通道不行
        resolve('poll')
      },
    })
  })
}

async function submit(): Promise<void> {
  if (!validate()) return

  status.value = 'loading'
  errorMessage.value = ''
  errorCode.value = undefined
  plan.value = null
  warnings.value = []
  progress.value = []
  nodeEvents.value = []
  canResumeFrom.value = null
  cacheInfo.value = { cached: false, count: 0, savedAt: null }
  loadingHint.value = '正在提交请求。'
  startTimer()

  const request = buildRequest()
  // 快照一份"这次提交用的偏好"：用户拿到结果后可能回去改表单，
  // 而发布到社区时要上报的是**当初生成这份行程时用的偏好**（不是现在表单里的）。
  submittedPreferences.value = [...request.preferences]

  try {
    // ---- 路径 1 / 2：异步 + 流式，失败就降级 ----
    let outcome: 'done' | 'poll' | 'unavailable' = 'unavailable'
    try {
      outcome = await runStreaming(request)
    } catch {
      // runStreaming 内部已经吞掉了预期的异常；这里兜住任何意外，保证还能降级
      outcome = 'unavailable'
    }

    if (outcome === 'done') return

    if (outcome === 'poll' && threadId.value) {
      loadingHint.value = '进度推送中断，正在改用轮询确认结果（不会重新生成）。'
      let polled: TripPlanResponse | null = null
      try {
        polled = await pollUntilReady(threadId.value)
      } catch (error) {
        // 轮询路上出现「会话没了」这种硬错误，直接报出来比再发同步请求更诚实
        if (error instanceof ApiError && error.code === 'THREAD_GONE') {
          status.value = 'error'
          errorMessage.value = error.message
          errorCode.value = error.code
          return
        }
        polled = null
      }
      if (polled) {
        applyResponse(polled)
        return
      }
      // 轮询也没等到：退回同步请求（这是最后一条路，宁可多花一次调用也不能让用户空手而归）
    }

    // ---- 路径 3：原有同步接口，行为与改动前完全一致 ----
    loadingHint.value = '完整规划通常需要 20~40 秒，要调外部接口多次，请不要关闭页面。'
    const response = await planTrip(request, {
      onProgress: (event) => {
        progress.value.push(event)
      },
      onComplete: (result) => {
        threadId.value = result.thread_id
      },
    })
    applyResponse(response)
  } catch (error) {
    status.value = 'error'
    // 只有同步路径的异常才能断定「后端不在线」；异步失败是静默降级的，不该影响这个判断
    if (error instanceof ApiError && error.code === 'NETWORK_ERROR') {
      backendOnline.value = false
    }
    if (error instanceof ApiError) {
      errorMessage.value = error.message
      errorCode.value = error.code
    } else {
      errorMessage.value = '发生未知错误，请重试。'
    }
    // 生成过程中断（流断了 / 后端出错）时，看看有没有可续的断点
    await refreshJournal()
  } finally {
    stopTimer()
  }
}

/** 从断点接着跑：已完成的节点不会重算，所以比重新提交便宜得多 */
async function resume(): Promise<void> {
  const id = threadId.value
  if (!id) return

  resuming.value = true
  status.value = 'loading'
  errorMessage.value = ''
  loadingHint.value = '正在从断点继续，已完成的步骤不会重跑。'
  startTimer()

  try {
    const response = await resumePlan(id)
    applyResponse(response)
    canResumeFrom.value = null
  } catch (error) {
    status.value = 'error'
    if (error instanceof ApiError) {
      errorMessage.value = error.message
      errorCode.value = error.code
    } else {
      errorMessage.value = '续跑失败，请重试。'
    }
  } finally {
    resuming.value = false
    stopTimer()
  }
}

/** 用户手动点「检查断点」：不想重试整轮，只想接着跑 */
async function checkResume(): Promise<void> {
  await refreshJournal()
  if (!canResumeFrom.value) {
    errorMessage.value = `${errorMessage.value}（后端说这次没有可继续的断点，只能重新生成。）`
  }
}

function reset(): void {
  cancelStream?.()
  cancelStream = undefined
  status.value = 'idle'
  plan.value = null
  warnings.value = []
  progress.value = []
  nodeEvents.value = []
  errorMessage.value = ''
  errorCode.value = undefined
  canResumeFrom.value = null
  cacheInfo.value = { cached: false, count: 0, savedAt: null }
}
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="brand">
        <Compass :size="26" />
        <span>多智能体旅行规划助手</span>
        <!-- 社区页的数据来自另一个服务（Java），但对用户来说只是"换个页面看看别人去哪" -->
        <router-link class="nav-link" to="/community">
          <TrendingUp :size="15" />
          社区行程榜
        </router-link>
      </div>
      <p class="subtitle">
        景点搜集 · 天气查询 · 行程编排 · 住宿求解（两路并行 + 住宿后置）
      </p>
      <div v-if="backendOnline === false" class="offline">
        <TriangleAlert :size="14" />
        后端未连接（http://127.0.0.1:8000）。请先启动后端，再点「生成行程」。
      </div>
    </header>

    <div class="layout">
      <!-- ---------- 左：表单 ---------- -->
      <aside class="panel">
        <a-form layout="vertical" :model="form">
          <a-form-item label="目的地城市" :validate-status="cityMissing ? 'error' : ''">
            <a-input
              v-model:value="form.city"
              placeholder="例如：杭州"
              size="large"
              allow-clear
              @change="cityMissing = false"
            >
              <template #prefix><Search :size="15" /></template>
            </a-input>
          </a-form-item>

          <a-form-item label="出行日期">
            <a-range-picker
              v-model:value="rangeValue"
              style="width: 100%"
              size="large"
              :allow-clear="false"
              @change="onRangeChange"
            />
          </a-form-item>

          <a-form-item label="天数">
            <a-input-number
              :value="form.travel_days"
              :min="1"
              :max="30"
              style="width: 100%"
              size="large"
              @change="onDaysChange"
            />
          </a-form-item>

          <!-- v0.2：住几晚决定住宿费（3 天通常住 2 晚），留空按「天数 - 1」算 -->
          <a-form-item>
            <template #label>
              住几晚
              <span style="color: #8c94a3; font-weight: 400">（留空 = {{ Math.max(form.travel_days - 1, 0) }} 晚）</span>
            </template>
            <a-input-number
              v-model:value="form.nights"
              :min="0"
              :max="form.travel_days"
              style="width: 100%"
              size="large"
              placeholder="留空按天数自动算"
            />
          </a-form-item>

          <a-form-item label="交通方式">
            <a-select v-model:value="form.transportation" size="large">
              <a-select-option v-for="item in TRANSPORT_OPTIONS" :key="item" :value="item">
                {{ item }}
              </a-select-option>
            </a-select>
          </a-form-item>

          <a-form-item label="住宿偏好">
            <a-select v-model:value="form.accommodation" size="large">
              <a-select-option v-for="item in ACCOMMODATION_OPTIONS" :key="item" :value="item">
                {{ item }}
              </a-select-option>
            </a-select>
          </a-form-item>

          <!-- ==== v0.2 锚点（硬约束）：填了就一定会被对账 ==== -->

          <a-form-item>
            <template #label>
              已订酒店
              <span style="color: #8c94a3; font-weight: 400">（选填，填了就按它排）</span>
            </template>
            <a-input
              v-model:value="form.fixed_hotel"
              placeholder="例如：杭州西湖某酒店（重名时建议补地址）"
              size="large"
              allow-clear
            />
          </a-form-item>

          <a-form-item>
            <template #label>
              必去景点
              <span style="color: #8c94a3; font-weight: 400">（选填，输入后回车）</span>
            </template>
            <a-select
              v-model:value="form.must_visit"
              mode="tags"
              size="large"
              style="width: 100%"
              placeholder="例如：西湖、灵隐寺"
              :token-separators="[',', '，']"
            />
          </a-form-item>

          <a-form-item label="旅行偏好（可多选）">
            <a-checkbox-group v-model:value="form.preferences" class="prefs">
              <a-checkbox v-for="item in PREFERENCE_OPTIONS" :key="item" :value="item">
                {{ item }}
              </a-checkbox>
            </a-checkbox-group>
          </a-form-item>

          <a-form-item label="额外要求（选填）">
            <a-textarea
              v-model:value="form.free_text_input"
              :rows="3"
              placeholder="例如：想有一天去周边，不要太赶"
              allow-clear
            />
          </a-form-item>
        </a-form>

        <a-button
          type="primary"
          size="large"
          block
          :loading="status === 'loading'"
          @click="submit"
        >
          {{ status === 'loading' ? '正在规划…' : '生成行程' }}
        </a-button>
      </aside>

      <!-- ---------- 右：结果 ---------- -->
      <main class="panel result">
        <!-- 初始 -->
        <div v-if="status === 'idle'" class="placeholder">
          <Compass :size="36" class="placeholder-icon" />
          <p class="placeholder-title">填好左边，点「生成行程」</p>
          <p class="placeholder-text">
            系统会先确认城市位置（顺手做可行性预检），再让景点与天气两路并行采集，
            然后由编排助手汇总成逐日行程，最后统一求解住宿与预算。
            你填的「必去景点」和「已订酒店」是硬约束，排不进去会如实告诉你。
          </p>
        </div>

        <!-- 加载中：进度列表来自 SSE（每个节点完成后立刻点亮），
             拿不到节点事件时回退为同步老路径的两条事件 -->
        <div v-else-if="status === 'loading'" class="loading-card">
          <div class="loading-head">
            <LoaderCircle :size="18" class="spin" />
            <span>正在规划，已用 {{ elapsedText }} 秒</span>
          </div>
          <ul class="progress-list">
            <li
              v-for="item in progressItems"
              :key="item.key"
              class="progress-item"
              :class="item.state"
            >
              <span class="marker" :class="item.state" />
              <span class="progress-label">{{ item.label }}</span>
              <span v-if="item.state === 'running'" class="progress-tag running">进行中</span>
              <span v-else-if="item.state === 'waiting'" class="progress-tag waiting">等待中</span>
              <span v-else class="progress-elapsed">
                {{ item.elapsedMs !== undefined ? `耗时 ${(item.elapsedMs / 1000).toFixed(1)} 秒` : '已完成' }}
              </span>
            </li>
          </ul>
          <p class="loading-hint">{{ loadingHint }}</p>
          <p class="loading-hint muted-hint">
            节点按固定顺序推进；「搜集景点」和「查询天气」是并行分支，先后到达属于正常现象。
          </p>
        </div>

        <!-- 出错 -->
        <div v-else-if="status === 'error'" class="error-card">
          <div class="error-head">
            <TriangleAlert :size="18" />
            <span>生成失败</span>
          </div>
          <p class="error-message">{{ errorMessage }}</p>
          <p v-if="errorCode" class="error-code">错误码：{{ errorCode }}</p>
          <!-- 断点续跑：只补算没做完的节点，比整轮重算省掉已经花掉的那部分调用 -->
          <div v-if="showResume" class="resume-box">
            <p class="resume-text">
              后端记录了这次的进度，可以从「{{
                NODE_LABELS[canResumeFrom as string] ?? canResumeFrom
              }}」接着往下跑。
            </p>
            <a-button type="primary" @click="resume">
              <template #icon><History :size="14" /></template>
              从「{{ NODE_LABELS[canResumeFrom as string] ?? canResumeFrom }}」继续（已完成的步骤不会重跑）
            </a-button>
          </div>
          <div class="error-actions">
            <a-button v-if="canRetry" :type="showResume ? 'default' : 'primary'" @click="submit">
              <template #icon><RotateCcw :size="14" /></template>
              重新生成
            </a-button>
            <a-button v-if="threadId && !showResume" @click="checkResume">
              <template #icon><History :size="14" /></template>
              检查是否有可继续的断点
            </a-button>
            <a-button @click="reset">返回</a-button>
          </div>
        </div>

        <!-- 成功 -->
        <template v-else>
          <div class="result-head">
            <span class="result-meta">
              实际耗时 {{ elapsedText }} 秒
              <span v-if="threadId" class="thread">会话 {{ threadId.slice(0, 8) }}</span>
            </span>
            <a-button type="text" size="small" @click="reset">
              <template #icon><RotateCcw :size="14" /></template>
              重新规划
            </a-button>
          </div>
          <TripSummary
            v-if="plan"
            :plan="plan"
            :cached="cacheInfo.cached"
            :cache-hit-count="cacheInfo.count"
            :cache-saved-at="cacheInfo.savedAt"
            :preferences="submittedPreferences"
          />
          <DayTimeline v-if="plan" :days="plan.days" :sources="plan.sources" />
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}

.hero {
  margin-bottom: 24px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 22px;
  font-weight: 650;
  color: #1b1f27;
}

.subtitle {
  margin: 8px 0 0;
  color: #6b7280;
  font-size: 14px;
}

.offline {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  padding: 6px 12px;
  background: #fffbf0;
  border: 1px solid #ffe3a3;
  border-radius: 8px;
  color: #8a6100;
  font-size: 13px;
}

.layout {
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: 20px;
  align-items: start;
}

.panel {
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 12px;
  padding: 20px;
}

.prefs {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px 12px;
  line-height: 1.8;
}

.result {
  min-height: 320px;
}

.placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 320px;
  text-align: center;
  color: #8c94a3;
}

.placeholder-icon {
  color: #c8d3e8;
  margin-bottom: 12px;
}

.placeholder-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: #5a6272;
}

.placeholder-text {
  margin: 0;
  max-width: 420px;
  font-size: 13px;
  line-height: 1.9;
}

.loading-card {
  padding: 8px 4px;
}

.loading-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: #1b1f27;
  margin-bottom: 16px;
}

.spin {
  animation: spin 1s linear infinite;
  color: #3566c9;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.progress-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.progress-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #5a6272;
}

/* 还没轮到的节点压暗，让「进度往前走」这件事一眼可见 */
.progress-item.waiting {
  color: #a8b0be;
}

.progress-item.done .progress-label {
  color: #1b1f27;
}

.progress-label {
  flex: 0 0 auto;
}

/* 状态标记用「圆点 / 转圈」区分，不用 emoji（GBK 环境下 emoji 会崩） */
.marker {
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #c8d3e8;
}

.marker.waiting {
  background: #dfe4ec;
}

.marker.running {
  background: transparent;
  border: 2px solid #3566c9;
  border-top-color: transparent;
  animation: spin 0.8s linear infinite;
}

.marker.done {
  background: #52a447;
}

.marker.degraded {
  background: #d79b00;
}

.marker.failed {
  background: #c94f4f;
}

.progress-tag {
  font-size: 11px;
  padding: 0 6px;
  border-radius: 4px;
  line-height: 18px;
}

.progress-tag.running {
  color: #2b5fb8;
  background: #e8f1ff;
}

.progress-tag.waiting {
  color: #8c94a3;
  background: #f2f4f7;
}

.progress-elapsed {
  margin-left: auto;
  font-size: 12px;
  color: #8c94a3;
}

.loading-hint {
  margin: 18px 0 0;
  font-size: 12px;
  color: #8c94a3;
}

.muted-hint {
  margin-top: 6px;
  color: #a8b0be;
}

/* 断点续跑提示块：和上面的错误说明隔开，避免用户以为「又报了一个错」 */
.resume-box {
  margin-top: 16px;
  padding: 14px 16px;
  background: #eef4ff;
  border: 1px solid #dbe6ff;
  border-radius: 10px;
}

.resume-text {
  margin: 0 0 12px;
  font-size: 13px;
  color: #2b5fb8;
  line-height: 1.8;
}

.error-card {
  padding: 4px;
}

.error-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: #c94f4f;
  margin-bottom: 12px;
}

.error-message {
  margin: 0 0 6px;
  color: #3d4453;
  font-size: 14px;
  line-height: 1.8;
}

.error-code {
  margin: 0;
  font-size: 12px;
  color: #8c94a3;
}

.error-actions {
  display: flex;
  gap: 10px;
  margin-top: 18px;
}

.result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.result-meta {
  font-size: 12px;
  color: #8c94a3;
}

.thread {
  margin-left: 10px;
  padding: 1px 6px;
  background: #f7f9fc;
  border-radius: 4px;
}

@media (max-width: 940px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

.nav-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: 10px;
  padding: 4px 12px;
  border: 1px solid #e3e6ec;
  border-radius: 999px;
  background: #fff;
  color: #4a5261;
  font-size: 13px;
  font-weight: 400;
  text-decoration: none;
  transition: all 0.15s;
}

.nav-link:hover {
  border-color: #c9cfd9;
  color: #1b1f27;
}
</style>
