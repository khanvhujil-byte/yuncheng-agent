<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { request } from '@/services/dialog'
import type { EditCandidate, EditCandidates, ModuleEdit } from '@/services/dialog'
import type { TripPlan } from '@/types/trip'

const props = defineProps<{ conversationId: string; plan: TripPlan; disabled?: boolean }>()
const emit = defineEmits<{ edit: [edit: ModuleEdit] }>()
const module = ref<ModuleEdit['module']>('meals')
const dayIndex = ref(0)
const stayScope = ref<'night' | 'segment' | 'all'>('night')
const itemIndex = ref(0)
const candidates = ref<EditCandidate[]>([])
const chosen = ref<string[]>([])
const loading = ref(false)
const loaded = ref(false)
const locked = ref(false)
const error = ref('')
const element = ref<HTMLElement | null>(null)
let sequence = 0
const day = computed(() => props.plan.days[dayIndex.value])
const items = computed(() => module.value === 'meals' ? day.value?.meals ?? [] : day.value?.attractions ?? [])
const multiple = computed(() => module.value === 'day')
const selectableDays = computed(() => module.value === 'hotel' && (props.plan.accommodation_version ?? 0) >= 11
  ? props.plan.days.filter(d => props.plan.stays?.some(s => s.check_in === d.date)) : props.plan.days)

function reset() {
  sequence++; candidates.value = []; chosen.value = []; loaded.value = false; locked.value = false
  loading.value = false; error.value = ''; itemIndex.value = 0
}
watch([module, dayIndex, stayScope, () => props.conversationId, () => props.plan], reset)
async function load() {
  const ticket = ++sequence
  loading.value = true; error.value = ''; chosen.value = []
  try {
    const query = new URLSearchParams({ module: module.value })
    if (module.value !== 'hotel' || ((props.plan.accommodation_version ?? 0) >= 11 && stayScope.value !== 'all')) query.set('day_index', String(dayIndex.value))
    const data = await request<EditCandidates>(`/conversations/${props.conversationId}/edit-candidates?${query}`)
    if (ticket !== sequence) return
    candidates.value = data.candidates; locked.value = data.locked; loaded.value = true
  } catch (e) { if (ticket === sequence) error.value = e instanceof Error ? e.message : '候选查询失败' }
  finally { if (ticket === sequence) loading.value = false }
}
function select(id: string, checked: boolean) {
  chosen.value = multiple.value ? checked ? [...chosen.value, id] : chosen.value.filter(v => v !== id) : [id]
}
function preview() {
  if (!chosen.value.length || locked.value || props.disabled) return
  emit('edit', { module: module.value,
    ...(module.value === 'hotel' ? ((props.plan.accommodation_version ?? 0) >= 11 ? { stay_scope: stayScope.value, ...(stayScope.value === 'all' ? {} : { day_index: dayIndex.value }) } : {}) : { day_index: dayIndex.value }),
    ...(['meals', 'attractions'].includes(module.value) ? { item_index: itemIndex.value } : {}),
    ...(multiple.value ? { candidate_ids: chosen.value } : { candidate_id: chosen.value[0] }),
  })
}
function price(candidate: EditCandidate) {
  if (candidate.estimated_cost == null || candidate.price_status === 'unknown') return '费用待查'
  return `¥${candidate.estimated_cost}${candidate.price_status === 'known' ? '' : '（参考）'}`
}
async function openStay(index: number) {
  if (props.disabled) return
  module.value = 'hotel'; dayIndex.value = index; stayScope.value = 'segment'
  await nextTick()
  element.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  await load()
}
defineExpose({ openStay })
</script>

<template>
  <section ref="element" class="module-editor">
    <h3>修改这份行程</h3>
    <p>选中要换的内容，先查看修改预览，再确认保存。系统会更新相关路线、时间安排和预算。</p>
    <div class="controls">
      <label>修改内容<select v-model="module" :disabled="disabled || loading"><option value="meals">一顿餐饮</option><option value="attractions">一个景点</option><option value="hotel">住宿安排</option><option value="day">某天的景点安排</option></select></label>
      <label v-if="module === 'hotel' && (plan.accommodation_version ?? 0) >= 11">修改范围<select v-model="stayScope" :disabled="disabled || loading"><option value="night">某一晚</option><option value="segment">连续同酒店的住宿段</option><option value="all">全部未锁定夜晚</option></select></label>
      <label v-if="module !== 'hotel' || ((plan.accommodation_version ?? 0) >= 11 && stayScope !== 'all')">{{ module === 'hotel' ? '入住日' : '哪一天' }}<select v-model.number="dayIndex" :disabled="disabled || loading"><option v-for="d in selectableDays" :key="d.date" :value="d.day_index">第 {{ d.day_index + 1 }} 天 · {{ d.date }}</option></select></label>
      <label v-if="module === 'meals' || module === 'attractions'">替换哪一项<select v-model.number="itemIndex" :disabled="disabled || loading"><option v-for="(item, index) in items" :key="index" :value="index">{{ item.name }}</option></select></label>
      <button :disabled="disabled || loading || ((module === 'meals' || module === 'attractions') && !items.length)" @click="load">{{ loading ? '查询候选中…' : '查询可替换候选' }}</button>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="locked">这家酒店已由你指定，请先修改住宿要求后再更换。</p>
    <p v-else-if="loaded && !candidates.length">暂时没有可替换候选，请稍后重查或调整要求。</p>
    <div v-else-if="loaded" class="candidates">
      <p v-if="multiple">按你期望的游览顺序勾选景点，建议每天 2～3 个。</p>
      <label v-for="candidate in candidates" :key="candidate.candidate_id" class="candidate">
        <input :type="multiple ? 'checkbox' : 'radio'" name="replacement" :checked="chosen.includes(candidate.candidate_id)" :disabled="disabled" @change="select(candidate.candidate_id, ($event.target as HTMLInputElement).checked)">
        <span><strong>{{ candidate.name }}</strong><small>{{ candidate.address }} · {{ price(candidate) }}</small><small v-if="candidate.ranking_name">{{ candidate.ranking_name }}{{ candidate.ranking_rank ? ` · 第 ${candidate.ranking_rank} 位` : '' }}</small></span>
      </label>
      <button :disabled="disabled || !chosen.length" @click="preview">预览这次修改</button>
    </div>
  </section>
</template>

<style scoped>
.module-editor{border-top:1px solid #e5eaf0;margin-top:20px;padding-top:16px}h3{font-size:17px;margin:0 0 8px}p,small{font-size:13px;color:#64748b;line-height:1.7}.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end}label{font-size:13px}select{display:block;margin-top:5px;padding:8px;max-width:280px;border:1px solid #cbd5e1;border-radius:6px}button{padding:9px 12px;border:1px solid #cbd5e1;border-radius:6px;background:#edf4ff;color:#2455a3;cursor:pointer}button:disabled{opacity:.5;cursor:default}.candidates{margin-top:12px;max-height:350px;overflow:auto}.candidate{display:flex;align-items:start;gap:10px;padding:10px;border:1px solid #e5eaf0;border-radius:6px;margin:7px 0}.candidate small{display:block}.error{color:#a2352b}
</style>
