import type { Attraction } from '../types/trip'

export function ticketText(spot: Attraction): string {
  if (spot.ticket_price_source === 'llm_estimated' && spot.ticket_price != null) return `日常门票参考 ¥${spot.ticket_price} · 模型估算`
  if (spot.ticket_price_source === 'venue_policy') return '普通入场不设门票预算 · 消费另计'
  if (spot.admission_policy === 'consumption') return '到店消费及入场收费待核实'
  if (spot.ticket_price_source === 'static_estimate' && spot.ticket_price != null) {
    return `票价待核实 · 预算预留 ¥${spot.ticket_price}`
  }
  if (spot.ticket_price == null || spot.ticket_price_status === 'unknown') return '门票待核实'
  const amount = spot.ticket_price === 0 ? '免费' : `¥${spot.ticket_price}`
  return `门票 ${amount}${spot.ticket_price_status === 'estimated' ? ' · 公开政策参考' : ''}`
}

export function ticketUnverified(spot: Attraction): boolean {
  return spot.ticket_price == null || spot.ticket_price_status === 'unknown' || ['static_estimate', 'llm_estimated'].includes(spot.ticket_price_source || '')
}

export function ticketReason(spot: Attraction): string {
  const note = spot.ticket_price_note || ''
  if (!ticketUnverified(spot)) return note
  if (note.includes('SOURCE_REQUIRES_RENDERING')) return '票价展示在动态页面中，本次未能读取到可核验的价格内容。'
  if (note.includes('SOURCE_ACCESS_CHALLENGE')) return '来源网站要求访问验证，本次未能取得票价资料。'
  if (note.includes('WEB_QUERY_BUDGET')) return '本次查询时间或次数已用完，尚未取得该景点的可靠票价。预算预留不代表实际收费。'
  if (note.includes('VENUE_OR_CITY_MISMATCH')) return '找到的页面未能确认属于这个景点，暂未采用其中价格。'
  if (note.includes('HISTORICAL_POLICY')) return '目前找到的是历史公告，尚未确认当前是否仍适用。'
  if (note.includes('OUTSIDE_POLICY_WINDOW')) return '出行日期超出当前票价政策的核验范围。'
  if (/HTTP_403|HTTP_412|HTTP_429|NETWORK_ERROR|TRANSPORT_ERROR|Timeout/.test(note)) return '票价来源暂时无法访问，尚未取得可核验的成人入场价格。'
  if (note.includes('ADULT_ADMISSION') || note.includes('DATED_OR_SEASONAL')) return '页面上的票种、适用日期或价格范围无法唯一确认，暂未采用。'
  if (note.includes('WEB_')) return '内部资料未命中，网上补查也未取得可核验的成人入场价格。'
  return note.replace(/\[[A-Z_0-9]+\]\s*/g, '') || '尚未获取可靠票价，请出发前向景区核实。'
}
