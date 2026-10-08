/**
 * 数据来源标记的读取与呈现。
 *
 * 后端的 TripPlan.sources 是一个 { 字段路径: 来源 } 的字典，
 * 这里把它翻译成界面上的标签。
 */

import type { DataSource, SourceMap } from '@/types/trip'

/** 来源 → 中文标签 */
const SOURCE_LABELS: Record<DataSource, string> = {
  amap: '高德实查',
  llm_estimated: '模型估算',
  degraded: '获取失败',
  // v0.2 新增：用户自己指定的锚点（规格化酒店）。它既不是查到的、也不是估算的，
  // 所以必须单独一档，否则会被误标成「模型估算」。
  user: '你指定的',
}

export function sourceLabel(source: DataSource | undefined): string {
  return source ? SOURCE_LABELS[source] : ''
}

/** 取某个字段的来源标记 */
export function getSource(
  sources: SourceMap | undefined,
  key: string,
): DataSource | undefined {
  return sources?.[key]
}

/**
 * 该字段是不是「不可信」的（估算值或降级值）。
 * 是的话界面要显式标注，不能让用户以为是查到的真实数据。
 */
export function isEstimated(
  sources: SourceMap | undefined,
  key: string,
): boolean {
  const source = getSource(sources, key)
  return source === 'llm_estimated' || source === 'degraded'
}

/** 整个列表是不是降级的 */
export function isDegraded(
  sources: SourceMap | undefined,
  key: string,
): boolean {
  return getSource(sources, key) === 'degraded'
}
