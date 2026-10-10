<script setup lang="ts">
/**
 * 导出面板：把整份行程导出成 Markdown 文件，或直接打印 / 存 PDF。
 *
 * 为什么需要它：
 * 行程最终是要带出门用的 —— 手机上翻网页、或者打印一张纸。所以导出物必须
 * 「离线可读」且「信息完整」：不能只导出景点名，预算、约束对账、warnings
 * 这些「这份行程可不可信」的信息必须一起带走，否则纸上的行程看起来像真的，
 * 而实际上门票价是估算的 —— 这正是本项目 sources/warnings 机制要防的事。
 *
 * 为什么用 Blob + a[download] 而不是 jsPDF / html2canvas：
 * package.json 里装了这两个包，但它们依赖 Canvas 渲染整页，导出的是「截图」，
 * 文字不可复制、体积大、分页不可控。Markdown 是纯文本，体积小、可编辑、
 * 能被任何笔记软件打开，是「带出门」最实用的格式。
 * 打印 / 存 PDF 交给浏览器原生的 window.print()，不额外引入渲染逻辑。
 */
import { Download, Printer } from 'lucide-vue-next'
import { computed } from 'vue'

import type { DataSource, DayPlan, Meal, TripPlan } from '@/types/trip'

const props = defineProps<{ plan: TripPlan }>()

/** 数据来源 → 中文说明。和后端 DataSource 联合类型逐字对应 */
const SOURCE_LABELS: Record<DataSource, string> = {
  amap: '高德实查',
  llm_estimated: '模型估算',
  degraded: '获取失败（已降级）',
  user: '用户指定',
}

/** 来源 key → 中文名。key 的约定写在 types/trip.ts 的 SourceMap 注释里 */
const SOURCE_KEY_LABELS: Record<string, string> = {
  attractions: '景点列表',
  weather: '天气',
  hotels: '酒店候选池',
  hotel: '最终住宿',
  'attraction.ticket_price': '门票价',
  'hotel.price_range': '酒店价格区间',
  'hotel.estimated_cost': '酒店每晚费用',
  'meal.estimated_cost': '餐饮费用',
  plan: '行程本身',
}

const MEAL_LABELS: Record<string, string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  snack: '加餐',
}

/** 把一句话压成单行，避免它把 Markdown 表格 / 列表结构撑坏 */
function oneLine(text: string | null | undefined): string {
  if (!text) return ''
  return text.replace(/\s*\n+\s*/g, ' ').trim()
}

function money(value: number | undefined | null): string {
  return value === undefined || value === null ? '--' : `¥${value}`
}

function mealLabel(type: string): string {
  return MEAL_LABELS[type] ?? type
}

function sourceText(key: string): string {
  const source = props.plan.sources?.[key]
  if (!source) return ''
  return SOURCE_LABELS[source] ?? source
}

/**
 * 生成 Markdown 全文。
 *
 * 为什么把「数据来源」和 warnings 放在正文而不是页脚：
 * 用户把文件发给同伴时，页脚最容易被删；这些信息一旦丢掉，
 * 估算值就会被当成实价，那是这个项目最不想发生的事。
 */
const markdown = computed(() => {
  const plan = props.plan
  const lines: string[] = []

  // ---------- 标题与概览 ----------
  const nights = plan.hotel_nights ?? 0
  lines.push(`# ${plan.city} ${plan.days.length} 日行程`)
  lines.push('')
  lines.push(
    `- 城市：${plan.city}`,
  )
  lines.push(`- 日期：${plan.start_date} 至 ${plan.end_date}`)
  lines.push(`- 天数 / 晚数：${plan.days.length} 天 ${nights} 晚`)

  // ---------- 住宿 ----------
  lines.push('')
  lines.push('## 住宿')
  lines.push('')
  if (plan.hotel) {
    const hotel = plan.hotel
    const hotelSource = sourceText('hotel')
    lines.push(`- 名称：${hotel.name}${hotelSource ? `（来源：${hotelSource}）` : ''}`)
    if (hotel.address) lines.push(`- 地址：${oneLine(hotel.address)}`)
    if (hotel.rating) lines.push(`- 评分：${hotel.rating}`)
    if (hotel.type) lines.push(`- 类型：${hotel.type}`)
    if (hotel.price_range) lines.push(`- 价格区间：${hotel.price_range}`)
    const unit = hotel.estimated_cost
    const perNight = `${money(unit)}/晚`
    if (unit) {
      lines.push(
        `- 费用：${perNight} × ${nights} 晚 = ${money(unit * nights)}` +
          `${sourceText('hotel.estimated_cost') ? `（${sourceText('hotel.estimated_cost')}）` : ''}`,
      )
    } else {
      lines.push('- 费用：未给出每晚价格')
    }
    if (hotel.distance) lines.push(`- 距离说明：${oneLine(hotel.distance)}`)
  } else {
    lines.push('（没有住宿信息。可能是候选酒店为空，或该路数据获取失败。）')
  }

  // ---------- 逐日行程 ----------
  lines.push('')
  lines.push('## 逐日行程')
  plan.days.forEach((day: DayPlan) => {
    lines.push('')
    lines.push(`### 第 ${day.day_index + 1} 天 · ${day.date}`)
    lines.push('')
    if (day.description) lines.push(`> ${oneLine(day.description)}`)
    if (day.description) lines.push('')
    if (day.transportation) lines.push(`- 交通：${day.transportation}`)
    if (day.accommodation) lines.push(`- 住宿类型：${day.accommodation}`)
    if (day.day_total_cost !== undefined) lines.push(`- 当日小计：${money(day.day_total_cost)}`)

    lines.push('')
    lines.push(`**景点（${day.attractions.length} 处，按游览顺序）**`)
    lines.push('')
    if (day.attractions.length) {
      day.attractions.forEach((spot, index) => {
        const tags: string[] = []
        // 「必去」是硬约束，导出的纸上必须能一眼看出系统有没有兑现
        if (spot.required) tags.push('必去')
        if (spot.category) tags.push(spot.category)
        if (spot.rating) tags.push(`评分 ${spot.rating}`)
        if (spot.ticket_price) tags.push(`门票 ${money(spot.ticket_price)}`)
        else tags.push('门票 免费或未提供')
        lines.push(`${index + 1}. **${spot.name}**（${tags.join(' / ')}）`)
        lines.push(`   - 地址：${oneLine(spot.address) || '未提供'}`)
        lines.push(`   - 建议时长：${spot.visit_duration} 分钟`)
        if (spot.description) lines.push(`   - 简介：${oneLine(spot.description)}`)
      })
    } else {
      lines.push('（当日无景点安排）')
    }

    lines.push('')
    lines.push('**餐饮**')
    lines.push('')
    if (day.meals.length) {
      day.meals.forEach((meal: Meal) => {
        const cost = meal.estimated_cost ? `，约 ${money(meal.estimated_cost)}` : ''
        lines.push(`- ${mealLabel(meal.type)}：${meal.name}${cost}`)
      })
    } else {
      lines.push('（未安排餐饮）')
    }
  })

  // ---------- 预算明细 ----------
  lines.push('')
  lines.push('## 预算明细')
  lines.push('')
  const budget = plan.budget
  if (budget) {
    lines.push('| 项目 | 金额 | 数据来源 |')
    lines.push('| --- | --- | --- |')
    const rows: Array<[string, number | undefined, string]> = [
      ['门票', budget.total_attractions, 'attraction.ticket_price'],
      ['住宿', budget.total_hotels, 'hotel.estimated_cost'],
      ['餐饮', budget.total_meals, 'meal.estimated_cost'],
      ['交通', budget.total_transportation, ''],
    ]
    rows.forEach(([label, value, key]) => {
      const source = key ? sourceText(key) || '未标注' : '未标注'
      lines.push(`| ${label} | ${money(value ?? 0)} | ${source} |`)
    })
    lines.push(`| **总计** | **${money(budget.total ?? 0)}** | 由后端代码加总 |`)
  } else {
    lines.push('（没有预算数据）')
  }

  // ---------- 约束对账 ----------
  lines.push('')
  lines.push('## 必去要求对账')
  lines.push('')
  const report = plan.constraint_report
  const required = report?.required_attractions ?? []
  if (required.length) {
    lines.push(`- 你要求的必去景点：${required.length} 个`)
    lines.push(`- 已安排 ${report?.satisfied?.length ?? 0} 个：${(report?.satisfied ?? []).join('、') || '无'}`)
    const unsatisfied = report?.unsatisfied ?? []
    lines.push(`- 未安排 ${unsatisfied.length} 个：${unsatisfied.join('、') || '无'}`)
    unsatisfied.forEach((name) => {
      lines.push(`  - ${name}：${report?.reasons?.[name] ?? '未能安排'}`)
    })
    if (report?.hotel_anchor) {
      lines.push(
        `- 住宿锚点：${report.hotel_anchor}（${report.hotel_source === 'user' ? '用户指定' : '系统推荐'}）`,
      )
    }
    if (report?.feasible === false) {
      lines.push('- 可行性预检：未通过（在调用模型之前就被判定不可行）')
    }
  } else {
    lines.push('（本次没有提出必去景点，无需对账）')
  }

  // ---------- warnings ----------
  lines.push('')
  lines.push('## 需要留意')
  lines.push('')
  const warnings = plan.warnings ?? []
  if (warnings.length) {
    warnings.forEach((text) => lines.push(`- ${oneLine(text)}`))
  } else {
    lines.push('（本次没有降级或估算提示）')
  }

  // ---------- sources ----------
  lines.push('')
  lines.push('## 数据来源')
  lines.push('')
  const sources = plan.sources ?? {}
  const sourceKeys = Object.keys(sources)
  if (sourceKeys.length) {
    lines.push('| 字段 | 来源 |')
    lines.push('| --- | --- |')
    sourceKeys.forEach((key) => {
      const source = sources[key] as DataSource
      lines.push(`| ${SOURCE_KEY_LABELS[key] ?? key} | ${SOURCE_LABELS[source] ?? source} |`)
    })
  } else {
    lines.push('（后端未标注数据来源）')
  }

  if (plan.overall_suggestions) {
    lines.push('')
    lines.push('## 总体建议')
    lines.push('')
    lines.push(oneLine(plan.overall_suggestions))
  }

  lines.push('')
  return lines.join('\n')
})

const fileName = computed(() => {
  // 文件名里的斜杠、冒号等在 Windows 上是非法字符，统一替换掉
  const city = (props.plan.city || '行程').replace(/[\\/:*?"<>|]/g, '')
  return `${city}${props.plan.days.length}日行程.md`
})

function download(): void {
  // 开头的 \uFEFF 是 UTF-8 BOM。
  // 为什么必须加：Windows 记事本 / Excel 打开没有 BOM 的 UTF-8 文件时
  // 会按 GBK 解码，中文全部变乱码 —— 而这个项目的环境正是 Windows。
  const text = `\uFEFF${markdown.value}`
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName.value
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  // 立刻回收会偶发导致下载失败（浏览器还没读完 blob），下一轮事件循环再释放
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function print(): void {
  window.print()
}
</script>

<template>
  <section class="export-panel">
    <div class="card">
      <div class="card-title">
        <Download :size="16" />
        导出
      </div>
      <p class="hint">
        导出的是纯文本 Markdown，可离线阅读、可继续编辑，预算与数据来源一起带走，
        不会只给一份看起来像真的、其实含估算值的行程。
      </p>
      <div class="actions">
        <button class="action-btn primary" type="button" @click="download">
          <Download :size="15" />
          导出 Markdown
        </button>
        <button class="action-btn" type="button" @click="print">
          <Printer :size="15" />
          打印 / 存 PDF
        </button>
      </div>
      <p class="footnote">
        「打印 / 存 PDF」会调用浏览器打印对话框，在目标位置选「另存为 PDF」即可。
        导出文件名：{{ fileName }}
      </p>
    </div>
  </section>
</template>

<style scoped>
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
  margin-bottom: 12px;
}

.hint {
  margin: 0 0 14px;
  font-size: 13px;
  color: #5a6272;
  line-height: 1.9;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 16px;
  font-size: 14px;
  font-family: inherit;
  color: #3d4453;
  background: #f7f9fc;
  border: 1px solid #e3e7ee;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.action-btn:hover {
  background: #eef2f8;
  border-color: #d3dcea;
}

.action-btn.primary {
  color: #fff;
  background: #3566c9;
  border-color: #3566c9;
}

.action-btn.primary:hover {
  background: #2b57b0;
  border-color: #2b57b0;
}

.footnote {
  margin: 12px 0 0;
  font-size: 12px;
  color: #8c94a3;
  line-height: 1.8;
  word-break: break-all;
}
</style>

<!--
  打印样式刻意不写在 scoped 块里，而是用一个不带 scoped 的 <style>。
  原因：打印时要改的是「整页」的观感（去掉灰底、去掉页面留白、隐藏左右两栏里的表单区），
  这些元素属于 HomeView / App.vue，不是本组件的后代，scoped 的属性选择器根本选不中它们。
  这里用的选择器全部限定在 .page / .layout / aside 这类结构类名上，
  并在注释里写明意图，避免变成无边界的全局覆盖。
-->
<style>
@media print {
  /* 去背景：浏览器默认不打印背景色，留着浅灰底会在纸上变成一片脏灰 */
  body {
    background: #fff;
  }

  .page {
    max-width: none;
    padding: 0;
  }

  /* 打印物是「行程」，左侧输入表单打出来没有意义，只会浪费纸 */
  .layout {
    display: block;
  }

  .layout > aside {
    display: none;
  }

  /* 结果面板本身是白底卡片，打印时不要重复的边框与内边距 */
  .layout > main.panel {
    border: none;
    border-radius: 0;
    padding: 0;
  }

  /* 结果头部的「实际耗时 / 重新规划」是操作项，不是行程内容 */
  .result-head {
    display: none;
  }

  /* 去阴影：屏幕上的层次靠阴影，纸上去掉更清楚，也更省墨 */
  .card,
  .day-card,
  .panel {
    box-shadow: none !important;
  }

  /* 本组件自己的按钮不该出现在纸上 */
  .export-panel .actions,
  .export-panel .footnote {
    display: none;
  }

  /* 卡片别被分页从中间切开：一天一张卡，切开了很难读。
     这里只对「一天」这个粒度下手，不给所有 .card 加 —— 因为 TripSummary 的
     预算卡、对账卡也都带 .card 类，一旦给它们加 avoid，打印时会被整块推到下一页，
     中间留出大片空白，反而更难读。 */
  .day-card {
    break-inside: avoid;
    page-break-inside: avoid;
  }
}
</style>
