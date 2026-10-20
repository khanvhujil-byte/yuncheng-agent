<script setup lang="ts">
import { Plane, TrainFront, ArrowUpRight } from 'lucide-vue-next'
import dayjs from 'dayjs'
import type { TripPlan } from '@/types/trip'
defineProps<{ plan: TripPlan }>()
const safeUrl = (url?: string | null) => /^https:\/\//i.test(url || '')
</script>

<template>
  <section v-if="plan.intercity_legs?.length || plan.transport_warnings?.length" class="intercity-summary" aria-label="往返交通">
    <header><h3>旅程，从这里开始</h3><span>城际交通 · {{ plan.adult_count || 1 }} 位成人</span></header>
    <div class="transport-legs">
      <article v-for="leg in plan.intercity_legs" :key="leg.offer_id" class="transport-leg">
        <div class="leg-heading"><span><Plane v-if="leg.mode === 'flight'" :size="15" /><TrainFront v-else :size="15" />{{ leg.direction === 'outbound' ? '去程' : '返程' }} · {{ leg.transport_no }}</span><strong>{{ leg.amount_per_adult == null ? '票价待核实' : `¥${leg.amount_per_adult} / 成人 · 参考` }}</strong></div>
        <div class="stations"><div><strong>{{ dayjs(leg.departure_at).format('HH:mm') }}</strong><span>{{ leg.departure_station }}</span><small>{{ dayjs(leg.departure_at).format('M月D日') }}</small></div><span class="journey-line">{{ leg.duration_minutes }} 分钟 →</span><div><strong>{{ dayjs(leg.arrival_at).format('HH:mm') }}</strong><span>{{ leg.arrival_station }}</span><small>{{ dayjs(leg.arrival_at).format('M月D日') }}</small></div></div>
        <p>{{ leg.seat_class }} · {{ leg.direction === 'outbound' ? `已预留 ${leg.exit_buffer_minutes} 分钟出站/取行李，再安排当地接驳` : `已预留 ${leg.boarding_buffer_minutes} 分钟候车/值机，行程提前结束并接驳至出发站` }}</p>
        <p v-if="!leg.local_endpoint" class="missing">机场或车站坐标待核验，接驳尚未确认。</p>
        <details><summary>报价与预订须知</summary><p>{{ leg.price_note }}</p><p>查询于 {{ dayjs(leg.observed_at).format('M/D HH:mm') }}，以实际预订时库存和价格为准。</p><a v-if="safeUrl(leg.url)" :href="leg.url!" target="_blank" rel="noopener noreferrer">去平台核实 <ArrowUpRight :size="12" /></a></details>
      </article>
    </div>
    <p v-for="notice in plan.transport_warnings" :key="notice" class="missing">{{ notice }}</p>
  </section>
</template>

<style scoped>
.intercity-summary{padding:18px;background:var(--yc-surface,#fff);border:1px solid var(--yc-border,#e1e7e1);border-radius:12px;margin:16px 0}header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:15px}h3{font-size:14px;font-weight:500;margin:0;color:var(--yc-ink,#263e33)}header>span{font-size:11px;color:var(--yc-muted,#728076)}.transport-legs{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));gap:16px}.transport-leg{min-width:0;padding:14px;background:var(--yc-page,#f6f8f5);border-radius:8px}.leg-heading{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;font-size:12px}.leg-heading>span{display:flex;align-items:center;gap:6px}.leg-heading>strong{font-weight:500;color:var(--yc-ink,#294b3b)}.stations{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:20px 0 14px}.stations>div{display:grid;gap:5px;min-width:0}.stations>div:last-child{text-align:right}.stations strong{font-size:23px;font-weight:500;color:var(--yc-ink,#294b3b)}.stations span{font-size:12px;overflow-wrap:anywhere}.stations small,.journey-line{font-size:10px;color:var(--yc-muted,#728076)}p,details{font-size:11px;line-height:1.8;color:var(--yc-muted,#728076)}summary{cursor:pointer}a{display:inline-flex;align-items:center;gap:4px;color:var(--yc-ink,#294b3b)}.missing{color:#927344}@media(max-width:450px){header{align-items:flex-start;flex-direction:column}.intercity-summary{padding:14px}}
</style>
