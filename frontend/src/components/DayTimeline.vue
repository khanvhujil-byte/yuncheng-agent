<script setup lang="ts">
/**
 * 逐日行程时间轴。
 */
import dayjs from 'dayjs'
import { Clock, Hotel, MapPin, Route, Ticket, Utensils } from 'lucide-vue-next'
import { computed } from 'vue'

import type { DayPlan, SourceMap } from '@/types/trip'
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
                  <span v-if="spot.ticket_price" class="meta-item">
                    <Ticket :size="13" />
                    ¥{{ spot.ticket_price }}
                    <span v-if="ticketEstimated" class="tag-estimated">预估</span>
                  </span>
                  <span v-if="spot.rating" class="meta-item">评分 {{ spot.rating }}</span>
                </div>
                <p v-if="spot.description" class="spot-desc">{{ spot.description }}</p>
                <div v-if="spot.address" class="spot-address muted">{{ spot.address }}</div>
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
              <span v-if="meal.estimated_cost" class="meal-cost">
                ¥{{ meal.estimated_cost }}
                <span v-if="mealEstimated" class="tag-estimated">预估</span>
              </span>
              <span v-if="meal.description" class="meal-desc muted">
                {{ meal.description }}
              </span>
            </div>
          </div>
          <div v-else class="empty">未安排餐饮</div>
        </div>

        <!-- 当日小计 -->
        <div v-if="day.day_total_cost" class="day-total">
          当日小计 <strong>¥{{ day.day_total_cost }}</strong>
        </div>
      </div>
    </article>

    <div v-if="!days.length" class="empty-days">没有行程数据</div>
  </section>
</template>

<style scoped>
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
