<script setup lang="ts">
import { computed } from 'vue'
import dayjs from 'dayjs'
import { CloudSun, Hotel, MapPin, Ticket, TriangleAlert, Wallet } from 'lucide-vue-next'
import ExportPanel from '@/components/ExportPanel.vue'
import IntercitySummary from '@/components/IntercitySummary.vue'
import type { CostItem, TripPlan } from '@/types/trip'
import { ticketReason } from '@/utils/tickets'
const props = defineProps<{ plan: TripPlan; cached?: boolean; cacheHitCount?: number; cacheSavedAt?: string | null; preferences?: string[]; hideActions?: boolean; editable?: boolean }>()
const emit = defineEmits<{ editStay: [dayIndex: number] }>()
const regionalDays = computed(() => (props.plan.regional_weather ?? []).filter(w => props.plan.days.some(d => d.date === w.date && d.attractions.some(a => a.poi_id === w.poi_id))))
const stayGroups = computed(() => {
  const groups: { check_in: string; check_out: string; hotel: NonNullable<TripPlan['hotel']>; nights: number; locked: boolean; reason: string }[] = []
  for (const stay of props.plan.stays ?? []) {
    const last = groups[groups.length - 1]
    if (last && last.check_out === stay.check_in && (last.hotel.poi_id || last.hotel.name) === (stay.hotel.poi_id || stay.hotel.name) && last.locked === stay.locked && last.hotel.estimated_cost === stay.hotel.estimated_cost && last.hotel.cost_status === stay.hotel.cost_status) {
      last.check_out = stay.check_out; last.nights++
    } else groups.push({ ...stay, nights: 1 })
  }
  return groups
})
const hotel = computed(() => props.plan.hotel)
const budget = computed(() => props.plan.budget)
const tickets = computed<(CostItem & { reason: string; evidence_url?: string | null })[]>(() => props.plan.days.flatMap(day => day.attractions.map(spot => ({
  ...(budget.value?.items?.find(item => item.category === 'attraction' && item.day_index === day.day_index && item.name === spot.name) ??
  { category: 'attraction', name: spot.name, day_index: day.day_index, amount: spot.ticket_price == null ? null : spot.ticket_price * (props.plan.adult_count || 1),
    status: spot.ticket_price == null ? 'unknown' : spot.ticket_price_status ?? 'estimated', source: spot.ticket_price_source ?? 'unknown' }),
  reason: ticketReason(spot), evidence_url: spot.ticket_price_evidence_url,
}))))
const categories = computed(() => [
  { name: '门票', amount: budget.value?.total_attractions, category: 'attraction' },
  { name: '住宿', amount: budget.value?.total_hotels, category: 'hotel' },
  { name: '餐饮', amount: budget.value?.total_meals, category: 'meal' },
  { name: '交通', amount: budget.value?.total_transportation, category: 'transportation' },
])
const verdict = computed(() => ({ within: '按参考费用在预算内', known_exceeded: '已知费用已超预算', estimated_exceeded: '参考费用可能超预算', unverified: '部分价格待核实', none: '' })[budget.value?.limit_status ?? 'none'])
function unknown(category: string) { return (budget.value?.items ?? []).filter(i => i.category === category && (i.status === 'unknown' || i.amount == null || (category === 'attraction' && i.source === 'static_estimate'))).length }
function money(item: CostItem) {
  if (item.source === 'venue_policy') return '普通入场无门票预算'
  if (item.status === 'unknown' || item.amount == null) return '待核实'
  if (item.source === 'static_estimate') return `待核实 · 预留 ¥${item.amount}`
  return `${item.amount === 0 ? '免费' : `¥${item.amount}`}${item.status === 'estimated' ? ' · 参考' : ''}`
}
function temp(value: number | string | undefined) { return value == null || value === '' ? '—' : `${value}°` }
</script>

<template>
  <section class="summary">
    <header class="trip-heading"><div><h2><MapPin :size="20" />{{ plan.city }} · {{ plan.days.length }} 日行程</h2><p>{{ plan.start_date }} — {{ plan.end_date }}<span v-if="cached"> · 复用历史行程，已重新核验日期相关资料</span></p></div></header>
    <div class="overview-grid">
      <section class="overview-card hotel-overview">
        <h3><Hotel :size="16" />住宿<span v-if="plan.sources?.hotel === 'user'" class="small-tag">你指定的</span></h3>
        <div v-if="stayGroups.length" class="stay-groups"><article v-for="stay in stayGroups" :key="stay.check_in" class="stay-segment"><span class="stay-dates">{{ dayjs(stay.check_in).format('M/D') }} 入住 — {{ dayjs(stay.check_out).format('M/D') }} 退房 · {{ stay.nights }} 晚<span v-if="stay.locked"> · 已锁定</span></span><h4>{{ stay.hotel.name }}</h4><p class="quiet">{{ stay.hotel.address }}</p><p class="stay-amount">{{ stay.hotel.cost_status === 'unknown' ? '房价待核实' : `¥${stay.hotel.estimated_cost} / 房晚 · ${stay.hotel.cost_source === 'static_estimate' ? '预算预留' : '参考'}` }}</p><details v-if="stay.reason" class="compact-detail"><summary>住宿安排依据</summary><p>{{ stay.reason }}</p><p>{{ stay.hotel.price_note }}</p><a v-if="/^https:\/\//.test(stay.hotel.provider_url || stay.hotel.price_evidence_url || '')" :href="(stay.hotel.provider_url || stay.hotel.price_evidence_url)!" target="_blank" rel="noopener noreferrer">查看平台住宿资料</a></details><button v-if="editable && !stay.locked" class="stay-edit" @click="emit('editStay', plan.days.findIndex(d => d.date === stay.check_in))">修改这段住宿</button></article><p v-if="plan.stay_comparison?.hotel_changes" class="quiet">换住 {{ plan.stay_comparison.hotel_changes }} 次，已计入 {{ plan.stay_comparison.transfer_minutes }} 分钟换住时间预留。</p></div>
        <template v-else-if="hotel"><h4>{{ hotel.name }}</h4><p class="quiet">{{ hotel.address }}</p><div class="hotel-facts"><span>{{ plan.hotel_nights ?? 0 }} 晚</span><span v-if="hotel.rating">评分 {{ hotel.rating }}</span><span v-if="hotel.type">{{ hotel.type }}</span></div><p v-if="hotel.cost_status !== 'unknown' && (hotel.estimated_cost || hotel.cost_status === 'known')" class="hotel-price">¥{{ hotel.estimated_cost }}<small>/ 晚{{ hotel.cost_status === 'estimated' ? ' · 参考' : '' }}</small></p><p v-else class="quiet">房价待核实</p><details v-if="plan.hotel_reason || hotel.distance || hotel.price_evidence_url" class="compact-detail"><summary>选择理由与价格依据</summary><p>{{ plan.hotel_reason }}</p><p>{{ hotel.distance }}</p><p>{{ hotel.price_note }}</p><a v-if="/^https:\/\//.test(hotel.price_evidence_url || '')" :href="hotel.price_evidence_url!" target="_blank" rel="noopener noreferrer">房价资料</a></details></template>
        <p v-else class="quiet">{{ plan.hotel_nights === 0 ? '本次行程无需住宿' : '尚未确定住宿' }}</p>
      </section>
      <section class="overview-card weather-overview">
        <h3><CloudSun :size="16" />天气</h3>
        <div v-if="plan.weather_info?.length" class="weather-days"><div v-for="w in plan.weather_info" :key="w.date" class="weather-day"><span>{{ dayjs(w.date).format('M/D') }}<small>{{ '周' + '日一二三四五六'[dayjs(w.date).day()] }}</small></span><strong>{{ w.beyond_window ? '暂无预报' : w.day_weather || '待查询' }}</strong><span class="weather-temp">{{ w.beyond_window ? '—' : `${temp(w.day_temp)} / ${temp(w.night_temp)}` }}</span></div></div>
        <p v-else class="quiet">暂未获取天气，出发前请再查询。</p>
        <details v-if="regionalDays.length" class="compact-detail"><summary>周边活动区域天气</summary><p v-for="w in regionalDays" :key="`${w.poi_id}-${w.date}`">{{ dayjs(w.date).format('M/D') }} · {{ w.name }}：{{ w.district_forecast?.day_weather ? `所在行政区预报 ${w.district_forecast.day_weather}（仅作参考）` : '现场天气待核实' }}。{{ w.note }}</p></details>
      </section>
      <section class="overview-card budget-overview">
        <h3><Wallet :size="16" />预算<span class="small-tag">{{ plan.adult_count || 1 }} 位成人 · {{ plan.room_count || 1 }} 间房</span></h3>
        <template v-if="budget"><div class="budget-top"><strong>¥{{ budget.total ?? 0 }}</strong><span>{{ verdict || '参考费用合计' }}<small v-if="budget.limit != null">预算上限 ¥{{ budget.limit }}</small></span></div><div class="budget-categories"><div v-for="c in categories" :key="c.name"><span>{{ c.name }}</span><strong>{{ c.amount == null || (unknown(c.category) && c.amount === 0) ? '待核实' : `¥${c.amount}` }}</strong><small v-if="unknown(c.category)">{{ unknown(c.category) }} 项待核实</small></div></div></template>
        <p v-else class="quiet">预算尚未汇总</p>
        <div class="ticket-breakdown"><h4><Ticket :size="13" />门票明细</h4><div v-for="(ticket, index) in tickets" :key="`${ticket.day_index}-${index}`" class="ticket-row"><span>{{ ticket.name }}<details v-if="ticket.reason || ticket.evidence_url" class="compact-detail"><summary>票价依据</summary><p>{{ ticket.reason }}</p><a v-if="/^https:\/\//.test(ticket.evidence_url || '')" :href="ticket.evidence_url!" target="_blank" rel="noopener noreferrer">查看原始资料</a></details></span><strong>{{ money(ticket) }}</strong></div><p v-if="!tickets.length" class="quiet">暂无景点门票项目</p></div>
        <details v-if="budget" class="compact-detail"><summary>其他费用明细与预算依据</summary><p>餐饮、门票按一人计，住宿按一间房计。预留金额是预算准备，不是景点报价。</p><div v-for="(item, index) in (budget.items ?? []).filter(i => i.category !== 'attraction')" :key="index" class="ticket-row"><span>{{ item.name }}</span><strong>{{ money(item) }}</strong></div><p v-if="budget.unknown_items?.length">尚未计入的未知费用：{{ budget.unknown_items.join('、') }}</p><p>{{ budget.limit_basis }}</p></details>
      </section>
    </div>
    <IntercitySummary :plan="plan" />
    <details v-if="plan.warnings?.length || plan.constraint_report?.unsatisfied?.length" class="trip-notes"><summary><TriangleAlert :size="14" />出行提醒与待核实信息 <span>{{ plan.warnings?.length ?? 0 }} 条</span></summary><ul><li v-for="(warning, index) in plan.warnings ?? []" :key="index">{{ warning }}</li><li v-for="name in plan.constraint_report?.unsatisfied ?? []" :key="name">必去景点尚未安排：{{ name }}。{{ plan.constraint_report?.reasons?.[name] }}</li></ul></details>
    <details v-if="plan.city_characteristics?.length || plan.overall_suggestions" class="city-guide"><summary>城市特点与旅行建议</summary><p v-for="text in plan.city_characteristics ?? []" :key="text">{{ text }}</p><p>{{ plan.overall_suggestions }}</p></details>
    <ExportPanel v-if="!hideActions" :plan="plan" :preferences="preferences" />
  </section>
</template>

<style scoped>
.stay-edit{margin-top:10px;padding:6px 10px;border:1px solid var(--yc-border);border-radius:6px;color:var(--yc-text);background:var(--yc-page);font-size:12px;cursor:pointer}
.stay-groups{display:grid;gap:13px}.stay-segment+.stay-segment{border-top:1px solid var(--yc-soft);padding-top:13px}.stay-dates{display:block;font-size:10px;color:var(--yc-muted);margin-bottom:7px}.stay-amount{font-size:12px;color:var(--yc-text);margin:8px 0}
.summary{display:grid;gap:16px}.trip-heading h2{display:flex;align-items:center;gap:8px;font-size:24px;font-weight:500;color:var(--yc-ink);margin:0}.trip-heading p{font-size:12px;color:var(--yc-muted);margin:9px 0 0;line-height:1.6}.overview-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.9fr) minmax(0,1.45fr);gap:12px;align-items:start}.overview-card{min-width:0;padding:18px;background:var(--yc-surface);border:1px solid var(--yc-border);border-radius:12px}.overview-card h3{display:flex;gap:7px;align-items:center;margin:0 0 17px;font-size:13px;font-weight:500;color:var(--yc-text)}.overview-card h4{font-size:16px;line-height:1.5;margin:0 0 7px;color:var(--yc-text)}.quiet{font-size:11px;line-height:1.7;color:var(--yc-muted);margin:7px 0}.small-tag{font-size:9px;background:var(--yc-soft);padding:3px 5px;border-radius:4px;color:var(--yc-muted);margin-left:auto}.hotel-facts{display:flex;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--yc-muted);margin-top:12px}.hotel-price{font-size:23px;color:var(--yc-text);margin:15px 0 0}.hotel-price small{font-size:10px;color:var(--yc-muted);margin-left:5px}.weather-days{display:grid;gap:12px}.weather-day{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--yc-muted)}.weather-day>span:first-child{min-width:32px}.weather-day small{display:block;font-size:9px;margin-top:3px;color:var(--yc-muted)}.weather-day strong{font-weight:500;flex:1}.weather-temp{font-size:11px;color:var(--yc-muted);text-align:right;white-space:nowrap}.budget-top{display:flex;align-items:center;gap:12px;margin-bottom:16px}.budget-top>strong{font-size:30px;color:var(--yc-text);font-weight:500}.budget-top>span{font-size:10px;color:var(--yc-muted);line-height:1.6}.budget-top small{display:block;font-size:9px}.budget-categories{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;background:var(--yc-page);border-radius:8px;padding:11px 8px}.budget-categories>div{min-width:0;text-align:center;display:grid;gap:5px}.budget-categories span{font-size:10px;color:var(--yc-muted)}.budget-categories strong{font-size:13px;font-weight:500;color:var(--yc-text)}.budget-categories small{font-size:8px;color:#b39462}.ticket-breakdown{margin-top:15px}.ticket-breakdown h4{display:flex;align-items:center;gap:5px;font-size:11px;font-weight:500;color:var(--yc-muted);margin-bottom:8px}.ticket-row{display:flex;justify-content:space-between;gap:12px;font-size:11px;line-height:1.7;padding:5px 0;border-bottom:1px dotted var(--yc-soft);color:var(--yc-muted)}.ticket-row strong{font-weight:500;white-space:nowrap;color:var(--yc-text)}.compact-detail{font-size:10px;color:var(--yc-muted);margin-top:13px;line-height:1.7}.compact-detail summary{cursor:pointer}.compact-detail p{margin:8px 0}.compact-detail a{color:var(--yc-text)}.trip-notes,.city-guide{font-size:11px;color:var(--yc-muted);border-bottom:1px solid var(--yc-soft);padding-bottom:10px;line-height:1.8}.trip-notes summary,.city-guide summary{display:flex;align-items:center;gap:7px;cursor:pointer;list-style:none}.trip-notes summary{color:#a18a60}.trip-notes summary span{margin-left:auto;font-size:10px}.trip-notes li{margin-top:5px}.city-guide p{margin:8px 0}@media(max-width:1200px){.overview-grid{grid-template-columns:1fr 1fr}.budget-overview{grid-column:1/-1}.ticket-breakdown{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}.ticket-breakdown h4{grid-column:1/-1}}@media(max-width:640px){.overview-grid{grid-template-columns:1fr}.budget-overview{grid-column:auto}.overview-card{padding:16px}.trip-heading h2{font-size:21px}.weather-days{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 15px}.ticket-breakdown{display:block}.hotel-price{margin-top:10px}}
/* The summary lives in both full pages and a narrow planning canvas. */
.summary { container-type: inline-size; }
@container (max-width: 760px) {
  .overview-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .budget-overview { grid-column: 1 / -1; }
  .ticket-breakdown { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 18px; }
  .ticket-breakdown h4 { grid-column: 1 / -1; }
  .overview-card h3 { font-size: 13px; }
  .weather-day, .weather-temp, .quiet, .stay-dates, .ticket-row { font-size: 12px; }
}
@container (max-width: 440px) {
  .overview-grid { grid-template-columns: minmax(0, 1fr); }
  .ticket-breakdown { display: block; }
}
</style>
