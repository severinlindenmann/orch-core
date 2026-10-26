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
  request(method: HttpMethod, path: string, body?: unknown, signal?: AbortSignal): Promise<TransportResponse>
}

/** In-process transport: no network, no service worker. Swap for createFetchTransport later. */
export function createMockTransport(store: MockStore, opts: { latency?: boolean } = {}): Transport {
  const handle = createMockHandler(store, opts)
  return { previewState: (ws) => store.mandatesPreview.state(ws), request: (method, path, body) => handle(method, path, body) }
}

export { createFetchTransport } from './fetchTransport'
