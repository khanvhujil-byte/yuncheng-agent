import type { Requirements, TripPlanResponse } from '@/types/trip'
import { API_BASE_URL as base } from './apiBase'

export type Slots = Record<string, string | number | boolean | string[] | Requirements | null>
export interface ClarificationQuestion {
  question_id: string
  module: string
  question: string
  options: { label: string }[]
}
export interface ClarificationAnswer { question_id: string; option_index: number }
export interface Conversation {
  conversation_id: string
  revision: number
  slots: Slots
  result: TripPlanResponse | null
  busy?: boolean
  pending_edit?: (ModuleEdit & { summary?: string; base_thread_id?: string }) | null
  edit_preview?: TripPlanResponse | null
  history: { message: string; reply: string; revision: number }[]
  clarifications?: ClarificationQuestion[]
}
export interface Turn {
  request_id: string
  expected_revision: number
  action: 'message' | 'confirm' | 'reject' | 'edit' | 'answer'
  message?: string
  slot_updates?: Slots
  edit?: ModuleEdit
  answer?: ClarificationAnswer
}
export interface ModuleEdit {
  module: 'hotel' | 'meals' | 'attractions' | 'day'
  stay_scope?: 'night' | 'segment' | 'all'
  day_index?: number
  item_index?: number
  candidate_id?: string
  candidate_ids?: string[]
  instruction?: string
}
export interface EditCandidate {
  candidate_id: string
  name: string
  address?: string
  estimated_cost?: number | null
  price_status?: string
  ranking_name?: string | null
  ranking_rank?: number | null
}
export interface EditCandidates {
  module: ModuleEdit['module']
  day_index?: number | null
  candidates: EditCandidate[]
  current_ids: string[]
  locked: boolean
}
export interface Citation { document_id: string; title: string; source: string; content: string; chunk_id: string }
export interface TurnResult extends Omit<Conversation, 'history'> {
  reply: string
  ready: boolean
  candidate_result: TripPlanResponse | null
  citations: Citation[]
}
export interface Preference { key: string; value: string | number | string[]; version: number; active: boolean; source: string }
export interface Document { document_id: string; title: string; source: string; valid_until: string | null }
export interface Quality { sample_runs: number; first_success_rate: number; retry_rate: number; final_success_rate: number; latency_p95_ms: number | null; alerts: { code: string; action: string }[] }

export class DialogError extends Error {
  constructor(message: string, public status = 0) { super(message) }
}
export function hasSession() { return !!sessionStorage.getItem('trip-session') }
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const token = sessionStorage.getItem('trip-session')
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 180_000)
  try {
    const response = await fetch(`${base}/api${path}`, {
      method, signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Session-Token': token } : {}),
        ...(import.meta.env.VITE_API_KEY ? { 'X-API-Key': import.meta.env.VITE_API_KEY } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    const data = await response.json()
    if (!response.ok) {
      if (response.status === 401) sessionStorage.removeItem('trip-session')
      throw new DialogError(typeof data.detail === 'string' ? data.detail : '输入不合法，请检查后重试。', response.status)
    }
    return data as T
  } catch (error) {
    if (error instanceof DialogError) throw error
    throw new DialogError('连接中断或等待超时。请重试原操作以确认结果。')
  } finally { window.clearTimeout(timer) }
}
export async function login(username: string, password: string) {
  const result = await request<{ token: string }>('/auth/login', 'POST', { username, password })
  sessionStorage.setItem('trip-session', result.token)
}
