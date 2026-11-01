// Production entry of `@/api/client` (aliased in vite.config.ts for `vite build`): the real api, never the mock.
export * from './apiFactory'
export { api } from './prodApi'

import { createFetchTransport } from './fetchTransport'
import { getToken } from './bootstrap'
export const switchTransport = createFetchTransport('', getToken)
