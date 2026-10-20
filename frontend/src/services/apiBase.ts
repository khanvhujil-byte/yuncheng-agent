/** Empty means same-origin: Vite proxies in development, nginx in production. */
export function normalizeApiBaseUrl(value?: string): string {
  return (value || '').trim().replace(/\/+$/, '')
}

export const API_BASE_URL = normalizeApiBaseUrl(import.meta.env?.VITE_API_BASE_URL)
