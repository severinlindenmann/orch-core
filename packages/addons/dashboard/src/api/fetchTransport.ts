import type { Transport } from './transport'

/** Real transport against the FastAPI backend. Bearer header only; credentials are never attached by the browser. */
export function createFetchTransport(baseUrl: string, token: () => string | null = () => null): Transport {
  const base = baseUrl.replace(/\/$/, '')
  return {
    async request(method, path, body, signal) {
      if (base && new URL(base, window.location.origin).origin !== window.location.origin) throw new Error('Transport must be same-origin')
      if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new Error('Invalid API path')
      const headers: Record<string, string> = {}
      const bearer = token()
      if (bearer) headers.Authorization = `Bearer ${bearer}`
      if (body !== undefined) headers['content-type'] = 'application/json'
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
    },
  }
}
