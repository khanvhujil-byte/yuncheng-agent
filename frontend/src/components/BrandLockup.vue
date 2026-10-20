<script setup lang="ts">
import { Cloud } from 'lucide-vue-next'
</script>
<template>
  <span class="yuncheng-brand"><Cloud :size="36" :stroke-width="1.6" aria-hidden="true" /><span class="brand-name">云程</span><small>YUNCHENG</small></span>
</template>
<style scoped>
.yuncheng-brand { display: inline-flex; align-items: center; gap: 10px; color: var(--yc-ink); white-space: nowrap; font-family: var(--yc-font); letter-spacing: 0; }
.yuncheng-brand>svg { color: var(--yc-primary); flex-shrink: 0; }
.brand-name { font-size: 23px; font-weight: 700; letter-spacing: 2px; line-height: 1; }
small { font-size: 12px; font-weight: 400; letter-spacing: .7px; color: var(--yc-muted); }
@media(max-width:720px) { .yuncheng-brand { gap: 7px; } .yuncheng-brand>svg { width: 29px; height: 29px; } .brand-name { font-size: 19px; } small { display: none; } }
</style>
