import type { TripPlanResponse } from '@/types/trip'

export type StreamOutcome = 'done' | 'poll' | 'unavailable'
export async function finishPlanning(
  start: () => Promise<StreamOutcome>,
  poll: () => Promise<TripPlanResponse | null>,
  legacy: () => Promise<TripPlanResponse>,
): Promise<{ kind: 'done' | 'waiting'; response?: never } | { kind: 'result'; response: TripPlanResponse }> {
  const outcome = await start()
  if (outcome === 'done') return { kind: 'done' }
  if (outcome === 'unavailable') return { kind: 'result', response: await legacy() }
  const response = await poll()
  return response ? { kind: 'result', response } : { kind: 'waiting' }
}

export function completedResult(data: TripPlanResponse & { status?: string }): TripPlanResponse | null {
  if (data.status === 'running' || data.status === 'pending') return null
  if (!data.data && !data.error_code && data.status !== 'error' && data.message?.includes('仍在生成中')) return null
  return data
}
