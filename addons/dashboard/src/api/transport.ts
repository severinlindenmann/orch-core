import { createMockHandler } from '@/mocks/router'
import type { MandatesPreviewState } from './mandatesPreview'
import type { MockStore } from '@/mocks/store'

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface TransportResponse {
  status: number
  json: unknown
}

export interface Transport {
  /** In-process Demo data only; absent on every real transport. */
  previewState?: (ws: string) => MandatesPreviewState
  request(method: HttpMethod, path: string, body?: unknown): Promise<TransportResponse>
}

/** In-process transport: no network, no service worker. Swap for createFetchTransport later. */
export function createMockTransport(store: MockStore, opts: { latency?: boolean } = {}): Transport {
  const handle = createMockHandler(store, opts)
  return { previewState: (ws) => store.mandatesPreview.state(ws), request: (method, path, body) => handle(method, path, body) }
}

/** Real transport against the FastAPI backend. Unused for now; same contract as the mock. */
export function createFetchTransport(baseUrl: string): Transport {
  const base = baseUrl.replace(/\/$/, '')
  return {
    async request(method, path, body) {
      const res = await fetch(base + path, {
        method,
        headers: body === undefined ? undefined : { 'content-type': 'application/json' },
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
