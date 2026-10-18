<script setup lang="ts">
/**
 * 逐日行程时间轴。
 */
import dayjs from 'dayjs'
import { Clock, Hotel, MapPin, Route, Ticket, Utensils } from 'lucide-vue-next'
import { computed } from 'vue'

import type { Attraction, DayPlan, SourceMap } from '@/types/trip'
import { isEstimated } from '@/utils/source'

const props = defineProps<{
  days: DayPlan[]
  sources?: SourceMap
}>()

const MEAL_LABELS: Record<string, string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  snack: '加餐',
}

/** 门票是否属于「估算」 */
const ticketEstimated = computed(() =>
  isEstimated(props.sources, 'attraction.ticket_price'),
)
/** 餐饮费用是否属于「估算」 */
const mealEstimated = computed(() =>
  isEstimated(props.sources, 'meal.estimated_cost'),
)

function weekday(date: string): string {
  return '周' + '日一二三四五六'[dayjs(date).day()]
}

function monthShort(date: string): string {
  return dayjs(date).format('M月')
}

function dayOfMonth(date: string): string {
  return dayjs(date).format('D')
}

function mealLabel(type: string): string {
  return MEAL_LABELS[type] ?? type
}

/**
 * 金额展示态。★ 三种「没有正数金额」的情况必须分开渲染：
 *   unknown  金额是 null/undefined —— 不知道多少钱
 *   free     金额 === 0 ------------ 真免费
 *   priced   有金额
 *
 * 以前的写法是 `v-if="spot.ticket_price"` / `v-if="meal.estimated_cost"`：
 * 金额为 null 时整块隐藏，于是「不知道」在界面上看起来和「不要钱」一模一样 ——
 * v0.7 要修的就是这个。门票和餐费共用同一套判定，免得两处又跑偏。
 */
function amountState(value: number | null | undefined): 'unknown' | 'free' | 'priced' {
  if (value === null || value === undefined) return 'unknown'
  return value === 0 ? 'free' : 'priced'
}

/**
 * 门票那行的「预估」标签优先看**这一项自己的**三态（v0.7 起后端逐项标了）；
 * 只有字段缺失（老响应）时才回退到全局的 sources 映射。
 */
function ticketEstimatedFor(spot: Attraction): boolean {
  if (spot.ticket_price_status) return spot.ticket_price_status === 'estimated'
  return ticketEstimated.value
}

/**
 * 当天有几项费用金额未知（门票 + 餐饮）。
 * 当日小计只含已知与估算，所以有小计的地方就得说明它漏了几项，
 * 否则「当日小计 ¥320」会被读成「这天就花这些钱」。
 */
function unknownCostCount(day: DayPlan): number {
  const tickets = day.attractions.filter(
    (spot) => amountState(spot.ticket_price) === 'unknown',
  ).length
  const meals = day.meals.filter((meal) => (meal.estimated_cost ?? null) === null).length
  return tickets + meals
}
</script>

<template>
  <section class="timeline">
    <article v-for="day in days" :key="day.date" class="day-card">
      <!-- 左侧日期块 -->
      <div class="date-block">
        <div class="date-month">{{ monthShort(day.date) }}</div>
        <div class="date-day">{{ dayOfMonth(day.date) }}</div>
        <div class="date-weekday">{{ weekday(day.date) }}</div>
        <div class="date-index">第 {{ day.day_index + 1 }} 天</div>
      </div>

      <!-- 右侧内容 -->
      <div class="day-body">
        <h3 v-if="day.description" class="day-desc">{{ day.description }}</h3>
        <div class="day-meta">
          <span v-if="day.transportation" class="meta-item">
            <Route :size="14" />
            {{ day.transportation }}
          </span>
          <span v-if="day.accommodation" class="meta-item">
            <Hotel :size="14" />
            {{ day.accommodation }}
          </span>
        </div>

        <!-- 住宿（v0.2：不再是每天一个，统一在顶部 TripSummary 里显示）-->

        <!-- 景点 -->
        <div class="block">
          <div class="block-title">
            <MapPin :size="15" />
            景点
            <span class="count">{{ day.attractions.length }} 处</span>
          </div>
          <div v-if="day.attractions.length" class="spots">
            <div
              v-for="(spot, index) in day.attractions"
              :key="spot.poi_id || spot.name"
              class="spot"
            >
              <div class="spot-index">{{ index + 1 }}</div>
              <div class="spot-body">
                <div class="spot-head">
                  <span class="spot-name">{{ spot.name }}</span>
                  <span v-if="spot.category" class="spot-category">{{ spot.category }}</span>
                </div>
                <div class="spot-meta">
                  <span class="meta-item">
                    <Clock :size="13" />
                    {{ spot.visit_duration }} 分钟
                  </span>
                  <!-- v0.7：三种状态三种文案。null 说「待查」而不是什么都不显示 ——
                       藏起来会让「未知」看起来像「免费」 -->
                  <span v-if="amountState(spot.ticket_price) === 'unknown'" class="meta-item">
                    <Ticket :size="13" />
                    <span class="muted">门票待查</span>
                  </span>
                  <span v-else-if="amountState(spot.ticket_price) === 'free'" class="meta-item">
                    <Ticket :size="13" />
                    免费
                  </span>
                  <span v-else class="meta-item">
                    <Ticket :size="13" />
                    ¥{{ spot.ticket_price }}
                    <span v-if="ticketEstimatedFor(spot)" class="tag-estimated">预估</span>
                  </span>
                  <span v-if="spot.rating" class="meta-item">评分 {{ spot.rating }}</span>
                </div>
                <p v-if="spot.description" class="spot-desc">{{ spot.description }}</p>
                <div v-if="spot.address" class="spot-address muted">{{ spot.address }}</div>
                <a v-if="spot.ticket_price_evidence_url && /^https:\/\//.test(spot.ticket_price_evidence_url)" :href="spot.ticket_price_evidence_url" target="_blank" rel="noopener noreferrer">门票资料出处</a>
              </div>
            </div>
          </div>
          <div v-else class="empty">当日无景点安排</div>
        </div>

        <!-- 三餐 -->
        <div class="block">
          <div class="block-title">
            <Utensils :size="15" />
            餐饮
          </div>
          <div v-if="day.meals.length" class="meals">
            <div v-for="meal in day.meals" :key="meal.type" class="meal">
              <span class="meal-type">{{ mealLabel(meal.type) }}</span>
              <span class="meal-name">{{ meal.name }}</span>
              <!-- 餐费同样分三态：没给金额就是「待查」，不能什么都不显示（那和免费没区别） -->
              <span v-if="meal.cost_status === 'unknown' || amountState(meal.estimated_cost) === 'unknown'" class="meal-cost muted">
                费用待查
              </span>
              <span v-else-if="amountState(meal.estimated_cost) === 'free'" class="meal-cost">
                免费
              </span>
              <span v-else class="meal-cost">
                ¥{{ meal.estimated_cost }}
                <span v-if="meal.cost_status ? meal.cost_status === 'estimated' : mealEstimated" class="tag-estimated">参考人均</span>
              </span>
              <span v-if="meal.description" class="meal-desc muted">
                {{ meal.description }}
              </span>
              <span v-if="meal.address" class="meal-desc muted">{{ meal.address }}</span>
              <span v-if="meal.ranking_name" class="spot-category">{{ meal.ranking_name }}{{ meal.ranking_rank ? ` · 第 ${meal.ranking_rank} 位` : '' }}</span>
              <a v-if="meal.ranking_url && /^https:\/\/(www\.)?amap\.com\//.test(meal.ranking_url)" :href="meal.ranking_url" target="_blank" rel="noopener noreferrer">查看榜单出处</a>
              <span v-if="meal.cost_note" class="meal-desc muted">{{ meal.cost_note }}</span>
              <a v-if="meal.evidence_url && /^https:\/\//.test(meal.evidence_url)" :href="meal.evidence_url" target="_blank" rel="noopener noreferrer">人均消费资料出处</a>
            </div>
          </div>
          <div v-else class="empty">未安排餐饮</div>
        </div>

        <div v-if="day.schedule?.length" class="block">
          <div class="block-title"><Clock :size="15" />时间安排<span class="count">含路程、用餐与休息</span></div>
          <ol class="schedule-list"><li v-for="(item, index) in day.schedule" :key="index"><time>{{ item.start_time }}—{{ item.end_time }}</time><span>{{ item.name }}</span><small>{{ item.duration_minutes }} 分钟{{ item.status === 'unknown' ? ' · 待核实' : item.status === 'estimated' ? ' · 建议' : '' }}</small></li></ol>
          <p class="muted">游玩 {{ day.visit_minutes }} 分钟 · 路程 {{ day.travel_minutes }} 分钟 · 休息 {{ day.rest_minutes }} 分钟 · 全日 {{ day.total_minutes }} 分钟</p>
        </div>
        <div v-if="day.route_legs?.length" class="block">
          <div class="block-title"><Route :size="15" />逐段路程</div>
          <div v-for="(leg, index) in day.route_legs" :key="index" class="route-leg"><span>{{ leg.from_name }} → {{ leg.to_name }}</span><strong>{{ leg.status === 'unknown' ? '耗时待查' : `${leg.duration_minutes} 分钟` }}</strong><small>{{ leg.mode }}{{ leg.distance_meters == null ? '' : ` · ${(leg.distance_meters / 1000).toFixed(1)} 公里` }}{{ leg.status === 'estimated' ? ' · 估算' : '' }}</small><small v-if="leg.note">{{ leg.note }}</small></div>
        </div>
        <p v-for="warning in day.comfort_warnings ?? []" :key="warning" class="comfort-warning">{{ warning }}</p>
        <!-- 当日小计 -->
        <!-- 当日小计：后端只把已知 + 估算摊进来，含未知项时必须点明，否则会读成当天全部开销 -->
        <div v-if="day.day_total_cost !== undefined" class="day-total">
          当日小计 <strong>¥{{ day.day_total_cost }}</strong>
          <span v-if="unknownCostCount(day)" class="day-total-note">
            （不含 {{ unknownCostCount(day) }} 项金额未知）
          </span>
        </div>
      </div>
    </article>

    <div v-if="!days.length" class="empty-days">没有行程数据</div>
  </section>
</template>

<style scoped>
.schedule-list{padding:0;list-style:none;font-size:13px}.schedule-list li{display:flex;gap:12px;flex-wrap:wrap;padding:8px 0;border-bottom:1px solid #f0f2f6}.schedule-list time{font-variant-numeric:tabular-nums;color:#3566c9;min-width:105px}.schedule-list small,.route-leg small{color:#788598}.route-leg{display:flex;gap:10px;flex-wrap:wrap;padding:9px 12px;margin:6px 0;background:#f7f9fc;border-radius:7px;font-size:13px}.route-leg strong{font-weight:500}.comfort-warning{background:#fff6e6;padding:9px 12px;border-radius:6px;font-size:13px;color:#8a6100}
.timeline {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.day-card {
  display: flex;
  gap: 18px;
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 12px;
  padding: 18px 20px;
}

.date-block {
  flex: 0 0 76px;
  text-align: center;
  border-right: 1px solid #f0f2f6;
  padding-right: 16px;
}

.date-month {
  font-size: 12px;
  color: #8c94a3;
}

.date-day {
  font-size: 30px;
  font-weight: 650;
  line-height: 1.15;
  color: #1b1f27;
}

.date-weekday {
  font-size: 12px;
  color: #8c94a3;
  margin-top: 2px;
}

.date-index {
  margin-top: 6px;
  font-size: 11px;
  color: #3566c9;
  background: #eef4ff;
  border-radius: 4px;
  padding: 2px 0;
}

.day-body {
  flex: 1;
  min-width: 0;
}

.day-desc {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 600;
  color: #1b1f27;
  line-height: 1.6;
}

.day-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-bottom: 6px;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #5a6272;
}

.block {
  margin-top: 16px;
}

.block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #1b1f27;
  margin-bottom: 10px;
}

.count {
  font-weight: 400;
  color: #8c94a3;
}

.spots {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.spot {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  background: #f7f9fc;
  border-radius: 10px;
}

.spot-index {
  flex: 0 0 20px;
  height: 20px;
  border-radius: 50%;
  background: #3566c9;
  color: #fff;
  font-size: 12px;
  line-height: 20px;
  text-align: center;
}

.spot-body {
  flex: 1;
  min-width: 0;
}

.spot-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.spot-name {
  font-size: 14px;
  font-weight: 600;
  color: #1b1f27;
}

.spot-category {
  font-size: 11px;
  color: #3566c9;
  background: #eef4ff;
  border-radius: 4px;
  padding: 1px 6px;
}

.spot-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 6px;
}

.spot-desc {
  margin: 8px 0 0;
  font-size: 13px;
  color: #5a6272;
  line-height: 1.8;
}

.spot-address {
  margin-top: 6px;
  font-size: 12px;
}

.hotel {
  padding: 12px 14px;
  background: #f7f9fc;
  border-radius: 10px;
}

.hotel-name {
  font-size: 14px;
  font-weight: 600;
  color: #1b1f27;
}

.hotel-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 6px;
  font-size: 12px;
}

.hotel-price {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 13px;
  color: #1b1f27;
}

.meals {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.meal {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: #f7f9fc;
  border-radius: 10px;
  font-size: 13px;
}

.meal-type {
  flex: 0 0 auto;
  font-size: 11px;
  color: #5a6272;
  background: #eceef2;
  border-radius: 4px;
  padding: 1px 6px;
}

.meal-name {
  font-weight: 600;
  color: #1b1f27;
}

.meal-cost {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #1b1f27;
}

.meal-desc {
  font-size: 12px;
}

.muted {
  color: #8c94a3;
}

.tag-estimated {
  font-size: 11px;
  font-weight: 400;
  color: #8a6100;
  background: #fff3d6;
  border-radius: 4px;
  padding: 1px 6px;
}

.empty {
  padding: 12px 14px;
  background: #f7f9fc;
  border-radius: 10px;
  color: #8c94a3;
  font-size: 13px;
}

.empty-days {
  padding: 40px;
  text-align: center;
  color: #8c94a3;
}

.day-total {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed #eceef2;
  text-align: right;
  font-size: 13px;
  color: #8c94a3;
}

.day-total strong {
  font-size: 15px;
  color: #1b1f27;
  margin-left: 4px;
}

.day-total-note {
  margin-left: 4px;
  font-size: 12px;
  color: #a8b0be;
}

@media (max-width: 720px) {
  .day-card {
    flex-direction: column;
  }

  .date-block {
    flex: none;
    display: flex;
    align-items: baseline;
    gap: 8px;
    border-right: none;
    border-bottom: 1px solid #f0f2f6;
    padding: 0 0 10px;
  }

  .date-day {
    font-size: 20px;
  }

  .date-index {
    margin: 0 0 0 auto;
    padding: 2px 8px;
  }
}
</style>
