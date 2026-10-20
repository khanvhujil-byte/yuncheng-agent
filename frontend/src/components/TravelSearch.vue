<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import dayjs from 'dayjs'
import { request, hasSession } from '@/services/dialog'

const props = defineProps<{ city?: string; origin?: string; startDate?: string; days?: number; disabled?: boolean }>()
type Offer = { name?: string; provider_id?: string; amount: number | null; url?: string | null; unit: string; ticket_name?: string; check_in?: string; check_out?: string; segments?: Record<string, string>[] }
type Result = { items: Offer[]; outcome: { status: string; message: string; error_code?: string }; cache_hit: boolean }
const kind = ref<'hotels' | 'trains' | 'flights'>('hotels')
const keyword = ref(''), busy = ref(false), error = ref(''), result = ref<Result | null>(null)
const searchDate = ref(''), returnLeg = ref(false)
let sequence = 0
const inputs = computed(() => [props.city, props.origin, props.startDate, props.days, kind.value, keyword.value, searchDate.value, returnLeg.value])
watch(inputs, () => { sequence++; result.value = null; error.value = '' })
const departure = computed(() => searchDate.value || dayjs(props.startDate).add(returnLeg.value ? Math.max(0, (props.days || 1)-1) : 0, 'day').format('YYYY-MM-DD'))
const queryDate = computed({ get: () => props.startDate || searchDate.value ? departure.value : '', set: value => { searchDate.value = value } })
async function search() {
  if (busy.value) return
  if (!hasSession()) { error.value = '请先到「我的旅程」登录，再查询平台报价。'; return }
  if (!props.city?.trim() || !props.startDate || (kind.value !== 'hotels' && !props.origin?.trim())) {
    error.value = '请先填写目的地、游玩日期；交通查询还需出发城市。'; return
  }
  const id = ++sequence
  busy.value = true; error.value = ''; result.value = null
  try {
    const body = kind.value === 'hotels' ? { city: props.city, keyword: keyword.value,
      check_in: departure.value, check_out: dayjs(departure.value).add(1, 'day').format('YYYY-MM-DD') } : {
      origin: returnLeg.value ? props.city : props.origin, destination: returnLeg.value ? props.origin : props.city,
      departure_date: departure.value }
    const response = await request<Result>(`/travel/${kind.value}`, 'POST', body)
    if (id !== sequence) return
    if (response.outcome.status !== 'ok') error.value = response.outcome.message
    else result.value = response
  } catch (e) { if (id === sequence) error.value = e instanceof Error ? e.message : '查询未完成，请稍后再试。' }
  finally { busy.value = false }
}
</script>

<template>
  <details class="travel-search">
    <summary>先看看酒店与交通报价</summary>
    <p>单独查一查，不会修改已生成行程。班次和房价以实际预订为准。</p>
    <div class="search-controls">
      <label>查询内容<select v-model="kind" aria-label="查询内容" :disabled="busy || disabled"><option value="hotels">酒店</option><option value="trains">火车票</option><option value="flights">机票</option></select></label>
      <label>查询日期<input v-model="queryDate" type="date" :disabled="busy || disabled"></label>
      <label v-if="kind === 'hotels'">区域或酒店<input v-model="keyword" placeholder="例如：西湖" maxlength="100" :disabled="busy || disabled"></label>
      <label v-else>方向<select v-model="returnLeg" aria-label="查询方向" :disabled="busy || disabled"><option :value="false">去程</option><option :value="true">返程</option></select></label>
    </div>
    <button type="button" :disabled="busy || disabled" @click="search">{{ busy ? '正在查询…' : '查询平台参考价' }}</button>
    <p v-if="error" role="alert" class="search-error">{{ error }}<RouterLink v-if="!hasSession()" to="/chat"> 去登录</RouterLink></p>
    <div v-if="result" class="search-results" aria-live="polite"><p v-if="!result.items.length">本次查询暂无结果，可调整日期或关键词后再查。</p><p v-else>找到 {{ result.items.length }} 项{{ result.cache_hit ? ' · 最近查询结果' : '' }}，展示前 6 项</p><article v-for="(offer, i) in result.items.slice(0, 6)" :key="offer.provider_id || i"><strong>{{ offer.name || offer.segments?.map(s => `${s.marketingTransportNo} ${s.depStationName} → ${s.arrStationName}`).join(' / ') }}</strong><span>{{ offer.amount == null ? '价格待核实' : `¥${offer.amount}${kind === 'hotels' ? ' / 房晚' : ' / 成人'} · 参考` }}</span><small v-if="offer.segments?.length">{{ offer.segments[0]?.depDateTime }} · {{ offer.segments[0]?.seatClassName }}</small><a v-if="/^https:\/\//i.test(offer.url || '')" :href="offer.url!" target="_blank" rel="noopener noreferrer">去平台核实</a></article></div>
  </details>
</template>

<style scoped>
.travel-search{margin:15px 0;border:1px solid var(--yc-border,#e1e7e1);border-radius:9px;padding:13px;color:var(--yc-text,#3d5148);font-size:12px;min-width:0}summary{cursor:pointer;font-weight:500}p{font-size:11px;color:var(--yc-muted,#748278);line-height:1.8}.search-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}label{display:grid;gap:6px;font-size:11px}input,select{width:100%;box-sizing:border-box;min-width:0;padding:9px;border:1px solid var(--yc-border,#dde5de);border-radius:6px;background:var(--yc-surface,#fff);color:inherit;font:inherit}button{margin-top:12px;border:0;border-radius:6px;background:var(--yc-ink,#294b3b);color:var(--yc-surface,#fff);padding:10px 14px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}.search-error{color:#9a683b}.search-results{display:grid;gap:8px}.search-results article{display:grid;gap:6px;padding:11px;background:var(--yc-page,#f6f8f5);border-radius:6px;overflow-wrap:anywhere}.search-results strong{font-size:12px;font-weight:500}.search-results small,.search-results span{font-size:11px}a{color:var(--yc-ink,#294b3b)}
</style>
