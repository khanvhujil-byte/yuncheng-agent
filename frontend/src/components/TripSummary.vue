<script setup lang="ts">
/**
 * 行程总览：目的地 / 天气条 / 预算卡 / 降级提示。
 */
import dayjs from 'dayjs'
import { Calendar, CloudSun, Hotel, Info, MapPin, ShieldCheck, TriangleAlert, Wallet } from 'lucide-vue-next'
import { computed } from 'vue'

import ExportPanel from '@/components/ExportPanel.vue'
import TripMap from '@/components/TripMap.vue'
import type { TripPlan } from '@/types/trip'
import { isEstimated } from '@/utils/source'

const props = defineProps<{ plan: TripPlan }>()

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

const budgetItems = computed(() => [
  { key: 'attractions', label: '门票', value: budget.value?.total_attractions ?? 0 },
  { key: 'hotels', label: '住宿', value: budget.value?.total_hotels ?? 0 },
  { key: 'meals', label: '餐饮', value: budget.value?.total_meals ?? 0 },
  { key: 'transportation', label: '交通', value: budget.value?.total_transportation ?? 0 },
])

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

    <!-- 预算 -->
    <div class="card">
      <div class="card-title">
        <Wallet :size="16" />
        预算
        <span v-if="budgetIsEstimated" class="tag-estimated">含估算项</span>
      </div>
      <div v-if="budget" class="budget-row">
        <div v-for="item in budgetItems" :key="item.key" class="budget-item">
          <div class="budget-label">{{ item.label }}</div>
          <div class="budget-value">¥{{ item.value }}</div>
        </div>
        <div class="budget-item total">
          <div class="budget-label">
            总计
            <span v-if="budgetIsEstimated" class="tag-estimated">预估</span>
          </div>
          <div class="budget-value">¥{{ budget.total ?? 0 }}</div>
        </div>
      </div>
      <div v-else class="empty-hint">没有预算数据。</div>
      <p v-if="budgetIsEstimated" class="footnote">
        门票与住宿价格高德接口不提供，这里的数值由模型按常识估算，仅供参考。
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
    <ExportPanel :plan="plan" />
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
