<script setup lang="ts">
import { reactive, watch } from 'vue'
import type { Requirements } from '@/types/trip'

const props = defineProps<{ value: Requirements; disabled?: boolean }>()
const emit = defineEmits<{ save: [value: Requirements] }>()
const modules: { key: keyof Requirements; label: string; placeholder: string }[] = [
  { key: 'food', label: '吃什么', placeholder: '例如：杭帮菜为主，想专程去吃值得推荐的店' },
  { key: 'attractions', label: '去哪里', placeholder: '例如：喜欢博物馆，不安排商业街' },
  { key: 'hotel', label: '住哪里', placeholder: '例如：优先靠近当天活动区域' },
  { key: 'transport', label: '怎么走', placeholder: '例如：自驾，但不想长时间连续开车' },
  { key: 'pace', label: '玩得多满', placeholder: '例如：带父母，游玩和通勤每天不超过六小时' },
  { key: 'weather', label: '天气安排', placeholder: '例如：下雨时优先安排室内活动' },
  { key: 'budget', label: '钱花在哪', placeholder: '例如：更愿意把预算花在餐饮上' },
]
const notes = reactive<Record<keyof Requirements, string>>({ food: '', attractions: '', hotel: '', transport: '', pace: '', weather: '', budget: '' })
const controls = reactive({ priority: 'nearby' as 'nearby' | 'food_first', scope: 'nearby' as 'nearby' | 'citywide',
  stayPolicy: 'auto' as 'auto' | 'prefer_same' | 'must_same', luggagePolicy: 'carry' as 'carry' | 'storage_required',
  meal: '', hotel: '', leg: '', activity: '', spots: 4, rain: false, estimated: true })
watch(() => props.value, value => {
  for (const module of modules) notes[module.key] = value[module.key]?.instructions ?? ''
  controls.priority = value.food?.priority ?? 'nearby'; controls.scope = value.food?.travel_scope ?? 'nearby'
  controls.stayPolicy = value.hotel?.stay_policy ?? 'auto'; controls.luggagePolicy = value.hotel?.luggage_policy ?? 'carry'
  controls.meal = String(value.food?.max_cost_per_meal ?? ''); controls.hotel = String(value.hotel?.max_price_per_night ?? '')
  controls.leg = String(value.transport?.max_leg_minutes ?? ''); controls.activity = String(value.pace?.max_activity_minutes ?? '')
  controls.spots = value.attractions?.max_per_day ?? 4; controls.rain = value.weather?.avoid_rain_outdoors ?? false
  controls.estimated = value.transport?.allow_estimated_routes ?? true
}, { immediate: true, deep: true })
function save() {
  const result: Requirements = JSON.parse(JSON.stringify(props.value))
  for (const module of modules) result[module.key] = { ...result[module.key], instructions: notes[module.key] }
  result.food = { ...result.food, priority: controls.priority, travel_scope: controls.scope, max_cost_per_meal: controls.meal === '' ? null : Number(controls.meal) }
  result.hotel = { ...result.hotel, stay_policy: controls.stayPolicy, luggage_policy: controls.luggagePolicy, max_price_per_night: controls.hotel === '' ? null : Number(controls.hotel) }
  result.attractions = { ...result.attractions, max_per_day: controls.spots }
  result.transport = { ...result.transport, max_leg_minutes: controls.leg === '' ? null : Number(controls.leg), allow_estimated_routes: controls.estimated }
  result.pace = { ...result.pace, max_activity_minutes: controls.activity === '' ? null : Number(controls.activity) }
  result.weather = { ...result.weather, avoid_rain_outdoors: controls.rain }
  emit('save', result)
}
</script>

<template>
  <details class="requirements-panel">
    <summary>每个环节，都按你的想法来</summary>
    <p>文字偏好会交给对应环节参考。明确的数值限制会逐项检查；无法核实的特殊要求需要先确认。</p>
    <form @submit.prevent="save">
      <div class="module-notes"><label v-for="module in modules" :key="module.key"><span>{{ module.label }}</span><textarea v-model="notes[module.key]" :disabled="disabled" :placeholder="module.placeholder" rows="2" maxlength="1000" /></label></div>
      <div class="rule-fields">
        <label>餐饮安排<select v-model="controls.priority" :disabled="disabled"><option value="nearby">景点为主，顺路吃饭</option><option value="food_first">美食优先，围绕餐厅安排</option></select></label>
        <label>找店范围<select v-model="controls.scope" :disabled="disabled"><option value="nearby">优先附近</option><option value="citywide">全城寻找，不优先附近</option></select></label>
        <label>每餐参考价上限 / 元<input v-model="controls.meal" type="number" min="1" max="100000" :disabled="disabled" placeholder="不限" /></label>
        <label>住宿安排<select v-model="controls.stayPolicy" :disabled="disabled"><option value="auto">结合动线自动评估连住或换住</option><option value="prefer_same">尽量连住，减少搬动</option><option value="must_same">必须全程同一家</option></select></label>
        <label>换住日行李<select v-model="controls.luggagePolicy" :disabled="disabled"><option value="carry">可随行，预留整理与入住时间</option><option value="storage_required">必须有已核实寄存安排</option></select></label>
        <label>每晚参考房价上限 / 元<input v-model="controls.hotel" type="number" min="1" max="100000" :disabled="disabled" placeholder="不限" /></label>
        <label>每天最多几个景点<input v-model.number="controls.spots" type="number" min="1" max="4" required :disabled="disabled" /></label>
        <label>单段交通上限 / 分钟<input v-model="controls.leg" type="number" min="1" max="300" :disabled="disabled" placeholder="不额外限制" /></label>
        <label>每天游玩加通勤 / 分钟<input v-model="controls.activity" type="number" min="30" max="600" :disabled="disabled" placeholder="不额外限制" /></label>
      </div>
      <label class="check"><input v-model="controls.rain" type="checkbox" :disabled="disabled" />下雨不安排户外活动，天气未知时先提示无法确认</label>
      <label class="check"><input v-model="controls.estimated" type="checkbox" :disabled="disabled" />路线查不到时，接受明确标注的估算</label>
      <p class="note">价格上限按有来源的参考价核算，不是成交保证。提交会更新本次要求，确认后再规划。</p>
      <button type="submit" :disabled="disabled">保存这些要求</button>
    </form>
  </details>
</template>

<style scoped>
.requirements-panel{margin:18px 0;padding:18px;border:1px solid #dce6e6;border-radius:12px;background:var(--yc-surface)}summary{font-weight:600;cursor:pointer}p{font-size:13px;color:var(--yc-text);line-height:1.7;margin:12px 0}.module-notes,.rule-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.module-notes label:last-child{grid-column:1/-1}label{display:block;font-size:13px;color:var(--yc-ink)}span{display:block;margin-bottom:6px}textarea,input,select{width:100%;padding:9px 10px;border:1px solid var(--yc-border);border-radius:7px;background:#fff;font:inherit;box-sizing:border-box}textarea{resize:vertical}.rule-fields{margin-top:18px}.rule-fields input,.rule-fields select{display:block;margin-top:6px}.check{display:flex;gap:8px;align-items:center;margin-top:14px}.check input{width:auto}button{background:var(--yc-ink);color:white;border:0;padding:10px 16px;border-radius:8px;cursor:pointer}button:disabled{opacity:.5;cursor:default}@media(max-width:600px){.module-notes,.rule-fields{grid-template-columns:1fr}}
</style>
