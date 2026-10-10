// Bindings: how a declarative node reads the slot context (ticket, workspace) without any code.
//   {"$ref": "ticket.addons.estimate.points"}   -> the value at that path (null when missing)
//   "CHF ${ticket.addons.usage.cents|cents}"      -> string interpolation, optional formatter after "|"
//   "addon.byTicket.$ticket"                      -> `$ticket` is the current ticket's key (state keyed by ticket)
// Only plain property reads; no expressions, no function calls.

export const FORMATTERS: Record<string, (v: unknown) => string> = {
  cents: (v) => (Number(v) / 100).toFixed(2),
  duration: (v) => {
    const min = Math.round(Number(v) / 60000)
    return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min` : `${min} min`
  },
  chf: (v) => `CHF ${(Number(v) / 100).toFixed(2)}`,
  /** A real token count: "84k", "2.4 M". */
  ktok: (v) => {
    const n = Number(v)
    const k = Math.round(n / 1000)
    // Decide the unit after rounding, so 999,600 reads "1.0 M", never "1000k".
    return k >= 1000 ? `${(n / 1_000_000).toFixed(1)} M` : `${k}k`
  },
  tokens: (v) => `${Math.round((Number(v) * 5800) / 1000)}k`,
}

export function getPath(root: unknown, path: string): unknown {
  let cur: unknown = root
  for (const raw of path.split('.')) {
    // `$ticket` stands for the key of the ticket in the slot context: {"$ref": "addon.sharesByTicket.$ticket"}.
    // The prototype guard runs on the segment AFTER that substitution, so a ticket key cannot reach the prototype.
    const part = raw === '$ticket' ? ((root as { ticket?: { key?: string } }).ticket?.key ?? '') : raw
    if (cur === null || typeof cur !== 'object' || part === '__proto__' || part === 'constructor') return undefined
    cur = (cur as Record<string, unknown>)[part]
  }
  return cur
}

function interpolate(s: string, ctx: unknown): string {
  return s.replace(/\$\{([a-zA-Z0-9_.$]+)(?:\|([a-z]+))?\}/g, (_, path: string, fmt?: string) => {
    const v = getPath(ctx, path)
    // Only a plain value is written into text: an object or array is never stringified (it could be arbitrarily deep).
    if (v === undefined || v === null || typeof v === 'object' || typeof v === 'function') return ''
    return fmt && FORMATTERS[fmt] ? FORMATTERS[fmt](v) : String(v)
  })
}

const LIST_KEYS = new Set(['oneOf', 'enum'])

export function resolveBindings(node: unknown, ctx: unknown): unknown {
  if (typeof node === 'string') return interpolate(node, ctx)
  if (Array.isArray(node)) return node.map((n) => resolveBindings(n, ctx))
  if (node && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    if (typeof obj.$ref === 'string' && Object.keys(obj).length === 1) return getPath(ctx, obj.$ref) ?? null
    // A form's choices bound to state that is not there yet are no choices (rjsf cannot draw a null oneOf/enum).
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => {
      const r = resolveBindings(v, ctx)
      return [k, r === null && LIST_KEYS.has(k) ? [] : r]
    }))
  }
  return node
}

/** What an addon's contribution may weigh before core walks it (bindings, the state check, rendering). */
export const NODE_BUDGET = { depth: 64, nodes: 20_000, bytes: 2_000_000 } as const

/**
 * Why core will not walk this untrusted value at all (null when it fits): nested deeper than `depth` objects/arrays,
 * more than `nodes` values in all, or more than `bytes` of strings and keys. Iterative, so no input can exhaust the
 * stack, and it stops at the first limit hit (security review #9). Core checks it before binding detection and
 * resolution; a contribution over budget becomes that addon's "could not be shown" box, nothing else fails.
 */
export function nodeBudgetProblem(root: unknown): string | null {
  const stack: [unknown, number][] = [[root, 0]]
  // Every value counted when it is queued, so the stack itself never holds more than the node budget.
  let nodes = 1
  let bytes = 0
  const tooMany = `more than ${NODE_BUDGET.nodes} nodes`
  while (stack.length) {
    const [v, depth] = stack.pop()!
    if (typeof v === 'string') {
      bytes += v.length
      if (bytes > NODE_BUDGET.bytes) return 'too large'
      continue
    }
    if (v === null || typeof v !== 'object') continue
    if (depth >= NODE_BUDGET.depth) return `nested deeper than ${NODE_BUDGET.depth}`
    if (Array.isArray(v)) {
      // The length is known up front: refuse before queuing anything of an array that cannot fit.
      if (nodes + v.length > NODE_BUDGET.nodes) return tooMany
      nodes += v.length
      for (let i = 0; i < v.length; i++) stack.push([v[i], depth + 1])
      continue
    }
    // Own keys one at a time (no keys array for a huge object), counted before each is queued.
    for (const k in v) {
      if (!Object.hasOwn(v, k)) continue
      if (++nodes > NODE_BUDGET.nodes) return tooMany
      bytes += k.length
      if (bytes > NODE_BUDGET.bytes) return 'too large'
      stack.push([(v as Record<string, unknown>)[k], depth + 1])
    }
  }
  return null
}
