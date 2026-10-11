import { decisionDigest } from '@/api/addons'
import type { MockStore } from '@/mocks/store'

/**
 * The body core's prompt would post for a decision answer: `body` plus the `digest` of the decision exactly as it is
 * offered to the current viewer now (security review #3: the host requires it). Looked up by addon, action and
 * `body.id`; when no such decision is offered (closed, hidden, not this viewer's) the body is returned unchanged, so the
 * host's own refusal (decision.closed, forbidden …) is what the test sees. Tests only.
 */
export function offered<B extends Record<string, unknown>>(store: MockStore, ws: string, addon: string, action: string, body: B): B & { digest?: string } {
  const d = store.addonDecisions(ws).find((x) => x.addon === addon && x.action === action && x.id === body.id)
  return d ? { ...body, digest: decisionDigest(d) } : body
}
