import { createApi, type Api } from './apiFactory'
import { createMockTransport } from './transport'
import { createMockStore } from '@/mocks/store'
import { setClock } from '@/lib/time'

/** The app-wide api object. Today it talks to the in-process mock; swap the transport for createFetchTransport(url). */
const isTest = import.meta.env.MODE === 'test'
/** Exposed for tests only (renderApp resets it). */
export const mockStore = createMockStore({ persist: !isTest, live: !isTest })
export const switchTransport = createMockTransport(mockStore, { latency: !isTest })
export const api: Api = createApi(switchTransport)
// Relative times are measured against the host's clock (the mock's "now"), not the browser's.
setClock(() => Date.parse(mockStore.now()))
export function resetMockStoreForTests() {
  mockStore.reset('normal')
}
