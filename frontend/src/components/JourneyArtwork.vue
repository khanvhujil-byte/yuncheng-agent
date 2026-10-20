<script setup lang="ts">
import landscape from '@/assets/yuncheng-lake.jpg'
</script>
<template>
  <figure class="journey-art"><img :src="landscape" width="1536" height="1024" alt="白云环绕远山，湖畔小路延伸向湛蓝湖水" decoding="async" fetchpriority="high" /></figure>
</template>
<style scoped>
.journey-art { margin: 0; width: 100%; overflow: hidden; border-radius: 9px; background: var(--yc-soft); }
.journey-art>img { display: block; width: 100%; height: auto; aspect-ratio: 1.8; object-fit: cover; object-position: center 58%; }
@media(max-width:720px) { .journey-art>img { aspect-ratio: 1.5; } }
</style>
