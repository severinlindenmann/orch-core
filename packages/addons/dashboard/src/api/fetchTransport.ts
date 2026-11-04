import { getToken } from './bootstrap'
import { CLICK_CONTRACT, CLICK_CONTRACT_HEADER } from './click'
import { requestStepUp, stepUpOf } from './stepUp'
import type { Transport } from './transport'

/** Real transport against the FastAPI backend. Bearer header only; credentials are never attached by the browser. */
export function createFetchTransport(baseUrl: string, token: () => string | null = () => null): Transport {
  const base = baseUrl.replace(/\/$/, '')
  return {
    async request(method, path, body, signal) {
      const first = await once(method, path, body, signal, token())
      const need = stepUpOf(first.status, first.json)
      if (!need || method === 'GET') return first // a refused read just shows its own error; a change asks first
      // A read-mostly (hub-made) session: ask the person once, then run the same action again with the full session.
      if (!(await requestStepUp(need))) return first
      return once(method, path, body, signal, getToken() ?? token())
    },
  }
  async function once(method: string, path: string, body: unknown, signal: AbortSignal | undefined, bearer: string | null) {
    {
      if (base && new URL(base, window.location.origin).origin !== window.location.origin) throw new Error('Transport must be same-origin')
      if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new Error('Invalid API path')
      const headers: Record<string, string> = {}
      if (bearer) headers.Authorization = `Bearer ${bearer}`
      if (body !== undefined) headers['content-type'] = 'application/json'
      if (/^\/api\/w\/[^/]+\/click\//.test(path)) headers[CLICK_CONTRACT_HEADER] = String(CLICK_CONTRACT) // C3: the click contract this page speaks
      const res = await fetch(base + path, {
        method,
        headers,
        credentials: 'omit',
        redirect: 'error',
        cache: 'no-store',
        signal,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const text = await res.text()
      let json: unknown = null
      if (text) {
        try {
          json = JSON.parse(text)
        } catch {
          json = { ok: false, error: { code: 'bad_response', message: text.slice(0, 200), retryable: false } }
        }
      }
      return { status: res.status, json }
    }
  }
}
