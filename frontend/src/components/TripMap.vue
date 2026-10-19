<script setup lang="ts">
/**
 * 行程地图：把每一天的景点按游览顺序画到地图上。
 *
 * 为什么用「高德静态地图 REST API」而不是 JS API：
 * 1. 项目里已经装了 @amap/amap-jsapi-loader，但那套 JS API 要求 Key 是「Web 端(JS API)」类型，
 *    而 backend/.env 里现有的 AMAP_API_KEY 是「Web 服务」类型 —— 两者不通用。用 JS API
 *    只会得到一张空白地图且不报错（项目的「坑 5」）。
 * 2. 静态地图走的是和现有后端同一个 Key 类型（Web 服务），不用再申请一个 Key。
 * 3. 静态地图是纯 <img>，不引入任何新的 npm 依赖，也不需要在页面里跑地图引擎。
 *
 * 为什么「一天一个请求」而不是「整趟一张图」：
 * 高德的 markers 参数是「一次请求里全部点位共用一个视野」，把 3 天的景点塞进一张图，
 * 跨天的点会被 zoom 压缩到分不清顺序，失去了「按天看动线」的意义。
 * 一天 2~4 个景点，正好是 markers 的舒适区。
 *
 * 【关键局限，必须写在这里】
 * 静态地图 REST API 只能通过 URL 传 Key，浏览器直接请求 restapi.amap.com 时，
 * Key 一定会出现在 img.src、浏览器网络面板、以及 Referer 之外的可复制 URL 里。
 * 这是静态地图 API 的固有代价（它没有「服务端签名」这一档），无法靠前端手段规避。
 * 生产环境正确做法是：由后端做一层 /api/map/static 代理，前端只带点位、不带 Key，
 * 由后端拼 Key 并把图片流回给前端。当前 V0 阶段为了不改后端接口，先用前端直连。
 *
 * 为什么还自带一套自绘 SVG：
 * 静态地图是「有网 + Key 有效 + 配额没跑完」才成立的外部依赖，任何一环坏了页面就会
 * 出现一张破图。所以 <img> 一旦加载失败（@error），就自动切到自绘 SVG ——
 * SVG 只用经纬度线性投影，不依赖任何外部服务，保证「地图这一块永远有东西可看」。
 */
import dayjs from 'dayjs'
import { Hotel as HotelIcon, ImageOff, Map as MapIcon, TriangleAlert } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { DayPlan, Hotel } from '@/types/trip'
import { dayStops } from '@/utils/itinerary'

const props = defineProps<{
  days: DayPlan[]
  /** 可选：行程级住宿（v0.2 起住宿是整趟一家，不是每天一个） */
  hotel?: Hotel | null
  /**
   * 可选：直接指定 Key，覆盖环境变量。
   * 存在的意义是「让组件可被测试」：静态地图是外部依赖，
   * 没有这个入口就没法在自动化测试里稳定地复现「Key 失效 → 降级 SVG」这条路。
   */
  keyOverride?: string
  embedded?: boolean
}>()

/** 高德静态地图接口。size 上限 1024*1024，这里取题目给的 700*420 */
const STATIC_MAP_ENDPOINT = 'https://restapi.amap.com/v3/staticmap'
const MAP_WIDTH = 700
const MAP_HEIGHT = 420

/**
 * Key 尝试顺序。
 *
 * 为什么要「一串」而不是「一个」：
 * 高德把 Key 按平台分成不同类型（Web 服务 / Web 端 JS API / iOS / Android …），
 * 用错类型接口不会报「类型不对」，只会回一个 status=0 的 JSON 或空白图，
 * 排查成本很高。这里按「最可能对」的顺序依次试，前一个失败了再换下一个，
 * 全失败才降级成 SVG —— 比直接写死一个 Key 更不容易让用户看到破图。
 *
 * 为什么不直接用 backend/.env 里的 AMAP_API_KEY 当默认值：
 * 那会把一份后端密钥在构建期烧进前端静态产物里，属于「默认泄露」。
 * 所以这里只读前端自己的环境变量，要复用后端 Key 时由用户显式在 .env.local 里
 * 填 VITE_AMAP_STATIC_KEY（开发机上的便利选择），而不是组件替他决定。
 */
const CANDIDATE_KEYS: string[] = [
  props.keyOverride,
  import.meta.env.VITE_AMAP_STATIC_KEY,
  import.meta.env.VITE_AMAP_JS_KEY,
].filter((item): item is string => typeof item === 'string' && item.trim().length > 0)

/**
 * 一旦某天出图失败，就把所有天一起切到 SVG。
 *
 * 为什么要「全切」而不是「只切失败的那天」：
 * 高德对同一个 Key 的失败通常是全局性的（Key 类型不对 / 配额跑完 / 权限没开），
 * 局部的偶发失败很少见。全切能保证页面观感一致 —— 一半真地图一半示意图
 * 会让用户以为「有些天没有地图数据」，那是误导。
 */
const staticMapDisabled = ref(CANDIDATE_KEYS.length === 0)
/** 已经因为加载失败而换掉几个 Key（换一个重试一次，全用完就降级） */
const failedAttempts = ref(0)

const activeKey = computed(() => CANDIDATE_KEYS[failedAttempts.value] ?? '')

type LocatedPoint = {
  label: string
  name: string
  longitude: number
  latitude: number
  /** true = 酒店（起点/落脚点），画法要和景点区分开 */
  isHotel: boolean
  isMeal?: boolean
}

/** 坐标是否可用。后端字段可为空，NaN 一旦进了 URL 会让高德直接回 20003 */
function hasLocation(location: { longitude: number; latitude: number } | null | undefined): boolean {
  return (
    !!location &&
    Number.isFinite(location.longitude) &&
    Number.isFinite(location.latitude) &&
    location.longitude !== 0 &&
    location.latitude !== 0
  )
}

/**
 * 一天的点位序列：酒店（如果有坐标）→ 按游览顺序的景点。
 *
 * 为什么酒店只画在第一天：v0.2 里住宿是行程级一家，天天都在同一个地方。
 * 每天都画一遍 S 只会让后几天的图多一个与当天动线无关的点位。
 * 酒店若当天本身就在景点列表里（去酒店本身是个景点），也不再重复加。
 */
function pointsOfDay(day: DayPlan): LocatedPoint[] {
  const points: LocatedPoint[] = []
  const hotel = props.hotel

  if (hasLocation(hotel?.location)) {
    points.push({
      label: 'S',
      name: hotel?.name ?? '住宿',
      longitude: hotel!.location!.longitude,
      latitude: hotel!.location!.latitude,
      isHotel: true,
    })
  }

  dayStops(day, hotel).forEach((stop, index) => {
    const spot = stop.place
    if (!spot.location || !hasLocation(spot.location)) return
    // 一天最多 9 个景点，超过就用字母续上（高德 marker 的 label 只接受很短的串）
    const label = index < 9 ? String(index + 1) : String.fromCharCode(65 + index - 9)
    points.push({
      label,
      name: spot.name,
      longitude: spot.location.longitude,
      latitude: spot.location.latitude,
      isHotel: false,
      isMeal: stop.kind === 'meal',
    })
  })

  return points
}

/** 每天的点位（用 computed 缓存，避免模板里重复算） */
const dayPoints = computed(() => props.days.map((day) => pointsOfDay(day)))

/**
 * 去重：同一个坐标出现两次（比如酒店和当天第一个景点其实是同一栋楼，
 * 或者两个景点共用一个坐标）会被高德判为非法请求，整张图变成 20003 错误。
 */
function dedupe(points: LocatedPoint[]): LocatedPoint[] {
  const seen = new Set<string>()
  const result: LocatedPoint[] = []
  for (const point of points) {
    const key = `${point.longitude},${point.latitude}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push(point)
  }
  return result
}

/**
 * 拼一天的静态地图 URL。
 *
 * 踩过的坑（已实测）：多个 marker 之间必须用竖线 | 分隔。
 * 用分号 ; 或逗号 , 会返回 {"status":"0","info":"UNKNOWN_ERROR","infocode":"20003"}，
 * 而且 HTTP 状态码仍然是 200 —— 只看状态码会以为请求成功了。
 * 实测证据：单点 markers=mid,,A:... 返回 image/png；改成两点用 ; 连接立刻变 20003；
 * 换成 | 连接又恢复正常。
 */
function staticMapUrl(points: LocatedPoint[]): string {
  const unique = dedupe(points)
  const longitudes = unique.map((item) => item.longitude)
  const latitudes = unique.map((item) => item.latitude)
  // 视野取所有点的外接矩形中心。高德没有「自动适配」参数，只能自己算中心
  const center = `${(Math.min(...longitudes) + Math.max(...longitudes)) / 2},${
    (Math.min(...latitudes) + Math.max(...latitudes)) / 2
  }`
  const markers = unique.map((item) => `mid,,${item.label}:${item.longitude},${item.latitude}`).join('|')

  const params = new URLSearchParams({
    location: center,
    // zoom 取 11：城市级景点（西湖/灵隐寺这种）在 11 上刚好全部落进 700*420 的视野
    zoom: '11',
    size: `${MAP_WIDTH}*${MAP_HEIGHT}`,
    markers,
    key: activeKey.value,
  })
  // URLSearchParams 会把 | : , 转义成 %7C %3A %2C。高德对转义后的分隔符解析不稳定，
  // 这里把它们还原成字面字符（这三个字符在 query 里是合法的）。
  const query = params.toString().replace(/%7C/gi, '|').replace(/%3A/gi, ':').replace(/%2C/gi, ',')
  return `${STATIC_MAP_ENDPOINT}?${query}`
}

/**
 * 图片加载失败的处理。
 *
 * 先换下一个候选 Key 重试（模板里 :key 绑定了失败次数，换 Key 会强制 <img> 重新请求）；
 * 候选都试完了才整块降级成自绘 SVG。
 *
 * 注意：换 Key 重试不会造成请求风暴 —— 候选 Key 最多 3 个，每个 Key 只试一次。
 */
function onImageError(): void {
  if (failedAttempts.value < CANDIDATE_KEYS.length - 1) {
    failedAttempts.value += 1
    return
  }
  staticMapDisabled.value = true
}

/**
 * 兜底超时：网络被墙 / 请求挂起时，<img> 既不会 onload 也不会 onerror，
 * 页面就会一直是空白占位。10 秒还没加载出来就当作失败，直接给 SVG。
 */
let watchdog: number | undefined
function onImageLoad(): void {
  if (watchdog !== undefined) {
    window.clearTimeout(watchdog)
    watchdog = undefined
  }
}

onMounted(() => {
  if (staticMapDisabled.value) return
  watchdog = window.setTimeout(() => {
    if (!staticMapDisabled.value) staticMapDisabled.value = true
  }, 10000)
})

onBeforeUnmount(() => {
  if (watchdog !== undefined) window.clearTimeout(watchdog)
})

/** 行程换了（重新生成）就重置失败状态，否则新行程会继承旧行程的降级结果 */
watch(
  () => props.days,
  () => {
    staticMapDisabled.value = CANDIDATE_KEYS.length === 0
    failedAttempts.value = 0
  },
)

function useSvgMode(): boolean {
  return staticMapDisabled.value
}

// ---------------- 以下是自绘 SVG 需要的投影计算 ----------------

/** SVG 画布尺寸（用 viewBox 逻辑单位，实际显示尺寸交给 CSS 缩放） */
const SVG_WIDTH = 720
const SVG_HEIGHT = 440
const SVG_PAD_X = 74
const SVG_PAD_TOP = 40
const SVG_PAD_BOTTOM = 62

type PlacedPoint = LocatedPoint & { x: number; y: number }

/**
 * 经纬度 → 画布坐标的线性投影。
 *
 * 为什么是「线性」而不是墨卡托：一天的行程跨度通常小于 0.3 度，在这个尺度上
 * 线性投影和墨卡托的差别小于一个像素，用线性可以让代码保持可读。
 * 纬度要翻转（y 向下增长，而纬度向北增长）。
 */
function projectDay(points: LocatedPoint[]): PlacedPoint[] {
  const unique = dedupe(points)
  if (!unique.length) return []

  const longitudes = unique.map((item) => item.longitude)
  const latitudes = unique.map((item) => item.latitude)
  const minLng = Math.min(...longitudes)
  const maxLng = Math.max(...longitudes)
  const minLat = Math.min(...latitudes)
  const maxLat = Math.max(...latitudes)

  // span 为 0（只有一个点，或所有点在同一条经/纬线上）时不能做除法，给一个最小的假跨度，
  // 让单点落在画布正中而不是 NaN 坐标上。
  const spanLng = maxLng - minLng || 0.01
  const spanLat = maxLat - minLat || 0.01
  const innerW = SVG_WIDTH - SVG_PAD_X * 2
  const innerH = SVG_HEIGHT - SVG_PAD_TOP - SVG_PAD_BOTTOM

  return unique.map((item) => ({
    ...item,
    x: SVG_PAD_X + ((item.longitude - minLng) / spanLng) * innerW,
    y: SVG_PAD_TOP + ((maxLat - item.latitude) / spanLat) * innerH,
  }))
}

/** 连线路径：按游览顺序连成折线。顺序就是 day.attractions 的顺序 */
function polyline(points: PlacedPoint[]): string {
  return points.map((item) => `${item.x.toFixed(1)},${item.y.toFixed(1)}`).join(' ')
}

/** 每个点的 SVG 数据（含避让后的标签位置） */
const svgDays = computed(() =>
  dayPoints.value.map((points) => {
    const placed = projectDay(points)
    // 已经放好的标签矩形，用来做碰撞检测
    const placedLabels: Array<{ x: number; y: number; width: number }> = []
    return placed.map((item) => {
      // 中文字在大约 12px 字号下宽度约等于字号本身，用它估算标签占位宽度
      const width = item.name.length * 12 + 6
      // 初始位置：点正上方 15px
      let labelY = item.y - 15
      const collides = (y: number): boolean =>
        placedLabels.some(
          (label) =>
            Math.abs(label.x - item.x) < (label.width + width) / 2 && Math.abs(label.y - y) < 15,
        )
      // 撞了就往上让 15px，最多让 6 次。真实的碰撞基本都在第 1~2 次就让开了，
      // 加次数上限是为了防止「所有点重合」这种极端输入把标签推到画布外面去。
      for (let guard = 0; guard < 6 && collides(labelY); guard += 1) {
        labelY -= 15
      }
      // 标签不许跑出画布上沿
      labelY = Math.max(14, labelY)
      placedLabels.push({ x: item.x, y: labelY, width })
      return { ...item, labelY }
    })
  }),
)

function formatDay(date: string): string {
  return dayjs(date).format('M月D日')
}

function hasAnyPoint(index: number): boolean {
  return (dayPoints.value[index] ?? []).length > 0
}
</script>

<template>
  <section class="trip-map" :class="{ embedded }">
    <div v-for="(day, index) in days" :key="day.date" class="card day-map-card">
      <div v-if="!embedded" class="card-title">
        <MapIcon :size="16" />
        第 {{ day.day_index + 1 }} 天
        <span class="muted">{{ formatDay(day.date) }}</span>
        <span class="muted" style="margin-left: auto; font-weight: 400">
          {{ (dayPoints[index] ?? []).length }} 个点位
        </span>
      </div>

      <!-- 当天一个可定位的点都没有：直接说明原因，不要画一张空图 -->
      <div v-if="!hasAnyPoint(index)" class="empty-hint">
        这一天没有可定位的点位（景点缺少经纬度），无法出图。
      </div>

      <template v-else>
        <!-- 首选：高德静态地图（一天一个请求） -->
        <img
          v-if="!useSvgMode()"
          :key="`${index}-${failedAttempts}`"
          class="map-image"
          :src="staticMapUrl(dayPoints[index] ?? [])"
          :alt="`第 ${day.day_index + 1} 天行程地图`"
          :width="MAP_WIDTH"
          :height="MAP_HEIGHT"
          loading="lazy"
          @load="onImageLoad"
          @error="onImageError"
        />

        <!-- 降级：自绘 SVG。不依赖任何外部服务，保证地图区块永远有内容 -->
        <svg
          v-else
          class="map-image map-svg"
          :viewBox="`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`"
          role="img"
          :aria-label="`第 ${day.day_index + 1} 天行程示意图`"
        >
          <rect x="0" y="0" :width="SVG_WIDTH" :height="SVG_HEIGHT" fill="#f7f9fc" />

          <!-- 指北针：自绘图没有底图上的「北」，不标的话用户无法判断这张图的方向 -->
          <text :x="SVG_WIDTH - 26" :y="28" text-anchor="middle" font-size="13" fill="#8c94a3">
            N
          </text>
          <path
            :d="`M ${SVG_WIDTH - 26} 34 L ${SVG_WIDTH - 31} 46 L ${SVG_WIDTH - 26} 43 L ${SVG_WIDTH - 21} 46 Z`"
            fill="#8c94a3"
          />

          <!-- 游览动线：实线 + 箭头。用虚线会让人以为「路线还没定」，
               而这里的顺序就是后端给出的确定游览顺序，必须画成实线 -->
          <defs>
            <!-- marker 的 id 必须每天都不一样：同页多张 SVG 用同一个 id 时，
                 浏览器只认第一个定义，后面几张图的箭头会指向错误的定义 -->
            <marker
              :id="`trip-map-arrow-${day.date}-${day.day_index}`"
              markerWidth="9"
              markerHeight="9"
              refX="7"
              refY="3"
              orient="auto"
              markerUnits="strokeWidth"
            >
              <path d="M0,0 L0,6 L8,3 z" fill="#8fa8d6" />
            </marker>
          </defs>
          <polyline
            :points="polyline(svgDays[index] ?? [])"
            fill="none"
            stroke="#8fa8d6"
            stroke-width="2"
            :marker-mid="`url(#trip-map-arrow-${day.date}-${day.day_index})`"
          />

          <g v-for="point in svgDays[index] ?? []" :key="`${point.label}-${point.name}`">
            <!-- 酒店单独一种颜色：它是每天的落脚点，不是游览顺序里的一站 -->
            <circle
              :cx="point.x"
              :cy="point.y"
              r="12"
              :fill="point.isHotel ? '#aa8b57' : point.isMeal ? '#c39a64' : '#789364'"
            />
            <text
              :x="point.x"
              :y="point.y + 4.5"
              text-anchor="middle"
              font-size="12"
              font-weight="600"
              fill="#ffffff"
            >
              {{ point.label }}
            </text>
            <text :x="point.x" :y="point.labelY" text-anchor="middle" font-size="12" fill="#3d4453">
              {{ point.name }}
            </text>
          </g>
        </svg>

        <!-- 图例：把「数字 = 游览顺序」「S = 住宿」讲清楚，否则用户不知道圆点里的字是什么意思 -->
        <div class="legend">
          <span class="legend-item"><span class="dot spot-dot" /> 编号对应下方景点与餐饮，同址点合并</span>
          <span v-if="(dayPoints[index] ?? []).some((item) => item.isHotel)" class="legend-item">
            <span class="dot hotel-dot" /> S = 住宿
          </span>
        </div>
      </template>
    </div>

    <!-- Key 缺失或失效时的说明。必须说清「为什么没有地图」和「怎么修」，不能静默空白 -->
    <p v-if="embedded" class="map-caption">{{ staticMapDisabled ? '地点与动线示意，连线不代表实际道路；路程耗时见下方卡片。' : '今日地点一览，路程耗时见下方卡片。' }}</p>
    <div v-if="staticMapDisabled && !embedded" class="card alert-card">
      <div class="alert-title">
        <TriangleAlert :size="16" />
        静态地图不可用，已切换为自绘示意图
      </div>
      <ul class="alert-list">
        <li v-if="!CANDIDATE_KEYS.length">
          没有配置高德 Key。静态地图需要「Web 服务」类型的 Key，
          请在 frontend/.env.local 里填 VITE_AMAP_STATIC_KEY 后重启开发服务器。
          注意它和 VITE_AMAP_JS_KEY 不是同一个 Key（高德按平台分类型，用错不会报错，只会出一张空白图）。
        </li>
        <li v-else>
          已配置的 Key 都被高德拒绝。常见原因：Key 类型不是「Web 服务」、
          Key 未开通静态地图权限、或当日调用量已用完。
        </li>
      </ul>
    </div>

    <div v-if="!days.length" class="empty-hint">没有行程数据，无法出图。</div>

    <!-- 把 Key 暴露在 URL 里的固有代价，写在界面上让使用者知道，而不是只写在代码注释里 -->
    <p v-if="!staticMapDisabled && !embedded" class="footnote">
      <ImageOff :size="12" />
      地图图片由高德静态地图接口直接提供，因此你的高德 Key 会出现在图片地址里。
      这是静态地图 API 的固有做法；生产环境应由后端代理图片请求，把 Key 留在服务端。
    </p>

    <p v-if="hotel && !embedded" class="footnote">
      <HotelIcon :size="12" />
      每天从住宿「{{ hotel.name }}」出发，S 标记为住宿。
    </p>
  </section>
</template>

<style scoped>
.trip-map.embedded{padding:0;background:transparent}.embedded .day-map-card{padding:0;border:0;background:transparent;box-shadow:none;border-radius:10px;overflow:hidden}.embedded .map-image{max-height:300px;object-fit:contain;background:#f5f7ed}.embedded .legend{font-size:10px;margin-top:8px;gap:12px}.map-caption{font-size:10px;line-height:1.6;color:#9ba38e;margin:6px 0 0}
.trip-map {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card {
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 12px;
  padding: 18px 20px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #1b1f27;
  margin-bottom: 14px;
}

.muted {
  color: #8c94a3;
  font-weight: 400;
}

.day-map-card {
  /* 静态地图是 700px 宽的固定图，窄容器下靠 max-width 缩放，
     这里允许横向兜底，避免图片把卡片撑破 */
  overflow: hidden;
}

.map-image {
  display: block;
  width: 100%;
  max-width: 100%;
  height: auto;
  border-radius: 10px;
  background: #f7f9fc;
  border: 1px solid #f0f2f6;
}

.map-svg {
  /* viewBox 自带比例，交给 width:100% + height:auto 保持比例 */
  aspect-ratio: 720 / 440;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 10px;
  font-size: 12px;
  color: #8c94a3;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}

.spot-dot {
  background: #3566c9;
}

.hotel-dot {
  background: #d79b00;
}

.alert-card {
  background: #fffbf0;
  border-color: #ffe3a3;
}

.alert-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #8a6100;
  margin-bottom: 8px;
}

.alert-list {
  margin: 0;
  padding-left: 20px;
  color: #8a6100;
  font-size: 13px;
  line-height: 1.9;
}

.empty-hint {
  padding: 14px 16px;
  background: #f7f9fc;
  border-radius: 10px;
  color: #8c94a3;
  font-size: 13px;
  line-height: 1.8;
}

.footnote {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0;
  padding: 0 4px;
  font-size: 12px;
  color: #8c94a3;
  line-height: 1.8;
}

.footnote :deep(svg) {
  flex: 0 0 auto;
  margin-top: 4px;
}

@media print {
  /* 打印时地图图片本身就是内容，保留；只去掉边框和背景，省墨 */
  .map-image {
    border-color: #d9dde4;
    background: #fff;
  }

  .footnote {
    display: none;
  }
}
</style>
