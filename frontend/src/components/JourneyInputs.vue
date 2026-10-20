<script setup lang="ts">
import type { TripRequest } from '@/types/trip'
defineProps<{ model: Pick<TripRequest, 'origin_city' | 'intercity_mode' | 'return_trip' | 'adult_count' | 'room_count'>; disabled?: boolean }>()
</script>

<template>
  <fieldset class="journey-inputs" :disabled="disabled">
    <legend>从出发，到抵达</legend>
    <div class="journey-fields">
      <label><span class="field-label">出发城市 <small>{{ !model.intercity_mode || model.intercity_mode === 'none' ? '选填' : '必填' }}</small></span><input v-model="model.origin_city" aria-label="出发城市" :required="!!model.intercity_mode && model.intercity_mode !== 'none'" maxlength="60" placeholder="例如：上海" autocomplete="off"></label>
      <label>城际交通<select v-model="model.intercity_mode" aria-label="城际交通"><option value="none">自行安排交通</option><option value="auto">比较火车与飞机</option><option value="train">火车</option><option value="flight">飞机</option></select></label>
      <label v-if="model.intercity_mode && model.intercity_mode !== 'none'">行程方向<select v-model="model.return_trip" aria-label="行程方向"><option :value="true">去程与返程</option><option :value="false">只安排去程</option></select></label>
      <label>同行成人<input v-model.number="model.adult_count" type="number" min="1" max="8" required></label>
      <label>住宿房间<input v-model.number="model.room_count" type="number" min="1" :max="Math.min(model.adult_count || 1, 4)" required></label>
    </div>
    <p>日期为在目的地游玩的日期。车次和航班会影响首末日安排；预算按同行人数与房间数合计。</p>
  </fieldset>
</template>

<style scoped>
.journey-inputs{min-width:0;margin:0 0 18px;padding:16px 0;border:0;border-bottom:1px solid var(--yc-border,#e5e8e5)}legend{font-size:13px;color:var(--yc-ink,#253d35);font-weight:500}.journey-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}label{display:flex;flex-direction:column;gap:7px;font-size:12px;line-height:18px;color:var(--yc-text,#3e5148)}.field-label{display:flex;align-items:center;gap:6px;height:18px}small{display:inline;color:var(--yc-muted,#78867e)}input,select{box-sizing:border-box;width:100%;min-width:0;height:40px;padding:8px 10px;background:var(--yc-surface,#fff);border:1px solid var(--yc-border,#dfe5df);border-radius:7px;color:inherit;font:inherit}input:focus,select:focus{outline:2px solid var(--yc-accent,#56806a);outline-offset:2px}p{font-size:11px;line-height:1.8;color:var(--yc-muted,#78867e);margin:12px 0 0}fieldset:disabled{opacity:.6}@media(max-width:350px){.journey-fields{grid-template-columns:minmax(0,1fr)}}
</style>
