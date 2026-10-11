<script setup lang="ts">
/**
 * 行程总览：目的地 / 天气条 / 预算卡 / 降级提示。
 */
import dayjs from 'dayjs'
import {
  Calendar,
  CircleAlert,
  CircleCheck,
  CircleQuestionMark,
  CloudSun,
  History,
  Hotel,
  Info,
  MapPin,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  Wallet,
} from 'lucide-vue-next'
import { computed, type Component } from 'vue'

import ExportPanel from '@/components/ExportPanel.vue'
import TripMap from '@/components/TripMap.vue'
import type { LimitStatus, TripPlan } from '@/types/trip'
import { isEstimated } from '@/utils/source'

/**
 * 缓存元信息通过 props 传进来，原因有两个：
 *   1. 这是纯展示组件，自己去发请求会让「谁在什么时候请求什么」变得不可追踪；
 *   2. 三个值可能挂在响应外层，也可能挂在 data 里，由父组件统一归一化一次就够了。
 */
const props = defineProps<{
  plan: TripPlan
  /** true = 本次结果直接来自行程库，没有消耗任何模型调用 */
  cached?: boolean
  /** 这条行程被复用了几次 */
  cacheHitCount?: number
  /** 最初生成时间（ISO 字符串） */
  cacheSavedAt?: string | null
  /**
   * 生成这份行程时用的偏好标签（由 HomeView 提交时快照传入）。
   * 为什么由外面传：TripPlan 里没有偏好 —— 它只记录"排好的每天去哪"，
   * 而偏好是**请求**的信息。发布到社区时 Java 要靠它累积"常见偏好"热度。
   */
  preferences?: string[]
}>()

/** 只有显式 cached === true 才显示徽标：undefined / false 都当没命中 */
const showCacheBadge = computed(() => props.cached === true)

/** 复用次数文案。为 0 或缺失时不硬编「第 0 次复用」 */
const hitText = computed(() => {
  const count = props.cacheHitCount ?? 0
  return count > 0 ? `第 ${count} 次复用` : '已复用'
})

/** 最初生成时间。后端给的是 ISO 字符串，这里只截到分钟，够用且不刺眼 */
const savedAtText = computed(() => {
  const raw = props.cacheSavedAt
  if (!raw) return ''
  const parsed = dayjs(raw)
  if (!parsed.isValid()) return ''
  return parsed.format('YYYY-MM-DD HH:mm')
})

const warnings = computed(() => props.plan.warnings ?? [])
const weatherList = computed(() => props.plan.weather_info ?? [])
const budget = computed(() => props.plan.budget ?? null)

/** v0.2：住宿是行程级一家，不再是每天一个 */
const hotel = computed(() => props.plan.hotel ?? null)
const hotelNights = computed(() => props.plan.hotel_nights ?? 0)
const hotelIsUser = computed(() => props.plan.sources?.hotel === 'user')
const hotelEstimated = computed(() => isEstimated(props.plan.sources, 'hotel.estimated_cost'))

/** v0.2：硬约束对账（必去景点满足情况） */
const report = computed(() => props.plan.constraint_report ?? null)
const unsatisfied = computed(() => report.value?.unsatisfied ?? [])
const satisfied = computed(() => report.value?.satisfied ?? [])
const required = computed(() => report.value?.required_attractions ?? [])

function reasonOf(name: string): string {
  return report.value?.reasons?.[name] ?? '未能安排'
}

/**
 * v0.7：分类里各有几项金额未知。
 *
 * 为什么要在小计旁边标出来：小计只统计「已知 + 估算」，一个景点的门票没查到
 * 就不会进「门票」这一格。不标的话，用户看到「门票 ¥0」会读成「门票不要钱」，
 * 而这恰恰是 v0.7 想修掉的那种误读。
 */
const unknownByCategory = computed(() => {
  const counts: Record<string, number> = {}
  for (const item of budget.value?.items ?? []) {
    if (item.status === 'unknown') {
      counts[item.category] = (counts[item.category] ?? 0) + 1
    }
  }
  return counts
})

const budgetItems = computed(() => [
  {
    key: 'attractions',
    label: '门票',
    value: budget.value?.total_attractions ?? 0,
    unknown: unknownByCategory.value.attraction ?? 0,
  },
  {
    key: 'hotels',
    label: '住宿',
    value: budget.value?.total_hotels ?? 0,
    unknown: unknownByCategory.value.hotel ?? 0,
  },
  {
    key: 'meals',
    label: '餐饮',
    value: budget.value?.total_meals ?? 0,
    unknown: unknownByCategory.value.meal ?? 0,
  },
  {
    key: 'transportation',
    label: '交通',
    value: budget.value?.total_transportation ?? 0,
    unknown: unknownByCategory.value.transportation ?? 0,
  },
])

// ---------------- v0.7：费用三态与「够不够」的结论 ----------------

/** 金额未知的费用项名字。★ 它们没有被算进 total —— 这是整张卡最要交代清楚的一件事 */
const unknownItems = computed(() => budget.value?.unknown_items ?? [])
const unknownCount = computed(() => unknownItems.value.length)

/** 未知项只列前 3 个，再多用「等 N 项」收口（避免长列表把卡片撑爆，也和后端文案一致） */
const unknownPreview = computed(() => {
  const names = unknownItems.value
  const head = names.slice(0, 3).join('、')
  return names.length > 3 ? `${head} 等 ${names.length} 项` : head
})

/**
 * 「总计」的口径说明，只在**有未知项时**出现。
 * 没有未知项时 total 就是全部，多写一句话反而像在暗示什么。
 */
const totalCaliber = computed(() => {
  if (!unknownCount.value) return ''
  return `已知 + 估算之和，不含 ${unknownCount.value} 项未知`
})

/** 已知 / 估算拆分。老响应没有这两个字段时整块不显示，而不是显示两个 0 */
const knownTotal = computed(() => budget.value?.known_total)
const estimatedTotal = computed(() => budget.value?.estimated_total)
const hasSplitTotals = computed(
  () => knownTotal.value !== undefined || estimatedTotal.value !== undefined,
)

/** 用户给的上限；null / undefined = 没设，不做预算约束 */
const limit = computed(() => budget.value?.limit ?? null)

/**
 * 五种结论的文案与配色。
 *
 * 措辞刻意保守，因为每种结论能证明的东西不一样：
 *   estimated_exceeded 只能说「按估算可能超」—— 估算项本身有误差，写成「无解」是过度断言；
 *   unverified 只能说「无法确认」—— 因为有金额未知的项，未知项不能按 0 元算作已满足。
 */
const LIMIT_BADGES: Record<LimitStatus, { text: string; tone: string; icon: Component }> = {
  // limit 有值却拿到 none，说明这次没有复核过（正常链路不会同时出现）——
  // 此时说实话「未做复核」，不能显示「未设上限」（那和上面的上限金额自相矛盾）
  none: { text: '本次未做预算复核', tone: 'neutral', icon: Info },
  within: { text: '在预算内（按当前口径）', tone: 'ok', icon: CircleCheck },
  known_exceeded: { text: '已超限（已知费用）', tone: 'bad', icon: ShieldAlert },
  estimated_exceeded: { text: '可能超限（含估算项）', tone: 'warn', icon: CircleAlert },
  unverified: { text: '无法确认是否在预算内', tone: 'unknown', icon: CircleQuestionMark },
}

/** 后端给了没登记过的结论值时的兜底，避免徽标变成空白 */
const UNKNOWN_BADGE: { text: string; tone: string; icon: Component } = {
  text: '结论未给出',
  tone: 'unknown',
  icon: CircleQuestionMark,
}

const limitBadge = computed(() => {
  const status = budget.value?.limit_status
  // 老响应（v0.7 之前的缓存）没有 limit_status：宁可不显示徽标，也不要显示一个空白徽标
  if (!status) return null
  return LIMIT_BADGES[status] ?? UNKNOWN_BADGE
})

/** 一句话解释当前结论的适用范围，防止用户把「按估算算的」当成「已经确定」 */
const LIMIT_HINTS: Partial<Record<LimitStatus, string>> = {
  within: '这个结论只在当前数据快照与估算口径下成立，不等于实际一定不会超。',
  known_exceeded: '已知费用本身已超过上限，这不是估算偏差能解释的，属于硬性超限。',
  estimated_exceeded:
    '把估算项算进去后超限了。估算有误差，所以这不等于实际旅行不可行 —— 可以换更省的选择或放宽上限。',
  unverified: '有费用项金额未知，无法确认是否满足预算 —— 未知项不能按 0 元算作已经满足。',
}

const limitHint = computed(() => {
  const status = budget.value?.limit_status
  return status ? (LIMIT_HINTS[status] ?? '') : ''
})

/** 后端给的依据里带 Markdown 的星号强调；这里是纯文本渲染，留着会露出一堆星号 */
const limitBasis = computed(() => (budget.value?.limit_basis ?? '').replace(/\*\*/g, ''))

/** 预算里只要有一项是估算的，整个卡片就要显示「预估」 */
const budgetIsEstimated = computed(
  () =>
    isEstimated(props.plan.sources, 'attraction.ticket_price') ||
    isEstimated(props.plan.sources, 'hotel.estimated_cost') ||
    isEstimated(props.plan.sources, 'meal.estimated_cost'),
)

const weatherIsDegraded = computed(() => weatherList.value.length === 0)

function dayLabel(date: string): string {
  return dayjs(date).format('M月D日')
}

function weekday(date: string): string {
  return '周' + '日一二三四五六'[dayjs(date).day()]
}

function tempText(value: number | string | undefined): string {
  if (value === undefined || value === null || value === '') return '--'
  return `${value}°`
}
</script>

<template>
  <section class="summary">
    <!-- 目的地 -->
    <div class="card hero-card">
      <div class="hero-main">
        <h2 class="city">
          <MapPin :size="22" />
          {{ plan.city }}
        </h2>
        <p class="date-range">
          <Calendar :size="16" />
          {{ plan.start_date }} 至 {{ plan.end_date }}
          <span class="sep">·</span>
          共 {{ plan.days.length }} 天
        </p>
        <!-- 缓存复用徽标：放在概览卡里，因为「这份行程是现成的还是刚算的」
             直接影响用户怎么读后面的内容（现成的就不会有本次的模型判断） -->
        <div v-if="showCacheBadge" class="cache-badge">
          <History :size="15" />
          <span>
            复用历史行程（{{ hitText }}<template v-if="savedAtText">，最初生成于 {{ savedAtText }}</template>）
          </span>
          <span class="cache-note">本次未消耗模型调用</span>
        </div>
      </div>
    </div>

    <!-- 降级 / 估算提示。有就必须显示，不能让用户以为是真实数据 -->
    <div v-if="warnings.length" class="card alert-card">
      <div class="alert-title">
        <TriangleAlert :size="16" />
        以下内容需要你留意
      </div>
      <ul class="alert-list">
        <li v-for="(text, index) in warnings" :key="index">{{ text }}</li>
      </ul>
    </div>

    <!-- 行程地图：放在概览靠上的位置，因为「景点在哪、怎么串起来」是用户看完
         城市和警示后最先要确认的事。放在这里也保证它出现在逐日时间轴（DayTimeline）之前，
         图文不会隔得太远 -->
    <TripMap :days="plan.days" :hotel="hotel" />

    <!-- 住宿（v0.2：行程级一家，不是每天一个） -->
    <div class="card">
      <div class="card-title">
        <Hotel :size="16" />
        住宿
        <span v-if="hotelIsUser" class="tag-estimated" style="background: #e8f1ff; color: #2b5fb8">
          你指定的
        </span>
        <span v-else-if="hotelEstimated" class="tag-estimated">预估</span>
      </div>
      <template v-if="hotel">
        <div style="font-weight: 600; font-size: 15px">{{ hotel.name }}</div>
        <div class="muted" style="margin-top: 4px; font-size: 13px">
          <span v-if="hotel.address">{{ hotel.address }}</span>
          <span v-if="hotel.type"> · {{ hotel.type }}</span>
          <span v-if="hotel.rating"> · 评分 {{ hotel.rating }}</span>
        </div>
        <div class="muted" style="margin-top: 4px; font-size: 13px">
          共 {{ hotelNights }} 晚
          <span v-if="hotel.estimated_cost">
            · 约 ¥{{ hotel.estimated_cost }}/晚 · 合计 ¥{{ hotel.estimated_cost * hotelNights }}
          </span>
        </div>
        <p v-if="hotel.distance" class="muted" style="margin: 6px 0 0; font-size: 12px">
          {{ hotel.distance }}
        </p>
        <p v-if="hotelIsUser" class="footnote">
          这家酒店是你自己指定的，系统没有另做选址；价格仍是估算值。
        </p>
      </template>
      <div v-else class="empty-hint">
        没有住宿信息。可能是候选酒店为空，或该路数据获取失败。
      </div>
    </div>

    <!-- 硬约束对账（v0.2）：你要求的必去景点，到底排进去了几个 -->
    <div v-if="required.length" class="card">
      <div class="card-title">
        <ShieldCheck :size="16" />
        你的必去要求
        <span class="muted" style="font-weight: 400">
          {{ satisfied.length }} / {{ required.length }} 已安排
        </span>
      </div>
      <ul class="alert-list" style="list-style: none; padding: 0; margin: 0">
        <li v-for="name in satisfied" :key="'ok-' + name" style="font-size: 13px">
          已安排 · {{ name }}
        </li>
        <li v-for="name in unsatisfied" :key="'no-' + name" style="font-size: 13px; color: #b85450">
          未安排 · {{ name }} —— {{ reasonOf(name) }}
        </li>
      </ul>
      <p v-if="unsatisfied.length" class="footnote">
        未安排的项会同时出现在上方的「需要你留意」里。系统不会静默丢掉你的要求，
        但你也可以减少必去景点或增加天数来让它排得下。
      </p>
    </div>

    <!-- 天气条 -->
    <div class="card">
      <div class="card-title">
        <CloudSun :size="16" />
        天气
        <span v-if="weatherIsDegraded" class="muted">（未获取到天气数据）</span>
      </div>
      <div v-if="weatherList.length" class="weather-row">
        <div v-for="item in weatherList" :key="item.date" class="weather-item">
          <div class="weather-date">
            {{ dayLabel(item.date) }}
            <span class="muted">{{ weekday(item.date) }}</span>
          </div>
          <!-- v0.2：超出预报窗口的天是「已知的空」，必须和「实况」区分开。
               混在一起显示，用户会以为"这天有实况数据" -->
          <template v-if="item.beyond_window">
            <div class="weather-text muted">无预报</div>
            <div class="weather-temp muted">-- / --</div>
            <div class="weather-wind muted">超出预报窗口</div>
          </template>
          <template v-else>
            <div class="weather-text">
              {{ item.day_weather || '--' }} / {{ item.night_weather || '--' }}
            </div>
            <div class="weather-temp">
              {{ tempText(item.day_temp) }} / {{ tempText(item.night_temp) }}
            </div>
            <div class="weather-wind muted">
              {{ item.wind_direction || '' }} {{ item.wind_power || '' }}
            </div>
            <div v-if="item.is_forecast === false" class="weather-flag">实况</div>
          </template>
        </div>
      </div>
      <div v-else class="empty-hint">
        没有天气数据。可能原因：出行日期超出预报窗口（高德只提供今天起 4 天），或该路数据获取失败。
      </div>
    </div>

    <!-- 预算：v0.7 起不只报总价，还要回答「在不在预算内」，并说清哪些钱是不知道的 -->
    <div class="card">
      <div class="card-title">
        <Wallet :size="16" />
        预算
        <span v-if="budgetIsEstimated" class="tag-estimated">含估算项</span>
        <span v-if="unknownCount" class="tag-unknown">{{ unknownCount }} 项金额未知</span>
      </div>

      <template v-if="budget">
        <!-- 结论放在明细之前：用户最想知道的是「够不够」，其余数字都是支撑 -->
        <div v-if="limit !== null" class="limit-box">
          <div class="limit-head">
            <span class="limit-label">你的预算上限</span>
            <span class="limit-value">¥{{ limit }}</span>
            <span v-if="limitBadge" class="limit-badge" :class="`tone-${limitBadge.tone}`">
              <component :is="limitBadge.icon" :size="13" />
              {{ limitBadge.text }}
            </span>
          </div>
          <p v-if="limitHint" class="limit-hint">{{ limitHint }}</p>
        </div>

        <div class="budget-row">
          <div v-for="item in budgetItems" :key="item.key" class="budget-item">
            <div class="budget-label">
              {{ item.label }}
              <span v-if="item.unknown" class="tag-unknown">含 {{ item.unknown }} 项未知</span>
            </div>
            <div class="budget-value">¥{{ item.value }}</div>
          </div>
          <div class="budget-item total">
            <div class="budget-label">
              总计
              <span v-if="budgetIsEstimated" class="tag-estimated">预估</span>
            </div>
            <div class="budget-value">¥{{ budget.total ?? 0 }}</div>
            <!-- 总计旁边必须写明口径，否则「总计 2060」会被当成实际要花的钱 -->
            <div v-if="totalCaliber" class="budget-caliber">{{ totalCaliber }}</div>
          </div>
        </div>

        <!-- 已知 / 估算拆分：让「总计」这个数可核对（哪些是真数据、哪些是估的） -->
        <div v-if="hasSplitTotals" class="split-row">
          <div class="split-item">
            <div class="split-label">已知费用</div>
            <div class="split-value">¥{{ knownTotal ?? 0 }}</div>
            <div class="split-note">有确切来源，可核对</div>
          </div>
          <div class="split-item">
            <div class="split-label">估算费用</div>
            <div class="split-value">¥{{ estimatedTotal ?? 0 }}</div>
            <div class="split-note">模型或固定表估的，有误差</div>
          </div>
        </div>

        <!-- 未知项必须显式列出来：只把金额藏起来，用户会把「没看见」读成「不要钱」 -->
        <div v-if="unknownCount" class="unknown-box">
          <div class="unknown-head">
            <TriangleAlert :size="14" />
            有 {{ unknownCount }} 项费用金额未知，<strong>未计入</strong>上面的总计
          </div>
          <div class="unknown-list">{{ unknownPreview }}</div>
          <p class="unknown-note">
            未知不等于免费（0 元才是免费）。实际开销可能高于总计，这些项需要另行确认。
          </p>
        </div>

        <!-- 依据：结论必须能被追问（「你凭什么说没超」），所以把后端的口径原文摆出来 -->
        <p v-if="limitBasis" class="basis-text">结论依据：{{ limitBasis }}</p>
      </template>
      <div v-else class="empty-hint">没有预算数据。</div>

      <p v-if="budget && (budgetIsEstimated || unknownCount)" class="footnote">
        门票与住宿价格高德接口不提供：查到真值的按已知计入，估出来的进「估算费用」，
        查不到的记为未知且不计入总计 —— 所以总计是「已知 + 估算」，不等于实际开销。
      </p>
    </div>

    <!-- 总体建议 -->
    <div v-if="plan.overall_suggestions" class="card">
      <div class="card-title">
        <Info :size="16" />
        总体建议
      </div>
      <p class="suggestions">{{ plan.overall_suggestions }}</p>
    </div>

    <!-- 导出面板放最底部：它是「整份行程看完了，要带走」的收尾动作，
         放顶部会打断阅读，放底部符合流程顺序（先看，再导出） -->
    <ExportPanel :plan="plan" :preferences="preferences" />
  </section>
</template>

<style scoped>
.summary {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card {
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 12px;
  padding: 18px 20px;
}

.hero-card {
  background: linear-gradient(135deg, #eef4ff 0%, #f7f9fc 100%);
  border-color: #dbe6ff;
}

.hero-main {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.city {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 22px;
  font-weight: 650;
  color: #1b1f27;
}

.date-range {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: #5a6272;
  font-size: 14px;
}

.sep {
  margin: 0 2px;
  color: #c2c7d0;
}

/* 缓存徽标：用绿色系和「预估/降级」的黄色系明确区分 ——
   复用是好事（省钱省时间），不是需要警惕的降级 */
.cache-badge {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding: 8px 12px;
  background: #eaf7ec;
  border: 1px solid #c6e6c9;
  border-radius: 8px;
  color: #2f6b39;
  font-size: 13px;
}

.cache-note {
  font-size: 12px;
  color: #4a8a55;
  background: #ffffff;
  border-radius: 4px;
  padding: 1px 6px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #1b1f27;
  margin-bottom: 14px;
}

.muted {
  color: #8c94a3;
  font-weight: 400;
}

.alert-card {
  background: #fffbf0;
  border-color: #ffe3a3;
}

.alert-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #8a6100;
  margin-bottom: 8px;
}

.alert-list {
  margin: 0;
  padding-left: 20px;
  color: #8a6100;
  font-size: 13px;
  line-height: 1.9;
}

.weather-row {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.weather-item {
  position: relative;
  flex: 0 0 auto;
  min-width: 104px;
  padding: 12px 14px;
  background: #f7f9fc;
  border-radius: 10px;
  text-align: center;
}

.weather-date {
  font-size: 13px;
  font-weight: 600;
  color: #1b1f27;
  margin-bottom: 6px;
}

.weather-date .muted {
  font-size: 12px;
  font-weight: 400;
  margin-left: 4px;
}

.weather-text {
  font-size: 13px;
  color: #5a6272;
  margin-bottom: 4px;
}

.weather-temp {
  font-size: 15px;
  font-weight: 600;
  color: #1b1f27;
}

.weather-wind {
  font-size: 12px;
  margin-top: 4px;
}

.weather-flag {
  position: absolute;
  top: 6px;
  right: 8px;
  font-size: 11px;
  color: #8c94a3;
  background: #eceef2;
  border-radius: 4px;
  padding: 0 4px;
}

.budget-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.budget-item {
  flex: 1 1 88px;
  padding: 12px 14px;
  background: #f7f9fc;
  border-radius: 10px;
}

.budget-item.total {
  background: #eef4ff;
}

.budget-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #8c94a3;
  margin-bottom: 6px;
}

.budget-value {
  font-size: 18px;
  font-weight: 650;
  color: #1b1f27;
}

.tag-estimated {
  font-size: 11px;
  font-weight: 400;
  color: #8a6100;
  background: #fff3d6;
  border-radius: 4px;
  padding: 1px 6px;
}

/* 「金额未知」用中性灰而不是警告黄：它不是错误，是「我们不知道」，
   用醒目的黄色会让人以为出了故障，用绿色又会显得可信 —— 灰色最贴近事实 */
.tag-unknown {
  font-size: 11px;
  font-weight: 400;
  color: #5a6272;
  background: #eceef2;
  border-radius: 4px;
  padding: 1px 6px;
}

/* ---------------- v0.7 预算结论 ---------------- */

.limit-box {
  padding: 12px 14px;
  margin-bottom: 12px;
  background: #f7f9fc;
  border: 1px solid #eceef2;
  border-radius: 10px;
}

.limit-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.limit-label {
  font-size: 12px;
  color: #8c94a3;
}

.limit-value {
  font-size: 16px;
  font-weight: 650;
  color: #1b1f27;
}

/* 徽标按结论配色：绿=在预算内，红=硬超限，橙=估算超限，灰=无法确认。
   四种颜色对应四种「可信程度」，不能混用 —— 颜色本身就是结论的一部分 */
.limit-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  border-radius: 999px;
  padding: 2px 10px;
}

.limit-badge.tone-ok {
  color: #2f6b39;
  background: #eaf7ec;
}

.limit-badge.tone-bad {
  color: #a33a3a;
  background: #fdecec;
}

.limit-badge.tone-warn {
  color: #8a6100;
  background: #fff3d6;
}

.limit-badge.tone-unknown,
.limit-badge.tone-neutral {
  color: #5a6272;
  background: #eceef2;
}

.limit-hint {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: #5a6272;
}

.budget-caliber {
  margin-top: 4px;
  font-size: 11px;
  color: #8c94a3;
}

.split-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.split-item {
  flex: 1 1 140px;
  padding: 10px 14px;
  border: 1px dashed #eceef2;
  border-radius: 10px;
}

.split-label {
  font-size: 12px;
  color: #8c94a3;
}

.split-value {
  font-size: 16px;
  font-weight: 650;
  color: #1b1f27;
  margin-top: 2px;
}

.split-note {
  margin-top: 2px;
  font-size: 11px;
  color: #8c94a3;
}

.unknown-box {
  margin-top: 12px;
  padding: 12px 14px;
  background: #f7f9fc;
  border-left: 3px solid #c2c7d0;
  border-radius: 6px;
}

.unknown-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #5a6272;
}

.unknown-list {
  margin-top: 6px;
  font-size: 13px;
  color: #3d4453;
  line-height: 1.8;
}

.unknown-note {
  margin: 6px 0 0;
  font-size: 12px;
  color: #8c94a3;
  line-height: 1.7;
}

.basis-text {
  margin: 12px 0 0;
  font-size: 12px;
  color: #8c94a3;
  line-height: 1.8;
}

.empty-hint {
  padding: 14px 16px;
  background: #f7f9fc;
  border-radius: 10px;
  color: #8c94a3;
  font-size: 13px;
  line-height: 1.8;
}

.footnote {
  margin: 12px 0 0;
  font-size: 12px;
  color: #8c94a3;
}

.suggestions {
  margin: 0;
  color: #3d4453;
  font-size: 14px;
  line-height: 1.9;
  white-space: pre-wrap;
}
</style>
