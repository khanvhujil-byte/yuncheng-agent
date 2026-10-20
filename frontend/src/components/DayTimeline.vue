<script setup lang="ts">
import dayjs from 'dayjs'
import { ArrowDown, MapPin, Route, Ticket, Utensils } from 'lucide-vue-next'
import TripMap from '@/components/TripMap.vue'
import type { Attraction, DayPlan, Hotel, Meal, SourceMap } from '@/types/trip'
import { dayStops, routeText } from '@/utils/itinerary'
import { ticketText, ticketReason, ticketUnverified } from '@/utils/tickets'
defineProps<{ days: DayPlan[]; sources?: SourceMap; hotel?: Hotel | null }>()
const MEALS: Record<string, string> = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '加餐' }
function mealText(m: Meal) {
  if (m.estimated_cost == null || m.cost_status === 'unknown') return '人均待核实'
  return `¥${m.estimated_cost} / 人 · ${m.cost_source === 'static_estimate' ? '预留' : '参考'}`
}
function unknownCount(d: DayPlan) {
  return d.attractions.filter(ticketUnverified).length + d.meals.filter(m => m.estimated_cost == null || m.cost_status === 'unknown').length
}
</script>

<template>
  <section class="timeline">
    <article v-for="day in days" :key="day.date" class="day-card">
      <header class="day-heading">
        <span class="day-number">DAY {{ String(day.day_index + 1).padStart(2, '0') }}</span>
        <div><h3>{{ day.description || '今日行程' }}</h3><p>{{ dayjs(day.date).format('M月D日') }} · {{ '周' + '日一二三四五六'[dayjs(day.date).day()] }}</p></div>
        <span v-if="day.transportation" class="day-transport"><Route :size="14" />{{ day.transportation }}</span>
      </header>
      <p v-if="day.dated_boundaries" class="stay-transition"><strong>{{ day.hotel_change ? '今日换住' : '今日动线' }}</strong> · {{ day.route_start?.name || '到达后的首个活动' }} → {{ day.route_end?.name || '末个活动（离开交通待补充）' }}<small v-if="day.hotel_change"> · 含 {{ day.transfer_minutes }} 分钟退房、行李与入住预留</small></p>
      <TripMap :days="[day]" :hotel="hotel" embedded />
      <div class="journey-stops">
        <article v-for="(stop, index) in dayStops(day, hotel)" :key="stop.key" class="place-card" :class="{ 'food-stop': stop.kind === 'meal' }">
          <div v-if="stop.incoming" class="arrival-route"><ArrowDown :size="14" /><span>从 {{ stop.incoming.from_name }} 出发</span><strong>{{ routeText(stop.incoming) }}</strong></div>
          <div class="place-content">
            <span class="place-number">{{ index + 1 }}</span>
            <div class="place-main">
              <div class="place-heading">
                <span class="place-kind"><Utensils v-if="stop.kind === 'meal'" :size="13" /><MapPin v-else :size="13" />{{ stop.kind === 'meal' ? (MEALS[(stop.place as Meal).type] || '餐饮') : '景点' }}</span>
                <span v-if="stop.kind === 'attraction' && (stop.place as Attraction).required" class="required-tag">必去</span>
                <span class="place-cost"><Ticket v-if="stop.kind === 'attraction'" :size="13" />{{ stop.kind === 'attraction' ? ticketText(stop.place as Attraction) : mealText(stop.place as Meal) }}</span>
              </div>
              <h4>{{ stop.place.name }}</h4>
              <div v-if="stop.kind === 'attraction'" class="place-facts"><span v-if="(stop.place as Attraction).category">{{ (stop.place as Attraction).category }}</span><span>建议游玩 {{ (stop.place as Attraction).visit_duration }} 分钟</span><span v-if="(stop.place as Attraction).rating">评分 {{ (stop.place as Attraction).rating }}</span></div>
              <p v-if="stop.place.description" class="place-description">{{ stop.place.description }}</p>
              <p v-if="stop.kind === 'attraction'" class="place-address">{{ ticketReason(stop.place as Attraction) }}</p>
              <p v-if="stop.place.address" class="place-address"><MapPin :size="12" />{{ stop.place.address }}</p>
              <template v-if="stop.kind === 'meal'">
                <span v-if="(stop.place as Meal).ranking_name" class="ranking-tag">{{ (stop.place as Meal).ranking_name }}{{ (stop.place as Meal).ranking_rank ? ` · 第 ${(stop.place as Meal).ranking_rank} 位` : '' }}</span>
                <details v-if="(stop.place as Meal).cost_note || (stop.place as Meal).ranking_url || (stop.place as Meal).evidence_url" class="place-evidence"><summary>价格与推荐依据</summary><p>{{ (stop.place as Meal).cost_note }}</p><a v-if="/^https:\/\/(www\.)?amap\.com\//.test((stop.place as Meal).ranking_url || '')" :href="(stop.place as Meal).ranking_url!" target="_blank" rel="noopener noreferrer">榜单出处</a><a v-if="/^https:\/\//.test((stop.place as Meal).evidence_url || '')" :href="(stop.place as Meal).evidence_url!" target="_blank" rel="noopener noreferrer">人均资料</a></details>
              </template>
              <a v-else-if="/^https:\/\//.test((stop.place as Attraction).ticket_price_evidence_url || '')" class="source-link" :href="(stop.place as Attraction).ticket_price_evidence_url!" target="_blank" rel="noopener noreferrer">门票资料出处</a>
            </div>
          </div>
          <div v-if="stop.returnToHotel" class="return-route"><Route :size="13" /><span>前往 {{ stop.returnToHotel.to_name }}</span><strong>{{ routeText(stop.returnToHotel) }}</strong></div>
        </article>
      </div>
      <details v-if="day.comfort_warnings?.length" class="day-notes"><summary>当日出行提醒 · {{ day.comfort_warnings.length }} 条</summary><p v-for="warning in day.comfort_warnings" :key="warning">{{ warning }}</p></details>
      <footer v-if="day.day_total_cost !== undefined" class="day-total"><span>当日参考小计 <small v-if="unknownCount(day)">另有 {{ unknownCount(day) }} 项价格待核实</small></span><strong>¥{{ day.day_total_cost }}</strong></footer>
    </article>
    <p v-if="!days.length" class="empty-days">暂无行程安排</p>
  </section>
</template>

<style scoped>
.stay-transition{font-size:12px;line-height:1.8;color:var(--yc-text);background:var(--yc-page);padding:10px 12px;border-radius:8px}.stay-transition small{color:var(--yc-muted)}
.timeline{display:grid;gap:24px}.day-card{display:block;border:1px solid var(--yc-border);border-radius:16px;background:var(--yc-surface);padding:22px}.day-heading{display:flex;align-items:center;gap:14px;margin-bottom:18px}.day-number{font-size:12px;letter-spacing:.1em;color:var(--yc-text);background:var(--yc-soft);padding:10px 12px;border-radius:8px;white-space:nowrap}.day-heading h3{font-size:16px;font-weight:600;margin:0;color:var(--yc-ink);line-height:1.5}.day-heading p{font-size:12px;color:var(--yc-muted);margin:5px 0 0}.day-transport{margin-left:auto;display:flex;align-items:center;gap:5px;color:var(--yc-muted);font-size:12px;white-space:nowrap}.journey-stops{display:grid;gap:12px;margin-top:18px}.place-card{border:1px solid var(--yc-soft);border-radius:12px;overflow:hidden;background:white}.arrival-route,.return-route{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:12px;color:var(--yc-muted);padding:10px 16px;background:var(--yc-page)}.arrival-route strong,.return-route strong{font-weight:500;color:var(--yc-text);margin-left:auto}.place-content{display:flex;gap:13px;padding:18px}.place-number{display:grid;place-items:center;flex:0 0 27px;height:27px;font-size:12px;color:var(--yc-text);background:var(--yc-soft);border-radius:50%;margin-top:2px}.food-stop .place-number{background:#f8eee0;color:#aa8554}.place-main{min-width:0;flex:1}.place-heading{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.place-kind{display:flex;align-items:center;gap:5px;color:var(--yc-muted);font-size:11px}.food-stop .place-kind{color:#ad8b58}.place-cost{margin-left:auto;font-size:12px;color:var(--yc-text);display:flex;align-items:center;gap:4px}.place-main h4{font-size:17px;color:var(--yc-ink);margin:8px 0;line-height:1.4}.place-facts{display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--yc-muted)}.place-description{font-size:13px;line-height:1.8;color:var(--yc-text);margin:10px 0}.place-address{display:flex;align-items:baseline;gap:5px;color:var(--yc-muted);font-size:11px;line-height:1.6;margin:7px 0}.place-address svg{flex-shrink:0}.required-tag,.ranking-tag{font-size:10px;color:#a27a41;background:#faf0df;padding:3px 7px;border-radius:4px}.place-evidence{margin-top:10px;font-size:11px;color:var(--yc-muted)}.place-evidence summary{cursor:pointer}.place-evidence p{line-height:1.6}.place-evidence a,.source-link{font-size:11px;color:var(--yc-muted);margin-right:12px;display:inline-block;margin-top:7px}.return-route{border-top:1px dashed var(--yc-border)}.day-notes{margin-top:14px;font-size:12px;color:#9a855e}.day-notes summary{cursor:pointer}.day-notes p{line-height:1.7;margin:7px 0}.day-total{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--yc-soft);padding-top:16px;margin-top:18px;font-size:12px;color:var(--yc-muted)}.day-total strong{font-size:20px;font-weight:500;color:var(--yc-text)}.day-total small{display:block;font-size:10px;margin-top:4px}.empty-days{text-align:center;color:var(--yc-muted);padding:25px}@media(max-width:640px){.day-card{padding:16px}.day-heading{gap:10px;flex-wrap:wrap}.day-heading h3{font-size:14px}.day-transport{display:none}.place-content{padding:14px;gap:9px}.place-cost{font-size:11px}.arrival-route strong,.return-route strong{margin-left:0}.day-number{padding:8px;font-size:10px}.place-main h4{font-size:16px}}
</style>
