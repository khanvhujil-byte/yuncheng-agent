<script setup lang="ts">
/**
 * 行程规划首页：左侧表单，右侧结果。
 *
 * 状态机：idle → loading → success / error
 * 提交走 services/api.ts 的 planTrip()，用回调方式接收进度事件。
 * V0 只会收到两条事件（提交 / 完成），后端接上 SSE 之后这里不用改。
 */
import dayjs, { type Dayjs } from 'dayjs'
import { Compass, LoaderCircle, RotateCcw, Search, TriangleAlert } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import DayTimeline from '@/components/DayTimeline.vue'
import TripSummary from '@/components/TripSummary.vue'
import { ApiError, checkHealth, planTrip, type PlanProgressEvent } from '@/services/api'
import type { TripPlan, TripRequest } from '@/types/trip'

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

const form = reactive<TripRequest>({
  city: '杭州',
  start_date: dayjs().add(1, 'day').format('YYYY-MM-DD'),
  end_date: dayjs().add(3, 'day').format('YYYY-MM-DD'),
  travel_days: 3,
  transportation: '公共交通',
  accommodation: '经济型酒店',
  preferences: ['自然风光'],
  free_text_input: '',
})

const rangeValue = ref<[Dayjs, Dayjs] | undefined>([
  dayjs(form.start_date),
  dayjs(form.end_date),
])

const status = ref<Status>('idle')
const progress = ref<PlanProgressEvent[]>([])
const elapsedMs = ref(0)
const errorMessage = ref('')
const errorCode = ref<string | undefined>(undefined)
const plan = ref<TripPlan | null>(null)
const warnings = ref<string[]>([])
const threadId = ref<string | undefined>(undefined)
const backendOnline = ref<boolean | null>(null)
const cityMissing = ref(false)

const elapsedText = computed(() => (elapsedMs.value / 1000).toFixed(1))
const canRetry = computed(() => errorCode.value !== 'CITY_NOT_FOUND')

let timer: number | undefined

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

onMounted(async () => {
  backendOnline.value = await checkHealth()
})

onBeforeUnmount(stopTimer)

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

async function submit(): Promise<void> {
  if (!validate()) return

  status.value = 'loading'
  errorMessage.value = ''
  errorCode.value = undefined
  plan.value = null
  warnings.value = []
  progress.value = []
  startTimer()

  try {
    const response = await planTrip(
      {
        ...form,
        city: form.city.trim(),
        preferences: [...form.preferences],
      },
      {
        onProgress: (event) => {
          progress.value.push(event)
        },
        onComplete: (result) => {
          threadId.value = result.thread_id
        },
      },
    )

    warnings.value = response.warnings ?? []
    threadId.value = response.thread_id
    plan.value = response.data ?? null
    backendOnline.value = true

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
  } catch (error) {
    status.value = 'error'
    backendOnline.value = false
    if (error instanceof ApiError) {
      errorMessage.value = error.message
      errorCode.value = error.code
    } else {
      errorMessage.value = '发生未知错误，请重试。'
    }
  } finally {
    stopTimer()
  }
}

function reset(): void {
  status.value = 'idle'
  plan.value = null
  warnings.value = []
  progress.value = []
  errorMessage.value = ''
  errorCode.value = undefined
}
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="brand">
        <Compass :size="26" />
        <span>多智能体旅行规划助手</span>
      </div>
      <p class="subtitle">
        四个专职助手分工协作：景点搜集 · 天气查询 · 酒店推荐 · 行程编排
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
            系统会依次让四个助手干活：先确认城市位置，再分别搜集景点、查询天气、推荐酒店，
            最后由编排助手汇总成逐日行程。
          </p>
        </div>

        <!-- 加载中 -->
        <div v-else-if="status === 'loading'" class="loading-card">
          <div class="loading-head">
            <LoaderCircle :size="18" class="spin" />
            <span>正在规划，已用 {{ elapsedText }} 秒</span>
          </div>
          <ul class="progress-list">
            <li v-for="(event, index) in progress" :key="index" class="progress-item">
              <span class="dot" :class="event.status" />
              <span>{{ event.label }}</span>
            </li>
          </ul>
          <p class="loading-hint">
            完整规划通常需要 20~40 秒，要调外部接口多次，请不要关闭页面。
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
          <div class="error-actions">
            <a-button v-if="canRetry" type="primary" @click="submit">
              <template #icon><RotateCcw :size="14" /></template>
              重试
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
          <TripSummary v-if="plan" :plan="plan" />
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

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c8d3e8;
}

.dot.running {
  background: #3566c9;
}

.dot.done {
  background: #52a447;
}

.dot.degraded {
  background: #d79b00;
}

.dot.failed {
  background: #c94f4f;
}

.loading-hint {
  margin: 18px 0 0;
  font-size: 12px;
  color: #8c94a3;
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
</style>
