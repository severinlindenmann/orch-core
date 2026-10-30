import { orgHandle } from './org'
import type { MockRouter } from './router'

export function registerOrgRoutes(r: MockRouter) {
  for (const [method, rest] of [['GET', 'org'], ['GET', 'org/item'], ['POST', 'org/accept'], ['POST', 'org/decline'], ['POST', 'org/sync'], ['POST', 'org/tool'], ['POST', 'org/tools']] as const) {
    r.add(method, `/api/w/:ws/${rest}`, (_s, c) => orgHandle(method, rest, c.query, c.body) ?? { status: 404, json: { ok: false, error: { code: 'not_found', message: 'x', retryable: false } } })
  }
}
